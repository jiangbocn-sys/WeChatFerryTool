"""离线读取微信本地库，导入**昵称 / 群名 / 群成员**。

**不注入、不启动、不碰微信进程** —— 只把 `contact.db` 连同 `-wal` 复制一份到临时目录，
用已采集的 SQLCipher 口令解密读取。这样既拿到全量联系人，也避免了
"早期注入 keyhook 导致微信读不出消息库（对话历史空白）" 的风险。

口令来源：`D:\\JiangBo\\Documents\\wechat-keys.jsonl`（keyhook 采到的记录）。
记录形如 ``{"ts":…,"len":99,"key":"<hex>","db":"0x…"}``，`key` 解出来常常是
**SQLCipher 的 `x'<64位hex>' 口令文本**（99 字节）—— 必须**按口令文本原样透传**，
而且拼进 SQL 时要把里面的 `'` 转义成 `''`（这里踩过坑：不转义会被当语法错误，误判"密钥无效"）。

产出：
* `data/wechat_contacts.json` —— 全量 `{contacts, groups, members, imported_at, counts}`
* `data/labels.json` —— **只补空白**；且只为"我们消息里出现过 / 已标定"的 wxid 建条目，
  不会把 5000 多个陌生人塞进标定（标定页会因此变卡）。

命令行（源码侧，需要 `sqlcipher3`）::

    python -m consumer.wechat_offline --show     # 只统计，不写文件
    python -m consumer.wechat_offline --import   # 写入 contact 缓存 + 合并进 labels.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paths import data_root  # noqa: E402

log = logging.getLogger("consumer.wechat_offline")

PROJECT_DIR = data_root()
LABELS_PATH = PROJECT_DIR / "data" / "labels.json"
CACHE_PATH = PROJECT_DIR / "data" / "wechat_contacts.json"


def documents_dir() -> Path:
    """真实「文档」目录（可能被重定向到别的盘，例如 D:\\JiangBo\\Documents）。"""
    try:
        import winreg  # noqa: PLC0415
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        val, _ = winreg.QueryValueEx(key, "Personal")
        return Path(str(val).replace("%USERPROFILE%", str(Path.home())))
    except Exception:  # noqa: BLE001
        return Path.home() / "Documents"


def keys_path_default() -> Path:
    return documents_dir() / "wechat-keys.jsonl"


def find_db_dir(account: str | None = None) -> Path | None:
    """找 `<文档>\\xwechat_files\\<账号>\\db_storage`；取最近修改的那个（或按账号名过滤）。"""
    base = documents_dir() / "xwechat_files"
    if not base.is_dir():
        return None
    cands = [p for p in base.glob("*/db_storage") if (p / "contact" / "contact.db").is_file()]
    if account:
        picked = [p for p in cands if account in p.parent.name]
        cands = picked or cands
    if not cands:
        return None
    return max(cands, key=lambda p: (p / "contact" / "contact.db").stat().st_mtime)



def _key_pragma(k: bytes, mode: str) -> str:
    """拼 SQLCipher 的 PRAGMA key。

    ⚠️ `pass` 模式必须把口令里的单引号转义成 `''` —— 微信传的是 `x'<hex>'` 形式，
    本身含单引号；不转义会变成语法错误，从而**误判"密钥无效"**（真实踩过）。
    """
    if mode == "raw":
        return "PRAGMA key = \"x'%s'\"" % k.hex()
    text = k.decode("utf-8", "replace").replace("'", "''")
    return "PRAGMA key = '%s'" % text


PROFILES: list[tuple[str, str, list[str]]] = [
    ("pass-default", "pass", []),
    ("raw-default", "raw", []),
    ("pass-compat4", "pass", ["PRAGMA cipher_compatibility = 4"]),
    ("raw-compat4", "raw", ["PRAGMA cipher_compatibility = 4"]),
]


def key_candidates(keys_path: Path | None = None) -> list[bytes]:
    """从 keyhook 的 jsonl 里取出候选口令字节串。

    每条记录都会派生出原样、前 67 字节（`x'…'` 文本）、前 64 字节（裸密钥）三个候选。
    """
    path = keys_path or keys_path_default()
    if not path.is_file():
        return []
    out: list[bytes] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        hx = rec.get("key")
        if not isinstance(hx, str) or not hx:
            continue
        try:
            b = bytes.fromhex(hx)
        except ValueError:
            continue
        for cand in (b, b[:67], b[:64]):
            if cand and cand not in out:
                out.append(cand)
    return out


def open_db(db_path: Path, keys: list[bytes]) -> sqlite3.Connection | None:
    """原地只读打开（不复制）——调用方应传入快照路径。返回连接或 None。"""
    try:
        import sqlcipher3  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "缺少 sqlcipher3（离线解密用，故意没打进 exe）。"
            "请在源码环境运行：.venv\\Scripts\\python.exe -m consumer.wechat_offline --import"
        ) from e
    uri = f"file:{db_path.as_posix()}?mode=ro"
    for key in keys:
        for prof_name, mode, pragmas in PROFILES:
            conn = None
            try:
                conn = sqlcipher3.connect(uri, uri=True, timeout=3)
                cur = conn.cursor()
                cur.execute(_key_pragma(key, mode))
                for p in pragmas:
                    cur.execute(p)
                cur.execute("SELECT count(*) FROM sqlite_master")
                cur.fetchone()
                log.info("打开成功：%s（口令 %dB / %s）", db_path.name, len(key), prof_name)
                return conn
            except Exception:  # noqa: BLE001
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
    return None


def snapshot(db: Path, workdir: Path) -> Path:
    """复制 db 及 -wal/-shm 到工作目录（只读读它们，不动原文件）。"""
    dst = workdir / db.name
    shutil.copy2(db, dst)
    for suf in ("-wal", "-shm"):
        p = Path(str(db) + suf)
        if p.is_file():
            shutil.copy2(p, workdir / (db.name + suf))
    return dst


def make_workdir() -> Path:
    """解密快照的工作目录：按 候选顺序 找一个**确实可写**的目录。

    顺序：`WFT_OFFLINE_WORK` 环境变量 → 数据根下 `data/_tmp` → 系统 temp。
    每一处都做一次真实写入探测（有些环境里目录能建、文件写不进去）。
    """
    roots: list[Path] = []
    override = os.environ.get("WFT_OFFLINE_WORK")
    if override:
        roots.append(Path(override))
    roots.append(PROJECT_DIR / "data" / "_tmp")
    try:
        roots.append(Path(tempfile.gettempdir()))
    except Exception:  # noqa: BLE001
        pass

    for root in roots:
        try:
            work = root / "wc_offline_work"
            work.mkdir(parents=True, exist_ok=True)
            probe = work / "_writeprobe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            return work
        except OSError:
            continue
    raise RuntimeError("找不到可写的工作目录（可用环境变量 WFT_OFFLINE_WORK 指定）")


# ---------------- 读取 ----------------

def read_contacts(conn: sqlite3.Connection) -> dict[str, str]:
    """wxid/roomid → 显示名（备注优先，其次昵称，再别名）。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT username, COALESCE(NULLIF(remark, ''), NULLIF(nick_name, ''), "
        "NULLIF(alias, ''), username) FROM contact "
        "WHERE username IS NOT NULL AND username <> ''"
    )
    out: dict[str, str] = {}
    for wx, nm in cur.fetchall():
        wx, nm = str(wx).strip(), str(nm or "").strip()
        if wx and nm and nm != wx:
            out[wx] = nm
    return out


