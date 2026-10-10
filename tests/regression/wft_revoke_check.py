"""回归：撤回消息**保留原文 + 标注撤回**（2026-10-10 用户要求）。

背景
----
用户反馈："撤回的信息原文显示怎么没有了，我希望保留原文显示，标注撤回"。

事实核查：原文**其实是存下来的**（撤回前就入库了）。撤回通知的 XML 自带
`<newmsgid>被撤回消息id</newmsgid>` → 可以回查。实测最近 40 条撤回里 **32 条**能回查到原文。
原来只显示了"某某撤回了一条消息"（把原文丢了）。

覆盖：
  A. `cards.parse` 从撤回 XML 里解出 `newmsgid` 与撤回人
  B. `cards.summary_line` 三种情形：有原文 / 无原文 / 无撤回人
  C. `digest.revoked_originals()`：按 newmsgid 批量回查、分批、容错
  D. 归档端到端：`generate_digest` 里撤回行带原文
  E. 总结端到端：`build_lines` 里撤回行带原文（含阶段总结的 `collect_group_range`）
  F. 提示词里有「已撤回」的语义说明（否则模型可能把原文当普通消息）
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-revoke-suite")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import yaml  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


GID = "g1@chatroom"
ORIG_ID = "8918183405914523358"          # 被撤回消息的 newmsgid
ORIG_TEXT = "其实我前年在云南和缅甸边境的时候，看见的缅甸人都很老实"

(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "filter": {"groups": [GID], "senders": []},
    "storage": {"sqlite_path": str(SC / "data" / "messages.db"),
                "ingest_exclude_types": [47]},
    "digest": {"enabled": True, "dir": str(SC / "reports"), "exclude_types": [47]},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")
(SC / "data" / "labels.json").write_text(json.dumps(
    {"groups": {GID: {"name": "测试群"}}, "senders": {}}, ensure_ascii=False), encoding="utf-8")

DB = SC / "data" / "messages.db"
db = sqlite3.connect(DB)
db.executescript("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
 msg_id TEXT UNIQUE, group_name TEXT, sender TEXT, sender_id TEXT, content TEXT,
 msg_type INTEGER DEFAULT 1, received_at INTEGER NOT NULL, score INTEGER,
 score_reason TEXT, pushed INTEGER DEFAULT 0, priority INTEGER DEFAULT 0,
 direction TEXT, transcript TEXT);""")
TS = int(datetime(2026, 10, 10, 11, 0).timestamp())
REVOKE_XML = ('<sysmsg type="revokemsg"><revokemsg><session>g1@chatroom</session>'
              '<msgid>1569538155</msgid>'
              f'<newmsgid>{ORIG_ID}</newmsgid>'
              '<replacemsg><![CDATA["许贵胜" 撤回了一条消息]]></replacemsg>'
              '</revokemsg></sysmsg>')
REVOKE_NO_ORIG = ('<sysmsg type="revokemsg"><revokemsg><session>g1@chatroom</session>'
                  '<msgid>1</msgid><newmsgid>9999999999999999999</newmsgid>'
                  '<replacemsg><![CDATA["周宏伟" 撤回了一条消息]]></replacemsg>'
                  '</revokemsg></sysmsg>')
# 原文在**撤回之前**入库（模拟真实时序）
db.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at)"
           " VALUES (?,?,?,?,?,?)", (ORIG_ID, GID, "wxid_x", ORIG_TEXT, 1, TS - 60))
db.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at)"
           " VALUES (?,?,?,?,?,?)", ("rev-1", GID, "wxid_x", REVOKE_XML, 10002, TS))
db.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at)"
           " VALUES (?,?,?,?,?,?)", ("rev-2", GID, "wxid_x", REVOKE_NO_ORIG, 10002, TS + 10))
db.commit()
db.close()

from consumer import cards, digest, summarize  # noqa: E402

print("[A] cards.parse 解出 newmsgid 与撤回人")
d = cards.parse(10002, REVOKE_XML)
check("kind=revoke", d.get("kind") == "revoke", str(d.get("kind")))
check("解出 newmsgid", d.get("newmsgid") == ORIG_ID, str(d.get("newmsgid")))
check("解出撤回人（去掉引号）", d.get("who") == "许贵胜", str(d.get("who")))

