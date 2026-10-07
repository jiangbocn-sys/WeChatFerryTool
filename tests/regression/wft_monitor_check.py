"""验证监控名单下拉勾选 + 每群重点关注人 + 归档范围规则。

覆盖：
* 监控名单增删（群/人）写入 filter.*，并在 labels 里打 monitored 标记
* 重点关注人：设置后"只归档这些人"；清空后"整群归档"
* 归档规则 `_in_digest` 的完整真值表（含敏感关键词、全局重点联系人、私聊）
* 归档范围与监控名单一致（加进监控就能在归档里看到）
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import yaml

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-monitor-test")
REAL_LABELS = PROJ / "accounts" / "ruibo_jiang_542e" / "data" / "labels.json"
REAL_CFG = PROJ / "config.yaml"

shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

FAILS = []


def check(name, cond, extra=""):
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        # 测试名里可能有 emoji（如 🧠），GBK 控制台直接 print 会崩 —— 降级成转义再打
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


CFG = {
    "hook": {"api_base": "http://127.0.0.1:30001"}, "filter": {"groups": [], "senders": []},
    "llm": {"base_url": "https://api.minimax.cn/v1", "api_key": "sk-x", "model": "M", "push_threshold": 4},
    "storage": {"sqlite_path": "data/messages.db", "ingest_exclude_types": [47]},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "summarize": True,
               "exclude_types": [47, 51, 10000, 10002]},
    "replies": {"enabled": False, "templates": []}, "voice": {"enabled": False, "dir": "data/voices"},
    "asr": {"enabled": False, "url": ""}, "bark": {"enabled": False, "server": "s", "key": "k"},
    "app": {"web_port": 6060},
}
(SC / "config.yaml").write_text(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False), encoding="utf-8")
(SC / "data" / "labels.json").write_text(json.dumps({
    "groups": {
        "195940014@chatroom": {"name": "幸福小家", "important": False},
        "958062774@chatroom": {"name": "中澳一家亲", "important": False},
        "777777777@chatroom": {"name": "工作群", "important": False,
                               "keywords": ["报价", "合同"]},
    },
    "senders": {
        "wxid_aaa111": {"name": "老张", "important": False},
        "wxid_bbb222": {"name": "小李", "important": False},
        "wxid_ccc333": {"name": "老板", "important": True},
        "wxid_ddd444": {"name": "路人", "important": False},
    },
}, ensure_ascii=False, indent=2), encoding="utf-8")

# 造一个 messages 表，让"库里出现过的人"能被下拉框列出来
db = SC / "data" / "messages.db"
conn = sqlite3.connect(db)
conn.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER UNIQUE,
 group_name TEXT, sender TEXT, sender_id INTEGER, content TEXT, msg_type INTEGER,
 received_at INTEGER, score INTEGER, score_reason TEXT, pushed INTEGER, priority INTEGER,
 direction TEXT, transcript TEXT)""")
conn.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at) "
             "VALUES (1, '195940014@chatroom', 'wxid_eee555', 'hi', 1, 1791289000)")
conn.commit()
conn.close()

import web.app as webapp  # noqa: E402

app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()

REAL_CFG_HASH = REAL_CFG.read_text(encoding="utf-8")
REAL_LABELS_HASH = REAL_LABELS.read_text(encoding="utf-8")
CFG_PATH = SC / "config.yaml"
LABELS_PATH = SC / "data" / "labels.json"


def cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))


def labels() -> dict:
    return json.loads(LABELS_PATH.read_text(encoding="utf-8"))


print("[1] 页面：监控名单区 + 下拉框（已标定的显示名字）")
r = c.get("/filter")
b = r.get_data(as_text=True)
check("GET /config -> 200", r.status_code == 200, str(r.status_code))
check("有监控名单区块", "监控名单" in b)
check("群下拉框存在", 'name="group"' in b and "添加" in b)
check("人下拉框存在", 'name="sender"' in b)
check("下拉里带标定名字（幸福小家）", "幸福小家" in b)
check("未标定的人**不**出现在下拉里（只列已标记的）", "wxid_eee555" not in
      __import__("re").search(r'value="add_sender".*?</form>', b, __import__("re").S).group(0))
