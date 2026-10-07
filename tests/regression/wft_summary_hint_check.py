"""回归：每群「总结提示」随群记录一起提交给 LLM（`labels.groups.<gid>.summary_hint`）。

用户需求（2026-10-07）：归档总结要能对每个群带一段提示，一起提交给大模型，
让它知道该怎么总结、哪些内容算噪音要剔除。

覆盖：
  A. `digest._summary_hint()` 取值（有/无/空）
  B. `summarize.build_lines()` 返回的键是**群 id**（提示按 id 存，用显示名会查不到）
  C. `build_prompt()` 注入：第 7 条规则 + 「本群总结提示」清单 + 群名渲染
  D. 没有提示时不出现第 7 条；内容含 `{}` 不会把 replace/format 搞炸
  E. 兼容旧调用（grouped 的键是**显示名**时也能反查并注入）
  F. 标定页：页面有输入框、保存进 labels.json、清空会删掉该键、其它字段不被破坏
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-hint")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
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


GID = "50279811726@chatroom"
GNAME = "岳章形家风水班"
HINT = "这是风水学习群：只记录课程要点、作业与答疑；忽略寒暄、表情、红包、广告与无关闲聊。"

(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001", "callback_host": "127.0.0.1",
             "callback_port": 8888, "api_timeout_s": 5.0, "self_wxid": "wxid_me"},
    "filter": {"groups": [GID], "senders": [], "keywords": [], "case_insensitive": True},
    "llm": {"base_url": "https://x/v1", "api_key": "k", "model": "m",
            "push_threshold": 4, "score_enabled": False},
    "storage": {"sqlite_path": str(SC / "data" / "messages.db"), "ingest_exclude_types": [47]},
    "digest": {"enabled": False, "exclude_types": [47, 51, 10000, 10002]},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")

LABELS = {"groups": {GID: {"name": GNAME, "summary_hint": HINT, "important": True,
                           "keywords": ["报价"]}},
          "senders": {"wxid_a": {"name": "张三", "important": True}}}
(SC / "data" / "labels.json").write_text(json.dumps(LABELS, ensure_ascii=False, indent=2),
                                         encoding="utf-8")

from consumer import digest as digest_mod  # noqa: E402
from consumer import summarize as S  # noqa: E402

print("[A] digest._summary_hint()")
check("取到提示", digest_mod._summary_hint(GID, LABELS) == HINT)
check("没有该群 → 空串", digest_mod._summary_hint("x@chatroom", LABELS) == "")
check("提示为空 → 空串",
      digest_mod._summary_hint(GID, {"groups": {GID: {"name": GNAME, "summary_hint": "   "}}}) == "")

print("\n[B] build_lines() 的键是群 id（提示按 id 存）")
import sqlite3  # noqa: E402
from datetime import datetime  # noqa: E402

conn = sqlite3.connect(str(SC / "data" / "messages.db"))
conn.executescript("""
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id TEXT UNIQUE, group_name TEXT NOT NULL,
  sender TEXT NOT NULL, sender_id TEXT, content TEXT NOT NULL, msg_type INTEGER DEFAULT 1,
  received_at INTEGER NOT NULL, score INTEGER, score_reason TEXT, pushed INTEGER DEFAULT 0,
  priority INTEGER DEFAULT 0, direction TEXT DEFAULT NULL, transcript TEXT DEFAULT NULL);