print("\n[B] cards.summary_line 三种情形")
s1 = cards.summary_line(10002, REVOKE_XML, revoked_original=ORIG_TEXT)
check("有原文 → 「〔已撤回〕原文：…」", s1.startswith("〔已撤回〕原文："), s1[:50])
check("带原文内容", ORIG_TEXT[:20] in s1, s1[:60])
check("标注撤回人", "许贵胜 撤回" in s1, s1[-20:])
s2 = cards.summary_line(10002, REVOKE_XML)
check("无原文 → 注明「原文未留存」", "原文未留存" in s2, s2)
check("无原文仍有撤回人", "许贵胜" in s2, s2)
check("无原文不含引号噪音", '"' not in s2, s2)
s3 = cards.summary_line(10002, '<sysmsg type="revokemsg"><revokemsg>'
                              '<replacemsg><![CDATA[撤回了一条消息]]></replacemsg>'
                              '</revokemsg></sysmsg>')
check("连撤回人都没有 → 不崩", "已撤回" in s3, s3)
check("连撤回人都没有时**不叠字**（'撤回了一条消息'只出现一次）",
      s3.count("撤回了一条消息") == 1, s3)
long_orig = "长" * 200
s4 = cards.summary_line(10002, REVOKE_XML, revoked_original=long_orig)
check("超长原文被截断（<=60 字 + 省略号）", "…" in s4 and len(s4) < 130, str(len(s4)))

print("\n[C] digest.revoked_originals() 批量回查")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM messages WHERE msg_type=10002 ORDER BY received_at").fetchall()
got = digest.revoked_originals(rows, DB)
conn.close()
check("rev-1 回查到原文", got.get("rev-1", "").startswith(ORIG_TEXT[:10]), str(got)[:60])
check("rev-2 查不到（原文不在库）", got.get("rev-2") is None, str(got.get("rev-2")))
check("不返回无关键", set(got) <= {"rev-1", "rev-2"}, str(set(got)))
check("空输入返回空", digest.revoked_originals([], DB) == {})

print("\n[D] 归档端到端：撤回行带原文")
p = digest.generate_digest("2026-10-10", db_path=DB, labels_path=SC / "data" / "labels.json",
                           out_dir=SC / "reports")
txt = Path(p).read_text(encoding="utf-8")
check("归档里出现「〔已撤回〕原文：」", "〔已撤回〕原文：" in txt)
check("归档里带回了原文内容", ORIG_TEXT[:16] in txt)
check("查不到原文的那条注明未留存", "原文未留存" in txt)

print("\n[E] 总结端到端：build_lines 带原文")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT msg_id, group_name, sender, content, msg_type, received_at, transcript"
                    " FROM messages ORDER BY received_at").fetchall()
conn.close()
labels = json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8"))
lines = summarize.build_lines(rows, labels)
flat = "\n".join(v for vs in lines.values() for v in vs)
check("总结输入行含「〔已撤回〕原文：」", "〔已撤回〕原文：" in flat, flat[:200])
check("总结输入行含原文", ORIG_TEXT[:16] in flat)

print("\n[F] 提示词里有「已撤回」的语义说明")
tpl = summarize.PROMPT_TEMPLATE
check("提示词提到「已撤回」", "已撤回" in tpl)
check("提示词说明原文是实时留存的、可信", "实时留存" in tpl or "留存" in tpl)
check("提示词说明「原文未留存」不要猜", "原文未留存" in tpl)

print("\n[G] 浏览页（web）也显示原文 + 标注撤回")
appsrc = (PROJ / "web" / "app.py").read_text(encoding="utf-8")
tplsrc = (PROJ / "web" / "templates" / "browse.html").read_text(encoding="utf-8")
check("browse 路由回查撤回原文", "revoked_originals" in appsrc)
check("把原文放进卡片供模板渲染", 'r["card"]["revoked_original"]' in appsrc)
check("模板标注「已撤回」", "已撤回" in tplsrc)
check("模板显示「原文：」", "原文：" in tplsrc)
check("模板对未留存的情形有说明", "原文未留存" in tplsrc)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("撤回消息保留原文 + 标注撤回 验证通过")
