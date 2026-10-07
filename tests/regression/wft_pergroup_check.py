"""验证「每群单独一份总结」（digest.summarize_mode = per_group）与合并模式。

用 monkeypatch 替换 `call_llm`，不联网；用真实的 collect()/build_prompt() 逻辑。
覆盖：
  * 每群一份：N 个群 → N 次调用、N 个文件，文件名含群名，正文只含该群
  * 合并模式：1 次调用、1 个文件，正文含所有群
  * 文件名安全：非法字符/空名/重名都处理
  * 某个群失败不影响其它群（errors 记录）
  * print_prompt 不调用 LLM
  * Web 接口返回文件清单
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
SC = Path(r"D:\projects\.wft-pergroup")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
(SC / "reports").mkdir(parents=True, exist_ok=True)
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
    "storage": {"sqlite_path": "data/messages.db"},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "summarize": True,
               "summarize_mode": "per_group", "exclude_types": [47]},
    "llm": {"base_url": "https://api.minimax.cn/v1", "api_key": "sk-x", "model": "MiniMax-M3"},
    "filter": {"groups": [], "senders": []},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")

# 三个群 + 一个名字含非法字符的群
(SC / "data" / "labels.json").write_text(json.dumps({
    "groups": {
        "g1@chatroom": {"name": "风水班"},
        "g2@chatroom": {"name": "业主群"},
        "g3@chatroom": {"name": "甲/乙:丙*丁?"},          # 非法文件名字符
        "g4@chatroom": {"name": ""},                       # 空名 → 用 id 兜底
    },
    "senders": {},
}, ensure_ascii=False, indent=2), encoding="utf-8")

db = SC / "data" / "messages.db"
conn = sqlite3.connect(db)
conn.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER UNIQUE,
 group_name TEXT, sender TEXT, sender_id INTEGER, content TEXT, msg_type INTEGER,
 received_at INTEGER, score INTEGER, score_reason TEXT, pushed INTEGER, priority INTEGER,
 direction TEXT, transcript TEXT)""")
from datetime import datetime  # noqa: E402
DAY = int(datetime(2026, 10, 6, 10, 0, 0).timestamp())   # 当天上午（本地时区），确保落在窗口内
mid = 0
for g, n in (("g1@chatroom", 3), ("g2@chatroom", 2), ("g3@chatroom", 2), ("g4@chatroom", 1)):
    for i in range(n):
        mid += 1
        conn.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type,"
                     " received_at, direction) VALUES (?,?,?,?,1,?,'in')",
                     (mid, g, "wxid_a", f"{g} 的第 {i+1} 条", DAY + 100 + mid))
conn.commit()
conn.close()

# 让 4 个群都算"在归档范围"（监控名单）
(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "storage": {"sqlite_path": "data/messages.db"},
    "filter": {"groups": ["g1@chatroom", "g2@chatroom", "g3@chatroom", "g4@chatroom"], "senders": []},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "summarize": True,
               "summarize_mode": "per_group", "exclude_types": [47]},
    "llm": {"base_url": "https://api.minimax.cn/v1", "api_key": "sk-x", "model": "MiniMax-M3"},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")

import consumer.summarize as sm  # noqa: E402

print("[1] 基础：模式读取与文件名清洗")
check("默认从 config 读到 per_group", sm.summarize_mode() == "per_group", sm.summarize_mode())
check("非法字符被替换", sm.safe_name("甲/乙:丙*丁?") == "甲_乙_丙_丁_", sm.safe_name("甲/乙:丙*丁?"))
check("空名用兜底", sm.safe_name("", fallback="g4") == "g4")
check("超长名被截断", len(sm.safe_name("群" * 100)) <= 60, str(len(sm.safe_name("群" * 100))))

print("\n[2] collect() 应该拿到 4 个会话、8 条")
collected = sm.collect("2026-10-06")
check("collect 有结果", collected is not None)
grouped, total = collected
check("4 个会话", len(grouped) == 4, str(list(grouped)))
check("8 条消息", total == 8, str(total))

