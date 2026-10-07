"""离线解密微信 4.x 本地消息库，把「私聊」历史回填进 messages.db。

为什么需要它
------------
aixed DLL 只能 hook 微信 PC 端**已经处理过**的消息，所以群里稳定、私聊时有时无
（会话没被加载就收不到）。而本地 SQLCipher 消息库里是完整的 —— 用 keyhook.dll
采到的密钥离线解开，就能把漏掉的私聊补齐。

三步流程
--------
1) keyhook.dll 注入微信 → 采集密钥 → 真实「文档」目录下的 wechat-keys.jsonl
2) 本脚本 --probe    : 试出「哪把密钥开哪个库」（结果缓存到 data/db_key_map.json）
3) 本脚本 --inspect  : 解密后 dump 表结构（换了微信版本时先看这个）
   本脚本 --run      : 提取私聊 + 去重入库（默认预览，加 --write 才落库）

用法
----
    python scripts/db_backfill.py --probe
    python scripts/db_backfill.py --inspect
    python scripts/db_backfill.py --run --since-hours 24            # 预览
    python scripts/db_backfill.py --run --since-hours 24 --write    # 落库

微信 4.x 的真实库结构（实测 4.1.10.27，2026-10-05）
--------------------------------------------------
* message_0.db 是「每个会话一张表」：Msg_<md5(会话id)>，另有 Name2Id(user_name,is_session)
  Name2Id 的 rowid 就是 real_sender_id 指向的 id
* 消息表列：local_id, server_id, local_type, sort_seq, real_sender_id, create_time,
  status, upload_status, download_status, server_seq, origin_source, source,
  message_content, compress_content, packed_info_data,
  WCDB_CT_message_content, WCDB_CT_source
* **内容压缩是「列压缩」**：WCDB_CT_message_content == 4 时 message_content 是
  zstd 帧（魔数 28 B5 2F FD），需要解压；== 0 时直接是明文 str
* 方向：real_sender_id == 自己（Name2Id 里 user_name 等于 self_wxid 的 rowid）
  → out，否则 in
* server_id 与 hook 的 msg_id 同源（都是 MsgSvrID），所以能跨来源去重

设计取舍（每条都对应一个真实 bug）
----------------------------------
* 原生 key 必须写成 x'<hex>'（带收尾单引号）；写成 'x{hex}' 会被当口令走 KDF，必然失败
* 微信实际传的是 SQLCipher 的 x'...' 文本形式，所以按「口令文本」原样透传才是对的
* 动态扫 message_*.db —— 本机只有 message_0.db，硬编码 message_1.db 会报错
* 必须连 -wal 一起复制 —— message_0.db-wal 有 4MB，最近记录都在里面
* 不猜 SQLCipher 参数，用试错矩阵以「能读 sqlite_master」为判定
* 复用 Store.insert_message（msg_id UNIQUE + INSERT OR IGNORE）实现自动去重
* 默认只回填私聊：群消息的发言人要另外反查（hook 已能稳定拿到群消息）
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from consumer.store import Store  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
def _default_documents_dir() -> Path:
    """取真实「文档」目录（可能被重定向到别的盘）。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        val, _ = winreg.QueryValueEx(key, "Personal")
        return Path(str(val).replace("%USERPROFILE%", str(Path.home())))
    except Exception:  # noqa: BLE001
        return Path.home() / "Documents"


DEFAULT_KEYS_PATH = _default_documents_dir() / "wechat-keys.jsonl"
KEY_MAP_PATH = PROJECT_DIR / "data" / "db_key_map.json"
MESSAGES_DB = PROJECT_DIR / "data" / "messages.db"

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# SQLCipher 参数试错矩阵：以「能读 sqlite_master」为成功判定。
# mode: raw = 把密钥字节当裸密钥写成 x'<hex>'；pass = 当口令文本原样透传
PROFILES: list[tuple[str, str, list[str]]] = [
    ("pass-default", "pass", []),                       # 微信实测命中这个
    ("pass-compat4", "pass", ["PRAGMA cipher_compatibility = 4"]),
    ("raw-default", "raw", []),
    ("raw-compat4", "raw", ["PRAGMA cipher_compatibility = 4"]),
    ("raw-compat4-ps4096", "raw", ["PRAGMA cipher_compatibility = 4", "PRAGMA page_size = 4096"]),
    ("raw-ps1024", "raw", ["PRAGMA page_size = 1024"]),
    ("raw-hmac512", "raw", [
        "PRAGMA cipher_hmac_algorithm = HMAC_SHA512",
        "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512",
        "PRAGMA page_size = 4096",
    ]),
    ("raw-kdf256k", "raw", [
        "PRAGMA kdf_iter = 256000",
        "PRAGMA cipher_hmac_algorithm = HMAC_SHA512",
        "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512",
    ]),
    ("raw-compat3", "raw", ["PRAGMA cipher_compatibility = 3"]),
]

