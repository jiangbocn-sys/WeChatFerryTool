"""WeChatFerryTool Web 管理后台。

启动：python -m web.app
访问：http://127.0.0.1:6060
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, jsonify, redirect, render_template, request, url_for

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
LABELS_PATH = PROJECT_DIR / "data" / "labels.json"
DB_PATH = PROJECT_DIR / "data" / "messages.db"


# ------- 配置读写 -------

def force_utf8():
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
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


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
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LABELS_PATH.open("w", encoding="utf-8") as f:
        json.dump(labels, f, ensure_ascii=False, indent=2, sort_keys=True)


# ------- SQLite 查询 -------

def db_query(sql: str, params: tuple = ()) -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
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

def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.route("/")
    def dashboard():
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
            n_important_groups=sum(1 for v in labels.get("groups", {}).values() if v.get("important")),
            n_important_senders=sum(1 for v in labels.get("senders", {}).values() if v.get("important")),
        )

    # ---- filter 配置 ----

    @app.route("/filter")
    def filter_page():
        cfg = load_config()
        return render_template("filter.html", cfg=cfg.get("filter", {}))

    @app.route("/filter/update", methods=["POST"])
    def filter_update():
        cfg = load_config()
        cfg.setdefault("filter", {})
        f = cfg["filter"]

        # groups / senders / keywords 都是字符串列表
        def parse_lines(s: str) -> list[str]:
            return [ln.strip() for ln in (s or "").splitlines() if ln.strip()]

        f["groups"] = parse_lines(request.form.get("groups", ""))
        f["senders"] = parse_lines(request.form.get("senders", ""))
        f["keywords"] = parse_lines(request.form.get("keywords", ""))
        f["case_insensitive"] = request.form.get("case_insensitive") == "on"

        save_config(cfg)
        return redirect(url_for("filter_page"))

    # ---- 标定 ----

    @app.route("/labels")
    def labels_page():
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
        )

    @app.route("/labels/save", methods=["POST"])
    def labels_save():
        labels = load_labels()
        labels.setdefault("groups", {})
        labels.setdefault("senders", {})

        # 表单字段格式：labels[kind][key]_name / labels[kind][key]_important
        for kind in ("groups", "senders"):
            for key, entry in labels[kind].items():
                name = request.form.get(f"labels[{kind}][{key}]_name", entry.get("name", ""))
                imp = request.form.get(f"labels[{kind}][{key}]_important") == "on"
                labels[kind][key] = {"name": name, "important": imp}

        # 新增标定：labels_new[kind]_key / _name / _important
        for kind in ("groups", "senders"):
            for suffix in ("new",):
                # 支持多组新增字段
                i = 0
                while True:
                    new_key = request.form.get(f"labels_new[{kind}]_key_{i}", "").strip()
                    if not new_key:
                        break
                    new_name = request.form.get(f"labels_new[{kind}]_name_{i}", "").strip()
                    new_imp = request.form.get(f"labels_new[{kind}]_important_{i}") == "on"
                    labels[kind][new_key] = {"name": new_name, "important": new_imp}
                    i += 1

        # 重点成员：focus_key[i] 指明组 key；focus[i] = 勾选的成员 wxid；focus_extra[i] = 手工补充
        i = 0
        while True:
            fkey = request.form.get(f"focus_key[{i}]")
            if fkey is None:
                break
            if fkey in labels.get("groups", {}):
                checked = request.form.getlist(f"focus[{i}]")
                extra_raw = request.form.get(f"focus_extra[{i}]", "") or ""
                extra = [ln.strip() for ln in extra_raw.splitlines() if ln.strip()]
                members = list(dict.fromkeys([m for m in checked if m] + extra))
                labels["groups"][fkey]["focus_members"] = members
            i += 1

        save_labels(labels)
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
        save_labels(labels)

        if redirect_to:
            return redirect(redirect_to)
        return redirect(url_for("browse_page"))

    # ---- 浏览 ----

    @app.route("/browse")
    def browse_page():
        labels = load_labels()
        per_page = max(10, min(int(request.args.get("per_page", "50")), 200))
        page = max(1, int(request.args.get("page", "1")))
        only_important = request.args.get("important") == "1"
        channel = request.args.get("channel", "").strip()   # group | private | ""
        msg_type = request.args.get("msg_type", "").strip()  # "1"/"51"/"other"/""
        group = request.args.get("group", "").strip() or None
        sender = request.args.get("sender", "").strip() or None
        keyword = request.args.get("keyword", "").strip() or None

        # 下拉候选：最近活跃的 20 个群 / 20 个发送人（按最新收到消息时间倒序）
        group_opts = db_query(
            "SELECT group_name AS k, COUNT(*) AS n, MAX(received_at) AS last_ts "
            "FROM messages GROUP BY group_name ORDER BY last_ts DESC LIMIT 20"
        )
        for g in group_opts:
            g["label"] = (labels.get("groups", {}).get(g["k"], {}) or {}).get("name") or g["k"]
            g["extra"] = False
        sender_opts = db_query(
            "SELECT sender AS k, COUNT(*) AS n, MAX(received_at) AS last_ts "
            "FROM messages GROUP BY sender ORDER BY last_ts DESC LIMIT 20"
        )
        for s in sender_opts:
            s["label"] = (labels.get("senders", {}).get(s["k"], {}) or {}).get("name") or s["k"]
            s["extra"] = False
        # 当前筛选值不在 top20 时，前置补一条，避免翻页筛选被重置
        if group and all(g["k"] != group for g in group_opts):
            group_opts.insert(0, {
                "k": group, "n": None, "extra": True,
                "label": (labels.get("groups", {}).get(group, {}) or {}).get("name") or group,
            })
        if sender and all(s["k"] != sender for s in sender_opts):
            sender_opts.insert(0, {
                "k": sender, "n": None, "extra": True,
                "label": (labels.get("senders", {}).get(sender, {}) or {}).get("name") or sender,
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
            "SELECT id, msg_id, group_name, sender, substr(content,1,200) AS preview, "
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

    @app.route("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "config_exists": CONFIG_PATH.exists(),
            "labels_exists": LABELS_PATH.exists(),
            "db_exists": DB_PATH.exists(),
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