check("说明写了归档范围规则（只决定归档/总结范围，不影响入库）",
      "只决定" in b and "归档与总结的范围" in b and "不影响入库" in b)
# 总设置页不再内嵌监控表格（只留"当前监控的群/人 + 去监控规则页设置"入口卡片），
# 这是刻意的"同一份配置只有一处能改"。
_cfg_page = c.get("/config").get_data(as_text=True)
check("总设置页给出去监控规则页的入口",
      "去监控规则页设置" in _cfg_page and 'href="/filter"' in _cfg_page)

print("\n[2] 添加监控群 → 写入 filter.groups + labels 打 monitored")
r = c.post("/config/filter/save", data={"action": "add_group", "group": "958062774@chatroom"},
           follow_redirects=False)
check("添加返回 302", r.status_code == 302, str(r.status_code))
check("filter.groups 已包含", "958062774@chatroom" in (cfg()["filter"]["groups"] or []), str(cfg()["filter"]["groups"]))
check("labels 里标了 monitored", labels()["groups"]["958062774@chatroom"].get("monitored") is True)
check("没设重点人（整群归档的前提）",
      not (labels()["groups"]["958062774@chatroom"].get("focus_members") or []))
r = c.post("/config/filter/save", data={"action": "add_group", "group": "958062774@chatroom"})
check("重复添加被挡", "error=" in (r.headers.get("Location") or ""), str(r.headers.get("Location"))[:80])

# 监控表格只在 /filter 渲染（/config 里那段是 {% if false %} 死代码，只留入口卡片）；
# 10-07 起表格新增「🧠 总结提示」列 —— 必须等"群已进监控名单"之后再断言。
_flt = c.get("/filter").get_data(as_text=True)
check("监控规则页有每群总结提示输入框（10-07 起在此维护）",
      'name="hint_958062774@chatroom"' in _flt and "保存总结提示" in _flt)
check("监控规则页显示了 ★ 语义（重点人不再过滤他人）",
      "整群对话都进归档" in _flt and "不会把其他人过滤掉" in _flt)

print("\n[3] 添加监控人")
c.post("/config/filter/save", data={"action": "add_sender", "sender": "wxid_bbb222"})
check("filter.senders 已包含", "wxid_bbb222" in (cfg()["filter"]["senders"] or []))
check("labels 里标了 monitored", labels()["senders"]["wxid_bbb222"].get("monitored") is True)

print("\n[4] 重点关注人选择页")
r = c.get("/config/focus/958062774@chatroom")
b = r.get_data(as_text=True)
check("GET 重点人页 -> 200", r.status_code == 200, str(r.status_code))
check("列出了可选的人（老张）", "老张" in b)
check("没排除自己（不显示群 id）", "958062774@chatroom" not in b.split("会话 ID")[0])
check("重点人页文案已更新（不再是'只归档这些人'）",
      "不会" in b and "过滤掉" in b and "整群对话都进归档" in b)
r = c.post("/config/filter/save", data={"action": "set_focus", "group": "958062774@chatroom",
                                        "focus": ["wxid_aaa111", "wxid_bbb222"]})
check("保存重点人 -> 302", r.status_code == 302, str(r.status_code))
check("labels 里 focus_members 已写入",
      sorted(labels()["groups"]["958062774@chatroom"]["focus_members"]) == ["wxid_aaa111", "wxid_bbb222"],
      str(labels()["groups"]["958062774@chatroom"].get("focus_members")))
r = c.get("/config/focus/958062774@chatroom")
check("再打开时勾选态回显", 'value="wxid_aaa111" checked' in r.get_data(as_text=True))
r = c.get("/filter")
b = r.get_data(as_text=True)
check("归档范围仍显示整群归档 + 2 位重点人打 ★",
      "整群归档" in b and "2 位重点人" in b, "")

print("\n[5] 归档规则真值表（_in_digest）")
import consumer.digest as dg  # noqa: E402


def row(group, sender, content="文字", mtype=1):
    return {"group_name": group, "sender": sender, "content": content, "msg_type": mtype,
            "transcript": "", "priority": 0}


L = labels()
EX = {47, 51, 10000, 10002}
MON = {"958062774@chatroom"}          # 只监控这一个群