# 列名候选：微信 4.x 实测名在前，3.x/其它分片的名字保留兼容
COL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "server_id": ("server_id", "MsgSvrID", "msgSvrId", "MsgSvrId", "serverId"),
    "local_id": ("local_id", "localId", "MsgLocalID", "rowid"),
    "talker": ("Talker", "talker", "StrTalker", "strTalker"),
    "talker_id": ("TalkerId", "talker_id"),
    "type": ("local_type", "MsgType", "msgType", "type"),
    "time": ("create_time", "CreateTime", "createTime"),
    "sender_id": ("real_sender_id", "RealSenderId", "sender_id"),
    "is_send": ("IsSend", "isSend", "is_send"),
    "content": ("message_content", "StrContent", "strContent", "content", "Content"),
    "content_ct": ("WCDB_CT_message_content", "WCDB_CT_Content"),
    "source": ("source", "Source"),
    "source_ct": ("WCDB_CT_source",),
    "compress": ("compress_content", "CompressContent", "compressContent", "Compress"),
}


# ---------------------------------------------------------------------------
# 密钥加载
# ---------------------------------------------------------------------------
def _dedup_keys(keys: list[bytes]) -> list[bytes]:
    seen: set[bytes] = set()
    out: list[bytes] = []
    for k in keys:
        for cand in (k, k.rstrip(b"\x00")):
            if cand and cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