def read_group_members(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """roomid → [成员 wxid]（靠 name2id.rowid 关联内部 id）。"""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT cr.username, n.username FROM chatroom_member m "
            "JOIN chat_room cr ON cr.id = m.room_id "
            "JOIN name2id n ON n.rowid = m.member_id"
        )
    except sqlite3.Error as e:
        log.warning("读群成员失败：%s", e)
        return {}
    out: dict[str, list[str]] = {}
    for room, member in cur.fetchall():
        room, member = str(room or "").strip(), str(member or "").strip()
        if room and member:
            out.setdefault(room, [])
            if member not in out[room]:
                out[room].append(member)
    return out


def read_all(db_dir: Path | None = None, keys_path: Path | None = None) -> dict:
    """读出联系人与群成员。返回 {"contacts", "groups", "members", "db", "counts"}。"""
    db_dir = db_dir or find_db_dir()
    if db_dir is None:
        raise RuntimeError("找不到微信 db_storage 目录（微信没登录过 / 路径不同）")
    db = db_dir / "contact" / "contact.db"
    if not db.is_file():
        raise RuntimeError(f"找不到 {db}")

    keys = key_candidates(keys_path)
    if not keys:
        raise RuntimeError(
            f"没有可用口令：{keys_path or keys_path_default()} 不存在或为空。"
            "口令由 keyhook 采集（现在默认不再自动注入）"
        )

    work = make_workdir()
    try:
        conn = open_db(snapshot(db, work), keys)
        if conn is None:
            raise RuntimeError(
                f"{len(keys)} 个候选口令都打不开 {db.name} —— 口令可能已轮换"
                "（可先用 --show 看候选数量）"
            )
        contacts = read_contacts(conn)
        members = read_group_members(conn)
        conn.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    groups = {k: v for k, v in contacts.items() if k.endswith("@chatroom")}
    return {"contacts": contacts, "groups": groups, "members": members,
            "db": str(db), "counts": {"contacts": len(contacts), "groups": len(groups),
                                      "members": sum(len(v) for v in members.values())}}


