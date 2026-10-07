"""验证消息浏览页的下拉：
 1. **已标定的群/人全部出现**（以前只列最近活跃 20 个，久未发言的就看不到）
 2. 未标定的只补最近活跃的若干个，并标注"未标定"
 3. **未标定的公众号（gh_...）不进下拉**；但显式筛选它时仍给出入口（否则翻页筛选会丢）
 4. 已标定但库里没有消息的，也要出现（n=0）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import yaml

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-browse-test")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "storage": {"sqlite_path": "data/messages.db", "ingest_exclude_types": [47]},
    "filter": {"groups": [], "senders": []},
    "llm": {"base_url": "", "api_key": "", "model": ""},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")

# 造库：20+ 个"最近活跃"的会话，把弟子群挤出去；再放 1 个已标定但无消息的群
db = SC / "data" / "messages.db"
conn = sqlite3.connect(db)
conn.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER UNIQUE,
 group_name TEXT, sender TEXT, sender_id INTEGER, content TEXT, msg_type INTEGER,
 received_at INTEGER, score INTEGER, score_reason TEXT, pushed INTEGER, priority INTEGER,
 direction TEXT, transcript TEXT)""")
BASE = 1791200000
mid = 0


def add(group, sender, ts, content="文字"):
    global mid
    mid += 1
    conn.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at,"
                 " direction) VALUES (?,?,?,?,1,?,'in')", (mid, group, sender, content, ts))


# 25 个近期活跃的未标定群 → 会把"弟子群"挤出 top20
for i in range(25):
    add(f"recent{i}@chatroom", f"wxid_r{i}", BASE + 1000 + i)
# 弟子群：消息多但很久没动静
for i in range(30):
    add("49366798260@chatroom", "wxid_4ou2y81yt6qp22", BASE + i)
# 未标定的公众号（未标定 → 不应进下拉）
add("gh_aaaabbbbcccc", "gh_aaaabbbbcccc", BASE + 2000)
# 未标定的普通人
add("wxid_unlabeled_person", "wxid_unlabeled_person", BASE + 900)
conn.commit()
conn.close()

# 标定：弟子群 + 群里的人 + 一个"库里没有消息"的群
LABELS = {
    "groups": {
        "49366798260@chatroom": {"name": "易青岚乙巳年弟子群", "monitored": True,
                                 "focus_members": ["wxid_4ou2y81yt6qp22", "sunhuang11"]},
        "no_msg_group@chatroom": {"name": "从没说过话的群"},
    },
    "senders": {
        "wxid_4ou2y81yt6qp22": {"name": "王同学"},
        "sunhuang11": {"name": "孙黄"},
        "wxid_named_but_idle": {"name": "很久没发言的人"},
    },
}
(SC / "data" / "labels.json").write_text(json.dumps(LABELS, ensure_ascii=False, indent=2), encoding="utf-8")

import web.app as webapp  # noqa: E402

app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()
html = c.get("/browse").get_data(as_text=True)


def options(sel_name: str) -> list[str]:
    m = re.search(r'<select name="' + sel_name + r'".*?</select>', html, re.S)
    return re.findall(r'<option value="([^"]*)"', m.group(0)) if m else []


gset, sset = set(options("group")), set(options("sender"))
print("[1] 已标定的群/人必须全部出现")
check("弟子群在群下拉里", "49366798260@chatroom" in gset)
check("弟子群显示标定名", "易青岚乙巳年弟子群" in html)
check("已标定但无消息的群也出现", "no_msg_group@chatroom" in gset)
check("已标定的人出现在发送人下拉", "wxid_4ou2y81yt6qp22" in sset)
check("久未发言的已标定的人也出现", "wxid_named_but_idle" in sset)
check("群下拉条数 > 20（不再只列 20 个）", len(gset) > 20, f"{len(gset)} 个")

print("\n[2] 未标定的：只补最近活跃的，并标注")
check("未标定的近期群仍在（补充用）", any(k.startswith("recent") for k in gset))
check("未标定条目带『未标定』标记", "未标定" in html)

print("\n[3] 公众号（gh_）处理")
check("未标定的公众号**不**进群下拉", "gh_aaaabbbbcccc" not in gset)
check("未标定的公众号**不**进发送人下拉", "gh_aaaabbbbcccc" not in sset)
h2 = c.get("/browse?group=gh_aaaabbbbcccc").get_data(as_text=True)
gset2 = set(re.findall(r'<option value="([^"]*)"',
                       re.search(r'<select name="group".*?</select>', h2, re.S).group(0)))
check("显式筛选该公众号时给出入口（翻页不丢筛选）", "gh_aaaabbbbcccc" in gset2)

print("\n[4] 已标定 / 未标定 分组展示")
check("有『已标定（全部）』分组", "已标定（全部）" in html)
check("有『最近活跃（未标定）』分组", "最近活跃（未标定）" in html)

print("\n[5] 显示条数")
check("能显示消息条数（如『30条』）", "30条" in html or "· 30" in html, "")

shutil.rmtree(SC, ignore_errors=True)
print("=" * 58)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("消息浏览页下拉 验证通过")