def load_keys(path: Path) -> list[bytes]:
    """支持两种格式：

    * keyhook.dll 产出的 JSONL：每行 {"ts":..,"src":"key","len":99,"key":"<hex>","db":"0x.."}
    * 早期规划的 JSON 对象：{"message_0.db": "<base64>", ...}
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []

    try:
        obj = json.loads(text)
        if (isinstance(obj, dict) and obj and "key" not in obj
                and all(isinstance(v, str) for v in obj.values())):
            return _dedup_keys([base64.b64decode(v) for v in obj.values()])
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    keys: list[bytes] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        hexkey = rec.get("key")
        if isinstance(hexkey, str) and hexkey:
            try:
                keys.append(bytes.fromhex(hexkey))
            except ValueError:
                continue
    return _dedup_keys(keys)


# ---------------------------------------------------------------------------
# 定位微信数据目录
# ---------------------------------------------------------------------------
def find_message_dbs(self_wxid: str) -> list[Path]:
    from consumer.voice import find_wechat_files_dir

    account = find_wechat_files_dir(self_wxid)
    if not account:
        return []
    msg_dir = account / "db_storage" / "message"
    if not msg_dir.is_dir():
        return []
    return sorted(msg_dir.glob("message_*.db"))


# ---------------------------------------------------------------------------
# SQLCipher 打开
# ---------------------------------------------------------------------------
def _import_sqlcipher():
    try:
        from sqlcipher3 import dbapi2 as sc
        return sc
    except ImportError as e:  # pragma: no cover
        sys.exit(f"缺少 sqlcipher3：{e}\n请先 pip install sqlcipher3-binary")


def key_pragma(key: bytes, mode: str) -> str:
    if mode == "raw":
        # 原生密钥：注意 x'...' 的收尾单引号，缺了就会被当口令走 KDF
        return "PRAGMA key = \"x'%s'\"" % key.hex()
    text = key.decode("utf-8", "replace").replace("'", "''")
    return "PRAGMA key = '%s'" % text


def try_open(copy_path: Path, key: bytes, profile: tuple[str, str, list[str]]):
    """用给定密钥+参数尝试打开；成功（能读 sqlite_master）返回 (连接, 表数)。"""
    sc = _import_sqlcipher()
    _, mode, extra = profile
    conn = None
    try:
        conn = sc.connect(str(copy_path))
        conn.execute(key_pragma(key, mode))
        for p in extra:
            conn.execute(p)
        n = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        return conn, int(n)
    except Exception:  # noqa: BLE001  错误密钥/参数一律抛异常，属预期
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return None


def snapshot(db_path: Path, dest_dir: Path) -> Path:
    """复制主库 + -wal（+ -shm）。-wal 里通常有最近的记录，绝不能漏。"""
    dest = dest_dir / db_path.name
    shutil.copy2(db_path, dest)
    for suffix in ("-wal", "-shm"):
        src = db_path.with_name(db_path.name + suffix)
        if src.exists():
            shutil.copy2(src, dest.with_name(dest.name + suffix))
    return dest


# ---------------------------------------------------------------------------
# key × db 配对
# ---------------------------------------------------------------------------
def load_key_map() -> dict:
    if KEY_MAP_PATH.exists():
        try:
            return json.loads(KEY_MAP_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_key_map(m: dict) -> None:
    try:
        KEY_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        KEY_MAP_PATH.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"  (缓存写入失败，不影响本次结果: {e})")


def pair_keys(dbs, keys, workdir, cached, verbose=False):
    """返回 {dbname: (snapshot 路径, key, profile)}。命中结果写入缓存。"""
    result = {}

    for db in dbs:
        snap = snapshot(db, workdir)

        c = cached.get(db.name)
        if c:
            try:
                key = keys[c["key_index"]]
                prof = next(p for p in PROFILES if p[0] == c["profile"])
                if try_open(snap, key, prof):
                    result[db.name] = (snap, key, prof)
                    print(f"  [缓存命中] {db.name}  key#{c['key_index']} / {prof[0]}")
                    continue
            except (KeyError, IndexError, StopIteration):
                pass

        found = False
        for ki, key in enumerate(keys):
            for prof in PROFILES:
                hit = try_open(snap, key, prof)
                if hit:
                    conn, n = hit
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    print(f"  [配对成功] {db.name}  key#{ki}({len(key)}B) / {prof[0]}")
                    result[db.name] = (snap, key, prof)
                    cached[db.name] = {"key_index": ki, "profile": prof[0]}
                    found = True
                    break
            if found:
                break

        if not found:
            print(f"  [配对失败] {db.name} —— 试了 {len(keys)} 把密钥 × {len(PROFILES)} 组参数")
            if verbose:
                print("             可能原因：密钥没采到 / 该库不是 SQLCipher / 需要别的参数组合")

    save_key_map(cached)
    return result


# ---------------------------------------------------------------------------
# 解压 / 取值
# ---------------------------------------------------------------------------
_DCTX = None


def _dctx():
    global _DCTX
    if _DCTX is None:
        try:
            import zstandard
            _DCTX = zstandard.ZstdDecompressor()
        except ImportError:
            _DCTX = False
    return _DCTX or None


def decode_cell(value, ct) -> str:
    """按 WCDB 列压缩标记解出文本。

    ct == 4 时值是 zstd 帧；ct == 0 时直接就是 str。
    为稳妥起见，即使 ct 说没压缩，也先看魔数。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
        if b[:4] == ZSTD_MAGIC:
            d = _dctx()
            if d is None:
                return ""          # 缺 zstandard，静默返回空（调用方会提示）
            try:
                return d.decompress(b).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                # 内容可能是「zstd 帧 + 尾部附加数据」，用流式解
                try:
                    dobj = d.decompressobj()
                    out = dobj.decompress(b)
                    return out.decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    return ""
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return f"[blob {len(b)}B]"
    return str(value)


# ---------------------------------------------------------------------------
# 表结构解析
# ---------------------------------------------------------------------------
def list_tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def list_msg_tables(conn) -> list[str]:
    """微信 4.x：每个会话一张 Msg_<md5> 表。"""
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg\\_%' ESCAPE '\\'"
    ).fetchall()]


