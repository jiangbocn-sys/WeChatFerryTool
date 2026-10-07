"""回归：每群「总结提示」在**监控规则页**（/filter）维护（10-07 用户要求搬家）。

用户理由：归档范围已经是"监控名单里的群的全部对话"，所以这套设置的自然归属是
监控规则页，而不是标定页。要求"这一栏可以改成总结提示词，允许用户编辑保存"。

覆盖：
  A. 监控规则页有 🧠 总结提示 输入框（每群一个）+ 保存按钮；关键词旧卡片已标注废弃
  B. 保存（action=save_hints）写进 labels.groups.<gid>.summary_hint，且不破坏其它字段
  C. 清空 → 删掉该键（不留空串）
  D. 只写监控名单里的群（页面外字段不被顺手写入）
  E. 保存后立刻影响当日总结的 prompt（同一份数据，build_prompt 能读到）
  F. 重点人语义已更新：监控群 + 设了重点人 → **整群归档**（不再只收重点人），文案同步
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-hintmon")
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
OTHER = "958062774@chatroom"          # 不在监控名单里（用来验 D）
HINT = "本群是风水教学群：保留课程要点、卦例分析与作业答疑；剔除寒暄、表情与无关闲聊。"

(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "filter": {"groups": [GID], "senders": [], "keywords": ["旧关键词"]},
    "storage": {"sqlite_path": str(SC / "data" / "messages.db")},
    "digest": {"enabled": True, "summarize_mode": "per_group", "exclude_types": [3, 47, 51, 10000]},
    "llm": {"base_url": "https://x/v1", "api_key": "k", "model": "m",
            "push_threshold": 4, "score_enabled": False},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")

LABELS = {"groups": {GID: {"name": "岳章形家风水班", "monitored": True,
                           "focus_members": ["wxid_a"], "keywords": ["报价"]},
                     OTHER: {"name": "中澳一家亲"}},
          "senders": {"wxid_a": {"name": "周宏伟", "important": True}}}
(SC / "data" / "labels.json").write_text(json.dumps(LABELS, ensure_ascii=False, indent=2),
                                         encoding="utf-8")

import web.app as wa  # noqa: E402
wa.CONFIG_PATH = SC / "config.yaml"
wa.LABELS_PATH = SC / "data" / "labels.json"
wa.DB_PATH = SC / "data" / "messages.db"
app = wa.create_app()
c = app.test_client()


def labels_now() -> dict:
    return json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8"))


print("[A] 监控规则页（/filter）有总结提示入口")
html = c.get("/filter").get_data(as_text=True)
check("页面能打开", c.get("/filter").status_code == 200)
check("有每群的 🧠 输入框", f'name="hint_{GID}"' in html)
check("有保存按钮", "保存总结提示" in html)
check("表头有 🧠 总结提示", "🧠 总结提示" in html)
check("说明了归档是整群、重点人只打 ★",
      "整群对话都进归档" in html and "★" in html)
check("归档关键词卡片已标注废弃", "已废弃" in html and "不再参与判定" in html)
check("关键词不再有可编辑表单（无 keywords 输入框）",
      'name="keywords"' not in html and 'name="group_keywords"' not in html)

print("\n[B] 保存 → 写进 labels.groups.<gid>.summary_hint")
r = c.post("/config/filter/save", data={"action": "save_hints", f"hint_{GID}": HINT},
           follow_redirects=False)
check("POST save_hints -> 302", r.status_code == 302, str(r.status_code))
lab = labels_now()
check("提示已写入", lab["groups"][GID].get("summary_hint") == HINT,
      repr(lab["groups"][GID].get("summary_hint"))[:60])
check("群名没被动", lab["groups"][GID].get("name") == "岳章形家风水班")
check("重点人没被动", lab["groups"][GID].get("focus_members") == ["wxid_a"])
check("敏感关键词没被动", lab["groups"][GID].get("keywords") == ["报价"])
check("senders 段没动", lab["senders"] == LABELS["senders"])
html2 = c.get("/filter").get_data(as_text=True)
check("页面回显已保存的提示", HINT in html2)
check("归档范围卡显示已设置群数", "个群已设置" in html2)

print("\n[C] 清空 → 删除该键")
c.post("/config/filter/save", data={"action": "save_hints", f"hint_{GID}": "   "})
check("键被删除（不留空串）", "summary_hint" not in labels_now()["groups"][GID],
      str(labels_now()["groups"][GID].get("summary_hint")))
check("清空不影响关键词", labels_now()["groups"][GID].get("keywords") == ["报价"])

print("\n[D] 只写监控名单里的群（页面外字段不被顺手写入）")
c.post("/config/filter/save", data={"action": "save_hints",
                                    f"hint_{GID}": HINT,
                                    f"hint_{OTHER}": "不该被写入的提示"})
lab = labels_now()
check("监控名单里的群写入成功", lab["groups"][GID].get("summary_hint") == HINT)
check("**不在监控名单里的群没被写入**", "summary_hint" not in lab["groups"][OTHER],
      str(lab["groups"][OTHER].get("summary_hint")))

print("\n[E] 保存后立刻进 prompt（同一份数据）")
from consumer import summarize as S  # noqa: E402
p = S.build_prompt("2026-10-07", {GID: ["[10:00] 周宏伟（文本）：今天讲第九课"]}, labels_now())
check("prompt 含该提示原文", HINT in p)
check("prompt 含「本群总结提示」段", "本群总结提示：" in p)
check("prompt 含 ★ 说明（重点人优先但别忽略他人）", "★" in p and "别人的提问与回应" in p)

print("\n[F] 重点人语义：监控群 + 设了重点人 → 仍整群归档")
from consumer import digest as D  # noqa: E402
row_lay = {"group_name": GID, "sender": "wxid_lay", "msg_type": 1,
           "content": "路人的话", "transcript": None, "priority": 0}
row_foc = {"group_name": GID, "sender": "wxid_a", "msg_type": 1,
           "content": "重点人的话", "transcript": None, "priority": 1}
excl = {3, 47, 51, 10000}
LAB = labels_now()
check("路人进归档（上下文优先）", D._in_digest(row_lay, LAB, excl, {GID}) is True)
check("重点人也进归档", D._in_digest(row_foc, LAB, excl, {GID}) is True)
check("重点人打 ★", D._is_star(row_foc, LAB) is True)
check("路人不打 ★", D._is_star(row_lay, LAB) is False)
focus_html = c.get(f"/config/focus/{GID}").get_data(as_text=True)
check("重点人页文案已更新（不再说'只归档这些人'）",
      "不会</u>把其他人过滤掉" in focus_html or "整群对话都进归档" in focus_html)
check("重点人页显示 🧠 总结提示 状态", "总结提示" in focus_html)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("每群总结提示（监控规则页维护）验证通过")