# ---------------- 合并进标定 ----------------

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


def _wxids_in_messages() -> set[str]:
    """我们消息库里出现过的 group_name / sender（只为这些人建标定条目）。"""
    out: set[str] = set()
    db = PROJECT_DIR / "data" / "messages.db"
    if not db.is_file():
        return out
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        for col in ("group_name", "sender"):
            for (v,) in conn.execute(f"SELECT DISTINCT {col} FROM messages WHERE {col} <> ''"):
                if v:
                    out.add(str(v))
        conn.close()
    except sqlite3.Error as e:
        log.warning("读消息库失败（只影响「要建条目」的判定）：%s", e)
    return out


def merge_into_labels(names: dict[str, str], labels_path: Path | None = None,
                      known: set[str] | None = None, overwrite: bool = True) -> dict:
    """把昵称并进 labels.json。

    规则：
    * 只给"已知的 wxid"（消息里出现过 / 已在标定里）建新条目，避免把几千个陌生人塞进标定
    * 空白名字 → 直接补上
    * 已有名字：`overwrite=True`（默认，用户要求"能查到昵称就覆盖"）时**覆盖**，
      但会把原名存进 `name_prev` 便于回退，`important` 等其它字段保持不动
    """
    labels_path = labels_path or LABELS_PATH
    labels = _load_labels(labels_path)
    known = known if known is not None else _wxids_in_messages()

    filled = created = skipped = overwritten = 0
    for wx, nm in names.items():
        kind = "groups" if wx.endswith("@chatroom") else "senders"
        ent = labels[kind].get(wx)
        if ent is None:
            if wx not in known:
                skipped += 1
                continue
            labels[kind][wx] = {"name": nm, "important": False,
                                "auto": True, "source": "wechat_db"}
            created += 1
            continue
        old = (ent.get("name") or "").strip()
        if not old:
            ent["name"] = nm
            ent["auto"] = True
            ent["source"] = "wechat_db"
            filled += 1
        elif overwrite and old != nm:
            ent["name_prev"] = old              # 留个回退点，别让手工名字凭空消失
            ent["name"] = nm
            ent["auto"] = True
            ent["source"] = "wechat_db"
            overwritten += 1

    if created or filled or overwritten:
        labels_path.parent.mkdir(parents=True, exist_ok=True)
        # 覆盖标定名前先留一份备份（与 web 侧 save_labels 同样的习惯，留最近 5 份）
        try:
            import shutil as _sh
            if labels_path.is_file():
                bak = labels_path.with_name(f"{labels_path.name}.bak-{int(time.time())}")
                _sh.copy2(labels_path, bak)
                olds = sorted(labels_path.parent.glob(f"{labels_path.name}.bak-*"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
                for p in olds[5:]:
                    try:
                        p.unlink()
                    except OSError:
                        pass
        except OSError as e:
            log.warning("标定备份失败（不影响导入）：%s", e)
        tmp = labels_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(labels, ensure_ascii=False, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(labels_path)
    return {"filled": filled, "created": created, "overwritten": overwritten,
            "skipped_unknown": skipped, "labels_path": str(labels_path)}


def run_import(db_dir: Path | None = None, keys_path: Path | None = None,
               overwrite: bool = True) -> dict:
    """完整导入：读库 → 写缓存 → 合并进标定（默认用微信库的名字覆盖标定名）。"""
    data = read_all(db_dir, keys_path)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "imported_at": int(time.time()),
        "db": data["db"],
        "counts": data["counts"],
        "contacts": data["contacts"],
        "groups": data["groups"],
        "members": data["members"],
    }
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    merged = merge_into_labels(data["contacts"], overwrite=overwrite)
    log.info("离线导入完成：联系人 %d / 群 %d / 群成员 %d；标定 补 %d 新建 %d 覆盖 %d（跳过陌生人 %d）",
             data["counts"]["contacts"], data["counts"]["groups"], data["counts"]["members"],
             merged["filled"], merged["created"], merged["overwritten"], merged["skipped_unknown"])
    return {"ok": True, "counts": data["counts"], **merged,
            "cache": str(CACHE_PATH), "sample": dict(list(data["contacts"].items())[:8])}


def load_cache() -> dict:
    """读 `data/wechat_contacts.json`（没有就返回空结构）。"""
    if not CACHE_PATH.is_file():
        return {"contacts": {}, "groups": {}, "members": {}}
    try:
        d = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        d.setdefault("contacts", {})
        d.setdefault("groups", {})
        d.setdefault("members", {})
        return d
    except Exception:  # noqa: BLE001
        return {"contacts": {}, "groups": {}, "members": {}}


def display_name(wxid: str, fallback: str | None = None) -> str:
    """按"标定 → 微信库缓存 → 原样 id"取显示名。"""
    lab = _load_labels(LABELS_PATH)
    for kind in ("groups", "senders"):
        nm = ((lab.get(kind) or {}).get(wxid) or {}).get("name")
        if nm and str(nm).strip():
            return str(nm).strip()
    nm = (load_cache().get("contacts") or {}).get(wxid)
    return str(nm).strip() if nm else (fallback or wxid)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description="离线读取微信库，导入昵称/群成员")
    ap.add_argument("--show", action="store_true", help="只统计，不写任何文件")
    ap.add_argument("--import", dest="do_import", action="store_true", help="写入缓存并合并进标定")
    ap.add_argument("--keys", default=None, help="口令文件（默认 文档\\wechat-keys.jsonl）")
    ap.add_argument("--db-dir", default=None, help="微信 db_storage 目录")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    kp = Path(args.keys) if args.keys else None
    dd = Path(args.db_dir) if args.db_dir else None
    print("候选口令:", len(key_candidates(kp)))
    if args.do_import:
        print(json.dumps(run_import(dd, kp), ensure_ascii=False, indent=2)[:1200])
        return
    data = read_all(dd, kp)
    print("统计:", json.dumps(data["counts"], ensure_ascii=False))
    print("样例:", json.dumps(dict(list(data["contacts"].items())[:10]), ensure_ascii=False))
    print("群:", json.dumps(dict(list(data["groups"].items())[:5]), ensure_ascii=False))
    print("群成员样例:", json.dumps(dict(list(data["members"].items())[:2]), ensure_ascii=False)[:300])


if __name__ == "__main__":
    main()
