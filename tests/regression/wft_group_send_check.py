"""回归：把每日总结发到群（用户按群勾选 + 全局总闸）。

需求（2026-10-10）："在监控规则设置页，每个监控的群加一个勾选项，'是否将总结发到群内'，
用户勾选此项后，每日总结生成后自动发入该群"。

⚠️ 这是本项目**唯一会主动往群里发言**的功能，所以本套件重点验证"安全闸门"：
  A. 文案清洗 `md_to_plain` 与切分 `chunk_text` / `make_messages`（纯函数，不发送）
  B. **双重开关**：总闸关 → 一条都不发；每群未勾选 → 该群不发；两者都开才发
  C. `combined` 模式（合并稿含多群内容）→ 一条都不发（防止串群）
  D. 私聊（非 @chatroom）不发
  E. 发送失败不抛异常、审计日志有记录、成功条数正确
  F. 审计日志与 SentTracker 记账（避免自己发的消息回流）
  G. 监控规则页有勾选框、保存动作会写 labels.groups.<gid>.send_summary
  H. 设置页有总闸字段 digest.send_to_group
全部用**假 hook**，绝不真发消息。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-groupsend-suite")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer.group_sender import (SummarySender, chunk_text, make_messages,  # noqa: E402
                                   md_to_plain)

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


class FakeHook:
    """假 hook：只记录调用，绝不真发消息。可注入异常。"""

    def __init__(self, fail_at: int | None = None):
        self.sent: list[tuple[str, str]] = []
        self.fail_at = fail_at

    def send_text(self, to_wxid: str, msg: str) -> dict:
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise RuntimeError("模拟发送失败")
        self.sent.append((to_wxid, msg))
        return {"ret": 0, "retmsg": "success"}


class FakeTracker:
    def __init__(self):
        self.recs: list[tuple[str, str]] = []

    def record(self, group: str, content: str) -> None:
        self.recs.append((group, content))


GID = "g1@chatroom"
GID2 = "g2@chatroom"

print("[A] 文案清洗与切分（纯函数）")
md = ("# 今日要点\n\n- **重点**：报价 1200 元\n- 第二点 `code`\n\n"
      "| a | b |\n|---|---|\n| 1 | 2 |\n\n```py\nx=1\n```\n\n### 小结\n\n*斜体*结束")
plain = md_to_plain(md)
check("去掉标题符 #", "#" not in plain and "今日要点" in plain, plain[:60])
check("去掉粗体标记 **", "**" not in plain and "重点" in plain, plain[:80])
check("列表符变成 ·", "· 重点" in plain, plain[:80])
check("去掉表格分隔行", "|---|" not in plain and "---" not in plain, plain[:120])
check("去掉代码围栏", "```" not in plain and "x=1" in plain, plain[:120])
check("去掉行内反引号", "`" not in plain, plain[:120])
check("正文数字/中文未被破坏", "1200 元" in plain and "小结" in plain)
long = "甲" * 300 + "。" + "乙" * 300 + "。" + "丙" * 300
parts = chunk_text(long, limit=400)
check("超长切成多段", len(parts) >= 3, str(len(parts)))
check("每段不超过上限(+少量空白)", all(len(p) <= 400 for p in parts if p), str([len(p) for p in parts]))
check("切分不丢内容", sum(len(p) for p in parts).__ge__(len(long) - 20), str(sum(len(p) for p in parts)))
check("短文本不切", chunk_text("短", limit=100) == ["短"])
check("空文本返回空", chunk_text("") == [])
check("空文本不产生消息", make_messages("2026-10-10", "群", "") == [])

msgs = make_messages("2026-10-10", "测试群", "内容一\n内容二")
check("首条是抬头（含日期与群名）",
      msgs and "2026-10-10" in msgs[0] and "测试群" in msgs[0], str(msgs[:1]))
check("抬头后有正文", len(msgs) == 2 and "内容一" in msgs[1], str(msgs))
big = make_messages("2026-10-10", "测试群", "长" * 5000, limit=1000)
check("长总结会分条并带序号", len(big) >= 6 and "（1/" in big[1], str(len(big)))

print("\n[B] 双重开关（核心安全闸门）")
tracker = FakeTracker()

# B1: 总闸关 → 即使某群勾选了也不发
hook = FakeHook()
s_off = SummarySender(hook, SC, {"enabled": False}, tracker=tracker)
check("总闸关：enabled_globally() 为假", s_off.enabled_globally() is False)
labels_send = {"groups": {GID: {"send_summary": True}}}
check("每群勾选为真", SummarySender.enabled_for_group(labels_send, GID) is True)
check("未勾选的群为假", SummarySender.enabled_for_group({"groups": {GID2: {}}}, GID2) is False)
check("不在 labels 里的群为假", SummarySender.enabled_for_group({}, GID) is False)

# B2: 总闸开 + 勾选 → 发
hook2 = FakeHook()
s_on = SummarySender(hook2, SC, {"enabled": True, "min_interval_s": 0}, tracker=tracker)
n = s_on.send_group(GID, ["第一条", "第二条"])
check("发送条数正确", n == 2 and len(hook2.sent) == 2, str(hook2.sent))
check("发给了正确的群", all(to == GID for to, _ in hook2.sent), str(hook2.sent))
check("内容原样发出", hook2.sent[0][1] == "第一条" and hook2.sent[1][1] == "第二条")

print("\n[C] 发送失败与审计")
hook3 = FakeHook(fail_at=1)
s3 = SummarySender(hook3, SC, {"enabled": True, "min_interval_s": 0}, tracker=FakeTracker())
n3 = s3.send_group(GID, ["a", "b", "c"])       # 第 2 条失败
check("失败后不抛异常、返回已成功条数", n3 == 1, str(n3))
check("失败后不再继续发后面的", len(hook3.sent) == 1, str(len(hook3.sent)))
audit = SC / "data" / "summary_sent.jsonl"
check("审计日志已写入", audit.is_file())
lines = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x.strip()]
check("审计记录含群与条数", any(r["group"] == GID for r in lines), str(lines[:2]))
check("审计记录了失败", any("模拟发送失败" in (r.get("error") or "") for r in lines),
      str([r.get("error") for r in lines]))

print("\n[D] SentTracker 记账（避免自己发的消息回流）")
hook4 = FakeHook()
tk = FakeTracker()
s4 = SummarySender(hook4, SC, {"enabled": True, "min_interval_s": 0}, tracker=tk)
s4.send_group(GID, ["x", "y"])
check("每条都记了 tracker", len(tk.recs) == 2, str(tk.recs))
check("记的是同一个群", all(g == GID for g, _ in tk.recs))

print("\n[E] Consumer 接线：三处安全判断都在")
import inspect  # noqa: E402
from consumer.main import Consumer  # noqa: E402
src = inspect.getsource(Consumer._send_summaries_to_groups)
check("有总闸判断", "enabled_globally" in src)
check("有每群勾选判断", "enabled_for_group" in src)
check("combined 模式不发", 'mode != "per_group"' in src or "per_group" in src)
check("私聊不发（只发 @chatroom）", "@chatroom" in src)
check("使用 group_sender.make_messages", "make_messages" in src)
init_src = inspect.getsource(Consumer.__init__)
check("归档循环里调用了发群", "_send_summaries_to_groups" in inspect.getsource(Consumer._digest_loop))
check("发群失败不会影响归档（独立 try）",
      "except Exception" in src and "不受影响" in src)

print("\n[F] 监控规则页：勾选框 + 保存动作")
appsrc = (PROJ / "web" / "app.py").read_text(encoding="utf-8")
tpl = (PROJ / "web" / "templates" / "_monitor_block.html").read_text(encoding="utf-8")
check("模板有「发到群内」勾选框", "发到群内" in tpl and 'name="send_{{ g.id }}"' in tpl)
check("模板说明了需要总闸", "digest.send_to_group" in tpl)
check("模板提示了风控风险", "风控" in tpl)
check("模板说明 combined 不发", "combined" in tpl)
check("group_rows 传了 send_summary", '"send_summary": bool(ent.get("send_summary"))' in appsrc)
check("保存动作处理 send_<群id>", 'f"send_{gkey}"' in appsrc)
check("取消勾选会删键", 'ent.pop("send_summary", None)' in appsrc)
check("设置页有总闸字段", '"path": "digest.send_to_group"' in appsrc)

print("\n[G] 真实保存流程（走 flask 测试客户端，不真发消息）")
import yaml  # noqa: E402
(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "filter": {"groups": [GID], "senders": []},
    "storage": {"sqlite_path": str(SC / "data" / "messages.db")},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")
(SC / "data" / "labels.json").write_text(json.dumps(
    {"groups": {GID: {"name": "测试群", "monitored": True}}, "senders": {}},
    ensure_ascii=False), encoding="utf-8")
try:
    import web.app as W
    app = W.create_app()
    app.config["TESTING"] = True
    cli = app.test_client()
    r = cli.post("/config/filter/save",
                 data={"action": "save_hints", f"hint_{GID}": "只留要点",
                       f"send_{GID}": "1"},
                 follow_redirects=False)
    check("保存请求被接受（无 500）", r.status_code in (200, 302), str(r.status_code))
    lab = json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8"))
    g = (lab.get("groups") or {}).get(GID) or {}
    check("勾选后 labels 里有 send_summary=True", g.get("send_summary") is True, str(g))
    check("总结提示同时保存", g.get("summary_hint") == "只留要点", str(g.get("summary_hint")))
    # 取消勾选
    r2 = cli.post("/config/filter/save",
                  data={"action": "save_hints", f"hint_{GID}": "只留要点"},
                  follow_redirects=False)
    lab2 = json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8"))
    g2 = (lab2.get("groups") or {}).get(GID) or {}
    check("取消勾选后 send_summary 被删掉", "send_summary" not in g2, str(g2))
    check("取消勾选不影响总结提示", g2.get("summary_hint") == "只留要点", str(g2))
except Exception as e:  # noqa: BLE001
    check(f"flask 测试客户端可用（{type(e).__name__}: {e}）", False)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("总结发到群（按群勾选 + 总闸 + 安全约束）验证通过")
