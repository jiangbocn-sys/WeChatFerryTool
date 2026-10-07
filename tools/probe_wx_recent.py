"""扫微信自己的库：每个会话最近的消息时间，用来看"10:09 那条属于哪个群"。

只读查询。用法：python tools\probe_wx_recent.py [HH:MM]
"""
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOOK = "http://127.0.0.1:30001"
LABELS = r"D:\projects\WeChatFerryTool\accounts\ruibo_jiang_542e\data\labels.json"


def post(path: str, payload: dict, timeout: int = 30):
    req = urllib.request.Request(HOOK + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return {"__error__": f"{type(e).__name__}: {e}"}


def q(db: str, sql: str):
    res = post("/QueryDB/execute", {"optDbName": db, "SQL": sql})
    return res if isinstance(res, dict) else {"status": -1}


def rows(res) -> list[dict]:
    return [r for r in (res.get("data") or []) if isinstance(r, dict)] if isinstance(res, dict) else []


def main() -> None:
    since = datetime.now() - timedelta(hours=2)
    if len(sys.argv) > 1:
        hh, mm = sys.argv[1].split(":")
        since = datetime.now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    t0 = int(since.timestamp())
    print(f"统计 {since:%H:%M} 之后有动静的会话（微信自己的库）\n")

    lab = json.load(open(LABELS, encoding="utf-8"))
    names = {gid: (v or {}).get("name") or gid for gid, v in (lab.get("groups") or {}).items()}

    res = q("message_0.db", "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")
    if res.get("status") != 0:
        print("拿不到表列表：", res.get("status"), res.get("desc"))
        return
    tabs = [r.get("name") for r in rows(res)]
    out = []
    for t in tabs:
        h = t[4:]  # 去掉 'Msg_'
        r = q("message_0.db", f"SELECT COUNT(*) AS c, MAX(create_time) AS mx FROM {t} WHERE create_time >= {t0}")
        if r.get("status") != 0:
            continue
        d = rows(r)
        if d and d[0].get("c"):
            out.append((d[0]["mx"], d[0]["c"], h))
    out.sort(reverse=True)
    for mx, c, h in out[:20]:
        gid = next((g for g in names if hashlib.md5(g.encode()).hexdigest() == h), None)
        nm = names.get(gid) if gid else f"(未标定 hash={h[:10]})"
        print(f"  {datetime.fromtimestamp(mx):%H:%M:%S}  {c:>4} 条  {nm}  {gid or ''}")


if __name__ == "__main__":
    main()
