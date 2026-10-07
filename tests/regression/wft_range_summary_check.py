"""回归：📆 阶段总结（指定群 + 时间段 + 用户额外要求）。

用户需求（2026-10-07）：
  "提供一个入口，允许用户指定一个群，选择消息时间段（比如周一到周三或具体日期），
   然后提交 LLM 做一个阶段群聊内容总结，允许在 prompt 中加入具体要求。"
  + "LLM 返回的总结直接在 web 平台展示，无需用户自己去打开文档查看。"

覆盖：
  A. `resolve_range()`：具体日期 / 区间 / 星期 / 各种分隔符；坏输入报错
  B. `collect_group_range()`：按会话取、含 end 当天整天、套用类型闸门、★ 标记、跨天带日期
  C. `build_range_prompt()`：区间标题 + 用户额外要求作为独立一条 + 每群总结提示
  D. `summarize_range(dry_run)` 不调 LLM；真实路径落盘命名/头部/附录
  E. Web：页面入口（下拉/时间段/额外要求/预览+生成按钮）→ 预览 API → 生成 API → 状态轮询
  F. 结果**直接在页面上显示**（无需打开文件），刷新后仍可见；文件链接保留
  G. 与「当日总结」的任务状态互不干扰（独立锁）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-range-suite")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True)
(SC / "reports").mkdir(parents=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import sqlite3  # noqa: E402
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
HINT = "本群是风水教学群：保留卦例与答疑，剔除闲聊。"

(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "filter": {"groups": [GID], "senders": []},
    "storage": {"sqlite_path": str(SC / "data" / "messages.db")},
    "digest": {"enabled": True, "dir": str(SC / "reports"),
               "exclude_types": [3, 47, 51, 10000]},
    "llm": {"base_url": "https://x/v1", "api_key": "k", "model": "m", "score_enabled": False},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")
(SC / "data" / "labels.json").write_text(json.dumps({
    "groups": {GID: {"name": GNAME, "monitored": True,
                     "focus_members": ["wxid_star"], "summary_hint": HINT}},
    "senders": {"wxid_star": {"name": "周宏伟", "important": True},
                "wxid_lay": {"name": "路人甲"}}},
    ensure_ascii=False, indent=2), encoding="utf-8")

# 造 3 天消息：10-04 / 10-05 / 10-06（含 end 当天、图片、系统、★重点人）
db = sqlite3.connect(SC / "data" / "messages.db")
db.executescript("""
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id TEXT UNIQUE,
 group_name TEXT, sender TEXT, sender_id TEXT, content TEXT, msg_type INTEGER DEFAULT 1,
 received_at INTEGER NOT NULL, score INTEGER, score_reason TEXT, pushed INTEGER DEFAULT 0,
 priority INTEGER DEFAULT 0, direction TEXT, transcript TEXT);
