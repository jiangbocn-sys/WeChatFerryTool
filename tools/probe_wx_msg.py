"""用 DLL 的 QueryDB 查微信自己的消息库，确认 9:47 那条消息在不在微信库里。

只读查询（SELECT），不写任何东西。
用法：python tools/probe_wx_msg.py <roomid> [HH:MM] [HH:MM]
      python tools/probe_wx_msg.py 50279811726@chatroom 09:45 09:50
"""
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOOK = "http://127.0.0.1:30001"


def post(path: str, payload: dict, timeout: int = 25):
    req = urllib.request.Request(HOOK + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return {"__error__": f"{type(e).__name__}: {e}"}


def q(db: str, sql: str):
    res = post("/QueryDB/execute", {"optDbName": db, "SQL": sql})
    if isinstance(res, dict) and "status" in res:
        return res
    return {"status": -1, "desc": f"unexpected: {str(res)[:200]}"}


def rows(res) -> list[dict]:
    if isinstance(res, dict) and isinstance(res.get("data"), list):
        return [r for r in res["data"] if isinstance(r, dict)]
    return []


def main() -> None:
    roomid = sys.argv[1] if len(sys.argv) > 1 else "50279811726@chatroom"
    hh1 = sys.argv[2] if len(sys.argv) > 2 else "09:45"
    hh2 = sys.argv[3] if len(sys.argv) > 3 else "09:50"
    today = datetime.now().date()
    t1 = int(datetime.combine(today, datetime.strptime(hh1, "%H:%M").time()).timestamp())
    t2 = int(datetime.combine(today, datetime.strptime(hh2, "%H:%M").time()).timestamp())
    print(f"目标会话 {roomid}  md5={hashlib.md5(roomid.encode()).hexdigest()}")
    print(f"时间窗 {hh1}~{hh2}  = epoch {t1} ~ {t2}\n")

    dbs = post("/QueryDB/GetAllDBName", {})
    names = [str(x.get("dbName")) for x in (dbs if isinstance(dbs, list) else []) if isinstance(x, dict)]
    print("DLL 挂载的库:", names or dbs)

    for db in ("message_0.db", "message/message_0.db"):
        res = q(db, "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")
        print(f"\n[{db}] status={res.get('status')} desc={res.get('desc')!r} 表数={len(rows(res))}")
        if res.get("status") != 0:
            continue
        target = "Msg_" + hashlib.md5(roomid.encode()).hexdigest()
        tabs = [r.get("name") for r in rows(res)]
        hit = target if target in tabs else None
        print("目标表:", target, "→", "找到" if hit else "未找到")
        if not hit:
            print("前 10 张表的名字:", tabs[:10])
            continue
        cols = q(db, f"PRAGMA table_info({hit})")
        colnames = [r.get("name") for r in rows(cols)]
        print("列:", colnames)
        print(f"\n查 {hit} 在 {hh1}~{hh2} 的消息（内容返回 hex，避免编码问题）:")
        sql = (f"SELECT local_id, server_id, local_type, create_time, real_sender_id, "
               f"hex(message_content) AS content_hex, WCDB_CT_message_content AS ct "
               f"FROM {hit} WHERE create_time >= {t1} AND create_time <= {t2} "
               f"ORDER BY create_time")
        r2 = q(db, sql)
        print("status =", r2.get("status"), "desc =", r2.get("desc"))
        for r in rows(r2):
            raw = bytes.fromhex(r.get("content_hex") or "")
            ct = r.get("ct")
            text = ""
            if ct == 4:
                try:
                    import zstandard
                    text = zstandard.ZstdDecompressor().decompressobj().decompress(raw) \
                        if False else zstandard.ZstdDecompressor().decompress(raw, max_output_size=1 << 20)
                except Exception as e:  # noqa: BLE001
                    text = f"<zstd 解压失败 {e}>"
            else:
                text = raw.decode("utf-8", "replace")
            print(f"  t={datetime.fromtimestamp(r['create_time']):%H:%M:%S} type={r['local_type']} "
                  f"sender_id={r['real_sender_id']} server_id={r['server_id']} "
                  f"content={text[:120]!r}")
        # 会话表里 senders 的名字
        n2 = q(db, "SELECT rowid, user_name FROM Name2Id")
        name_map = {r.get("rowid"): r.get("user_name") for r in rows(n2)}
        print("\nName2Id 命中:", {k: v for k, v in name_map.items()
                                 if k in {r.get("real_sender_id") for r in rows(r2)}})


if __name__ == "__main__":
    main()