def load_name2id(conn) -> dict[int, str]:
    """Name2Id: rowid -> user_name（real_sender_id 指向这个 rowid）。"""
    for t in list_tables(conn):
        if t.lower() != "name2id":
            continue
        try:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
            uc = next((c for c in cols if c.lower() in ("user_name", "username", "name")), cols[0])
            return {int(r[0]): str(r[1]) for r in
                    conn.execute(f'SELECT rowid, "{uc}" FROM "{t}"').fetchall()}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def md5_to_talker(name2id: dict[int, str]) -> dict[str, str]:
    return {hashlib.md5(n.encode()).hexdigest(): n for n in name2id.values() if n}


def pick_column(cols: list[str], kind: str) -> str | None:
    lower = {c.lower(): c for c in cols}
    for cand in COL_CANDIDATES.get(kind, ()):
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def resolve_message_table(conn):
    """兼容老 schema（单一 message/MSG 表）。返回 (表名, {语义: 列名})。"""
    tables = [t for t in list_tables(conn) if not t.startswith("sqlite_")
              and not t.startswith("Msg_")]
    ordered = [t for t in tables if t.lower() in ("message", "msg")] + \
              [t for t in tables if t.lower() not in ("message", "msg")]
    for t in ordered:
        try:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
        except Exception:  # noqa: BLE001
            continue
        m = {}
        for kind in COL_CANDIDATES:
            c = pick_column(cols, kind)
            if c:
                m[kind] = c
        if ("talker" in m or "talker_id" in m) and "time" in m and "type" in m:
            return t, m
    return "", {}


def is_private(name: str) -> bool:
    return "@chatroom" not in name and not name.startswith("gh_")


# ---------------------------------------------------------------------------
# 提取
# ---------------------------------------------------------------------------
def self_rowid(name2id: dict[int, str], self_wxid: str) -> int | None:
    if not self_wxid:
        return None
    for rid, n in name2id.items():
        if n == self_wxid:
            return rid
    return None


def extract_private(conn, dbname: str, since: int, limit: int | None,
                    self_wxid: str = "") -> list[dict]:
    """提取私聊消息。返回 [{msg_id, group_name, sender, content, msg_type, received_at, direction}]"""
    out: list[dict] = []
    name2id = load_name2id(conn)
    me = self_rowid(name2id, self_wxid)
    md5map = md5_to_talker(name2id)

    msg_tables = list_msg_tables(conn)

    if msg_tables:
        # ---- 微信 4.x：每会话一张表 ----
        for t in msg_tables:
            talker = md5map.get(t[4:])
            if not talker or not is_private(talker):
                continue
            try:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
            except Exception:  # noqa: BLE001
                continue
            c_id = pick_column(cols, "server_id")
            c_t = pick_column(cols, "time")
            c_ty = pick_column(cols, "type")
            c_ct = pick_column(cols, "content")
            c_ctf = pick_column(cols, "content_ct")
            c_sid = pick_column(cols, "sender_id")
            c_lid = pick_column(cols, "local_id")
            if not (c_t and c_ty and c_ct):
                continue

            sel = [f'"{c_id}"' if c_id else "NULL",
                   f'"{c_lid}"' if c_lid else "rowid",
                   f'"{c_ty}"', f'"{c_t}"', f'"{c_ct}"',
                   f'"{c_ctf}"' if c_ctf else "0",
                   f'"{c_sid}"' if c_sid else "NULL"]
            sql = (f'SELECT {", ".join(sel)} FROM "{t}" WHERE "{c_t}" >= ? '
                   f'ORDER BY "{c_t}" ASC')
            params: list = [since]
            if limit:
                sql += " LIMIT ?"
                params.append(limit)
            try:
                rows = conn.execute(sql, params).fetchall()
            except Exception:  # noqa: BLE001
                continue

            for server_id, local_id, mtype, mtime, cval, ctf, rsid in rows:
                content = decode_cell(cval, ctf)
                direction = None
                if me is not None and rsid is not None:
                    direction = "out" if int(rsid) == me else "in"
                out.append({
                    "msg_id": str(server_id) if server_id else f"{dbname}:{local_id}",
                    "group_name": talker,
                    "sender": talker,
                    "sender_id": "",
                    "content": content,
                    "msg_type": int(mtype or 0),
                    "received_at": int(mtime or 0),
                    "direction": direction,
                })
        return out

    # ---- 老 schema 兜底 ----
    table, m = resolve_message_table(conn)
    if not table:
        return out
    sel = []
    sel.append(f'"{m["server_id"]}"' if "server_id" in m else "NULL")
    sel.append(f'"{m["local_id"]}"' if "local_id" in m else "rowid")
    sel.append(f'"{m["talker"]}"' if "talker" in m else "NULL")
    sel.append(f'"{m["talker_id"]}"' if "talker_id" in m else "NULL")
    sel.append(f'"{m["type"]}"')
    sel.append(f'"{m["time"]}"')
    sel.append(f'"{m["content"]}"' if "content" in m else "''")
    sel.append(f'"{m["content_ct"]}"' if "content_ct" in m else "0")
    sql = (f'SELECT {", ".join(sel)} FROM "{table}" WHERE "{m["time"]}" >= ? '
           f'ORDER BY "{m["time"]}" ASC')
    params = [since]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    for server_id, local_id, talker, talker_id, mtype, mtime, cval, ctf in conn.execute(sql, params):
        name = str(talker) if talker else (
            name2id.get(int(talker_id), f"(未知会话:{talker_id})") if talker_id is not None else "")
        if not name or not is_private(name):
            continue
        out.append({
            "msg_id": str(server_id) if server_id else f"{dbname}:{local_id}",
            "group_name": name,
            "sender": name,
            "sender_id": "",
            "content": decode_cell(cval, ctf),
            "msg_type": int(mtype or 0),
            "received_at": int(mtime or 0),
            "direction": None,
        })
    return out


