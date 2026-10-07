"""wxid/roomid → 昵称 自动关联（不用手工标定）。

思路：**不自己解密**，而是借用注入微信的 DLL 的 `POST /QueryDB/execute` ——
让微信进程用它自己持有的密钥去查库，再把结果给我们。
好处：不受 SQLCipher 密钥轮换影响、不需要 sqlcipher3/zstandard、不需要快照。

契约（来自 WeChatHook-src/README.md + src/QueryDB.cpp）::

    POST /QueryDB/GetAllDBName  {}                       -> [{"dbName":"x.db","dbHandle":123}]
    POST /QueryDB/execute       {"optDbName":"x.db","SQL":"SELECT ..."}
                                                          -> {"status":0,"desc":"","data":[...]}

**关键**：`optDbName` 不能省，否则回 `{"status":-1,"desc":"get database handle which named failed"}`
（实测过）。而且磁盘文件名（contact.db）与 DLL 里挂的名字**未必相同**，
所以库名靠 `GetAllDBName` 动态发现，不硬编码。

发现流程（`discover()`）：
    1. GetAllDBName 拿库名列表
    2. 每个库里 `SELECT name FROM sqlite_master WHERE type='table'` 找看起来像联系人的表
    3. 有 contact 表 → 用 `PRAGMA table_info` 读列名，再从里面挑「wxid 列 / 备注列 / 昵称列」
       **动态拼 SQL**（微信各版本列名不同，硬编码必然失效）
    4. 没有 contact 表就退而求其次找 chatroom_member 之类的群成员表
    5. 结果缓存在 data/contact_source.json —— 之后同步直接复用，失效了会自动重发现

对外主要接口
------------
    probe(hook)                      # 诊断：报告库列表 + 每个候选查询的可用性（不写业务文件）
    fetch_names(hook)                # 返回 {wxid: 昵称}
    sync_labels(hook, labels_path)   # 并进 labels.json（只填空白，绝不覆盖手工名）

命令行（源码侧）
----------------
    python -m consumer.contact_sync --probe
    python -m consumer.contact_sync --sync
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paths import data_root  # noqa: E402

log = logging.getLogger("consumer.contact_sync")

PROJECT_DIR = data_root()
LABELS_PATH = PROJECT_DIR / "data" / "labels.json"
SOURCE_CACHE = PROJECT_DIR / "data" / "contact_source.json"

# 列名候选：wxid 一列 + 显示名若干列（按优先级：备注 > 昵称 > 别名）
WXID_COLS = ("username", "user_name", "member_id", "wxid", "userName", "UserName")
NAME_COLS = ("remark", "conremark", "display_name", "nickname", "nick_name",
             "alias", "Remark", "NickName", "DisplayName")

CONTACT_TABLE_HINTS = ("contact", "rcontact")
MEMBER_TABLE_HINTS = ("chatroom_member", "chatroommember", "roommember", "ChatRoomUser")

BLOCKED_COL = re.compile(r"[^A-Za-z0-9_]")


def _ok(res: dict) -> bool:
    """DLL 约定：status == 0 才算成功（注意不是 ret）。"""
    return isinstance(res, dict) and int(res.get("status", -1) or 0) == 0


def _rows(res) -> list[dict]:
    if isinstance(res, list):
        return [r for r in res if isinstance(r, dict)]
    if isinstance(res, dict) and isinstance(res.get("data"), list):
        return [r for r in res["data"] if isinstance(r, dict)]
    return []


def _pick(row: dict, cols) -> str:
    low = {str(k).lower(): v for k, v in row.items()}
    for c in cols:
        v = low.get(c.lower())
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _q(hook, db: str, sql: str) -> list[dict]:
    return _rows(hook.query_db(sql, db))


def list_dbs(hook) -> list[str]:
    out: list[str] = []
    try:
        for item in hook.list_dbs():
            nm = str(item.get("dbName") or item.get("db_name") or "").strip()
            if nm and nm not in out:
                out.append(nm)
    except Exception as e:  # noqa: BLE001
        log.warning("GetAllDBName 失败: %s", e)
    return out


def _tables(hook, db: str) -> list[str]:
    try:
        return [str(r.get("name") or "") for r in
                _q(hook, db, "SELECT name FROM sqlite_master WHERE type='table'")]
    except Exception:  # noqa: BLE001
        return []


def _columns(hook, db: str, table: str) -> list[str]:
    if BLOCKED_COL.search(table):
        return []
    try:
        return [str(r.get("name") or "") for r in _q(hook, db, f"PRAGMA table_info({table})")]
    except Exception:  # noqa: BLE001
        return []


def _build_sql(table: str, cols: list[str]) -> str | None:
    """按实际列名拼 SQL：取 wxid 列 + 最优先的显示名列。"""
    low = {c.lower(): c for c in cols}
    wxid = next((low[c.lower()] for c in WXID_COLS if c.lower() in low), None)
    if not wxid:
        return None
    name_cols = [low[c.lower()] for c in NAME_COLS if c.lower() in low]
    if not name_cols:
        return None
    parts = ", ".join(f"NULLIF({c}, '')" for c in name_cols)
    expr = f"COALESCE({parts}, {wxid})"
    return (f"SELECT {wxid} AS wxid_col, {expr} AS name_col FROM {table} "
            f"WHERE {wxid} IS NOT NULL AND {wxid} <> ''")


def discover(hook, verbose: bool = False) -> dict:
    """发现「哪个库的哪张表能给出 wxid→昵称」。返回 plan，并写入缓存。"""
    dbs = list_dbs(hook)
    if verbose:
        print(f"库列表（{len(dbs)} 个）: {dbs}")
    plan: dict = {"dbs": dbs, "attempts": [], "source": None}

    for db in dbs:
        tables = _tables(hook, db)
        if verbose:
            print(f"  {db}: {len(tables)} 张表")
        cand = ([t for t in tables if t.lower() in CONTACT_TABLE_HINTS]
                or [t for t in tables if any(h in t.lower() for h in CONTACT_TABLE_HINTS)] or [])
        kind = "contact"
        if not cand:
            cand = [t for t in tables if any(h.lower() in t.lower() for h in MEMBER_TABLE_HINTS)]
            kind = "member"
        for t in cand:
            cols = _columns(hook, db, t)
            sql = _build_sql(t, cols)
            att = {"db": db, "table": t, "kind": kind, "columns": cols, "sql": sql, "ok": False,
                   "rows": 0, "error": ""}
            if not sql:
                att["error"] = f"表里找不到可用的 wxid/昵称 列（列={cols[:12]}）"
                plan["attempts"].append(att)
                continue
            try:
                rows = _q(hook, db, sql)
                att["rows"] = len(rows)
                att["ok"] = len(rows) > 0
                att["sample"] = rows[:3]
                if not att["ok"]:
                    att["error"] = "查询成功但 0 行"
            except Exception as e:  # noqa: BLE001
                att["error"] = f"{type(e).__name__}: {e}"
            plan["attempts"].append(att)
            if att["ok"]:
                plan["source"] = {"db": db, "table": t, "kind": kind, "sql": sql}
                if verbose:
                    print(f"    ✅ 命中 {db} / {t}（{att['rows']} 行）")
                    print(f"       SQL: {sql}")
                _save_plan(plan)
                return plan
    if verbose:
        print("    ❌ 没有找到可用的联系人来源")
    return plan


def _save_plan(plan: dict) -> None:
    try:
        SOURCE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SOURCE_CACHE.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log.debug("缓存联系人来源失败: %s", e)


def load_cached_source() -> dict | None:
    """读来源缓存。**只做结构校验**（能否真的查通要等 fetch_names 实测）。"""
    if not SOURCE_CACHE.is_file():
        return None
    try:
        d = json.loads(SOURCE_CACHE.read_text(encoding="utf-8"))
        src = d.get("source")
        if isinstance(src, dict) and src.get("sql") and src.get("db"):
            return src
    except Exception:  # noqa: BLE001
        pass
    return None


def probe(hook) -> dict:
    """诊断用：库列表 + 所有尝试的可用性。不写 labels.json。"""
    plan = discover(hook, verbose=False)
    return {"dbs": plan["dbs"], "source": plan["source"], "attempts": plan["attempts"]}


def fetch_names(hook, force_rediscover: bool = False) -> dict[str, str]:
    """返回 {wxid/roomid: 显示名}。优先用缓存来源；**查不通就自动重新发现**。"""
    src = None if force_rediscover else load_cached_source()
    if src is None:
        src = discover(hook).get("source")
    if src is None:
        log.warning("没找到可用的联系人来源（先跑 probe 看诊断）")
        return {}

    rows: list[dict] | None = None
    failed = False
    try:
        res = hook.query_db(src["sql"], src["db"])
        if _ok(res):
            rows = _rows(res)
        else:
            failed = True
            log.warning("缓存来源查询未成功（%s / %s）：%s",
                        src.get("db"), src.get("table"), str(res)[:160])
    except Exception as e:  # noqa: BLE001
        failed = True
        log.warning("缓存来源查询异常（%s / %s）: %s", src.get("db"), src.get("table"), e)

    # 缓存指向的库/表已失效（换微信版本、库名变了）→ 重新发现一次
    if failed and not force_rediscover:
        log.info("来源失效，重新发现联系人来源…")
        return fetch_names(hook, force_rediscover=True)
    if rows is None:
        return {}

    names: dict[str, str] = {}
    for r in rows:
        wx = _pick(r, ("wxid_col", "wxid", "username", "user_name", "member_id"))
        nm = _pick(r, ("name_col", "name", "remark", "nickname", "nick_name", "alias"))
        if not wx or not nm or nm == wx:
            continue
        names.setdefault(wx, nm)
    log.info("联系人来源 %s / %s，拿到 %d 个名字",
             src.get("db"), src.get("table"), len(names))
    return names


def _load_labels(p: Path) -> dict:
    if not p.is_file():
        return {"groups": {}, "senders": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"groups": {}, "senders": {}}
    d.setdefault("groups", {})
    d.setdefault("senders", {})
    return d


def sync_labels(hook, labels_path: Path | None = None, create_missing: bool = True) -> dict:
    """把 DLL 查到的昵称并进 labels.json。

    规则（重要）：
    * **只填空白**：已有非空 name 的条目绝不覆盖（你手工改的名字优先）
    * `create_missing=True` 时，为"消息库里出现过但没标定"的对话/发送人也建条目
    * 自动填的名字会打上 `"auto": true` 标记，方便你之后区分
    """
    labels_path = labels_path or LABELS_PATH
    labels = _load_labels(labels_path)
    names = fetch_names(hook)

    filled_g = filled_s = created = 0
    for wx, nm in names.items():
        kind = "groups" if wx.endswith("@chatroom") else "senders"
        ent = labels[kind].get(wx)
        if ent is None:
            if not create_missing:
                continue
            labels[kind][wx] = {"name": nm, "important": False, "auto": True}
            created += 1
        elif not (ent.get("name") or "").strip():
            ent["name"] = nm
            ent["auto"] = True
            if kind == "groups":
                filled_g += 1
            else:
                filled_s += 1

    if names:
        labels_path.parent.mkdir(parents=True, exist_ok=True)
        labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=2, sort_keys=True),
                               encoding="utf-8")
    return {"ok": bool(names), "found": len(names), "filled_groups": filled_g,
            "filled_senders": filled_s, "created": created,
            "labels_path": str(labels_path), "sample": dict(list(names.items())[:8])}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description="wxid → 昵称 自动关联（走 DLL 的 QueryDB）")
    ap.add_argument("--probe", action="store_true", help="诊断：列出库与候选查询结果")
    ap.add_argument("--sync", action="store_true", help="把昵称并进 labels.json（只填空白）")
    ap.add_argument("--rediscover", action="store_true", help="忽略缓存重新发现来源")
    ap.add_argument("--api-base", default=None, help="DLL 地址（默认读 config.yaml）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import yaml

    api_base = args.api_base
    if not api_base:
        try:
            cfg = yaml.safe_load((PROJECT_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
            api_base = (cfg.get("hook") or {}).get("api_base") or "http://127.0.0.1:30001"
        except Exception:  # noqa: BLE001
            api_base = "http://127.0.0.1:30001"

    from consumer.hook_client import HookClient, HookConfig

    hook = HookClient(HookConfig(api_base=api_base, timeout_s=15.0))
    try:
        print("DLL:", hook.status())
    except Exception as e:  # noqa: BLE001
        sys.exit(f"连不上 DLL（微信没开或 version.dll 没注入？）: {e}")

    if args.probe or not args.sync:
        dbs = list_dbs(hook)
        print(f"\n库列表（{len(dbs)}）: {dbs}")
        for it in discover(hook, verbose=True)["attempts"]:
            flag = "OK  " if it["ok"] else "FAIL"
            print(f"[{flag}] {it['db']} / {it['table']}  行数={it['rows']}")
            if it["ok"]:
                print(f"        SQL: {it['sql']}")
                print(f"        样例: {json.dumps(it['sample'][:2], ensure_ascii=False)[:200]}")
            else:
                print(f"        {it['error'][:200]}")
    if args.sync:
        print(json.dumps(sync_labels(hook), ensure_ascii=False, indent=2)[:900])


if __name__ == "__main__":
    main()
