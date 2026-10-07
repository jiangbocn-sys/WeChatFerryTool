"""看正在运行的 app 是否在正常抓取入库（只读检查）。"""
import datetime
import os
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(r"D:\projects\WeChatFerryTool\dist\WeChatFerryApp\accounts\ruibo_jiang_542e")
src = ROOT / "data" / "messages.db"
print("db:", src, "存在:", src.is_file(), "大小:", src.stat().st_size if src.is_file() else None)

conn = None
try:
    conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    conn.execute("select 1").fetchone()
except Exception as e:  # noqa: BLE001
    print("只读打开失败，改用副本:", type(e).__name__, e)
    if conn:
        conn.close()
    tmp = Path(r"D:\projects\.wft-peek")
    tmp.mkdir(parents=True, exist_ok=True)
    dst = tmp / "messages.db"
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.is_file():
            shutil.copy2(s, tmp / ("messages.db" + suffix))
    conn = sqlite3.connect(dst)

n, mx = conn.execute("select count(*), max(received_at) from messages").fetchone()
print("总条数:", n, "| 最新:", datetime.datetime.fromtimestamp(mx).strftime("%Y-%m-%d %H:%M:%S") if mx else None)
today = datetime.date.today()
start = int(datetime.datetime(today.year, today.month, today.day).timestamp())
cnt = conn.execute("select count(*) from messages where received_at >= ?", (start,)).fetchone()[0]
print(f"今天({today}) 入库:", cnt, "条")
print("最近 6 条:")
for row in conn.execute(
        "select datetime(received_at,'unixepoch','localtime'), msg_type, "
        "substr(coalesce(group_name,''),1,28), substr(coalesce(content,''),1,36) "
        "from messages order by received_at desc limit 6"):
    print("   ", tuple(row))
conn.close()