check("监控群 + 设了重点人 → **重点人照样归档**",
      dg._in_digest(row("958062774@chatroom", "wxid_aaa111"), L, EX, MON) is True)
# 2026-10-07 用户确认：归档要的是**上下文**，重点人只用 ★ 标记、不再把别人过滤掉
check("监控群 + 设了重点人 → **非重点人也归档**（整群对话，上下文优先）",
      dg._in_digest(row("958062774@chatroom", "wxid_ddd444"), L, EX, MON) is True)
check("非重点人的行会被打 ★ 吗 → 不会（只有重点人打 ★）",
      dg._is_star(row("958062774@chatroom", "wxid_ddd444"), L) is False)
check("重点人的行会打 ★", dg._is_star(row("958062774@chatroom", "wxid_aaa111"), L) is True)
check("没监控的群 → 不归档", dg._in_digest(row("195940014@chatroom", "wxid_aaa111"), L, EX, MON) is False)
check("排除类型（表情）永不归档",
      dg._in_digest(row("958062774@chatroom", "wxid_aaa111", mtype=47), L, EX, MON) is False)
check("私聊非重点 → 不归档",
      dg._in_digest(row("wxid_ddd444", "wxid_ddd444"), L, EX, MON) is False)
check("私聊且是全局重点联系人 → 归档",
      dg._in_digest(row("wxid_ccc333", "wxid_ccc333"), L, EX, MON) is True)

# 清掉重点人 → 回到整群归档
c.post("/config/filter/save", data={"action": "set_focus", "group": "958062774@chatroom"})
check("清空重点人后 focus_members 为空",
      not (labels()["groups"]["958062774@chatroom"].get("focus_members") or []))
L2 = labels()
check("（清空重点人后 = 无重点人）整群归档：路人也进",
      dg._in_digest(row("958062774@chatroom", "wxid_ddd444"), L2, EX, MON) is True)
check("清空后：原本被排除的人也进归档",
      dg._in_digest(row("958062774@chatroom", "wxid_ddd444"), L2, EX, MON) is True)

# 敏感关键词群（未加监控）：关键词命中才归档
check("未监控但配了关键词的群：命中关键词 → 归档",
      dg._in_digest(row("777777777@chatroom", "wxid_ddd444", "今天报价多少"), L2, EX, MON) is True)
check("未监控但配了关键词的群：没命中 → 不归档",
      dg._in_digest(row("777777777@chatroom", "wxid_ddd444", "吃饭了吗"), L2, EX, MON) is False)

# 监控名单为空 = 不限制（退回旧 labels 规则）
check("监控名单为空时退回旧规则（labels.important 才算）",
      dg._in_digest(row("195940014@chatroom", "wxid_aaa111"), L2, EX, None) is False)

print("\n[6] 移除监控")
c.post("/config/filter/save", data={"action": "remove_group", "group": "958062774@chatroom"})
check("filter.groups 已移除", "958062774@chatroom" not in (cfg()["filter"]["groups"] or []))
check("monitored 标记关掉", labels()["groups"]["958062774@chatroom"].get("monitored") is False)
c.post("/config/filter/save", data={"action": "remove_sender", "sender": "wxid_bbb222"})
check("filter.senders 已移除", "wxid_bbb222" not in (cfg()["filter"]["senders"] or []))

print("\n[7] 单个人增删重点人")
c.post("/config/filter/save", data={"action": "focus_add", "group": "195940014@chatroom",
                                    "sender": "wxid_ccc333"})
check("单个添加重点人", labels()["groups"]["195940014@chatroom"]["focus_members"] == ["wxid_ccc333"],
      str(labels()["groups"]["195940014@chatroom"].get("focus_members")))
c.post("/config/filter/save", data={"action": "focus_remove", "group": "195940014@chatroom",
                                    "sender": "wxid_ccc333"})
check("单个移除重点人", not (labels()["groups"]["195940014@chatroom"].get("focus_members") or []))

check("真实 config.yaml 未被测试改动", REAL_CFG.read_text(encoding="utf-8") == REAL_CFG_HASH)
check("真实 labels.json 未被测试改动", REAL_LABELS.read_text(encoding="utf-8") == REAL_LABELS_HASH)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("监控名单 + 重点关注人 + 归档范围 验证通过")
