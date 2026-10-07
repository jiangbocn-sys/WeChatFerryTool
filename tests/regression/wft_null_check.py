"""回归：消息表里出现 NULL 字段时，各页面不能 500（真实库里旧消息可能有 NULL）。

覆盖 /browse 与 /（仪表盘）在 priority / score / transcript / sender_id 为 NULL 时正常渲染。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

SC = Path(r"D:\projects\.wft-null-test")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, r"D:\projects\WeChatFerryTool")

(SC / "config.yaml").write_text(
    "storage:\n  sqlite_path: data/messages.db\nfilter:\n  groups: []\n  senders: []\n",
    encoding="utf-8")
(SC / "data" / "labels.json").write_text(
    json.dumps({"groups": {"g1@chatroom": {"name": "测试群"}}, "senders": {}}, ensure_ascii=False),
    encoding="utf-8")

c = sqlite3.connect(SC / "data" / "messages.db")
c.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY, msg_id INTEGER, group_name TEXT,
 sender TEXT, sender_id INTEGER, content TEXT, msg_type INTEGER, received_at INTEGER,
 score INTEGER, score_reason TEXT, pushed INTEGER, priority INTEGER, direction TEXT,
 transcript TEXT)""")
# 各种 NULL 组合（含 priority=NULL、score=NULL、content/sender 缺失）
rows = [
    (1, "g1@chatroom", "w1", "正常消息", 1, 1791200000, 4, None, 1),
    (2, "g1@chatroom", "w1", "priority 为 NULL", 1, 1791200001, None, None, None),
    (3, "g1@chatroom", None, "sender 为 NULL", 1, 1791200002, 5, None, 0),
    (4, "g1@chatroom", "w1", None, 34, 1791200003, None, None, None),
    (5, "g1@chatroom", "w1", "score 与 priority 都 NULL", 1, 1791200004, None, None, None),
]
for mid, g, s, content, mt, ts, score, reason, prio in rows:
    c.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at,"
              " score, score_reason, priority, pushed, direction, transcript)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (mid, g, s, content, mt, ts, score, reason, prio, 0, "in", None))
c.commit()
c.close()

import web.app as w  # noqa: E402

app = w.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
cl = app.test_client()

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


print("[1] 含 NULL 的消息不能让页面崩")
for url in ("/browse", "/", "/browse?important=1", "/browse?group=g1@chatroom",
            "/browse?channel=group", "/browse?msg_type=1", "/browse?group=g1@chatroom&sender=w1"):
    r = cl.get(url)
    check(f"GET {url} -> 200", r.status_code == 200, str(r.status_code))

print("\n[2] 内容仍然渲染出来")
b = cl.get("/browse").get_data(as_text=True)
for txt in ("正常消息", "priority 为 NULL", "sender 为 NULL", "score 与 priority 都 NULL"):
    check(f"含『{txt}』", txt in b)

print("\n[3] 空 varchar 也算 NULL 边界（content 为空串）")
c2 = sqlite3.connect(SC / "data" / "messages.db")
c2.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at,"
           " direction) VALUES (99,'g1@chatroom','w1','',1,1791200005,'in')")
c2.commit()
c2.close()
check("空内容消息下 /browse 仍 200", cl.get("/browse").status_code == 200)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 55)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("NULL 数据渲染 验证通过")