# ---------------------------------------------------------------------------
# 自省
# ---------------------------------------------------------------------------
def inspect_db(conn, dbname: str, self_wxid: str = "") -> None:
    print(f"\n{'=' * 70}\n{dbname}\n{'=' * 70}")
    tabs = [t for t in list_tables(conn) if not t.startswith("sqlite_")]
    print(f"  表总数 {len(tabs)}（其中 Msg_ 会话表 {len(list_msg_tables(conn))} 张）")

    name2id = load_name2id(conn)
    me = self_rowid(name2id, self_wxid)
    print(f"  Name2Id 条目 {len(name2id)}；自己({self_wxid!r}) = rowid {me}")

    md5map = md5_to_talker(name2id)
    priv, grp, unk = [], [], 0
    msg_tabs = list_msg_tables(conn)
    for t in msg_tabs:
        nm = md5map.get(t[4:])
        if nm is None:
            unk += 1
        elif is_private(nm):
            priv.append(nm)
        else:
            grp.append(nm)
    print(f"  会话构成: 私聊 {len(priv)} / 群聊 {len(grp)} / 未能反查 {unk}")
    if priv:
        print(f"  私聊样例: {', '.join(priv[:8])}")

    if msg_tabs:
        t = msg_tabs[0]
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
        print(f"\n  会话表结构（样例 {t}）:")
        print(f"    {', '.join(cols)}")