print("\n[3] 每群一份：4 个群 → 4 次调用、4 个文件")
calls: list[str] = []


def fake_llm(prompt: str, llm: dict, timeout_s: float = 180.0) -> str:
    calls.append(prompt)
    return "总结正文（第 %d 次调用）" % len(calls)


sm.call_llm = fake_llm            # type: ignore[assignment]
run = sm.summarize("2026-10-06", out_dir=SC / "reports", mode="per_group")
check("返回 SummarizeRun", run is not None and run.mode == "per_group")
check("调用次数 = 群数（4）", len(calls) == 4, str(len(calls)))
check("产出 4 份文件", len(run.results) == 4, str(run.files))
names = sorted(run.files)
print("    文件名:", names)
check("文件名含日期与群名", any("summary-2026-10-06-风水班.md" == n for n in names), str(names))
check("非法字符被清洗进文件名", any(n == "summary-2026-10-06-甲_乙_丙_丁_.md" for n in names), str(names))
check("空名群用 id 兜底", any(n == "summary-2026-10-06-g4.md" for n in names), str(names))
check("文件确实落盘", all((SC / "reports" / n).is_file() for n in names))
for r in run.results:
    body = r.path.read_text(encoding="utf-8")
    check(f"「{r.group}」的正文只含本群",
          r.group in body and all(o not in body.split("## 附")[0] for o in ()),
          "")
# 每份文件的 prompt 只含自己的群
for r in run.results:
    others = [g for g in grouped if g != r.group]
    body_all = r.path.read_text(encoding="utf-8")
    only_own = all(o not in body_all for o in others)
    check(f"「{r.group}」文件里不含其它群的原始记录", only_own, str(others))

print("\n[4] 合并模式：1 次调用、1 个文件、含所有群")
calls.clear()
run2 = sm.summarize("2026-10-06", out_dir=SC / "reports", mode="combined")
check("调用次数 = 1", len(calls) == 1, str(len(calls)))
check("产出 1 份文件", len(run2.results) == 1, str(run2.files))
check("文件名是 summary-<日期>.md", run2.files == ["summary-2026-10-06.md"], str(run2.files))
body = run2.results[0].path.read_text(encoding="utf-8")
check("正文含所有群", all(g in body for g in grouped), "")

print("\n[5] 某个群失败不影响其它群")
calls.clear()


def flaky_llm(prompt: str, llm: dict, timeout_s: float = 180.0) -> str:
    calls.append(prompt)
    if "风水班" in prompt:
        raise RuntimeError("模拟该群调用失败")
    return "ok"


sm.call_llm = flaky_llm           # type: ignore[assignment]
run3 = sm.summarize("2026-10-06", out_dir=SC / "reports", mode="per_group")
check("失败群被记录", any("风水班" in e for e in run3.errors), str(run3.errors))
check("其它群照常产出 3 份", len(run3.results) == 3, str(run3.files))

print("\n[6] print_prompt 不调用 LLM")
calls.clear()
r = sm.summarize("2026-10-06", print_prompt=True)
check("返回 None", r is None)
check("没有调用 LLM", len(calls) == 0, str(len(calls)))

print("\n[7] Web 接口")
import web.app as webapp  # noqa: E402
app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()
r = c.post("/api/digest/preview", json={"date": "2026-10-06"})
j = r.get_json()
check("preview 200", r.status_code == 200 and j.get("ok"), str(r.status_code))
check("preview 报出模式", j.get("mode") == "per_group", str(j.get("mode")))
check("preview 给了每群 prompt", len(j.get("per_group") or []) == 4, str(len(j.get("per_group") or [])))
b = c.get("/digest").get_data(as_text=True)
check("/digest 页正常", "总结" in b)
cfg_html = c.get("/config").get_data(as_text=True)
check("总设置页有『当日总结方式』下拉", "digest.summarize_mode" in cfg_html or "当日总结方式" in cfg_html)
check("下拉含两个选项", "每个群单独一份总结" in cfg_html and "所有群合并成一份总结" in cfg_html)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("每群单独一份总结 验证通过")
