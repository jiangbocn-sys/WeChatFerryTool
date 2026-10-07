"""WeChatFerryTool Web 管理后台。

启动：python -m web.app
访问：http://127.0.0.1:6060
"""
import argparse
import json
import logging
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, url_for)

from paths import config_path, data_root, resource_root  # noqa: E402
from consumer import cleanup as cleanup_mod  # noqa: E402  （R-001 消息库按期清理）

PROJECT_DIR = data_root()
CONFIG_PATH = config_path()
LABELS_PATH = PROJECT_DIR / "data" / "labels.json"
DB_PATH = PROJECT_DIR / "data" / "messages.db"
APP_STATE_PATH = PROJECT_DIR / "data" / "app_state.json"

# 消息分类（入库闸门用的 msg_type）。这里的"分类"就是 DLL 给的 msg_type，
# 只是给了中文名与说明，方便在 Web 上勾选。is_system 的分类默认建议排除。
INGEST_TYPE_MAP: dict[int, dict] = {
    1: {"label": "文本", "descr": "纯文字消息"},
    3: {"label": "图片", "descr": "图片（无 OCR，只有占位符）"},
    34: {"label": "语音", "descr": "语音（可转写成文字）"},
    42: {"label": "名片", "descr": "个人名片"},
    43: {"label": "视频", "descr": "视频（无内容提取）"},
    47: {"label": "表情", "descr": "表情/动图 —— 恒不入库（不可取消）", "forced": True},
    48: {"label": "位置", "descr": "位置分享"},
    49: {"label": "链接/文件", "descr": "链接、文件、小程序等"},
    50: {"label": "通话", "descr": "音视频通话记录"},
    51: {"label": "系统", "descr": "系统事件（入群、改名等）", "is_system": True},
    10000: {"label": "系统", "descr": "系统事件（另一类 msg_type）", "is_system": True},
    10002: {"label": "撤回", "descr": "撤回消息", "is_system": True},
}

# 表情：**恒排除**。Web 上显示为"已选且不可取消"，后端保存与 consumer 读取时都会加回。
FORCED_INGEST_EXCLUDE = 47

# 备注行：整行以 # 开头，或 # 后跟空白（"# 说明"）。这样"#合同"仍可当关键词。
_COMMENT_RE = re.compile(r"^\s*(?:#(?=\s|$).*|;.*|//.*)$")


def parse_keywords(raw: str) -> list[str]:
    """把文本框里的关键词解析成列表。

    * 每行一个；也支持**逗号/顿号**分隔（用户习惯混用）
    * 整行注释（`# 说明`、`;`、`//`）忽略；像 `#合同` 这种没有空格的仍算关键词
    * 去重保序
    """
    out: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or _COMMENT_RE.match(line):
            continue
        for part in re.split(r"[,，、]", line):
            p = part.strip()
            if p:
                out.append(p)
    return list(dict.fromkeys(out))


def sanitize_ingest_exclude(values) -> list[int]:
    """入库排除类型：只留已知 msg_type，并**强制包含表情 47**。"""
    out: set[int] = set()
    for v in values or []:
        try:
            t = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if t in INGEST_TYPE_MAP:
            out.add(t)
    out.add(FORCED_INGEST_EXCLUDE)
    return sorted(out)


# app.main 启动 Web 时会把自己注册进来：同进程时状态与"控制"都走它。
# 独立运行 Web（web.sh）时没有控制器，只能读状态文件，控制按钮会禁用。
_app_controller = None


def set_app_controller(ctrl) -> None:
    """由 app.main 在启动 Web 之前调用。"""
    global _app_controller
    _app_controller = ctrl


def app_state(max_age_s: int = 30) -> dict:
    """取应用状态：优先问同进程控制器，否则读状态文件（独立 Web 场景）。"""
    if _app_controller is not None:
        try:
            st = dict(_app_controller.snapshot())
            st["running"] = True
            st["controllable"] = True
            st["source"] = "live"
            return st
        except Exception:  # noqa: BLE001
            pass
    try:
        raw = json.loads(APP_STATE_PATH.read_text(encoding="utf-8"))
        age = time.time() - float(raw.get("_ts") or 0)
        raw["controllable"] = False
        raw["source"] = "file"
        raw["stale"] = age > max_age_s
        raw["running"] = not raw["stale"]
        return raw
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {"running": False, "controllable": False, "source": "none"}


# ------- 配置读写 -------

