"""验证「从已抓消息挖昵称」+ 手工批量补充。

用真实的 messages 表结构（含 content 里的引用 XML）造一个小库，
验证：挖掘、只填空白、人工补充优先、Web 接口。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-harvest-test")
REAL_LABELS = PROJ / "accounts" / "ruibo_jiang_542e" / "data" / "labels.json"

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


# ---- 造一个和真实结构一致的 messages 表 ----
db = SC / "data" / "messages.db"
conn = sqlite3.connect(db)
conn.execute("""CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER UNIQUE, group_name TEXT,
    sender TEXT, sender_id INTEGER, content TEXT, msg_type INTEGER, received_at INTEGER,
    score INTEGER, score_reason TEXT, pushed INTEGER, priority INTEGER, direction TEXT,
    transcript TEXT)""")

QUOTE = ('<?xml version="1.0"?><msg><appmsg><title>在吗</title><type>57</type><refermsg>'
         '<type>1</type><svrid>1</svrid><fromusr>50279811726@chatroom</fromusr>'
         '<chatusr>{wx}</chatusr><displayname>{name}</displayname>'
         '<content>你好</content><createtime>1791289332</createtime></refermsg>'
         '</appmsg><fromusername>{wx}</fromusername></msg>')

rows = [
    # 普通群消息（无引用）—— 挖不到
    ("195940014@chatroom", "wxid_aaa111", 1, "大家早"),
    # 引用消息：同一个 wxid 出现多次，昵称变过一次 → 取出现最多的
    ("195940014@chatroom", "wxid_aaa111", 49, QUOTE.format(wx="wxid_aaa111", name="老张")),
    ("195940014@chatroom", "wxid_aaa111", 49, QUOTE.format(wx="wxid_aaa111", name="老张")),
    ("195940014@chatroom", "wxid_aaa111", 49, QUOTE.format(wx="wxid_aaa111", name="张先生")),
    # 另一个 wxid
    ("195940014@chatroom", "wxid_bbb222", 49, QUOTE.format(wx="wxid_bbb222", name="小李")),
    # 群 id 也能挖到（引用里 quote 的是群）
    ("195940014@chatroom", "wxid_ccc333", 49, QUOTE.format(wx="958062774@chatroom", name="中澳一家亲")),
    # 空 displayname / 名字等于 wxid → 跳过
    ("195940014@chatroom", "wxid_ddd444", 49, QUOTE.format(wx="wxid_ddd444", name="")),
    ("195940014@chatroom", "wxid_eee555", 49, QUOTE.format(wx="wxid_eee555", name="wxid_eee555")),
    # 含 @ 的普通消息（只作旁证，不该建映射）
    ("195940014@chatroom", "wxid_fff666", 1, "@姜波\u2005你看这个"),
]
for i, (g, s, t, c) in enumerate(rows, start=1):
    conn.execute("INSERT INTO messages (msg_id, group_name, sender, sender_id, content, msg_type,"
                 " received_at, direction) VALUES (?,?,?,?,?,?,?,?)",
                 (1000 + i, g, s, 0, c, t, 1791289000 + i, "in"))
conn.commit()
conn.close()

# ---- 已有标定：手工名 / 空白名 ----
(SC / "data" / "labels.json").write_text(json.dumps({
    "groups": {"958062774@chatroom": {"name": "", "important": False}},
    "senders": {"wxid_aaa111": {"name": "老张（我改过的）", "important": True},
                "wxid_bbb222": {"name": "", "important": False}},
}, ensure_ascii=False, indent=2), encoding="utf-8")

from consumer import name_harvest  # noqa: E402

print("[1] 挖掘")
names = name_harvest.harvest(db)
check("挖到 3 个（aaa/bbb/群）", len(names) == 3, str(names))
check("aaa 取出现最多的『老张』", names.get("wxid_aaa111") == "老张", str(names.get("wxid_aaa111")))
check("bbb 挖到小李", names.get("wxid_bbb222") == "小李")
check("群 id 也能挖到", names.get("958062774@chatroom") == "中澳一家亲")
check("空 displayname 被跳过", "wxid_ddd444" not in names)
check("名字=wxid 被跳过", "wxid_eee555" not in names)
check("纯 @ 消息不建映射", "wxid_fff666" not in names)

print("\n[2] 套用到 labels.json：只填空白")
res = name_harvest.apply_to_labels(SC / "data" / "labels.json", names=names)
saved = json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8"))
check("手工名未被覆盖", saved["senders"]["wxid_aaa111"]["name"] == "老张（我改过的）",
      saved["senders"]["wxid_aaa111"]["name"])
check("★重点保留", saved["senders"]["wxid_aaa111"].get("important") is True)
check("空白名被填", saved["senders"]["wxid_bbb222"]["name"] == "小李")
check("群空白名被填", saved["groups"]["958062774@chatroom"]["name"] == "中澳一家亲")
check("自动填的标 auto+source", saved["senders"]["wxid_bbb222"].get("auto") is True
      and saved["senders"]["wxid_bbb222"].get("source") == "harvest")
check("手工条目没有 auto 标记", not saved["senders"]["wxid_aaa111"].get("auto"))

print("\n[3] 人工补充优先于挖掘")
name_harvest.save_overrides({"wxid_aaa111": "老张（人工纠正）", "wxid_zzz999": "新来的"})
names2 = name_harvest.harvest(db)
check("人工补充覆盖挖掘结果", names2.get("wxid_aaa111") == "老张（人工纠正）", str(names2.get("wxid_aaa111")))
check("人工补充的新 wxid 也在", names2.get("wxid_zzz999") == "新来的")

print("\n[4] Web 接口")
import web.app as webapp  # noqa: E402
app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()

r = c.get("/api/contacts/harvest")
j = r.get_json()
check("harvest 统计接口 OK", r.status_code == 200 and j.get("found", 0) >= 3, json.dumps(j, ensure_ascii=False)[:150])

r = c.post("/api/contacts/sync")
j = r.get_json()
check("sync 接口 OK 且含 sources", r.status_code == 200 and "sources" in j, json.dumps(j, ensure_ascii=False)[:200])
check("sync 报出消息挖掘来源", "messages" in (j.get("sources") or {}), str(list((j.get("sources") or {}).keys())))
check("sync 找到名字", j.get("found", 0) >= 3, str(j.get("found")))

r = c.post("/api/contacts/overrides", json={"text": "wxid_ooo111 王五\n赵六, wxid_ppp222\n# 注释行\n坏行"})
j = r.get_json()
check("手工补充接口 OK", r.status_code == 200 and j.get("ok") is True, json.dumps(j, ensure_ascii=False)[:200])
check("解析出 2 条", j.get("parsed") == 2, str(j.get("parsed")))
check("坏行被记入 skipped", "坏行" in (j.get("skipped") or []), str(j.get("skipped")))
ov = json.loads((SC / "data" / "name_overrides.json").read_text(encoding="utf-8"))
check("正反两种写法都能解析", ov.get("wxid_ooo111") == "王五" and ov.get("wxid_ppp222") == "赵六", str(ov))
b = c.get("/labels").get_data(as_text=True)
check("页面有挖掘按钮", "btn-sync-names" in b and "从已抓消息挖昵称" in b)
check("页面有手工补充区", "override-text" in b and "btn-save-overrides" in b)

check("真实 labels.json 未被测试改动", "老张" not in REAL_LABELS.read_text(encoding="utf-8"))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("昵称挖掘（从消息里）+ 手工补充 验证通过")