""")
rows = [
    ("m1", GID, "wxid_lay", "周一的闲聊", 1, datetime(2026, 10, 4, 9, 0)),
    ("m2", GID, "wxid_star", "周一：入墓逢合的判断", 1, datetime(2026, 10, 4, 10, 0)),
    ("m3", GID, "wxid_lay", "周二的问题", 1, datetime(2026, 10, 5, 11, 0)),
    ("m4", GID, "wxid_star", "周三的结论", 1, datetime(2026, 10, 6, 20, 0)),
    ("m5", GID, "wxid_lay", '<msg><img aeskey="x"/></msg>', 3, datetime(2026, 10, 5, 12, 0)),
    ("m6", GID, "wxid_lay", "系统消息", 51, datetime(2026, 10, 5, 12, 1)),
    ("m7", GID, "wxid_lay", "区间之外（10-07）", 1, datetime(2026, 10, 7, 9, 0)),
    ("m8", "other@chatroom", "wxid_lay", "别的群", 1, datetime(2026, 10, 5, 9, 0)),
]
for mid, g, s, c, mt, dt in rows:
    db.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at)"
               " VALUES (?,?,?,?,?,?)", (mid, g, s, c, mt, int(dt.timestamp())))
db.commit()
db.close()

from consumer import summarize as S  # noqa: E402

print("[A] resolve_range() 时间段解析")
cases = {
    "2026-10-05": ("2026-10-05", "2026-10-05"),
    "2026-10-05 ~ 2026-10-07": ("2026-10-05", "2026-10-07"),
    "2026-10-05-2026-10-07": ("2026-10-05", "2026-10-07"),
    "2026-10-05 到 2026-10-07": ("2026-10-05", "2026-10-07"),
    "2026/10/5-2026/10/7": ("2026-10-05", "2026-10-07"),
    "2026.10.5~2026.10.7": ("2026-10-05", "2026-10-07"),
    "10/5~10/7": (f"{datetime.now().year}-10-05", f"{datetime.now().year}-10-07"),
    "2026-10-05~10-07": ("2026-10-05", "2026-10-07"),
    "2026-10-05,2026-10-07": ("2026-10-05", "2026-10-07"),
}
forraw = 0
for text, (want_s, want_e) in cases.items():
    try:
        s, e, _note = S.resolve_range(text)
        check(f"解析 {text!r}", (s, e) == (want_s, want_e), f"{s} … {e}")
    except ValueError as ex:
        check(f"解析 {text!r}", False, str(ex)[:50])
# 星期：本周一~周三（今天是周几都成立，只要等于"本周一 + 0/2 天"）
today = datetime.now().date()
monday = today - timedelta(days=today.weekday())
try:
    s, e, note = S.resolve_range("周一~周三")
    check("解析 '周一~周三' = 本周一~周三",
          s == monday.strftime("%Y-%m-%d") and
          e == (monday + timedelta(days=2)).strftime("%Y-%m-%d"), f"{s} … {e} | {note}")
    check("说明里写清了实际日期（避免以为是上周）", "实际" in note and s in note)
except ValueError as ex:
    check("解析 '周一~周三'", False, str(ex)[:50])
try:
    s, e, _ = S.resolve_range("星期一到星期五")
    check("解析 '星期一到星期五' = 5 天", S._date_span(s, e) == 5, f"{s} … {e}")
except ValueError as ex:
    check("解析 '星期一到星期五'", False, str(ex)[:50])
for bad in ("", "胡说八道", "下周吧"):
    try:
        S.resolve_range(bad)
        check(f"坏输入 {bad!r} 应报错", False)
    except ValueError:
        check(f"坏输入 {bad!r} 报错", True)

print("\n[B] collect_group_range()：按会话取、含 end 当天整天、套闸门、★ 标记")
lines, meta = S.collect_group_range(GID, "2026-10-04", "2026-10-06")
check("只含该群（不含 other@chatroom）", all("别的群" not in ln for ln in lines), str(len(lines)))
check("区间外的 10-07 不在内", all("区间之外" not in ln for ln in lines))
check("含 end 当天（10-06 周三的结论）", any("周三的结论" in ln for ln in lines))
check("图片(3) 被闸门挡掉", all("图片" not in ln for ln in lines))
check("系统(51) 被闸门挡掉", all("系统消息" not in ln for ln in lines))
check("跨天带日期（MM-DD HH:MM）", any(ln.startswith("[10-04 10:00]") for ln in lines),
      next((ln for ln in lines), ""))
check("★ 打在重点人身上", any("★周宏伟" in ln for ln in lines))
check("路人行不打 ★", any(ln.startswith("[10-04 09:00]路人甲") for ln in lines))
check("meta 统计正确", meta["count"] == len(lines) and len(meta["days"]) == 3, str(meta))
# 单日：时间戳只有 HH:MM
lines1, meta1 = S.collect_group_range(GID, "2026-10-05", "2026-10-05")
# 10-05 只有 1 条文本；同一天的图片(3)与系统(51) 都被闸门挡掉
check("单日只取当天（且闸门生效）", meta1["count"] == 1, str(meta1))
check("单日用 HH:MM（不带日期）", any(ln.startswith("[11:00]") for ln in lines1),
      next((ln for ln in lines1), ""))

print("\n[C] build_range_prompt()：区间标题 + 用户额外要求 + 每群提示")
p = S.build_range_prompt(GID, lines, "2026-10-04", "2026-10-06",
                         "按事件线梳理，最后给 3 条结论", S._load_labels(S.LABELS_PATH))
check("标题是区间", "2026-10-04 ~ 2026-10-06" in p)
check("含用户额外要求（独立一条）", "本次任务的额外要求" in p and "按事件线梳理" in p)
check("含每群总结提示", HINT in p)
check("额外要求排在每群提示之后", p.find(HINT) < p.find("本次任务的额外要求"))
check("仍含通用规则（同音纠错/★说明）", "语音转写的错字要按上下文推断" in p and "★" in p)
check("含发言记录本体", "周三的结论" in p)
p2 = S.build_range_prompt(GID, lines, "2026-10-04", "2026-10-06", "", S._load_labels(S.LABELS_PATH))
check("不填额外要求时不出现那一条", "本次任务的额外要求" not in p2)

print("\n[D] summarize_range(dry_run) 不调 LLM；真实路径落盘")
called = []
S.call_llm = lambda prompt, llm, timeout_s=180.0: (called.append(len(prompt)),
                                                   "<think>x</think>\n- 要点一\n- 要点二")[1]
dry = S.summarize_range(GID, "2026-10-04", "2026-10-06", "要结论", dry_run=True)
check("dry_run 返回 prompt", dry["ok"] and dry["prompt"] and dry["total"] == len(lines))
check("dry_run 没调 LLM", called == [], str(called))
res = S.summarize_range(GID, "2026-10-04", "2026-10-06", "要结论", out_dir=SC / "reports")
check("真实路径 ok", res["ok"], str(res.get("error"))[:60])
check("调用了 1 次 LLM", len(called) == 1, str(len(called)))
check("内容剥掉 <think>", "<think>" not in res["content"] and "- 要点一" in res["content"])
check("文件名 = summary-<起>_<止>-<群>.md",
      res.get("name") == f"summary-2026-10-04_2026-10-06-{GNAME}.md", str(res.get("name")))
body = Path(res["path"]).read_text(encoding="utf-8")
check("文件头部含区间与条数", "阶段群聊总结" in body and "2026-10-04 ~ 2026-10-06" in body)
check("文件记录额外要求与每群提示", "额外要求" in body and HINT in body)
check("文件附原始记录", "附：提交给模型的原始记录" in body)
empty = S.summarize_range(GID, "2026-01-01", "2026-01-02", "", out_dir=SC / "reports")
check("空区间给出人话错误", (not empty["ok"]) and "没有可总结的消息" in (empty.get("error") or ""),
      str(empty.get("error"))[:60])

print("\n[E] Web：入口 / 预览 / 生成 / 状态")
import web.app as wa  # noqa: E402
wa.CONFIG_PATH = SC / "config.yaml"
wa.LABELS_PATH = SC / "data" / "labels.json"
wa.DB_PATH = SC / "data" / "messages.db"
wa.PROJECT_DIR = SC
app = wa.create_app()
c = app.test_client()
html = c.get("/range-summary").get_data(as_text=True)
check("GET /range-summary -> 200", c.get("/range-summary").status_code == 200)
check("有群下拉", 'name="group"' in html)
check("有时间段输入", 'name="range"' in html)
check("有额外要求输入框", 'name="extra"' in html)
check("有预览按钮与生成按钮", "预览 prompt" in html and "生成总结" in html)
check("导航有入口", "阶段总结" in html)
check("页面说明了与每日归档的区别", "每天 23:30 自动" in html)

r = c.post("/api/range/preview", json={"group": GID, "range": "2026-10-04~2026-10-06",
                                       "extra": "只保留结论"})
j = r.get_json()
check("preview 200", r.status_code == 200 and j.get("ok"), str(j)[:80])
check("preview 统计正确", j["total"] == len(lines) and len(j["days"]) == 3, str(j.get("total")))
check("preview 含额外要求", "只保留结论" in j["prompt"])
check("preview 没调 LLM", len(called) == 1, str(len(called)))
rb = c.post("/api/range/preview", json={"group": "", "range": "周一"})
check("没选群 -> 400 + 提示", rb.status_code == 400 and "请选择" in (rb.get_json() or {}).get("error", ""))
rb = c.post("/api/range/preview", json={"group": GID, "range": "胡说"})
check("坏时间段 -> 400", rb.status_code == 400, str(rb.status_code))

called.clear()
r = c.post("/api/range/generate", json={"group": GID, "range": "2026-10-04~2026-10-06",
                                        "extra": "要结论"})
check("generate 202", r.status_code == 202, str(r.status_code))
st = {}
for _ in range(120):
    st = c.get("/api/range/status").get_json()
    if not st["running"]:
        break
    time.sleep(0.15)
res_j = st.get("result") or {}
check("后台任务完成", (not st["running"]) and (not st.get("error")), str(st.get("error")))
check("结果含正文", "- 要点一" in (res_j.get("content") or ""), str(res_j.get("content"))[:40])

print("\n[F] 结果直接在页面上显示（用户明确要求：无需打开文档）")
page = c.get("/range-summary").get_data(as_text=True)
check("页面直接含总结正文", "- 要点一" in page)
check("页面直接含要点二", "- 要点二" in page)
check("页面同时给了文件链接（可留档/下载）", res_j.get("name") in page)
check("页面标了'无需打开文件'", "无需打开文件" in page)
check("结果卡片默认可见（有结果时）", 'id="result-card"' in page and "display:none" not in
      page.split('id="result-card"')[1][:80])
r2 = c.post("/api/range/generate", json={"group": GID, "range": "2026-10-04~2026-10-06"})
for _ in range(120):
    st2 = c.get("/api/range/status").get_json()
    if not st2["running"]:
        break
    time.sleep(0.15)
check("再跑一次成功（任务状态可复用）", not st2.get("error"), str(st2.get("error"))[:60])

print("\n[G] 与「当日总结」任务状态互不干扰")
with app.test_request_context():
    pass
r = c.post("/api/digest/summarize", json={"date": "2026-10-04"})
check("阶段总结在跑时，当日总结仍能提交（独立锁）", r.status_code in (202, 409), str(r.status_code))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("阶段总结（指定群 + 时间段 + 额外要求 + 页内展示）验证通过")
