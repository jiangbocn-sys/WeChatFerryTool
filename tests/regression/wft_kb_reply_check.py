"""回归：知识库问答**接入 replier** 的行为（P2）。

全部用桩（不打网络、不发微信）：
  A. 触发匹配：以 `@<虚拟号>` 开头才接管；名字**按群可配**；不接管则回退模板回复
  B. 按群开关：总闸 + `kb_on` 都要开；未配置的群不接管
  C. 测试模式（默认）：**只记日志不发送**，但**照样接管**（避免又被模板答一遍）
  D. 限速：每群 2 分钟；冷却期内**排队**（不丢弃）
  E. 队列上限：满 10 条丢**最早**那条并记警告
  F. 去重：同群同人相似问题在窗口内只答一次
  G. 延迟参数：5~10 秒（`pick_delay` 落在区间内）
  H. 发送后记限速 + 写审计 `kb_qa.jsonl`
  I. 独立状态文件：知识库限速**不与模板回复共用** `reply_state.json`
  J. 空问题（只 @ 不说话）不接管、不排队
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-kbreply-suite")
shutil.rmtree(SC, ignore_errors=True)
SC.mkdir(parents=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer.replier import Replier  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


G1 = "g1@chatroom"
G2 = "g2@chatroom"
G3 = "g3@chatroom"

(SC / "labels.json").write_text(json.dumps({
    "groups": {
        G1: {"name": "甲群", "kb_on": True, "kb_at": "小易"},
        G2: {"name": "乙群", "kb_on": True, "kb_at": "小助"},
        G3: {"name": "丙群", "kb_on": False, "kb_at": "小易"},
    },
    "senders": {"w1": {"name": "张三"}},
}, ensure_ascii=False), encoding="utf-8")


class FakeHook:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to, msg):
        self.sent.append((to, msg))
        return {"ret": 0, "retmsg": "success"}


def make(**kb) -> tuple[Replier, FakeHook]:
    hook = FakeHook()
    base = {"enabled": True, "kb": {"enabled": True, "test_only": True,
                                    "cooldown_s": 120, "delay_min_s": 5,
                                    "delay_max_s": 10, "max_queue": 10,
                                    "dedup_window_s": 120}}
    base["kb"].update(kb)
    r = Replier.from_yaml(base, hook=hook, state_path=SC / "reply_state.json",
                          labels_path=SC / "labels.json")
    r._kb_data_root = SC
    # 桩：不真的查知识库/调 LLM
    r._kb_ask = lambda q, g, dr, **kw: type("R", (), {
        "answer": f"资料里说：{q}（据资料整理）", "hits": [], "grounded": True,
        "dropped": "", "llm_calls": 2, "from_kb": True})()
    return r, hook


print("[A] 触发匹配：以 @<该群虚拟号> 开头才接管")
r, hook = make()
check("甲群 @小易 → 接管", r.maybe_reply_kb(group_name=G1, sender="w1",
                                           content="@小易 三合局条件") is True)
check("乙群 @小助 → 接管（名字按群可配）",
      r.maybe_reply_kb(group_name=G2, sender="w1", content="@小助 三合局条件") is True)
check("乙群 @小易 → **不接管**",
      r.maybe_reply_kb(group_name=G2, sender="w1", content="@小易 三合局条件") is False)
check("不在开头 → 不接管",
      r.maybe_reply_kb(group_name=G1, sender="w1", content="大家说@小易 是啥") is False)
check("没有 @ → 不接管",
      r.maybe_reply_kb(group_name=G1, sender="w1", content="三合局条件") is False)
check("**只 @ 不说话 → 不接管**（空问题）",
      r.maybe_reply_kb(group_name=G1, sender="w1", content="@小易") is False)

print("\n[B] 按群开关：总闸 + kb_on 都要开")
r2, _ = make()
check("丙群 kb_on=false → 不接管",
      r2.maybe_reply_kb(group_name=G3, sender="w1", content="@小易 三合局") is False)
r3, _ = make(enabled=False)
check("总闸关 → 所有群都不接管",
      r3.maybe_reply_kb(group_name=G1, sender="w1", content="@小易 三合局") is False)

print("\n[C] 测试模式：接管但不发送")
r4, hook4 = make(test_only=True)
took = r4.maybe_reply_kb(group_name=G1, sender="w1", content="@小易 三合局条件")
time.sleep(0.3)
check("测试模式仍**接管**（避免被模板再答一次）", took is True)
check("测试模式**不发送**", hook4.sent == [], str(hook4.sent))

print("\n[D] 限速与排队：每群 2 分钟，冷却期内入队不丢")
r5, hook5 = make(test_only=False)
r5.maybe_reply_kb(group_name=G1, sender="w1", content="@小易 第一个问题")
with r5._kb_lock:
    first_q = len(r5._kb_q.get(G1) or [])
check("第一条不排队（直接安排）", first_q == 0, str(first_q))
# 模拟"刚回答过" → 触发冷却
r5._kb_rl.record_reply(G1)
r5.maybe_reply_kb(group_name=G1, sender="w2", content="@小易 第二个问题")
r5.maybe_reply_kb(group_name=G1, sender="w3", content="@小易 第三个问题")
with r5._kb_lock:
    q = list(r5._kb_q.get(G1) or [])
check("冷却期内**入队**（不丢弃）", len(q) == 2, f"{len(q)} 条")
check("队列里是问题文本", any("第二个问题" in str(x[1]) for x in q), str(q[:1]))
check("**每群独立**：乙群不受甲群冷却影响",
      r5._kb_rl.can_reply(G2)[0] is True)
check("甲群确实在冷却", r5._kb_rl.can_reply(G1)[0] is False)

print("\n[E] 队列上限：满 10 条丢最早")
r6, _ = make(test_only=False, max_queue=10)
r6._kb_rl.record_reply(G1)
for i in range(12):
    r6.maybe_reply_kb(group_name=G1, sender=f"w{i}", content=f"@小易 问题编号{i}")
with r6._kb_lock:
    q6 = list(r6._kb_q.get(G1) or [])
check("队列不超过上限", len(q6) <= 10, str(len(q6)))
check("**丢了最早的**（保留最新的）", "问题编号0" not in str(q6), str(q6[0][1])[:20])
check("最新那条在队列里", any("问题编号11" in str(x[1]) for x in q6))

print("\n[F] 去重：同群同人相似问题只答一次")
r7, _ = make(test_only=False)
a = r7.maybe_reply_kb(group_name=G1, sender="w1", content="@小易 三合局条件是什么")
b = r7.maybe_reply_kb(group_name=G1, sender="w1", content="@小易 三合局条件是什么？")
check("两次都被接管（不让模板插话）", a is True and b is True)
check("第二次**未新增排队/安排**（去重生效）",
      len(r7._timers) <= 1, f"timers={len(r7._timers)}")
c = r7.maybe_reply_kb(group_name=G1, sender="w1", content="@小易 完全不同的问题内容")
# ⚠️ 第一条已经"占位"（安排了延迟发送），所以不同问题进的是**冷却队列**，
#    不能用 `_timers` 计数来判断（那是直接安排的定时器）。
with r7._kb_lock:
    q7 = list(r7._kb_q.get(G1) or [])
check("不同问题**不被去重**（进队列等着答）",
      any("完全不同" in str(x[1]) for x in q7), str([x[1] for x in q7])[:60])
d = r7.maybe_reply_kb(group_name=G1, sender="w9", content="@小易 三合局条件是什么")
check("**换个人问同一问题** → 不去重（各自回答）", d is True)

print("\n[G] 延迟参数 5~10 秒")
r8, _ = make(test_only=False)
delays = [r8._kb_rl.pick_delay() for _ in range(30)]
check("延迟落在 5~10 秒", all(5 <= x <= 10 for x in delays),
      f"{min(delays)}~{max(delays)}")

print("\n[H] 发送后记账 + 审计")
r9, hook9 = make(test_only=False)
r9._kb_answer_one(G1, "w1", "三合局条件")
check("真的调用了 send_text", len(hook9.sent) == 1, str(len(hook9.sent)))
check("发送内容带 @ 前缀", hook9.sent and hook9.sent[0][1].startswith("@张三"), 
      hook9.sent[0][1][:30] if hook9.sent else "")
check("发送到正确的群", hook9.sent and hook9.sent[0][0] == G1)
check("已记限速（再问会冷却）", r9._kb_rl.can_reply(G1)[0] is False)
audit = SC / "data" / "kb_qa.jsonl"
check("审计文件已写", audit.is_file())
if audit.is_file():
    rec = json.loads(audit.read_text(encoding="utf-8").strip().splitlines()[-1])
    check("审计含问题/回答/自检结论",
          rec.get("question") == "三合局条件" and rec.get("grounded") is True
          and "answer" in rec, str(list(rec.keys())))

print("\n[I] 知识库限速与模板回复**互不干扰**")
r10, _ = make(test_only=False)
check("两个状态文件不同",
      r10._kb_rl.state_path != r10.rl.state_path,
      f"{r10._kb_rl.state_path.name} vs {r10.rl.state_path.name}")
r10._kb_rl.record_reply(G1)
check("知识库冷却时，模板回复通道仍可用", r10.rl.can_reply(G1)[0] is True)

print("\n[J] 热重载按群配置")
(SC / "labels.json").write_text(json.dumps({
    "groups": {G1: {"name": "甲群", "kb_on": True, "kb_at": "小易"},
               G2: {"name": "乙群", "kb_on": True, "kb_at": "小新名"}},
    "senders": {},
}, ensure_ascii=False), encoding="utf-8")
r11, _ = make()
before = r11.kb_trigger_name(G2)
r11.reload_kb_group_fields()
check("热重载后虚拟号名更新", r11.kb_trigger_name(G2) == "小新名",
      f"{before} → {r11.kb_trigger_name(G2)}")

print("\n[K] 页面：设置页有总闸段、监控页有群级字段、保存能落盘")
import json as _json  # noqa: E402
GID = "g1@chatroom"
(SC / "config.yaml").write_text("filter:\n  groups:\n    - " + GID + "\n", encoding="utf-8")
(SC / "data").mkdir(exist_ok=True)
(SC / "data" / "labels.json").write_text(json.dumps(
    {"groups": {GID: {"name": "甲群", "monitored": True}}, "senders": {}},
    ensure_ascii=False), encoding="utf-8")
import importlib  # noqa: E402
import paths  # noqa: E402
importlib.reload(paths)
from web.app import create_app  # noqa: E402
_c = create_app().test_client()
_cfg_html = _c.get("/config").get_data(as_text=True)
check("设置页有「群知识库问答」段", "群知识库问答" in _cfg_html)
check("设置页有测试模式开关说明", "只记日志不发送" in _cfg_html)
check("设置页说明「总闸 + 群勾选都要开」", "每个群还要各自勾选" in _cfg_html
      or "总闸 + 群勾选" in _cfg_html, "")
_flt_html = _c.get("/filter").get_data(as_text=True)
check("监控页有群级知识库列", "知识库问答" in _flt_html)
check("监控页有资料目录输入框", f"kb_dir_{GID}" in _flt_html)
check("监控页有虚拟号名输入框", f"kb_at_{GID}" in _flt_html)
check("监控页有启用勾选", f"kb_on_{GID}" in _flt_html)
_save = _c.post("/config/filter/save", data={
    "action": "save_hints", f"kb_on_{GID}": "1", f"kb_at_{GID}": "小易",
    f"kb_dir_{GID}": r"D:\JiangBo\Documents\wechatdocs\弟子群"},
    follow_redirects=False)
check("保存返回 302（重定向成功）", _save.status_code == 302, str(_save.status_code))
_ent = (json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8"))
        .get("groups") or {}).get(GID) or {}
check("**kb_on / kb_at / kb_dir 都落盘**",
      _ent.get("kb_on") is True and _ent.get("kb_at") == "小易"
      and _ent.get("kb_dir", "").endswith("弟子群"), str({k: _ent.get(k) for k in
                                                          ("kb_on", "kb_at", "kb_dir")}))
_c.post("/config/filter/save", data={"action": "save_hints", f"kb_on_{GID}": "",
                                     f"kb_at_{GID}": "", f"kb_dir_{GID}": ""},
        follow_redirects=False)
_ent2 = (json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8"))
         .get("groups") or {}).get(GID) or {}
check("取消勾选/清空 → 键被删除（不留空值）",
      "kb_on" not in _ent2 and "kb_at" not in _ent2 and "kb_dir" not in _ent2,
      str(sorted(_ent2.keys())))
_r3 = _c.post("/config/filter/save", data={
    "action": "save_hints", f"kb_on_{GID}": "1", f"kb_dir_{GID}": r"D:\no\such\dir\xyz"},
    follow_redirects=False)
check("资料目录不存在时给出提示（不静默）",
      "error=" in (_r3.headers.get("Location") or ""), "")
check("设置页说明回答限定资料内", "不编造" in _cfg_html or "资料中未涉及" in _cfg_html)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("知识库问答接入 replier（触发/开关/队列/去重/限速/审计）验证通过")