""")
ts = int(datetime(2026, 10, 7, 10, 0).timestamp())
conn.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at)"
             " VALUES (?,?,?,?,?,?)", ("h1", GID, "wxid_a", "今天讲第九课", 1, ts))
conn.commit()
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM messages").fetchall()
grouped = S.build_lines(rows, LABELS)
check("键是群 id", list(grouped.keys()) == [GID], str(list(grouped.keys())))
check("有该群 1 条", len(grouped.get(GID, [])) == 1, str(grouped))

print("\n[C] build_prompt() 注入提示")
p = S.build_prompt("2026-10-07", grouped, LABELS)
check("含第 7 条规则", "7. **下面「本群总结提示」" in p)
check("含「本群总结提示」段", "本群总结提示：" in p)
check("含提示原文", HINT in p)
check("提示与该群名并列", f"- 【{GNAME}】" in p, p[p.find("本群总结提示"):][:120])
check("群名渲染成显示名", f"### 群：{GNAME}（1 条）" in p)
check("规则里说明了优先级", "优先级高于上面的通用要求" in p)

print("\n[D] 边界：无提示 / 花括号内容")
p_no = S.build_prompt("2026-10-07", {"g@chatroom": ["[10:00] 甲（文本）：hi"]},
                      {"groups": {"g@chatroom": {"name": "别的群"}}})
check("无提示 → 不出现第 7 条", "本群总结提示" not in p_no and "优先级高于" not in p_no)
p_brace = S.build_prompt("2026-10-07",
                         {GID: ['[10:00] 甲（文本）：<appmsg>{"k":"v"}</appmsg> {难度}']}, LABELS)
check("正文含 {} 不会报错且原样保留", '{"k":"v"}' in p_brace and "{难度}" in p_brace)

print("\n[E] 兼容旧调用（键 = 群显示名）")
p_old = S.build_prompt("2026-10-07", {GNAME: grouped[GID]}, LABELS)
check("旧键也能反查到 id 并注入提示", HINT in p_old, p_old[p_old.find("本群"):][:80])
check("旧键渲染群名正确", f"### 群：{GNAME}（1 条）" in p_old)

print("\n[F] 标定页：输入框 / 保存 / 清空 / 不破坏其它字段")
import web.app as webapp  # noqa: E402
webapp.CONFIG_PATH = SC / "config.yaml"
webapp.LABELS_PATH = SC / "data" / "labels.json"
webapp.DB_PATH = SC / "data" / "messages.db"
app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()
html = c.get("/labels").get_data(as_text=True)
check("页面有 hint-ta 输入框", 'class="hint-ta"' in html)
check("页面回显了已保存的提示", HINT in html)
check("页面标了「总结提示」入口", "总结提示" in html)

# 保存：改提示 + 同时提交其它字段（模拟真实表单，**勾选框也要带上** ——
# 服务端语义是"复选框没提交 = 取消重点"，漏提交会被正确地置为 False）
r = c.post("/labels/save", data={
    "group_key": [GID],
    "group_focus": [""],
    "group_keywords": ["报价\n合同"],
    "group_hints": ["新提示：只看价格与交期，忽略一切闲聊"],
    f"labels[groups][{GID}]_name": GNAME,
    f"labels[groups][{GID}]_important": "on",
    "labels[senders][wxid_a]_name": "张三",
    "labels[senders][wxid_a]_important": "on",
}, follow_redirects=False)
check("保存 -> 302", r.status_code in (200, 302), str(r.status_code))
saved = json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8"))
ent = saved["groups"][GID]
check("提示已写入 labels.json", ent.get("summary_hint") == "新提示：只看价格与交期，忽略一切闲聊",
      str(ent.get("summary_hint")))
check("关键词同时更新（没被提示字段弄坏）", ent.get("keywords") == ["报价", "合同"], str(ent.get("keywords")))
check("★重点仍在", ent.get("important") is True, str(ent.get("important")))
check("display name 仍在", ent.get("name") == GNAME, str(ent.get("name")))
check("senders 段未动", saved["senders"] == LABELS["senders"], str(saved["senders"]))
check("保存留了备份", bool(list((SC / "data").glob("labels.json.bak-*"))))

# 清空提示 → 键应被删除（不留空串）
c.post("/labels/save", data={"group_key": [GID], "group_focus": [""],
                             "group_keywords": ["报价\n合同"], "group_hints": ["   "],
                             f"labels[groups][{GID}]_name": GNAME})
saved2 = json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8"))
check("清空提示 → 删除该键（不留空串）", "summary_hint" not in saved2["groups"][GID],
      str(saved2["groups"][GID].get("summary_hint")))
check("清空提示不影响关键词", saved2["groups"][GID].get("keywords") == ["报价", "合同"])

# 保存后 promote：collect() 出来的 grouped 用真 labels 时提示能注入
p_final = S.build_prompt("2026-10-07", grouped, saved2)
check("清空后 prompt 不再带提示", "本群总结提示" not in p_final)

conn.close()
shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("每群总结提示（一起提交给 LLM）验证通过")