def analyze_db(conn, dbname: str, since: int, self_wxid: str) -> None:
    print(f"\n{'-' * 70}\n[解析] {dbname}\n{'-' * 70}")
    msgs = extract_private(conn, dbname, since, None, self_wxid)
    priv_convs = {m["group_name"] for m in msgs}
    n_out = sum(1 for m in msgs if m["direction"] == "out")
    n_in = sum(1 for m in msgs if m["direction"] == "in")
    n_unk = sum(1 for m in msgs if m["direction"] is None)
    print(f"  可提取私聊消息 {len(msgs)} 条，涉及 {len(priv_convs)} 个会话")
    print(f"  方向: out {n_out} / in {n_in} / 未判定 {n_unk}")
    empty = sum(1 for m in msgs if not m["content"])
    if empty:
        print(f"  ⚠ 内容为空 {empty} 条（可能缺 zstandard，或该消息本身无文本）")
    for m in msgs[:3]:
        c = m["content"][:60] if m["content"] else "(空)"
        print(f"    样例: [{m['direction']}] {m['group_name']} type={m['msg_type']} "
              f"id={m['msg_id']} :: {c}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="离线解密微信消息库并回填私聊历史")
    ap.add_argument("--keys", default=str(DEFAULT_KEYS_PATH))
    ap.add_argument("--account", default="", help="self_wxid（默认读 config.yaml）")
    ap.add_argument("--probe", action="store_true", help="只做 key×db 配对")
    ap.add_argument("--inspect", action="store_true", help="解密后 dump 表结构并试提取")
    ap.add_argument("--run", action="store_true", help="提取并回填")
    ap.add_argument("--write", action="store_true", help="真正写入 messages.db（默认只预览）")
    ap.add_argument("--since-hours", type=float, default=24.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    keys_path = Path(args.keys)
    keys = load_keys(keys_path)
    print(f"密钥文件: {keys_path}")
    if not keys:
        sys.exit("没读到密钥。\n  1) 先编译并注入 keyhook（见 WeChatKeyHook/README.md）\n"
                 "  2) 注入后在微信里正常收发消息\n  3) 或用 --keys 指定其它路径")
    print(f"读到 {len(keys)} 个去重后的候选密钥")

    account = args.account
    if not account:
        try:
            import yaml
            cfg = yaml.safe_load((PROJECT_DIR / "config.yaml").read_text(encoding="utf-8"))
            account = str((cfg.get("hook") or {}).get("self_wxid") or "")
        except Exception:  # noqa: BLE001
            account = ""
    print(f"self_wxid = {account!r}")

    dbs = find_message_dbs(account)
    if not dbs:
        sys.exit(f"没找到 message_*.db（account={account!r}）")
    print(f"发现 {len(dbs)} 个消息库: " + ", ".join(d.name for d in dbs))

    since = int(time.time() - args.since_hours * 3600)
    print(f"时间下限: {since}（最近 {args.since_hours} 小时）\n")

    # 临时目录只用来放解密快照；清理失败（文件被占用/权限受限）不该让整步失败。
    with tempfile.TemporaryDirectory(prefix="wc_db_backfill_",
                                     ignore_cleanup_errors=True) as td:
        workdir = Path(td)
        print("== key × db 配对 ==")
        paired = pair_keys(dbs, keys, workdir, load_key_map(), verbose=args.verbose)
        if not paired:
            sys.exit("\n没有任何库配对成功。加 --verbose 看细节。")

        conns = {}
        for n_, (snap, key, prof) in paired.items():
            hit = try_open(snap, key, prof)
            if hit:
                conns[n_] = hit[0]

        if args.inspect:
            for n_, c in conns.items():
                inspect_db(c, n_, account)
                analyze_db(c, n_, since, account)
            for c in conns.values():
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            return

        if not args.run:
            print("\n（只做了配对。加 --inspect 看结构，或 --run 提取）")
            for c in conns.values():
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            return

        print("\n== 提取私聊 ==")
        all_msgs: list[dict] = []
        for n_, c in conns.items():
            try:
                msgs = extract_private(c, n_, since, args.limit or None, account)
            except Exception as e:  # noqa: BLE001
                print(f"  [错误] {n_}: {e}")
                msgs = []
            print(f"  {n_}: 私聊候选 {len(msgs)} 条")
            all_msgs.extend(msgs)

        # 提取完就断开：否则临时目录里的 .db 被占用，TemporaryDirectory 清理会失败
        for c in conns.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

        seen: set[str] = set()
        uniq = []
        for m in all_msgs:
            if m["msg_id"] in seen:
                continue
            seen.add(m["msg_id"])
            uniq.append(m)
        print(f"  批内去重后 {len(uniq)} 条（原始 {len(all_msgs)} 条）")

        store = Store(str(MESSAGES_DB))
        inserted = skipped = 0
        try:
            existing = {r[0] for r in
                        store._conn.execute("SELECT msg_id FROM messages").fetchall()}  # noqa: SLF001
            for m in uniq:
                if m["msg_id"] in existing:
                    skipped += 1
                    continue
                if args.write:
                    got = store.insert_message(**m)  # type: ignore[arg-type]
                    if got is None:
                        skipped += 1
                    else:
                        inserted += 1
                else:
                    inserted += 1
        finally:
            store.close()

        verb = "已写入" if args.write else "将写入（预览，未落库）"
        print(f"\n{verb}: {inserted} 条；已存在跳过: {skipped} 条")
        if not args.write and inserted:
            print("确认无误后加 --write 落库。")


if __name__ == "__main__":
    main()