def force_utf8():
    # --windowed 打包时 sys.stdout 是 None；如果显式传 None，Python 会回退成
    # sys.__stdout__（也是 None）→ 抛异常，所以这里显式判断。
    if sys.stdout is None:
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    """写回 config.yaml。写之前**自动备份**一份，写不了就抛 RuntimeError 给出人话提示。

    从 Web 改配置的人多半不会自己留副本，所以这里兜底：每次写之前把现存的
    config.yaml 复制成 `config.yaml.bak-<时间戳>`，并只保留最近 5 份（避免堆积）。
    冻结打包时这个界面可能装在 Program Files 里 → 备份写不了不阻塞主写入。
    """
    if CONFIG_PATH.is_file():
        try:
            import shutil
            bak = CONFIG_PATH.with_name(f"config.yaml.bak-{int(time.time())}")
            shutil.copy2(CONFIG_PATH, bak)
            olds = sorted(CONFIG_PATH.parent.glob("config.yaml.bak-*"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for p in olds[5:]:
                try:
                    p.unlink()
                except OSError:
                    pass
        except OSError as e:
            logging.getLogger("web.app").warning("配置备份失败（不影响保存）: %s", e)
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    except OSError as e:
        raise RuntimeError(f"写不了配置文件 {CONFIG_PATH}（{e}）—— "
                           f"检查该文件是否只读、目录是否有写权限") from e


def load_labels() -> dict:
    if not LABELS_PATH.exists():
        return {"groups": {}, "senders": {}}
    try:
        with LABELS_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("groups", {})
        data.setdefault("senders", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"groups": {}, "senders": {}}


def save_labels(labels: dict) -> None:
    """写回 labels.json。写不了就抛 RuntimeError 给出人话提示（否则会静默丢标定）。

    与 `save_config` 一样，写之前**自动备份**一份（保留最近 5 份）：
    标定里有你的名字/★重点/重点人/关键词，误操作一次代价很大。
    """
    if LABELS_PATH.is_file():
        try:
            import shutil
            bak = LABELS_PATH.with_name(f"labels.json.bak-{int(time.time())}")
            shutil.copy2(LABELS_PATH, bak)
            olds = sorted(LABELS_PATH.parent.glob("labels.json.bak-*"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for p in olds[5:]:
                try:
                    p.unlink()
                except OSError:
                    pass
        except OSError as e:
            logging.getLogger("web.app").warning("标定备份失败（不影响保存）: %s", e)
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LABELS_PATH.open("w", encoding="utf-8") as f:
            json.dump(labels, f, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError as e:
        raise RuntimeError(f"写不了标定文件 {LABELS_PATH}（{e}）—— "
                           f"检查该文件是否只读、目录是否有写权限") from e


# ------- SQLite 查询 -------

def db_query(sql: str, params: tuple = ()) -> list[dict]:
    """查消息库。

    库不存在 → 返回空（页面显示"还没有消息"）。
    **打不开**（被占用/权限不足/WAL 不可写）→ 抛可读的 RuntimeError，
    由 /browse 等页面接住显示成提示，而不是甩一个 500 页面出来。
    """
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise RuntimeError(f"打不开消息库 {DB_PATH}（{e}）—— "
                           f"检查它是否被占用、或所在目录是否可写（SQLite 需要写 -wal/-shm）") from e
    return [dict(r) for r in rows]


def db_stats() -> dict:
    stats = {
        "total_messages": 0,
        "important_messages": 0,
        "groups": 0,
        "private_chats": 0,
        "senders": 0,
        "latest": None,
    }
    if not DB_PATH.exists():
        return stats
    stats["total_messages"] = db_query("SELECT COUNT(*) AS n FROM messages")[0]["n"]
    stats["important_messages"] = db_query("SELECT COUNT(*) AS n FROM messages WHERE priority >= 1")[0]["n"]
    # 群（有 @chatroom 后缀）vs 私聊（没有）
    grp_row = db_query("SELECT COUNT(DISTINCT group_name) AS n FROM messages WHERE group_name LIKE '%@chatroom'")[0]
    stats["groups"] = grp_row["n"]
    pvt_row = db_query("SELECT COUNT(DISTINCT group_name) AS n FROM messages WHERE group_name NOT LIKE '%@chatroom'")[0]
    stats["private_chats"] = pvt_row["n"]
    stats["senders"] = db_query("SELECT COUNT(DISTINCT sender) AS n FROM messages")[0]["n"]
    latest = db_query("SELECT id, received_at, group_name, sender, substr(content,1,50) AS preview FROM messages ORDER BY id DESC LIMIT 1")
    if latest:
        stats["latest"] = latest[0]
    return stats


def fmt_ts(ts: int | None) -> str:
    if not ts:
        return "未知"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# ------- Flask app -------

def _config_context() -> dict:
    """监控规则页需要的上下文：labels、监控名单、可选群/人、每个群的重点关注人。

    放在模块级（不在 create_app 里），因为 /filter 与 /config 都要用。
    """
    cfg = load_config()
    labels = load_labels()
    lab_groups = labels.get("groups") or {}
    lab_senders = labels.get("senders") or {}
    filter_cfg = cfg.get("filter") or {}

    mon_groups = [str(x).strip() for x in (filter_cfg.get("groups") or []) if str(x).strip()]
    mon_senders = [str(x).strip() for x in (filter_cfg.get("senders") or []) if str(x).strip()]

    def disp(kind: str, key: str) -> str:
        ent = (lab_groups if kind == "groups" else lab_senders).get(key) or {}
        return (ent.get("name") or "").strip() or key

    def is_labeled(kind: str, key: str) -> bool:
        """是否"已标记"。下拉菜单只放已标记的项 —— 没标记的不会是重要群/人。

        群：有名字 / ★重点 / 已设重点关注人 / 已配敏感关键词 都算标记过。
        人：有名字 / ★重点。
        """
        ent = (lab_groups if kind == "groups" else lab_senders).get(key) or {}
        if (ent.get("name") or "").strip():
            return True
        if ent.get("important"):
            return True
        if kind == "groups":
            if [m for m in (ent.get("focus_members") or []) if m]:
                return True
            if [k for k in (ent.get("keywords") or []) if str(k).strip()]:
                return True
        return False

    # 下拉候选：只放已标记的（外加"已在监控名单里"的，以免移除入口消失）
    all_groups = sorted(k for k in (set(lab_groups) | set(mon_groups)) if is_labeled("groups", k))
    all_senders = sorted(k for k in (set(lab_senders) | set(mon_senders)) if is_labeled("senders", k))

    group_rows = []
    for gid in mon_groups:
        ent = lab_groups.get(gid) or {}
        focus = [m for m in (ent.get("focus_members") or []) if m]
        group_rows.append({
            "id": gid, "name": disp("groups", gid),
            "important": bool(ent.get("important")),
            "keywords": len([k for k in (ent.get("keywords") or []) if str(k).strip()]),
            "focus": [{"id": m, "name": disp("senders", m)} for m in focus],
            # 每群"总结提示"：跟着当日总结一起提交给 LLM（存在 labels.groups.<gid>.summary_hint）
            "hint": str(ent.get("summary_hint") or ""),
        })
    sender_rows = [{"id": s, "name": disp("senders", s)} for s in mon_senders]

    return {
        "mon_groups": mon_groups, "mon_senders": mon_senders,
        "group_rows": group_rows, "sender_rows": sender_rows,
        "focus_map": {g["id"]: g["focus"] for g in group_rows},
        "all_groups": [{"id": g, "name": disp("groups", g)} for g in all_groups],
        "all_senders": [{"id": s, "name": disp("senders", s)} for s in all_senders],
        "labels_path": str(LABELS_PATH),
    }


def create_app() -> Flask:
    # 用绝对路径：冻结打包后 Flask 自己推算的 root_path 会指错（模板会找不到）
    app = Flask(
        __name__,
        template_folder=str(resource_root() / "web" / "templates"),
        static_folder=str(resource_root() / "web" / "static"),
    )

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    def save_error_page(what: str, err: Exception):
        """写盘失败（只读目录等）时给个人话错误页，而不是一片 traceback。"""
        return render_template("save_error.html", what=what, error=str(err)), 500

    @app.route("/")
    def dashboard():
        try:
            return _dashboard()
        except RuntimeError as e:
            return render_template("save_error.html", what="仪表盘", error=str(e)), 503

    def _dashboard():
        stats = db_stats()
        cfg = load_config()
        labels = load_labels()
        # 最近 10 条消息（含 priority 标记）
        recent = db_query(
            "SELECT id, msg_id, group_name, sender, substr(content,1,80) AS preview, "
            "score, score_reason, received_at, priority "
            "FROM messages ORDER BY id DESC LIMIT 10"
        )
        # 套用标定
        for r in recent:
            g_name = labels.get("groups", {}).get(r["group_name"], {}).get("name", "")
            s_name = labels.get("senders", {}).get(r["sender"], {}).get("name", "")
            r["group_label"] = g_name or r["group_name"]
            r["sender_label"] = s_name or r["sender"]
            r["ts"] = fmt_ts(r["received_at"])
        return render_template(
            "dashboard.html",
            stats=stats,
            recent=recent,
            cfg=cfg,
            app_state=app_state(),
            n_important_groups=sum(1 for v in labels.get("groups", {}).values() if v.get("important")),
            n_important_senders=sum(1 for v in labels.get("senders", {}).values() if v.get("important")),
        )

    # ---- filter 配置 ----

    @app.route("/filter")
    def filter_page():
        """监控规则页 = 监控名单（下拉勾选 + 每群重点关注人）+ 入库关键词。

        监控名单统一收在这里（总设置页只放一个链接），避免两处都能改同一份配置。
        """
        cfg = load_config()
        return render_template("filter.html", cfg=cfg.get("filter", {}), **_config_context())

    @app.route("/filter/update", methods=["POST"])
    def filter_update():
        """监控规则页的保存（R-002 之后：本页只维护**归档范围**，不碰入库）。

        ⚠️ `filter.keywords` 已不再参与任何判定（入库不看、归档也不看），
        所以这里**不再接收也不再写它** —— 避免"看着能改、其实没用"的误导。
        它的现存值原样保留，不动用户配置。
        """
        cfg = load_config()
        cfg.setdefault("filter", {})
        f = cfg["filter"]

        def parse_lines(s: str) -> list[str]:
            return [ln.strip() for ln in (s or "").splitlines() if ln.strip()]

        # 监控名单已由 /config/filter/save（下拉勾选）维护 —— 本表单不提交 groups/senders，
        # 所以**只在表单确实带了该字段时才覆盖**，否则会把监控名单清空。
        if "groups" in request.form:
            f["groups"] = parse_lines(request.form.get("groups", ""))
        if "senders" in request.form:
            f["senders"] = parse_lines(request.form.get("senders", ""))
        if "case_insensitive" in request.form:
            f["case_insensitive"] = request.form.get("case_insensitive") == "on"

        try:
            save_config(cfg)
        except (OSError, RuntimeError) as e:
            return save_error_page("监控规则", e)
        logging.getLogger("web.app").info("监控规则已保存（归档范围：群 %d / 人 %d）",
                                          len(f.get("groups") or []), len(f.get("senders") or []))
        return redirect(url_for("filter_page", saved="归档范围"))

    # ---- 消息分类（入库类型闸门） ----

    @app.route("/settings")
    def settings_page():
        """消息分类勾选：决定哪些 msg_type **不入库**（写 storage.ingest_exclude_types）。"""
        try:
            return _settings_page()
        except RuntimeError as e:
            return render_template("save_error.html", what="消息分类", error=str(e)), 503

    def _settings_page():
        cfg = load_config()
        excluded = sanitize_ingest_exclude((cfg.get("storage") or {}).get("ingest_exclude_types"))
        counts = {
            int(r["msg_type"]): int(r["n"])
            for r in db_query("SELECT msg_type, COUNT(*) AS n FROM messages GROUP BY msg_type")
        }
        known: set[int] = set()

        def rows_for(selected: bool) -> list[dict]:
            out = []
            for t, meta in INGEST_TYPE_MAP.items():
                if meta.get("forced"):
                    continue
                if t in known:
                    continue
                if bool(t in excluded) != selected:
                    continue
                known.add(t)
                out.append({"type": t, "count": counts.get(t, 0), "selected": selected, **meta})
            # 已勾选：先按消息量；未勾选：先按消息量，再按类型号（保证顺序稳定）
            out.sort(key=(lambda r: -r["count"]) if selected else (lambda r: (-r["count"], r["type"])))
            return out

        type_rows = rows_for(True) + rows_for(False)
        unknown = sorted((t, n) for t, n in counts.items() if t not in INGEST_TYPE_MAP)
        db_types = [{"type": t, "count": n, "label": (INGEST_TYPE_MAP.get(t) or {}).get("label", "")}
                    for t, n in unknown]
        return render_template(
            "settings.html",
            cfg=cfg.get("storage") or {},
            type_rows=type_rows,
            db_types=db_types,
            excluded=excluded,
            n_excluded=len(excluded),
            # 表情恒排除，不在 type_rows 里（它不可取消），单独一行显示
            forced={"type": FORCED_INGEST_EXCLUDE, "count": counts.get(FORCED_INGEST_EXCLUDE, 0),
                    **INGEST_TYPE_MAP[FORCED_INGEST_EXCLUDE]},
            cleanup=_cleanup_context(cfg),
        )

    def _cleanup_context(cfg: dict) -> dict:
        """「数据清理」区块要显示的一切（含 dry-run 预览：按当前设置会删多少条）。

        ⚠️ 只读：这里的 count 走 dry_run，绝不删数据。
        """
        sc = cleanup_mod.read_storage_cfg(cfg)
        state = cleanup_mod.load_state()
        db = cleanup_mod.resolve_db_path(sc["path"])
        try:
            rows = db_query("SELECT COUNT(*) AS n FROM messages")
            total = int(rows[0]["n"]) if rows else 0
        except (RuntimeError, IndexError, KeyError, TypeError, ValueError) as e:
            total = None
            logging.getLogger("web.app").info("清理预览读库失败: %s", e)
        preview = -1
        if sc["retention_days"] > 0 and total is not None:
            preview = cleanup_mod.count_older(db, sc["retention_days"])
            if preview < 0:
                preview = 0          # 还没建表/读不到结构 —— 当作"没东西可删"，别显示成错误
        last_at = state.get("at")
        return {
            "enabled": bool(sc["auto_cleanup"] and sc["retention_days"] >= cleanup_mod.MIN_RETENTION_DAYS),
            "days": sc["retention_days"],
            "auto": sc["auto_cleanup"],
            "time": sc["cleanup_time"],
            "vacuum": sc["vacuum"],
            "min_days": cleanup_mod.MIN_RETENTION_DAYS,
            "db_path": str(db),
            "db_exists": db.exists(),
            "total": total,
            "preview": preview,
            "cutoff": (datetime.fromtimestamp(
                cleanup_mod.retention_cutoff(sc["retention_days"])).strftime("%Y-%m-%d %H:%M")
                if sc["retention_days"] > 0 else ""),
            "last_date": state.get("last_date") or "",
            "last_at_str": (datetime.fromtimestamp(float(last_at)).strftime("%Y-%m-%d %H:%M")
                            if last_at else ""),
            "last_deleted": state.get("deleted"),
            "last_days": state.get("days"),
            "last_vacuumed": bool(state.get("vacuumed")),
        }

    @app.route("/settings/cleanup", methods=["POST"])
    def settings_cleanup():
        """手动清理：dry-run 预览 或 立即真删一次（R-001）。

        只动消息库里的老行，不碰配置/标定/归档文件。写盘前不备份 ——
        因为这里写的是**消息库**（删除本身不可回滚），所以页面上给了预览与确认。
        """
        cfg = load_config()
        sc = cleanup_mod.read_storage_cfg(cfg)
        mode = (request.form.get("mode") or "").strip()
        if sc["retention_days"] <= 0:
            return redirect(url_for("settings_page",
                                    cleanup_error="还没设置保留天数（现在是 0 = 不清理）"))
        db = cleanup_mod.resolve_db_path(sc["path"])
        if mode == "dry_run":
            r = cleanup_mod.cleanup_messages(db, retention_days=sc["retention_days"], dry_run=True)
            if r.errors and not r.matched:
                return redirect(url_for("settings_page",
                                        cleanup_error="读库失败：" + "；".join(r.errors)))
            logging.getLogger("web.app").info("清理预演：命中 %d 条", r.matched)
            return redirect(url_for("settings_page",
                                    cleanup_saved=f"预演（未删除）：按保留 {r.days} 天，"
                                                  f"将删除 {r.matched} 条，当前共 {r.total} 条"))
        # 立即清理一次（真删 + checkpoint）
        try:
            r = cleanup_mod.cleanup_messages(
                db, retention_days=sc["retention_days"],
                vacuum=bool(request.form.get("vacuum") == "on"),
            )
        except Exception as e:  # noqa: BLE001
            logging.getLogger("web.app").exception("手动清理异常")
            return redirect(url_for("settings_page", cleanup_error=f"清理失败：{e}"))
        if r.errors and not r.deleted:
            return redirect(url_for("settings_page",
                                    cleanup_error="清理未完成：" + "；".join(r.errors)))
        logging.getLogger("web.app").info("手动清理完成：%s", r.log_line())
        return redirect(url_for("settings_page",
                                cleanup_saved=f"清理完成：删除 {r.deleted} 条"
                                              f"（保留 {r.days} 天），当前共 {r.total} 条，"
                                              f"耗时 {r.elapsed_ms} ms"
                                              + ("，已 VACUUM" if r.vacuumed else "")))

    @app.route("/settings/update", methods=["POST"])
    def settings_update():
        cfg = load_config()
        cfg.setdefault("storage", {})
        # 注意：**未勾选 = 要入库**，所以表单里传的是"要排除"的类型
        cfg["storage"]["ingest_exclude_types"] = sanitize_ingest_exclude(request.form.getlist("exclude_types"))
        try:
            save_config(cfg)
        except (OSError, RuntimeError) as e:
            return save_error_page("消息分类", e)
        return redirect(url_for("settings_page"))

    # ---- 总设置页：表单化（用户填参数，配置由应用生成） ----
    # 设计：SECTION_FIELDS 描述"每一行长什么样、写到配置的哪一项"，
    #       页面按它渲染表单；提交时按同一张表把表单值写回 config.yaml。
    #       —— 用户不需要看/写 JSON。

    FIELDS: list[dict] = [
        {"key": "hook", "title": "微信 / DLL 连接",
         "note": "抓取消息用的本地接口，一般不用改。", "fields": [
             {"path": "hook.api_base", "label": "DLL 接口地址", "kind": "text",
              "impact": "微信被注入 version.dll 后监听的本地 HTTP 地址；改了就连不上、抓不到消息",
              "suggest": "默认 http://127.0.0.1:30001（端口由 DLL 决定，不要随意改）"},
             {"path": "hook.callback_host", "label": "回调监听地址", "kind": "text",
              "impact": "本工具接收消息事件的地址；只能本机访问最安全",
              "suggest": "保持 127.0.0.1"},
             {"path": "hook.callback_port", "label": "回调端口", "kind": "number", "min": 1, "max": 65535,
              "impact": "本工具监听的端口；被占用时收不到消息",
              "suggest": "默认 8888"},
             {"path": "hook.self_wxid", "label": "本人 wxid", "kind": "text",
              "impact": "判断\"哪条消息是我发的\"（决定 direction=out）；填错会把别人的话当成自己说的",
              "suggest": "留空 = 按账号目录名自动推导（推荐）"},
             {"path": "hook.api_timeout_s", "label": "接口超时（秒）", "kind": "number", "step": "0.5",
              "impact": "调用 DLL 接口的等待上限；太短会在微信卡顿时误判失败",
              "suggest": "默认 5.0"},
         ]},
        {"key": "filter", "title": "监控名单（只决定归档/总结范围）",
         "note": "⚠️ 这里**不影响入库**：所有会话的消息都会入库（只挡类型，见「存储 / 入库闸门」）。"
                 "监控名单是用来筛选 **23:30 归档与当日总结** 的范围。", "fields": [
             {"path": "filter.groups", "label": "监控的群 / 会话（归档范围）", "kind": "list",
              "impact": "只有名单里的群会进当日归档与总结（群里没设重点人 = 整群归档）；"
                        "**名单外的群消息照样入库、可在消息浏览里查**",
              "suggest": "每行一个 roomid（如 195940014@chatroom）；留空 = 不按群归档"},
             {"path": "filter.senders", "label": "监控的人（归档范围）", "kind": "list",
              "impact": "这些人的发言会进归档（无论哪个群）；同样不影响入库",
              "suggest": "每行一个 wxid"},
             {"path": "filter.keywords", "label": "归档关键词", "kind": "list",
              "impact": "内容命中任一关键词就进归档（大小写不敏感）；**不影响入库**",
              "suggest": "如：报价 / 合同 / 付款 / deadline"},
             {"path": "filter.case_insensitive", "label": "关键词忽略大小写", "kind": "bool",
              "impact": "关掉后英文关键词必须大小写完全一致才命中",
              "suggest": "建议勾选（开）"},
         ]},
        {"key": "llm", "title": "大模型（评分 / 回复 / 总结）", "fields": [
             {"path": "llm.base_url", "label": "接口地址 base_url", "kind": "text",
              "impact": "所有 LLM 功能都走这个地址；填错会评分失败、自动回复失效、当日总结报错",
              "suggest": "MiniMax https://api.minimax.cn/v1 ｜ DeepSeek https://api.deepseek.com/v1 ｜ OpenAI https://api.openai.com/v1 ｜ 本地 Ollama http://127.0.0.1:11434/v1"},
             {"path": "llm.api_key", "label": "API Key", "kind": "password",
              "impact": "调用大模型的凭证；错误/过期会导致调用全部失败",
              "suggest": "已保存过就留空 = 不修改；要换就填新的；想停用 LLM 就勾\"清空\""},
             {"path": "llm.model", "label": "模型名", "kind": "text",
              "impact": "决定用哪个模型；填了接口不支持的模型名会直接报错",
              "suggest": "如 MiniMax-M3 / deepseek-chat / gpt-4o-mini"},
             {"path": "llm.score_enabled", "label": "逐条消息用大模型评分（默认关闭）", "kind": "bool",
              "impact": "开着会对**每条别人发来的文本消息**调一次大模型打分（1~5）并写进数据库。"
                        "它唯一的实际用途是给 Bark 推送做阈值判断 —— **自动回复走模板匹配、"
                        "归档走 priority（标定/监控名单/重点人），都不看这个分**。"
                        "关掉后完全不调 LLM、不写 score，也不会有高分推送",
              "suggest": "建议**关闭**（省钱、也少一层 LLM 依赖）；只有想让 Bark 按分数筛选推送时才打开"},
             {"path": "llm.push_threshold", "label": "推送阈值（1~5，仅评分开启时生效）", "kind": "number", "min": 1, "max": 5,
              "impact": "评分 ≥ 该值才标记为\"要推送\"；⚠️ **只在「逐条消息用大模型评分」开启时才有意义**"
                        "（关掉评分就没有分数可比）。另外 Bark 关闭时只影响标记，不会真发通知",
              "suggest": "默认 4（只有高分才推）；调成 3 会多推、5 更少"},
         ]},
        {"key": "storage", "title": "存储 / 入库闸门", "fields": [
             {"path": "storage.sqlite_path", "label": "数据库路径", "kind": "text",
              "impact": "消息库位置（相对数据根）；改了会新建一个空库、看不到老消息",
              "suggest": "保持 data/messages.db"},
             {"path": "storage.ingest_exclude_types", "label": "不入库的消息类型", "kind": "typecheck",
              "impact": "勾中的类型不写数据库（归档与总结也看不到）；表情 47 恒排除、不可取消",
              "suggest": "想省空间可勾系统(51/10000)与撤回(10002)；想保留全部证据就只留 47"},
         ]},
         {"key": "cleanup", "title": "数据清理（消息库按期自动清理）",
          "note": "超过保留天数的消息会被**真正删除**（不可恢复，归档 Markdown 不受影响）。"
                  "清理在后台静默进行，每天最多一次。填 0 = 不自动清理。",
          "fields": [
             {"path": "storage.retention_days", "label": "保留最近多少天的消息", "kind": "number",
              "min": 0, "max": 3650,
              "impact": "填 N = 保留最近 N 个自然日（含今天），更早的消息被删除；"
                        "**填 0 = 不自动清理**（推荐先观察一段时间）；1~6 视为非法（不执行）",
              "suggest": "建议 90；打开后想看更久的历史归档就调大"},
             {"path": "storage.auto_cleanup", "label": "启用自动清理", "kind": "bool",
              "impact": "勾上后 app 运行期间每天到点清理一次（只删超期消息，当天的永远不删）；"
                        "取消勾选 = 只保留设置、不动数据",
              "suggest": "想清理就勾上；同时把「保留天数」设成 ≥7"},
             {"path": "storage.cleanup_time", "label": "每天几点清理", "kind": "time",
              "impact": "到点后且当天没清理过才执行；⚠️ 必须让 app 一直开着才会触发；"
                        "与归档时间（23:30）错开可避免同时占库",
              "suggest": "默认 00:00（凌晨）；格式 HH:MM，例如 04:30"},
          ]},
        {"key": "digest", "title": "每日归档 / 当日总结", "fields": [
             {"path": "digest.enabled", "label": "启用每日归档", "kind": "bool",
              "impact": "关掉后不会自动生成 Markdown 归档",
              "suggest": "建议开"},
             {"path": "digest.time", "label": "每天生成时间", "kind": "text",
              "impact": "到点（且当天没生成过）就归档；⚠️ 必须让 app 一直开着才会触发",
              "suggest": "默认 23:30；格式 HH:MM，例如 23:00"},
             {"path": "digest.dir", "label": "归档输出目录", "kind": "text",
              "impact": "Markdown 归档与总结写到哪里（相对数据根）",
              "suggest": "默认 reports"},
             {"path": "digest.summarize", "label": "归档后自动让 LLM 写总结", "kind": "bool",
              "impact": "勾上则每次归档后额外调用一次大模型写当日总结（消耗额度）；没配 LLM 会自动跳过",
              "suggest": "建议勾选；只想留归档不想花额度就取消"},
             {"path": "digest.summarize_mode", "label": "当日总结方式", "kind": "select",
              "options": [("per_group", "每个群单独一份总结（推荐）"),
                          ("combined", "所有群合并成一份总结")],
              "impact": "决定总结产出几份文件、调用几次大模型：per_group → "
                        "`summary-<日期>-<群名>.md`，**每个群各一次调用**（群之间互不干扰、读起来清爽）；"
                        "combined → 一份 `summary-<日期>.md`，所有群拼进同一个 prompt（只调 1 次，"
                        "但输出是一篇混着所有群的整体叙述）",
              "suggest": "群主题差别大就选 per_group（调用次数 = 群数）；想省额度或只要一份总览就选 combined"},
             {"path": "digest.exclude_types", "label": "归档排除的消息类型", "kind": "typelist",
              "impact": "这些类型不进归档（也就不会提交给 LLM）；与\"入库闸门\"不同：这里只影响归档，"
                        "改完可直接重跑当天归档。⚠️ `3 图片` 目前**无法把图片内容交给模型**"
                        "（本地是加密的 .dat、微信只在内存解密），所以建议排除图片，避免 prompt 里"
                        "只出现一行 `[图片 214×480]` 占位、既没信息又占篇幅",
              "suggest": "建议 3 图片 / 47 表情 / 51 系统 / 10000 系统；"
                         "链接(49)/引用(57) 是**带文字的卡片**，建议保留"},
         ]},
        {"key": "voice", "title": "语音捕获", "fields": [
             {"path": "voice.enabled", "label": "启用语音捕获", "kind": "bool",
              "impact": "关掉后语音不存档、也不转写（消息仍会入库为 [语音] 占位）",
              "suggest": "需要语音转写就开"},
             {"path": "voice.dir", "label": "语音存档目录", "kind": "text",
              "impact": "原始 silk 与解码后 wav 放哪里（相对数据根）",
              "suggest": "默认 data/voices"},
             {"path": "voice.conv_overrides", "label": "会话名手工映射", "kind": "overrides",
              "impact": "微信把语音缓存放在 `cache\\<会话 md5>\\VoiceTemp\\` 里，**只给 md5、不给 wxid**，"
                        "所以自己发出的语音（没有消息事件）会落成 `(未知会话:xxxxxxxx)`。"
                        "这里把那串 md5 对应到具体会话：**填 wxid / roomid 才会归到真实会话**，"
                        "填显示名只是做个标记（会变成独立会话名）",
              "suggest": "每行一个：`md5=微信id`。md5 取语音文件名 `data\\voices\\raw\\<md5>_<时间>.bin` "
                         "下划线前那段；**前 8 位或完整 32 位都认**（推荐填 wxid / roomid）"},
         ]},
        {"key": "asr", "title": "语音转写（ASR）", "fields": [
             {"path": "asr.enabled", "label": "启用语音转写", "kind": "bool",
              "impact": "把语音送到 whisper 服务转成文字；关掉则语音只存档、归档里显示 [语音 ~Ns]",
              "suggest": "有 whisper 服务就开"},
             {"path": "asr.url", "label": "转写服务地址", "kind": "text",
              "impact": "OpenAI 兼容的 /v1/audio/transcriptions 地址；不通就一直 [待转写]",
              "suggest": "如 http://JiangdeMac-mini.local:8170/v1/audio/transcriptions"},
             {"path": "asr.timeout_s", "label": "转写超时（秒）", "kind": "number",
              "impact": "长语音需要更久；太短会转写失败",
              "suggest": "默认 120"},
         ]},
        {"key": "bark", "title": "推送（Bark，仅 iOS）", "fields": [
             {"path": "bark.enabled", "label": "启用推送", "kind": "bool",
              "impact": "评分达阈值时推送到手机；仅 iOS 的 Bark 可用",
              "suggest": "安卓用户建议关闭"},
             {"path": "bark.server", "label": "Bark 服务器", "kind": "text",
              "impact": "推送服务地址；自建服务器时改这里",
              "suggest": "默认 https://api.day.app"},
             {"path": "bark.key", "label": "Bark Key", "kind": "password",
              "impact": "你的设备 key；填错收不到推送",
              "suggest": "从 Bark App 里复制（形如 xxxxxxxx）"},
         ]},
        {"key": "app", "title": "应用 / 托盘", "fields": [
             {"path": "app.web_host", "label": "管理平台监听地址", "kind": "text",
              "impact": "改成 0.0.0.0 会让局域网内其他设备也能打开管理页（有泄露风险）",
              "suggest": "建议保持 127.0.0.1（仅本机）"},
             {"path": "app.web_port", "label": "管理平台端口", "kind": "number", "min": 1, "max": 65535,
              "impact": "改完要重启 app 才生效；端口被占用会自动顺延",
              "suggest": "默认 6060"},
             {"path": "app.launch_wechat", "label": "启动时自动拉起微信", "kind": "bool",
              "impact": "关掉则 app 只等待，需要你自己开微信。"
                        "⚠️ 建议**保持关闭**：app 自己启动微信时会在微信开库**之前**注入 keyhook，"
                        "实测可能让微信对话历史变空白（见下面那条开关）",
              "suggest": "建议关；用「先手动开微信 → 再启动 app」的顺序最安全"},
             {"path": "app.auto_inject_keyhook", "label": "注入 keyhook 采集数据库密钥（有风险）",
              "kind": "bool",
              "impact": "⚠️ **默认关闭**。实测：在微信打开数据库**之前**注入 keyhook3.dll，"
                        "会让微信读不出消息库（**对话历史空白**）；库开好之后再注入则无害但采不到密钥。"
                        "抓消息**不需要**它（消息靠 hook 的回调推送）——它只用于离线解密回填历史",
              "suggest": "保持关闭（推荐）。确实要采密钥时才临时打开，并在采完后用托盘「完全恢复」把 hook 移走"},
         ]},
    ]

    DIGEST_TYPE_OPTIONS = [(1, "文本"), (3, "图片"), (34, "语音"), (42, "名片"), (43, "视频"),
                           (47, "表情（恒排除）"), (48, "位置"), (49, "链接/文件"), (50, "通话"),
                           (51, "系统"), (10000, "系统(2)"), (10002, "撤回")]

    TEMPLATE_FIELDS: list[dict] = [
        {"path": "replies.templates[].name", "label": "模板名（仅自己看的备注）", "kind": "text",
         "impact": "只是标识，方便你在列表里区分；不影响匹配", "suggest": "如 mr-gao-llm"},
        {"path": "replies.templates[].trigger", "label": "触发词", "kind": "text",
         "impact": "消息内容命中它才可能回复；留空则该模板永不触发（等于停用）",
         "suggest": "如 @姜波、报价、几点"},
        {"path": "replies.templates[].match_type", "label": "匹配方式", "kind": "select",
         "options": [("substring", "包含（子串）"), ("exact", "完全相等"), ("regex", "正则")],
         "impact": "包含最宽松；正则可以写复杂规则但容易误触发", "suggest": "建议用\"包含\""},
        {"path": "replies.templates[].response", "label": "回复内容 / 人设", "kind": "textarea",
         "impact": "开了\"用大模型生成\"时这里是**人设**（决定语气与身份）；关闭时这里是**固定回复原文**，原样发出",
         "suggest": "固定回复例：我暂时离开，稍后回复；人设例：你是姜波，说话简短随意"},
        {"path": "replies.templates[].use_llm", "label": "用大模型生成回复", "kind": "bool",
         "impact": "开启后由 LLM 按人设拟稿（消耗额度、每次内容不同）；关闭则发固定文案",
         "suggest": "想稳定可控就关；想自然就开"},
        {"path": "replies.templates[].fallback", "label": "LLM 不可用时的兜底文案", "kind": "text",
         "impact": "只在\"用大模型生成\"时生效：调用失败就发这句，避免冷场或露人设",
         "suggest": "如：我现在不方便，稍后回你"},
        {"path": "replies.templates[].test_only", "label": "只测试不真发", "kind": "bool",
         "impact": "勾上后命中只写日志、不真的发消息（调规则时强烈建议先勾）",
         "suggest": "调好再取消"},
        {"path": "replies.templates[].scope.groups", "label": "生效会话（每行一个 roomid）", "kind": "list",
         "impact": "只在这些会话里生效；留空 = 不限会话", "suggest": "如 195940014@chatroom"},
        {"path": "replies.templates[].scope.senders", "label": "生效发送人（每行一个 wxid）", "kind": "list",
         "impact": "只在这些人发言时生效；留空 = 不限人", "suggest": "如 wxid_8cjwgonnvyq822"},
    ]
    REPLY_GLOBAL: list[dict] = [
        {"path": "replies.enabled", "label": "启用自动回复", "kind": "bool",
         "impact": "总开关。⚠️ 开启后可能真的替你在微信里发言（有账号风控风险）",
         "suggest": "不确认时保持关闭；测试期把模板设为\"只测试不真发\""},
        {"path": "replies.history_count", "label": "附带最近多少条对话作为上下文", "kind": "number",
         "impact": "数值越大回复越贴合上下文，但消耗 token 越多",
         "suggest": "默认 10"},
        {"path": "replies.rate_limit.per_group_cooldown_s", "label": "同群冷却（秒）", "kind": "number",
         "impact": "两次回复的最小间隔（冷却期内的触发会排队，不丢弃）",
         "suggest": "默认 15，建议不少于 10"},
        {"path": "replies.rate_limit.global_daily_limit", "label": "每天回复上限（条）", "kind": "number",
         "impact": "超过后当天不再回复，避免刷屏", "suggest": "默认 100"},
        {"path": "replies.rate_limit.min_delay_s", "label": "回复前最小随机延迟（秒）", "kind": "number",
         "impact": "让回复更像真人；太短容易被看出是机器人", "suggest": "默认 5"},
        {"path": "replies.rate_limit.max_delay_s", "label": "回复前最大随机延迟（秒）", "kind": "number",
         "impact": "延迟上限", "suggest": "默认 30"},
    ]

    MISSING = object()          # 表单里的"空值"哨兵：配置里本来没有这一项时，别写入空值

    def _dig(d: dict, path: str, default=None):
        cur = d
        for part in path.split("."):
            if not isinstance(cur, dict):
                return default
            cur = cur.get(part)
            if cur is None:
                return default
        return cur

    def _put(d: dict, path: str, value) -> None:
        if value is MISSING:                       # 空值且原本没有 → 保持"没有"
            return
        parts = path.split(".")
        cur = d
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value

    def _form_value(f: dict, cfg: dict, errors: list[str] | None = None):
        """按字段类型把表单值解析成 Python 值。校验失败时抛 ValueError（调用方记账）。"""
        name = "f_" + f["path"].replace(".", "__").replace("[]", "")
        kind = f["kind"]
        if kind == "bool":
            cur = _dig(cfg, f["path"], None)
            if name not in request.form:
                # 复选框没提交 = 取消勾选。
                # 但**配置里本来就没有这一项**（例如老配置还没这些新字段）时保持"没有"：
                # 否则"原样保存一次"就会凭空多出 `xxx: false`，破坏"改一处不动其它"
                # 的隔离性（回归 wft_isolation_check / wft_form_check 锁着这条）。
                return MISSING if cur is None else False
            return request.form.get(name) == "on"
        if kind == "list":
            raw = request.form.get(name, "") or ""
            return [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if kind == "overrides":
            out: dict[str, str] = {}
            for ln in (request.form.get(name, "") or "").splitlines():
                ln = ln.strip()
                if not ln or "=" not in ln:
                    continue
                k, _, v = ln.partition("=")
                if k.strip():
                    out[k.strip()] = v.strip()
            return out
        if kind == "typecheck":
            picked = {int(x) for x in request.form.getlist(name)}
            if f["path"].endswith("ingest_exclude_types"):
                picked.add(47)                     # 表情恒排除
            return sorted(picked)
        if kind == "typelist":
            picked = {int(x) for x in request.form.getlist(name)}
            return sorted(picked)
        if kind == "select":
            raw = request.form.get(name)
            if raw is None:
                return MISSING          # 未提交 → 保持原值（别把下拉设成空）
            raw = raw.strip()
            opts = {str(v) for v, _ in (f.get("options") or [])}
            if opts and raw not in opts:
                raise ValueError(f"只能选 {'、'.join(sorted(opts))}，收到 {raw!r}")
            return raw
        if kind == "time":
            # HH:MM（00:00~23:59）。格式复检在 /config/save 里（回退旧值 + 报错）。
            raw = (request.form.get(name, "") or "").strip()
            if not raw:
                # 空值：字段**没被提交**（老配置/别的夹具）或原本就没有 → 不写；
                # 原本有值 → 保持原值。绝不能在这里兜默认值：
                # 那会让"原样保存一次"凭空多出 cleanup_time（隔离性回归锁着这条）。
                if name not in request.form:
                    return MISSING
                cur = _dig(cfg, f["path"], None)
                return MISSING if cur is None else cur
            return raw
        if kind == "number":
            raw = (request.form.get(name, "") or "").strip()
            if raw in ("", "None", "none"):
                if name not in request.form:
                    return MISSING
                cur = _dig(cfg, f["path"], None)
                return MISSING if cur is None else cur
            try:
                val = float(raw) if f.get("step") else int(float(raw))
            except ValueError:
                raise ValueError(f"需要填数字，收到 {raw!r}") from None
            if "min" in f and val < f["min"]:
                raise ValueError(f"不能小于 {f['min']}")
            if "max" in f and val > f["max"]:
                raise ValueError(f"不能大于 {f['max']}")
            if isinstance(val, float) and not f.get("step") and val.is_integer():
                val = int(val)
            return val
        raw_val = request.form.get(name, "")
        if raw_val is None:
            raw_val = ""
        if kind in ("textarea", "list", "overrides"):
            # 多行内容**不要 strip**：人设/固定回复里的换行与结尾换行是有意义的，
            # strip 掉会让"每次保存都悄悄改动人设"（踩过）。
            raw_val = raw_val.replace("\r\n", "\n").replace("\r", "\n")
            if not raw_val.strip() and _dig(cfg, f["path"], None) is None:
                return MISSING
            return raw_val
        raw_text = raw_val.strip()
        if not raw_text and _dig(cfg, f["path"], None) is None:
            return MISSING                             # 原本没有的项，别凭空写成空串
        return raw_text

    def _collect_templates(cfg: dict) -> list[dict]:
        """从 repeat 区块收集自动回复模板（整段重建，索引对齐）。"""
        # 字段名固定映射：直接由请求里的实际字段名反推，最不容易错位
        LEAF = {"trigger": "trigger", "match_type": "match_type", "response": "response",
                "use_llm": "use_llm", "test_only": "test_only", "fallback": "fallback",
                "scope_groups": "scope_groups", "scope_senders": "scope_senders",
                "name": "name"}
        out: list[dict] = []
        i = 0
        while True:
            if f"tpl_{i}__trigger" not in request.form and f"tpl_{i}__response" not in request.form:
                break
            t = {
                "name": (request.form.get(f"tpl_{i}__name", "") or "").strip() or f"template-{i}",
                "trigger": (request.form.get(f"tpl_{i}__trigger", "") or "").strip(),
                "match_type": request.form.get(f"tpl_{i}__match_type", "substring"),
                # 多行内容原样保留（人设里的换行有意义，别 strip）
                "response": (request.form.get(f"tpl_{i}__response", "") or "").replace("\r\n", "\n"),
                "use_llm": request.form.get(f"tpl_{i}__use_llm") == "on",
                "test_only": request.form.get(f"tpl_{i}__test_only") == "on",
                "scope": {
                    "groups": [ln.strip() for ln in (request.form.get(f"tpl_{i}__scope_groups", "") or "").splitlines() if ln.strip()],
                    "senders": [ln.strip() for ln in (request.form.get(f"tpl_{i}__scope_senders", "") or "").splitlines() if ln.strip()],
                },
            }
            fb = (request.form.get(f"tpl_{i}__fallback", "") or "").strip()
            if fb:
                t["fallback"] = fb
            if t["trigger"] or t["response"]:
                out.append(t)
            i += 1
        return out

    @app.route("/config")
    def config_page():
        cfg = load_config()
        old_key = str(_dig(cfg, "llm.api_key", "") or "").strip()
        sections = []
        for sec in FIELDS:
            fields = []
            for f in sec["fields"]:
                if f["kind"] == "typecheck":
                    cur = set(_dig(cfg, f["path"], [47]) or [])
                    f = {**f, "options": [{"v": v, "t": t, "on": v in cur} for v, t in DIGEST_TYPE_OPTIONS]}
                elif f["kind"] == "typelist":
                    cur = set(_dig(cfg, f["path"], []) or [])
                    f = {**f, "options": [{"v": v, "t": t, "on": v in cur} for v, t in DIGEST_TYPE_OPTIONS]}
                elif f["kind"] == "password":
                    saved = bool(str(_dig(cfg, f["path"], "") or "").strip())
                    f = {**f, "value": "", "key_saved": saved,
                         "key_hint": _mask_key(str(_dig(cfg, f["path"], "") or "")) if saved else ""}
                else:
                    v = _dig(cfg, f["path"], None)
                    if f["kind"] == "list":
                        v = "\n".join(str(x) for x in (v or []))
                    elif f["kind"] == "overrides":
                        v = "\n".join(f"{k}={val}" for k, val in (v or {}).items())
                    elif f["kind"] == "number":
                        v = "" if v is None else v          # 缺失就留空，别渲染成 "None"
                    elif v is None:
                        v = ""
                    f = {**f, "value": v}
                if f["kind"] == "typecheck":
                    f = {**f, "name": "f_" + f["path"].replace(".", "__")}
                else:
                    f = {**f, "name": "f_" + f["path"].replace(".", "__").replace("[]", "")}
                fields.append(f)
            sections.append({**sec, "fields": fields})

        # 自动回复：全局项 + 每个模板一套 repeat 字段
        reply_sec = {"key": "replies", "title": "自动回复", "fields": REPLY_GLOBAL,
                     "note": "⚠️ 开启后可能真的替你在微信里发言（账号风控风险）。测试期请先勾『只测试不真发』。"}
        templates = _dig(cfg, "replies.templates", []) or []
        tpl_blocks = []
        for i, t in enumerate(templates):
            rows = []
            for tf in TEMPLATE_FIELDS:
                leaf = tf["path"].split(".")[-1].replace("[]", "")
                # 表单字段名：scope.groups → tpl_i__scope_groups（保存端按这个名字读）
                fname = ("scope_" + leaf) if tf["path"].startswith("replies.templates[].scope.") else leaf
                if tf["kind"] == "list":
                    cur = (t.get("scope") or {}).get(leaf, []) if tf["path"].startswith("replies.templates[].scope.") \
                        else (t.get(leaf) or [])
                    cur = "\n".join(str(x) for x in (cur or []))
                elif tf["kind"] == "bool":
                    cur = bool(t.get(leaf))
                else:
                    cur = t.get(leaf, "") or ""
                rows.append({**tf, "name": f"tpl_{i}__{fname}", "value": cur})
            tpl_blocks.append({"index": i, "name": t.get("name") or f"template-{i}", "fields": rows})
        reply_fields = []
        for f in (reply_sec["fields"] if reply_sec else []):
            v = _dig(cfg, f["path"], None)
            if f["kind"] == "list":
                v = "\n".join(str(x) for x in (v or []))
            elif v is None:
                v = ""
            reply_fields.append({**f, "value": v, "name": "f_" + f["path"].replace(".", "__")})

        ctx = _config_context()
        return render_template("config_form.html", sections=sections, reply=reply_sec,
                               reply_fields=reply_fields, tpl_blocks=tpl_blocks,
                               config_path=str(CONFIG_PATH),
                               saved=request.args.get("saved") or "",
                               error=request.args.get("error") or "",
                               **ctx)

    @app.route("/config/focus/<gid>")
    def config_focus_page(gid: str):
        """某个群的重点关注人选择页（勾选式）。

        ⚠️ 10-07 起语义变了：重点人**不再过滤归档范围**（监控名单里的群一律整群归档，
        因为总结需要上下文），勾选的作用是"在总结里打 ★"。页面文案已同步。
        """
        cfg = load_config()
        labels = load_labels()
        g = (labels.get("groups") or {}).get(gid) or {}
        lab_senders = labels.get("senders") or {}

        def disp(sid: str) -> str:
            return ((lab_senders.get(sid) or {}).get("name") or "").strip() or sid

        focus = [m for m in (g.get("focus_members") or []) if m]
        # 离线导入过微信库的话，这里能拿到**该群的真实成员**：成员优先展示、标"本群"。
        try:
            from consumer import wechat_offline
            cache = wechat_offline.load_cache()
            members = set((cache.get("members") or {}).get(gid) or [])
            cache_names = cache.get("contacts") or {}
        except Exception:  # noqa: BLE001
            members, cache_names = set(), {}

        senders = []
        for sid in sorted(set(lab_senders) | set(focus) | members):
            if sid.endswith("@chatroom"):
                continue
            ent = lab_senders.get(sid) or {}
            named = bool((ent.get("name") or "").strip()) or bool(ent.get("important"))
            is_member = sid in members
            if not named and sid not in focus and not is_member:
                continue                     # 没标记、又不是本群成员 → 不进候选
            nm = disp(sid)
            if nm == sid and cache_names.get(sid):
                nm = str(cache_names[sid])       # 用离线导入的名字兜底显示
            senders.append({"id": sid, "name": nm,
                            "important": bool(ent.get("important")), "member": is_member})
        # 本群成员优先，其次已选中的，最后按名字
        senders.sort(key=lambda x: (not x["member"], x["id"] not in focus, x["name"]))
        return render_template(
            "config_focus.html", gid=gid,
            gname=((g.get("name") or "").strip() or gid),
            focus=focus, senders=senders,
            n_keywords=len([k for k in (g.get("keywords") or []) if str(k).strip()]),
            hint=str(g.get("summary_hint") or ""),
        )

    @app.route("/config/filter/save", methods=["POST"])
    def config_filter_save():
        """监控名单的增删改（群 / 人 / 每个群的重点关注人 / 每群总结提示）。

        一次请求只做一件事（`action` 决定），避免大表单里改一处丢一处：
          add_group / remove_group        —— 监控的群
          add_sender / remove_sender      —— 监控的人
          set_focus                       —— 某个群的重点关注人（复选框全量替换）
          focus_add / focus_remove        —— 单个重点成员的增删
          save_hints                      —— **每群总结提示**（按 `hint_<群id>` 字段全量提交）
        """
        cfg = load_config()
        filt = cfg.setdefault("filter", {})
        labels = load_labels()
        lab_groups = labels.setdefault("groups", {})
        action = (request.form.get("action") or "").strip()
        gid = (request.form.get("group") or "").strip()
        sid = (request.form.get("sender") or "").strip()

        def _lst(key: str) -> list[str]:
            return [str(x).strip() for x in (filt.get(key) or []) if str(x).strip()]

        err = ""
        if action == "add_group":
            if not gid:
                err = "没选群"
            elif gid in _lst("groups"):
                err = "这个群已经在监控名单里了"
            else:
                filt["groups"] = _lst("groups") + [gid]
                g = lab_groups.setdefault(gid, {})
                g.setdefault("name", gid)
                g["monitored"] = True            # 标记"已加入监控"
                g.setdefault("important", False)  # 未设重点人 → 整群归档
        elif action == "remove_group":
            filt["groups"] = [x for x in _lst("groups") if x != gid]
            if gid in lab_groups:
                lab_groups[gid]["monitored"] = False
        elif action == "add_sender":
            if not sid:
                err = "没选人"
            elif sid in _lst("senders"):
                err = "这个人已经在监控名单里了"
            else:
                filt["senders"] = _lst("senders") + [sid]
                s = labels.setdefault("senders", {}).setdefault(sid, {})
                s.setdefault("name", sid)
                s["monitored"] = True
        elif action == "remove_sender":
            filt["senders"] = [x for x in _lst("senders") if x != sid]
            if sid in (labels.get("senders") or {}):
                labels["senders"][sid]["monitored"] = False
        elif action == "save_hints":
            # 每群总结提示：字段名 hint_<群id>，一次提交所有监控群的提示。
            # ⚠️ 只处理**监控名单里**的群，避免页面之外的字段被顺手写进来。
            saved_n = 0
            for gkey in _lst("groups"):
                field = f"hint_{gkey}"
                if field not in request.form:
                    continue
                raw = request.form.get(field) or ""
                hint = raw.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
                ent = lab_groups.setdefault(gkey, {})
                ent.setdefault("name", gkey)
                if hint.strip():
                    ent["summary_hint"] = hint
                else:
                    ent.pop("summary_hint", None)   # 清空 = 删掉该键，不留空串
                saved_n += 1
            if not saved_n:
                err = "没有可保存的群（监控名单为空？）"
        elif action == "set_focus":
            picked = [x.strip() for x in request.form.getlist("focus") if x.strip()]
            if gid not in lab_groups:
                err = f"{gid} 不在标定里"
            else:
                lab_groups[gid]["focus_members"] = picked
                lab_groups[gid]["important"] = False
        elif action == "focus_add":
            picked = [x.strip() for x in request.form.getlist("focus_now") if x.strip()]
            if not sid:
                err = "没选人"
            elif gid not in lab_groups:
                err = f"{gid} 不在标定里"
            else:
                cur = [m for m in (lab_groups[gid].get("focus_members") or []) if m]
                for s in ([sid] + picked):
                    if s not in cur:
                        cur.append(s)
                lab_groups[gid]["focus_members"] = cur
                lab_groups[gid]["important"] = False
        elif action == "focus_remove":
            if gid in lab_groups:
                cur = [m for m in (lab_groups[gid].get("focus_members") or []) if m and m != sid]
                lab_groups[gid]["focus_members"] = cur
        elif action == "important_toggle":
            if gid in lab_groups:
                lab_groups[gid]["important"] = request.form.get("on") == "on"
                if lab_groups[gid]["important"]:
                    lab_groups[gid]["focus_members"] = []   # 整群重点与重点人互斥
        else:
            err = f"未知操作 {action!r}"

        if err:
            return redirect(url_for("filter_page", error=err))
        try:
            save_config(cfg)
            save_labels(labels)
        except (OSError, RuntimeError) as e:
            return save_error_page("监控名单", e)
        logging.getLogger("web.app").info("监控名单已更新：action=%s group=%s sender=%s", action, gid, sid)
        return redirect(url_for("filter_page", saved=f"监控名单（{action}）"))

    @app.route("/config/save", methods=["POST"])
    def config_save():
        """表单提交 → 合并进现有配置 → 写 config.yaml（只覆盖表单负责的项）。"""
        cfg = load_config()
        errors: list[str] = []
        # 兜底（防止"改一处把整份配置清空"）：
        # 页面上任何其它按钮/表单如果误提交到这里（例如 form 嵌套导致"添加监控群"
        # 被当成整份设置提交），请求里会缺少绝大多数设置字段 → 写下去就等于把没提交的
        # 字段全清成空/False。所以这里先算"应到字段里到了多少"，太少就**整份拒绝**。
        expected: list[str] = []
        for sec in FIELDS:
            for f in sec["fields"]:
                expected.append("f_" + f["path"].replace(".", "__").replace("[]", ""))
        present = [n for n in expected if n in request.form]
        if not present or len(present) < max(3, len(expected) // 3):
            logging.getLogger("web.app").warning(
                "拒绝 /config/save：提交里只有 %d/%d 个设置字段（疑似误提交），不写盘",
                len(present), len(expected))
            return redirect(url_for("config_page",
                                    error=f"这次提交只带了 {len(present)}/{len(expected)} 个设置项，"
                                          f"不像是「总设置」页的保存操作，已拒绝写入以免清空配置。"))
        for sec in FIELDS:
            for f in sec["fields"]:
                if f["kind"] == "password":
                    name = "f_" + f["path"].replace(".", "__")
                    raw = (request.form.get(name, "") or "").strip()
                    if f["path"].endswith("api_key"):
                        if request.form.get(name + "__clear") == "on":
                            _put(cfg, f["path"], "")
                        elif raw:
                            _put(cfg, f["path"], raw)
                        # 留空 = 保持原值（什么都不做）
                    elif raw:
                        _put(cfg, f["path"], raw)
                    continue
                try:
                    val = _form_value(f, cfg, errors)
                    if f["path"] == "digest.time":
                        tv = str(val or "").strip()
                        if tv and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", tv):
                            errors.append(f"每日归档 · 每天生成时间：要写成 HH:MM（例如 23:30），现在是 {tv!r}")
                            val = _dig(cfg, "digest.time", "23:30")     # 回退旧值，别写坏
                    if f["path"] == "storage.cleanup_time":
                        # 只在**确实提交了内容**且格式不对时才回退：
                        # 字段为空/没提交时 val 是 MISSING 哨兵，不能当成非法值去报错，
                        # 更不能兜 "00:00"（凭空多出这一项会破坏隔离性，回归锁着）。
                        tv = val if isinstance(val, str) else ""
                        tv = tv.strip()
                        if tv and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", tv):
                            errors.append(f"数据清理 · 每天几点清理：要写成 HH:MM（例如 00:00），现在是 {tv!r}")
                            val = _dig(cfg, "storage.cleanup_time", MISSING)
                    if f["path"] == "storage.retention_days":
                        # 1~6 天视为非法（拒绝执行，但不阻断保存）：给用户一个明确提示。
                        try:
                            rv = int(val)
                        except (TypeError, ValueError):
                            rv = 0
                        if 0 < rv < cleanup_mod.MIN_RETENTION_DAYS:
                            errors.append(
                                f"数据清理 · 保留天数：{rv} 天太短（下限 "
                                f"{cleanup_mod.MIN_RETENTION_DAYS} 天），已保存但不会执行清理")
                    _put(cfg, f["path"], val)
                except ValueError as e:
                    errors.append(f"{sec['title']} · {f['label']}：{e}")
        # 自动回复：全局项 + 模板整段重建
        for f in REPLY_GLOBAL:
            try:
                _put(cfg, f["path"], _form_value(f, cfg, errors))
            except ValueError as e:
                errors.append(f"自动回复 · {f['label']}：{e}")
        cfg.setdefault("replies", {})["templates"] = _collect_templates(cfg)

        t = str(_dig(cfg, "digest.time", "") or "").strip()
        if t and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t) and \
                not any("生成时间" in e for e in errors):
            errors.append(f"每日归档 · 每天生成时间：要写成 HH:MM（例如 23:30），现在是 {t!r}")
            _put(cfg, "digest.time", "23:30")
        base = str(_dig(cfg, "llm.base_url", "") or "").strip()
        model = str(_dig(cfg, "llm.model", "") or "").strip()
        if base and not model:
            errors.append("大模型 · 模型名：填了 base_url 就必须填 model")

        try:
            save_config(cfg)                           # 出错也保存能保存的部分
        except (OSError, RuntimeError) as e:
            return save_error_page("总设置", e)
        if errors:
            return redirect(url_for("config_page", error=" ｜ ".join(errors)))
        logging.getLogger("web.app").info("总设置已保存（表单）")
        return redirect(url_for("config_page", saved="1"))

    # ---- 高级：按段编辑原始 JSON（等同于直接改 config.yaml） ----

    @app.route("/config/raw")
    def config_raw_index():
        cfg = load_config()
        rows = []
        for name, meta in SECTIONS.items():
            data = cfg.get(name)
            rows.append({"name": name, "title": meta["title"], "note": meta["note"],
                         "exists": data is not None,
                         "preview": json.dumps(_mask_secrets(data), ensure_ascii=False, indent=2) if data is not None else ""})
        return render_template("config.html", rows=rows,
                               unknown=sorted(k for k in cfg if k not in SECTIONS),
                               config_path=str(CONFIG_PATH),
                               saved=request.args.get("saved") or "")

    # ---- 总设置（原始 JSON 编辑，高级） ----
    # 目标：用户不必知道有哪几个页面、也不必手改 config.yaml。
    # 做法：每段一个"结构化编辑器"（JSON 文本），保存前做校验 + 自动备份，
    #       语义特殊的段（storage/digest/llm）再加针对性校验，避免写出能跑但会出错的配置。

    SECRET_KEYS = ("api_key", "key", "token", "secret", "password")
    SECTIONS: dict[str, dict] = {
        "hook": {"title": "微信 / DLL 连接", "note": "DLL 的 HTTP 地址、回调端口、本人 wxid"},
        "filter": {"title": "监控规则", "note": "哪些群/人/关键词才入库（子串匹配）"},
        "llm": {"title": "大模型", "note": "评分、自动回复、当日总结都用它"},
        "storage": {"title": "存储 / 入库闸门", "note": "数据库路径、哪些消息类型不入库（47 恒排除）"},
        "digest": {"title": "每日归档", "note": "生成时间、输出目录、排除类型、是否自动总结"},
        "replies": {"title": "自动回复", "note": "模板、人设、限速（结构较复杂，建议用 /replies 页）"},
        "voice": {"title": "语音捕获", "note": "存档目录、会话名手工映射"},
        "asr": {"title": "语音转写（ASR）", "note": "whisper 服务地址"},
        "bark": {"title": "推送（Bark，iOS）", "note": "当前默认关闭"},
        "app": {"title": "应用 / 托盘", "note": "Web 端口、是否自动拉起微信、运行模式"},
    }

    def _mask_secrets(obj):
        """把疑似密钥的字段显示成掩码（只用于展示，绝不写回）。"""
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    out[k] = _mask_secrets(v)
                elif any(s in str(k).lower() for s in SECRET_KEYS) and str(v or "").strip():
                    sv = str(v)
                    out[k] = f"{sv[:6]}…{sv[-4:]}" if len(sv) > 12 else "••••••"
                else:
                    out[k] = v
            return out
        if isinstance(obj, list):
            return [_mask_secrets(x) for x in obj]
        return obj

    def _validate_section(name: str, data) -> str | None:
        """返回错误信息（None = 通过）。只挡"能写进去但一定会出错"的情况。"""
        if not isinstance(data, dict):
            return f"{name} 段必须是一个 JSON 对象（{{...}}）"
        if name == "storage":
            excl = data.get("ingest_exclude_types")
            if excl is not None:
                if not isinstance(excl, list):
                    return "storage.ingest_exclude_types 必须是数组，例如 [47, 51]"
                try:
                    types = {int(x) for x in excl}
                except (TypeError, ValueError):
                    return "ingest_exclude_types 里必须是数字类型码（如 47）"
                if 47 not in types:
                    types.add(47)
                    data["ingest_exclude_types"] = sorted(types)
            if not str(data.get("sqlite_path") or "").strip():
                return "storage.sqlite_path 不能为空"
        elif name == "digest":
            t = str(data.get("time") or "").strip()
            if t and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
                return f'digest.time 必须是 "HH:MM"（例如 "23:30"），现在是 {t!r}'
            excl = data.get("exclude_types")
            if excl is not None:
                if not isinstance(excl, list):
                    return "digest.exclude_types 必须是数组"
                try:
                    data["exclude_types"] = sorted({int(x) for x in excl})
                except (TypeError, ValueError):
                    return "digest.exclude_types 里必须是数字类型码"
        elif name == "llm":
            base = str(data.get("base_url") or "").strip()
            model = str(data.get("model") or "").strip()
            key = str(data.get("api_key") or "").strip()
            if key and not base:
                return "填了 api_key 就必须填 base_url"
            if base and not model:
                return "填了 base_url 就必须填 model"
            pt = data.get("push_threshold")
            if pt is not None:
                try:
                    data["push_threshold"] = max(1, min(int(pt), 5))
                except (TypeError, ValueError):
                    return "llm.push_threshold 必须是 1~5 的整数"
        elif name == "hook":
            port = data.get("callback_port")
            if port is not None:
                try:
                    data["callback_port"] = int(port)
                except (TypeError, ValueError):
                    return "hook.callback_port 必须是整数"
            if not str(data.get("api_base") or "").strip():
                return "hook.api_base 不能为空"
        elif name == "app":
            wp = data.get("web_port")
            if wp is not None:
                try:
                    data["web_port"] = int(wp)
                except (TypeError, ValueError):
                    return "app.web_port 必须是整数"
        elif name == "filter":
            for k in ("groups", "senders", "keywords"):
                v = data.get(k)
                if v is not None and not isinstance(v, list):
                    return f"filter.{k} 必须是数组（每行一个）"
        return None

    @app.route("/config/raw/<name>")
    def config_section_page(name: str):
        if name not in SECTIONS:
            abort(404)
        cfg = load_config()
        data = cfg.get(name)
        if data is None:
            data = {}                                  # 该段还不存在 → 从空对象开始
        return render_template(
            "config_edit.html",
            name=name, meta=SECTIONS[name],
            text=json.dumps(data, ensure_ascii=False, indent=2),
            config_path=str(CONFIG_PATH),
            has_key=bool(((cfg.get("llm") or {}).get("api_key") or "").strip()) if name == "llm" else False,
        )

    @app.route("/config/raw/<name>/save", methods=["POST"])
    def config_section_save(name: str):
        if name not in SECTIONS:
            abort(404)
        raw = (request.form.get("text") or "").strip()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            return render_template("save_error.html", what=f"{name} 段的 JSON",
                                   error=f"JSON 语法错误：{e}"), 400
        err = _validate_section(name, data)
        if err:
            return render_template("save_error.html", what=f"{name} 段", error=err), 400

        cfg = load_config()
        cfg[name] = data
        try:
            save_config(cfg)
        except (OSError, RuntimeError) as e:
            return save_error_page(name, e)
        logging.getLogger("web.app").info(
            "总设置页保存了 %s 段：%s", name,
            json.dumps(_mask_secrets(data), ensure_ascii=False)[:300])
        return redirect(url_for("config_raw_index", saved=name))

    # ---- 大模型设置（独立入口：初始化完成之后也能改） ----
    # 初始化向导（/setup）只适合第一次；它按模板**重建整份 config.yaml**，
    # 会把你后来在别处改的东西盖掉。这个页面只动 llm 段。

    def _mask_key(key: str) -> str:
        k = (key or "").strip()
        if not k:
            return ""
        return f"{k[:6]}…{k[-4:]}" if len(k) > 12 else "已设置"

    @app.route("/llm")
    def llm_page():
        cfg = load_config()
        llm = dict(cfg.get("llm") or {})
        key = str(llm.get("api_key") or "")
        return render_template(
            "llm.html",
            llm={**llm, "api_key": ""},          # 永不回显 key
            key_saved=bool(key.strip()),
            key_hint=_mask_key(key),
            config_path=str(CONFIG_PATH),
        )

    @app.route("/llm/update", methods=["POST"])
    def llm_update():
        cfg = load_config()
        old = dict(cfg.get("llm") or {})
        base_url = (request.form.get("base_url") or "").strip()
        model = (request.form.get("model") or "").strip()
        key_in = (request.form.get("api_key") or "").strip()
        if request.form.get("clear_key") == "on":
            key_in = ""                                  # 明确要求清空
        elif not key_in:
            key_in = str(old.get("api_key") or "")       # 留空 = 不修改

        if key_in and not base_url:
            return render_template("save_error.html", what="大模型设置",
                                   error="填了 API Key 就必须同时填 base_url"), 400
        if base_url and not model:
            return render_template("save_error.html", what="大模型设置",
                                   error="填了 base_url 就必须填模型名（model）"), 400

        try:
            push_threshold = int(request.form.get("push_threshold") or 4)
        except ValueError:
            push_threshold = 4

        cfg["llm"] = {
            **old,
            "base_url": base_url,
            "api_key": key_in,
            "model": model,
            "push_threshold": max(1, min(push_threshold, 5)),
        }
        try:
            save_config(cfg)
        except (OSError, RuntimeError) as e:
            return save_error_page("大模型设置", e)
        return redirect(url_for("llm_page", saved="1"))

    @app.route("/api/llm/test", methods=["POST"])
    def api_llm_test():
        """用**表单里当前的**参数试调一次 LLM（不写配置）。用来确认 key/模型填对没。"""
        data = request.get_json(silent=True) or {}
        base_url = str(data.get("base_url") or "").strip()
        model = str(data.get("model") or "").strip()
        key = str(data.get("api_key") or "").strip()
        if not key:
            key = str((load_config().get("llm") or {}).get("api_key") or "")   # 用已保存的
        if not base_url or not model:
            return jsonify({"ok": False, "error": "base_url 与模型名都要填"}), 400
        try:
            from openai import OpenAI
            t0 = time.time()
            client = OpenAI(base_url=base_url, api_key=key or "not-needed", timeout=30.0)
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "回复两个字：可用"}],
            )
            txt = (resp.choices[0].message.content or "").strip()
            return jsonify({"ok": True, "seconds": round(time.time() - t0, 2),
                            "reply": txt[:80], "model": model})
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 200

    # ---- wxid → 昵称 自动关联（走 DLL 的 QueryDB，不用自己解密） ----
    # 背景：微信本地库加密且**密钥会轮换**（实测离线解密已失效），但注入微信的 DLL
    # 有 `POST /QueryDB/execute`，让微信用它自己的密钥去查库。所以自动关联走 DLL。

    def _hook_client():
        from consumer.hook_client import HookClient, HookConfig
        cfg = load_config()
        base = str(((cfg.get("hook") or {}).get("api_base")) or "http://127.0.0.1:30001")
        return HookClient(HookConfig(api_base=base, timeout_s=20.0))

    @app.route("/api/contacts/probe")
    def api_contacts_probe():
        """探测：列出 DLL 已挂的库、逐个找联系人表、报告命中来源（不写 labels.json）。

        换微信版本 / 表名变了时，用它一行就能看出该改哪里。
        """
        try:
            from consumer import contact_sync
            res = contact_sync.probe(_hook_client())
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}",
                            "hint": "需要微信在运行且 version.dll 已注入（仪表盘可看 DLL 状态）"}), 200
        return jsonify({"ok": bool(res.get("source")), **res})

    @app.route("/api/contacts/sync", methods=["POST"])
    def api_contacts_sync():
        """把昵称并进 labels.json。两条来源都试：

        1) **从已抓到的消息里挖**（`consumer/name_harvest`）—— 引用消息 XML 里有
           `<chatusr>wxid</chatusr><displayname>名字</displayname>`，不依赖 DLL/解密，**主路**
        2) DLL 的 QueryDB —— 实测在本机 DLL 上不可用（`g_IsLogin` 恒为 0），保留作备选

        两条都**只填空白**，不覆盖你手工改的名字。
        """
        result: dict = {"ok": False, "sources": {}, "filled": 0, "created": 0, "found": 0}

        # 1) 从自己库的消息里挖（主路）
        try:
            from consumer import name_harvest
            res = name_harvest.apply_to_labels(LABELS_PATH)
            result["sources"]["messages"] = {k: v for k, v in res.items() if k != "sample"}
            result["found"] += res.get("found", 0)
            result["filled"] += res.get("filled", 0)
            result["created"] += res.get("created", 0)
            result["sample"] = res.get("sample", {})
            result["ok"] = result["ok"] or bool(res.get("found"))
        except Exception as e:  # noqa: BLE001
            result["sources"]["messages"] = {"error": f"{type(e).__name__}: {e}"}

        # 2) DLL（本机 DLL 的 GetAllDBName 返回空，通常无用；留作将来）
        try:
            from consumer import contact_sync
            res2 = contact_sync.sync_labels(_hook_client(), LABELS_PATH)
            result["sources"]["dll"] = {k: v for k, v in res2.items() if k != "sample"}
            result["found"] += res2.get("found", 0)
            result["filled"] += res2.get("filled_senders", 0) + res2.get("filled_groups", 0)
            result["created"] += res2.get("created", 0)
            result["ok"] = result["ok"] or bool(res2.get("found"))
        except Exception as e:  # noqa: BLE001
            result["sources"]["dll"] = {"error": f"{type(e).__name__}: {e}"}

        logging.getLogger("web.app").info(
            "昵称自动关联：来源=%s 共找到 %s，补 %s / 新建 %s",
            {k: v.get("found") for k, v in result["sources"].items()},
            result["found"], result["filled"], result["created"])
        return jsonify(result)

    @app.route("/api/contacts/overrides", methods=["POST"])
    def api_contacts_overrides():
        """手工批量补充/纠正昵称。每行 `wxid<空格或逗号或制表符>昵称`，也支持 `昵称<分隔>wxid`。

        存到 `data/name_overrides.json`（优先级**高于**挖掘结果），并立即套用到 labels.json。
        """
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text") or request.form.get("text") or "")

        def looks_wxid(s: str) -> bool:
            s = s.strip()
            return bool(s) and (s.startswith("wxid_") or s.endswith("@chatroom")
                                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{3,}", s) is not None)

        from consumer import name_harvest
        added: dict[str, str] = {}
        skipped: list[str] = []
        for raw in str(text).splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in re.split(r"[\t,，;；]+|\s{1,}", line) if p.strip()]
            if len(parts) < 2:
                skipped.append(line)
                continue
            a, b = parts[0], " ".join(parts[1:]) if len(parts) == 2 else " ".join(parts[1:])
            wx, nm = (a, b) if looks_wxid(a) else (parts[-1], " ".join(parts[:-1]))
            if not (wx and nm):
                skipped.append(line)
                continue
            added[wx] = nm
        if not added:
            return jsonify({"ok": False, "error": "没有解析出任何一行有效映射（每行：wxid 昵称）",
                            "skipped": skipped[:10]}), 200

        cur = name_harvest.load_overrides()
        cur.update(added)
        name_harvest.save_overrides(cur)
        res = name_harvest.apply_to_labels(LABELS_PATH)
        logging.getLogger("web.app").info("手工补充昵称 %d 条，套用结果：补 %s / 新建 %s",
                                          len(added), res.get("filled"), res.get("created"))
        return jsonify({"ok": True, "parsed": len(added), "skipped": skipped[:10],
                        "applied": {k: v for k, v in res.items() if k != "sample"},
                        "sample": dict(list(added.items())[:8])})

    @app.route("/api/contacts/harvest")
    def api_contacts_harvest_stats():
        """只统计：从已抓消息里能挖出多少 wxid→昵称（不写任何文件）。"""
        from consumer import name_harvest
        names = name_harvest.harvest()
        return jsonify({"ok": bool(names), "found": len(names),
                        "db": str(name_harvest._db_path()),
                        "sample": dict(list(names.items())[:15])})

    @app.route("/api/contacts/wechat-db", methods=["GET", "POST"])
    def api_contacts_wechat_db():
        """离线读取微信本地库，导入**昵称 / 群名 / 群成员**。

        GET  → 报告现有缓存状态（不读库）
        POST → 真正导入：读 contact.db（只读快照）→ 写 `data/wechat_contacts.json`
               → 把昵称合并进 `labels.json`

        POST 可带 `{"overwrite": true|false}`：**默认 true** —— 微信库里能查到名字（备注/昵称）
        就覆盖标定名（原名会存进 `name_prev` 以便回退）。传 false 则只补空白。

        **不注入、不启动、不碰微信进程** —— 因此不会出现
        "早期注入 keyhook 导致微信读不出消息库、对话历史空白"的风险。
        需要 `sqlcipher3`（源码侧依赖，故意没打进 exe）。
        """
        from consumer import wechat_offline
        if request.method == "GET":
            cache = wechat_offline.load_cache()
            return jsonify({"ok": True, "cache": str(wechat_offline.CACHE_PATH),
                            "imported_at": cache.get("imported_at"),
                            "counts": {k: len(cache.get(k) or {}) for k in ("contacts", "groups")},
                            "keys_file": str(wechat_offline.keys_path_default()),
                            "keys_exist": wechat_offline.keys_path_default().is_file()})
        body = request.get_json(silent=True) or {}
        overwrite = body.get("overwrite", request.form.get("overwrite", "1"))
        overwrite = str(overwrite).lower() not in ("0", "false", "no", "off")
        try:
            res = wechat_offline.run_import(overwrite=overwrite)
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)}), 200
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 200
        logging.getLogger("web.app").info(
            "离线导入微信库（overwrite=%s）：联系人 %s / 群 %s / 群成员 %s；标定补 %s 新建 %s 覆盖 %s",
            overwrite, res["counts"]["contacts"], res["counts"]["groups"], res["counts"]["members"],
            res["filled"], res["created"], res.get("overwritten"))
        return jsonify(res)

    # ---- 标定 ----

    @app.route("/labels")
    def labels_page():
        try:
            return _labels_page()
        except RuntimeError as e:
            # 消息库打不开（被占用/权限不足）→ 可读提示，别甩 500
            return render_template("save_error.html", what="标定", error=str(e)), 503

    def _labels_page():
        labels = load_labels()
        # 数据库里出现过的 ID（用于新增标定）
        db_groups = {r["group_name"] for r in db_query("SELECT DISTINCT group_name FROM messages")}
        db_senders = {r["sender"] for r in db_query("SELECT DISTINCT sender FROM messages")}
        # 每个已标定群里的已知发送人（用于勾选"重点成员"）
        group_senders: dict[str, list[dict]] = {}
        for gk in labels.get("groups", {}):
            rows = db_query(
                "SELECT sender, COUNT(*) AS n FROM messages WHERE group_name = ? "
                "GROUP BY sender ORDER BY n DESC LIMIT 40",
                (gk,),
            )
            focus = set(labels["groups"].get(gk, {}).get("focus_members") or [])
            group_senders[gk] = [
                {
                    "wxid": r["sender"],
                    "count": r["n"],
                    "name": (labels.get("senders", {}).get(r["sender"], {}) or {}).get("name", ""),
                    "focus": r["sender"] in focus,
                }
                for r in rows
            ]
        return render_template(
            "labels.html",
            labels=labels,
            db_groups=sorted(db_groups),
            db_senders=sorted(db_senders),
            group_senders=group_senders,
            group_keys=list(labels.get("groups", {})),
            # 每群"总结提示"（原样文本，不按关键词切分 —— 它是给 LLM 的口径说明）
            hint_list=[str((labels.get("groups", {}).get(gk) or {}).get("summary_hint") or "")
                       for gk in labels.get("groups", {})],
        )

    @app.route("/labels/save", methods=["POST"])
    def labels_save():
        """保存标定。

        除了显示名 / ★重点，本页还管**每群敏感关键词**（进归档的判定条件之一）。
        因为标定条目里还有别的字段（`focus_members`、以后可能再加的），保存时
        **只更新被提交的字段**，不重建整个条目 —— 免得把没在本页出现的字段弄丢。
        """
        labels = load_labels()
        labels.setdefault("groups", {})
        labels.setdefault("senders", {})

        # 表单字段格式：labels[kind][key]_name / labels[kind][key]_important
        for kind in ("groups", "senders"):
            for key, entry in labels[kind].items():
                name_field = f"labels[{kind}][{key}]_name"
                if name_field in request.form:
                    entry["name"] = request.form.get(name_field, "").strip()
                imp_field = f"labels[{kind}][{key}]_important"
                if imp_field in request.form:
                    entry["important"] = request.form.get(imp_field) == "on"
                else:
                    entry["important"] = False   # 复选框没勾就不会提交，这里等于"取消重点"
                labels[kind][key] = entry

        # 新增标定：labels_new[kind]_key / _name / _important
        for kind in ("groups", "senders"):
            i = 0
            while True:
                new_key = request.form.get(f"labels_new[{kind}]_key_{i}", "").strip()
                if not new_key:
                    break
                new_name = request.form.get(f"labels_new[{kind}]_name_{i}", "").strip()
                new_imp = request.form.get(f"labels_new[{kind}]_important_{i}") == "on"
                labels[kind][new_key] = {"name": new_name, "important": new_imp}
                i += 1

        # 重点成员 + 敏感关键词：按 group_key 数组顺序对齐解析。
        # 用数组而不是 focus[i] 这类数字下标，是为了让同一群的"勾选成员 + 手工补充"
        # 合并成一个字段后，后面的群不会因为前面群多勾了几个而错位。
        group_keys = request.form.getlist("group_key")
        focus_lists = request.form.getlist("group_focus")
        keyword_lists = request.form.getlist("group_keywords")
        hint_lists = request.form.getlist("group_hints")
        groups = labels["groups"]
        for i, gkey in enumerate(group_keys):
            if gkey not in groups:
                continue
            entry = groups[gkey]
            if i < len(focus_lists):
                members = parse_keywords(focus_lists[i])   # 同样支持换行/逗号/顿号/注释
                entry["focus_members"] = members
            if i < len(keyword_lists):
                entry["keywords"] = parse_keywords(keyword_lists[i])
            if i < len(hint_lists):
                # 总结提示是**给 LLM 的自然语言**：不切分、不 strip 内容（保留换行），
                # 全空就删掉该键，避免 labels.json 里堆一串 summary_hint: ""
                hint = str(hint_lists[i] or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
                if hint.strip():
                    entry["summary_hint"] = hint
                else:
                    entry.pop("summary_hint", None)

        # 兼容旧版表单（focus_key[i] / focus[i] / focus_extra[i]）：没有新字段时才走这里
        if not group_keys:
            i = 0
            while True:
                fkey = request.form.get(f"focus_key[{i}]")
                if fkey is None:
                    break
                if fkey in groups:
                    checked = request.form.getlist(f"focus[{i}]")
                    extra = parse_keywords(request.form.get(f"focus_extra[{i}]", "") or "")
                    groups[fkey]["focus_members"] = list(dict.fromkeys([m for m in checked if m] + extra))
                i += 1

        try:
            save_labels(labels)
        except (OSError, RuntimeError) as e:
            return save_error_page("标定/敏感关键词", e)
        return redirect(url_for("labels_page"))

    @app.route("/labels/quick_save", methods=["POST"])
    def labels_quick_save():
        """从消息浏览页一键给单个 wxid/roomid 加标定。"""
        labels = load_labels()
        kind = request.form.get("kind", "")
        key = (request.form.get("key", "") or "").strip()
        name = (request.form.get("name", "") or "").strip()
        important = request.form.get("important") == "on"
        redirect_to = request.form.get("redirect", "")

        if kind not in ("groups", "senders") or not key:
            return ("bad request", 400)
        if not name and not important:
            # 名称和重点都没填 = 等于删标定
            labels.setdefault(kind, {}).pop(key, None)
        else:
            labels.setdefault(kind, {})[key] = {"name": name, "important": important}
        try:
            save_labels(labels)
        except (OSError, RuntimeError) as e:
            return save_error_page("标定", e)

        if redirect_to:
            return redirect(redirect_to)
        return redirect(url_for("browse_page"))

    # ---- 浏览 ----

    @app.route("/browse")
    def browse_page():
        try:
            return _browse_page()
        except RuntimeError as e:
            # 消息库打不开（被占用/权限）→ 显示可读提示，不要甩 500
            return render_template("save_error.html", what="消息浏览", error=str(e)), 503

    def _browse_page():
        labels = load_labels()
        per_page = max(10, min(int(request.args.get("per_page", "50")), 200))
        page = max(1, int(request.args.get("page", "1")))
        only_important = request.args.get("important") == "1"
        channel = request.args.get("channel", "").strip()   # group | private | ""
        msg_type = request.args.get("msg_type", "").strip()  # "1"/"51"/"other"/""
        group = request.args.get("group", "").strip() or None
        sender = request.args.get("sender", "").strip() or None
        keyword = request.args.get("keyword", "").strip() or None

        # 下拉候选：**已标定的全部** + 最近活跃的若干个（未标定的补充）。
        # 以前只取"最近活跃 20 个"，导致久未发言的已标定群（例如 639 条消息的弟子群）
        # 在下拉里看不到 —— 用户明确反馈过。已标定的一律列出，未标定的最多补 20 个。
        RECENT = 20
        lab_groups = labels.get("groups", {}) or {}
        lab_senders = labels.get("senders", {}) or {}

        def _label(d: dict, k: str) -> str:
            return ((d.get(k) or {}).get("name") or "").strip()

        recent_groups = db_query(
            "SELECT group_name AS k, COUNT(*) AS n, MAX(received_at) AS last_ts "
            "FROM messages GROUP BY group_name ORDER BY last_ts DESC LIMIT ?",
            (RECENT,),
        )
        recent_senders = db_query(
            "SELECT sender AS k, COUNT(*) AS n, MAX(received_at) AS last_ts "
            "FROM messages GROUP BY sender ORDER BY last_ts DESC LIMIT ?",
            (RECENT,),
        )

        def _counts(table: str, col: str, keys: list[str]) -> dict:
            """给指定 keys 查消息条数（已标定但没在 recent 里的也要显示条数）。"""
            out: dict[str, tuple[int, int | None]] = {}
            if not keys:
                return out
            for i in range(0, len(keys), 200):
                chunk = keys[i:i + 200]
                ph = ",".join("?" * len(chunk))
                for r in db_query(
                        f"SELECT {col} AS k, COUNT(*) AS n, MAX(received_at) AS last_ts "
                        f"FROM messages WHERE {col} IN ({ph}) GROUP BY {col}", tuple(chunk)):
                    out[r["k"]] = (r["n"], r["last_ts"])
            return out

        def _opts(lab: dict, recent: list[dict], col: str) -> list[dict]:
            # 库里可能有 NULL / 空字符串的 group_name、sender（旧数据或异常记录）→ 滤掉，
            # 否则下游 startswith / 标定查询会炸（踩过：AttributeError NoneType）。
            recent = [r for r in recent if (r.get("k") or "").strip()]
            seen = {r["k"] for r in recent}
            extra_keys = [k for k in lab if k and str(k).strip() and k not in seen]
            counts = _counts("messages", col, extra_keys)
            out: list[dict] = []
            # 已标定的：先按最近活跃排，没有活跃记录的排后面
            recent_lab = [r for r in recent if _label(lab, r["k"])]
            recent_unlab = [r for r in recent if not _label(lab, r["k"])]
            rest_lab = sorted(
                extra_keys,
                key=lambda k: (-(counts.get(k, (0, 0))[1] or 0), _label(lab, k)),
            )
            for r in recent_lab:
                out.append({"k": r["k"], "n": r["n"], "label": _label(lab, r["k"]), "lab": True})
            for k in rest_lab:
                n, _ = counts.get(k, (0, None))
                out.append({"k": k, "n": n, "label": _label(lab, k), "lab": True})
            for r in recent_unlab:
                # 未标定的 **公众号**（gh_...）不是"人/群"，只在被显式筛选中时才给入口
                if r["k"].startswith("gh_") and r["k"] != (group or sender or ""):
                    continue
                out.append({"k": r["k"], "n": r["n"], "label": r["k"], "lab": False})
            out.sort(key=lambda o: (not o["lab"],))     # 已标定的排前面（稳定排序，内部保持原顺序）
            return out

        group_opts = _opts(lab_groups, recent_groups, "group_name")
        sender_opts = _opts(lab_senders, recent_senders, "sender")

        # 当前筛选值不在列表里时，前置补一条，避免翻页筛选被重置
        if group and all(g["k"] != group for g in group_opts):
            group_opts.insert(0, {
                "k": group, "n": None, "lab": group in lab_groups,
                "label": (lab_groups.get(group, {}) or {}).get("name") or group,
            })
        if sender and all(s["k"] != sender for s in sender_opts):
            sender_opts.insert(0, {
                "k": sender, "n": None, "lab": sender in lab_senders,
                "label": (lab_senders.get(sender, {}) or {}).get("name") or sender,
            })

        # 构造 WHERE 子句（count 和实际查询共用）
        where_sql = ""
        where_params: list = []
        if channel == "group":
            where_sql += " AND group_name LIKE '%@chatroom'"
        elif channel == "private":
            where_sql += " AND group_name NOT LIKE '%@chatroom'"
        if msg_type == "other":
            # 排除已知类型
            where_sql += " AND msg_type NOT IN (1,3,34,43,47,49,51,10000)"
        elif msg_type:
            where_sql += " AND msg_type = ?"
            where_params.append(int(msg_type))
        if group:
            where_sql += " AND group_name = ?"
            where_params.append(group)
        if sender:
            where_sql += " AND sender = ?"
            where_params.append(sender)
        if keyword:
            where_sql += " AND content LIKE ?"
            where_params.append(f"%{keyword}%")

        # 1) 查总数（用于分页）
        count_sql = "SELECT COUNT(*) AS n FROM messages WHERE 1=1" + where_sql
        total = db_query(count_sql, tuple(where_params))[0]["n"] if DB_PATH.exists() else 0

        # 2) 查当页（OFFSET/LIMIT）
        offset = (page - 1) * per_page
        sql = (
            "SELECT id, msg_id, group_name, sender, content, "
            "msg_type, direction, score, score_reason, received_at, priority, pushed, transcript "
            "FROM messages WHERE 1=1"
            + where_sql
        )
        params = list(where_params)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([per_page, offset])

        rows = db_query(sql, tuple(params))
        if only_important:
            filtered = []
            for r in rows:
                g_imp = labels.get("groups", {}).get(r["group_name"], {}).get("important", False)
                s_imp = labels.get("senders", {}).get(r["sender"], {}).get("important", False)
                if g_imp or s_imp:
                    r["important_match"] = True
                    filtered.append(r)
            rows = filtered

        for r in rows:
            g = labels.get("groups", {}).get(r["group_name"], {})
            s = labels.get("senders", {}).get(r["sender"], {})
            r["group_label"] = g.get("name") or r["group_name"]
            r["sender_label"] = s.get("name") or r["sender"]
            r["group_important"] = g.get("important", False)
            r["sender_important"] = s.get("important", False)
            r["ts"] = fmt_ts(r["received_at"])
            # 把原始 payload 解析成"卡片"（图片/链接/引用/文件…），模板按 kind 渲染。
            # 解析是纯函数、零依赖、不会抛异常；失败时退回原始文本预览。
            try:
                import consumer.cards as cards_mod
                r["card"] = cards_mod.parse(r.get("msg_type") or 0, r.get("content") or "")
                if (r.get("msg_type") or 0) == 34 and (r.get("transcript") or "").strip():
                    r["card"]["transcript"] = r["transcript"]
            except Exception:  # noqa: BLE001
                r["card"] = None
            r["preview"] = (r.get("content") or "")[:200]

        total_pages = max(1, (total + per_page - 1) // per_page) if total else 0
        # 超过最大页数时重定向到最后一页
        if page > total_pages and total > 0:
            from urllib.parse import urlencode
            args = request.args.to_dict(flat=False)
            args["page"] = [str(total_pages)]
            qs = urlencode({k: v[0] if len(v) == 1 else v for k, v in args.items()})
            return redirect(f"{url_for('browse_page')}?{qs}")
        return render_template(
            "browse.html",
            rows=rows,
            per_page=per_page,
            page=page,
            total=total,
            total_pages=total_pages,
            only_important=only_important,
            channel=channel,
            msg_type=msg_type,
            group=group or "",
            sender=sender or "",
            keyword=keyword or "",
            labels=labels,
            group_opts=group_opts,
            sender_opts=sender_opts,
        )

    @app.route("/types")
    def types_page():
        """消息类型分布——开发期观察用。"""
        try:
            return _types_page()
        except RuntimeError as e:
            return render_template("save_error.html", what="消息类型", error=str(e)), 503

    def _types_page():
        rows = db_query(
            "SELECT msg_type, COUNT(*) AS n FROM messages GROUP BY msg_type ORDER BY n DESC"
        )
        # 每种类型抽 1 条样本
        for r in rows:
            sample = db_query(
                "SELECT group_name, sender, substr(content,1,200) AS preview, received_at "
                "FROM messages WHERE msg_type = ? ORDER BY id DESC LIMIT 1",
                (r["msg_type"],),
            )
            r["sample"] = sample[0] if sample else None
            r["sample_ts"] = fmt_ts(r["sample"]["received_at"]) if r["sample"] else ""
        return render_template("types.html", rows=rows)

    # ---- 静态健康检查 ----

    @app.route("/api/app")
    def api_app():
        return jsonify(app_state())

    @app.route("/api/app/control", methods=["POST"])
    def api_app_control():
        """控制 app：自动回复开关 / 暂停抓取 / 完全恢复 / 退出。

        只有 app 同进程托管 Web 时才可用（独立 web.sh 没有控制器）。
        """
        if _app_controller is None:
            return jsonify({
                "ok": False,
                "error": "本 Web 未由 app 托管（独立运行），无法控制。请用托盘或从 app 启动。",
            }), 409
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or "")
        try:
            if action == "replies":
                _app_controller.set_replies(bool(data.get("value")))
            elif action == "pause":
                _app_controller.set_paused(bool(data.get("value")))
            elif action == "restore":
                import threading
                threading.Thread(target=_app_controller.restore_normal,
                                 name="restore", daemon=True).start()
            elif action == "quit":
                _app_controller.request_quit()
            else:
                return jsonify({"ok": False, "error": f"未知动作: {action}"}), 400
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "state": app_state()})

    # ---- 初始化向导 ----
    # 只在"待初始化"或用户主动进入时用到；采集与部署逻辑在 setup_env.py / deploy.py，
    # 这里只做展示与转调，避免把环境探测逻辑写进 Web 层。

    @app.errorhandler(Exception)
    def _unhandled(e):
        """兜底：任何没被接住的异常都要变成**可读 JSON**（或错误页），别给前端一个 HTML 500。

        前端 `.then(r => r.json())` 碰上 HTML 错误页会抛 "Unexpected token <"，
        在浏览器里就显示成莫名的 `TypeError: Failed to fetch` —— 排查时最容易被带偏。
        """
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        import traceback
        traceback.print_exc()
        logging.getLogger("web.app").exception("未处理异常: %s", request.path)
        if request.path.startswith("/api/"):
            return jsonify({"ok": False,
                            "error": f"{type(e).__name__}: {e}",
                            "hint": "详见 app.log / 控制台 traceback"}), 500
        return render_template("save_error.html", what=request.path, error=f"{type(e).__name__}: {e}"), 500

    @app.route("/setup")
    def setup_page():
        try:
            import setup_env
            import deploy
            import setup as setup_mod
            rep = setup_env.collect_report()
            plan = deploy.plan()
            peek = setup_mod.peek_answers_from_report(rep)
        except Exception as e:  # noqa: BLE001
            # 向导本身不能因为探测失败就打不开
            return render_template(
                "setup.html",
                rep={"ready_to_configure": False, "blockers": [f"环境探测失败: {e}"],
                     "warnings": [], "wechat": {}, "hook": {}, "account": {},
                     "data": {}, "dll_api": {}, "llm": {}, "installer": {},
                     "config": {}},
                plan={"needs_deploy": False, "steps": [], "blockers": [str(e)]},
                peek={"digest_time": "23:30", "self_wxid": "",
                      "llm_base_url": "", "llm_model": "", "llm_api_key": "",
                      "llm_api_key_saved": False, "llm_api_key_hint": "",
                      "config_path": str(CONFIG_PATH)},
            ), 200
        return render_template("setup.html", rep=rep, plan=plan, peek=peek)

    @app.route("/api/setup/report")
    def api_setup_report():
        import setup_env
        return jsonify(setup_env.collect_report())

    @app.route("/api/setup/apply", methods=["POST"])
    def api_setup_apply():
        ans = request.get_json(silent=True) or {}
        logging.getLogger("web.app").info(
            "向导保存：run_mode=%s self_wxid=%s 有key=%s base_url=%s model=%s 触发词=%s",
            ans.get("run_mode"), ans.get("self_wxid"), bool(ans.get("llm_api_key")),
            ans.get("llm_base_url"), ans.get("llm_model"),
            (ans.get("template") or {}).get("trigger"),
        )
        try:
            import setup as setup_mod
            res = setup_mod.apply_answers(ans)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("web.app").exception("向导保存失败")
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}",
                            "hint": "写入配置时出错；路径与权限见 app.log"}), 500
        return jsonify(res), (200 if res.get("ok") else 500)

    @app.route("/api/setup/deploy-dll", methods=["POST"])
    def api_setup_deploy_dll():
        try:
            import deploy
            res = deploy.deploy_orchestrate()
        except Exception as e:  # noqa: BLE001
            logging.getLogger("web.app").exception("部署 version.dll 失败")
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
        return jsonify(res), (200 if res.get("ok") else 500)

    @app.route("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "config_exists": CONFIG_PATH.exists(),
            "labels_exists": LABELS_PATH.exists(),
            "db_exists": DB_PATH.exists(),
        })

    @app.route("/api/version")
    def api_version():
        """版本自检：确认当前跑的是哪一版、关键修复是否在位。

        以前发生过"以为跑的是新包、其实是旧的"，有它就不用猜。
        `build` 取**本文件（或打包后的 pyc/自身）的修改时间**，不手写字符串 —— 免得又对不上。
        """
        import inspect
        try:
            bsrc = inspect.getsource(_browse_page)
        except (OSError, TypeError):
            bsrc = ""
        try:
            dsrc = inspect.getsource(db_query)
        except (OSError, TypeError):
            dsrc = ""
        try:
            mtime = datetime.fromtimestamp(Path(__file__).stat().st_mtime)
            build = mtime.strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            build = "unknown"
        return jsonify({
            "app": "WeChatFerryTool",
            "build": build,
            "frozen": bool(getattr(sys, "frozen", False)),
            "exe": sys.executable,
            "data_root": str(PROJECT_DIR),
            "config_path": str(CONFIG_PATH),
            "features": {
                "browse_已标定全部列出": "extra_keys" in bsrc,
                "browse_排除未标定公众号": "gh_" in bsrc,
                "db_打不开给可读提示": "RuntimeError" in dsrc,
                "监控名单在监控规则页": True,
            },
        })

    # ---- 自动回复 ----

    @app.route("/replies")
    def replies_page():
        cfg = load_config()
        replies = cfg.get("replies") or {}
        return render_template("replies.html", replies=replies)

    @app.route("/replies/update", methods=["POST"])
    def replies_update():
        cfg = load_config()
        cfg.setdefault("replies", {})
        r = cfg["replies"]

        r["enabled"] = request.form.get("enabled") == "on"

        # templates：每行一个模板字段，索引 i
        new_templates: list[dict] = []
        i = 0
        while True:
            trigger = request.form.get(f"templates[{i}][trigger]", "").strip()
            response = request.form.get(f"templates[{i}][response]", "").strip()
            delete = request.form.get(f"templates[{i}][_delete]") == "on"
            # 遇到第一个完全空的索引就停（新增模板留空时正常）
            all_empty = not request.form.get(f"templates[{i}][trigger]") \
                and not request.form.get(f"templates[{i}][response]") \
                and not request.form.get(f"templates[{i}][_delete]")
            if all_empty:
                break
            if delete:
                i += 1
                continue  # 删除这条
            if not trigger and not response:
                # 空模板不保存
                i += 1
                continue
            t = {
                "name": request.form.get(f"templates[{i}][name]", "").strip() or f"template-{i}",
                "trigger": trigger,
                "match_type": request.form.get(f"templates[{i}][match_type]", "substring"),
                "response": response,
                "scope": {
                    "groups": [ln.strip() for ln in (request.form.get(f"templates[{i}][scope_groups]", "") or "").splitlines() if ln.strip()],
                    "senders": [ln.strip() for ln in (request.form.get(f"templates[{i}][scope_senders]", "") or "").splitlines() if ln.strip()],
                },
                "test_only": request.form.get(f"templates[{i}][test_only]") == "on",
                "use_llm": request.form.get(f"templates[{i}][use_llm]") == "on",
            }
            new_templates.append(t)
            i += 1
        r["templates"] = new_templates

        rl = r.setdefault("rate_limit", {})
        rl["per_group_cooldown_s"] = int(request.form.get("rl_per_group_cooldown_s", 600))
        rl["global_daily_limit"] = int(request.form.get("rl_global_daily_limit", 30))
        rl["min_delay_s"] = int(request.form.get("rl_min_delay_s", 60))
        rl["max_delay_s"] = int(request.form.get("rl_max_delay_s", 300))

        save_config(cfg)
        return redirect(url_for("replies_page"))

    # ---- 每日归档 / 当日总结 ----
    # 「生成当日总结」= 把当天**进入归档**的发言交给 LLM 归纳（consumer/summarize.py）。
    # 两段式，避免手滑直接烧 token：
    #   预览 prompt —— 本地拼装，不联网、不花钱，先看清楚要发什么
    #   真实生成   —— 后台线程跑 LLM（可能几十秒），前端轮询进度

    def reports_dir() -> Path:
        cfg = load_config()
        return PROJECT_DIR / str((cfg.get("digest") or {}).get("dir") or "reports")

    summarize_job: dict = {"running": False, "started": 0.0, "finished": 0.0,
                          "date": "", "error": None, "result": None}
    summarize_lock = threading.Lock()

    def _summarize_worker(date_str: str) -> None:
        try:
            import consumer.summarize as summarize_mod
            res = summarize_mod.summarize(date_str, out_dir=reports_dir())
            with summarize_lock:
                if res is None:
                    summarize_job["error"] = (
                        "当天没有进入归档的消息。检查「标定」页的 ★重点群 / 重点成员 / 敏感关键词，"
                        "或用下面的「预览将发送的 prompt」看看选到了什么。"
                    )
                else:
                    summarize_job["result"] = {
                        "mode": res.mode,
                        # 兼容旧字段（页面/前端用它显示第一份）
                        "path": str(res.path) if res.path else "",
                        "name": res.path.name if res.path else "",
                        "total": res.total,
                        "conversations": len(res.results),
                        "chars": res.chars,
                        "content": res.content,
                        # 新增：每群一份时的文件清单
                        "files": res.files,
                        "items": [{"name": r.path.name, "group": r.group or "（合并）",
                                   "total": r.total, "chars": r.chars,
                                   "content": r.content} for r in res.results],
                        "errors": res.errors,
                    }
        except Exception as e:  # noqa: BLE001
            log_exc = f"{type(e).__name__}: {e}"
            with summarize_lock:
                summarize_job["error"] = log_exc
        finally:
            with summarize_lock:
                summarize_job["running"] = False
                summarize_job["finished"] = time.time()

    def _summarize_status() -> dict:
        with summarize_lock:
            st = dict(summarize_job)
        st["seconds"] = round((st["finished"] or time.time()) - st["started"], 1) if st["started"] else 0
        return st

    def _parse_date(raw: str) -> str:
        """校验 YYYY-MM-DD；不合法就返回空串（调用方给默认值）。"""
        s = (raw or "").strip()
        if not s:
            return ""
        try:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return ""

    def _safe_report(name: str) -> Path:
        """只允许读取 reports 目录下的文件（防目录穿越）。"""
        base = reports_dir().resolve()
        p = (base / name).resolve()
        if p.parent != base or not p.is_file():
            abort(404)
        return p

    @app.route("/digest")
    def digest_page():
        cfg = load_config()
        rd = reports_dir()
        wanted = _parse_date(request.args.get("date", ""))
        default_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        date_str = wanted or default_date

        reports: list[dict] = []
        if rd.is_dir():
            for p in rd.iterdir():
                if not p.is_file() or p.suffix.lower() != ".md":
                    continue
                reports.append({
                    "name": p.name,
                    "size": p.stat().st_size,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "summary": p.name.startswith("summary-"),
                    "is_date": p.name == f"digest-{date_str}.md",
                })
        reports.sort(key=lambda r: r["mtime"], reverse=True)

        preview_job = _summarize_status()
        return render_template(
            "digest.html",
            cfg=cfg.get("digest") or {},
            date_str=date_str,
            default_date=default_date,
            reports=reports[:40],
            reports_dir=str(rd),
            job=preview_job,
            llm=cfg.get("llm") or {},
        )

    @app.route("/api/digest/generate", methods=["POST"])
    def api_digest_generate():
        """只生成归档 Markdown（纯本地筛选，不调用 LLM）。"""
        data = request.get_json(silent=True) or request.form
        date_str = _parse_date(str(data.get("date") or "")) or \
            (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            import consumer.digest as digest_mod
            path = digest_mod.generate_digest(
                date_str,
                db_path=DB_PATH,
                labels_path=LABELS_PATH,
                out_dir=reports_dir(),
            )
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        # 数一下归档正文里的条目行（"- **HH:MM**"）
        total = text.count("\n- **")
        return jsonify({"ok": True, "date": date_str, "name": path.name, "path": str(path),
                        "total": total, "chars": len(text)})

    @app.route("/api/digest/preview", methods=["POST"])
    def api_digest_preview():
        """本地拼 prompt（不联网、不花钱），用来确认当天到底选了什么进归档。"""
        data = request.get_json(silent=True) or request.form
        date_str = _parse_date(str(data.get("date") or "")) or \
            (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            import consumer.summarize as summarize_mod
            collected = summarize_mod.collect(date_str)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
        if collected is None:
            return jsonify({
                "ok": False,
                "error": f"找不到数据库 {DB_PATH}（先让 consumer 跑起来，或确认账号数据目录）",
            }), 404
        grouped, total = collected
        prompt = summarize_mod.build_prompt(date_str, grouped)
        mode = summarize_mod.summarize_mode()
        per_group = [{"name": k, "n": len(v), "chars": len(summarize_mod.build_prompt(date_str, {k: v})),
                      "prompt": summarize_mod.build_prompt(date_str, {k: v})}
                     for k, v in grouped.items()]
        return jsonify({
            "ok": True,
            "date": date_str,
            "mode": mode,
            "total": total,
            "conversations": len(grouped),
            "chars": len(prompt),
            "groups": [{"name": k, "n": len(v), "sample": v[:3]} for k, v in grouped.items()],
            "prompt": prompt,                 # 合并视图（兼容旧前端）
            "per_group": per_group if mode == "per_group" else [],
        })

    def start_summarize(date_str: str) -> bool:
        """占位启动一个总结任务；已有任务在跑返回 False。

        注意：**不能在持锁时再调 `_summarize_status()`**（`threading.Lock` 不可重入，
        会自己把自己锁死），所以这里只更新标志，状态由调用方在锁外取。
        """
        with summarize_lock:
            if summarize_job["running"]:
                return False
            summarize_job.update({"running": True, "started": time.time(), "finished": 0.0,
                                  "date": date_str, "error": None, "result": None})
            return True

    @app.route("/api/digest/summarize", methods=["POST"])
    def api_digest_summarize():
        """真实调用 LLM 生成当日总结（后台线程，避免 HTTP 超时）。"""
        data = request.get_json(silent=True) or request.form
        date_str = _parse_date(str(data.get("date") or "")) or \
            (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if not start_summarize(date_str):
            return jsonify({"ok": False, "error": "已经有一个总结任务在跑，等它结束再点",
                            "job": _summarize_status()}), 409
        threading.Thread(target=_summarize_worker, args=(date_str,),
                         name="web-summarize", daemon=True).start()
        return jsonify({"ok": True, "date": date_str}), 202

    @app.route("/api/digest/summarize/status")
    def api_digest_summarize_status():
        return jsonify(_summarize_status())

    @app.route("/reports/<path:name>")
    def report_view(name: str):
        """在浏览器里看归档/总结原文。"""
        p = _safe_report(name)
        text = p.read_text(encoding="utf-8", errors="replace")
        return render_template("report.html", name=p.name, text=text)

    @app.route("/reports/<path:name>/download")
    def report_download(name: str):
        p = _safe_report(name)
        return send_file(p, as_attachment=True, download_name=p.name)

    return app


def main() -> None:
    force_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1 仅本机访问）")
    parser.add_argument("--port", type=int, default=6060, help="端口（默认 6060）")
    parser.add_argument("--debug", action="store_true", help="开发模式")
    args = parser.parse_args()

    app = create_app()
    print(f"Web 管理后台: http://{args.host}:{args.port}")
    print(f"配置文件: {CONFIG_PATH}")
    print(f"标定文件: {LABELS_PATH}")
    print(f"数据库:   {DB_PATH}")
    print("Ctrl+C 停止")
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
