"""回归：知识库**蒸馏台账 + 检索 + 生成 + 自检**（consumer/kb_notes.py, kb_answer.py）。

全部用**桩 LLM**，不调用真实模型、不花钱。

覆盖：
  A. chunk_text 分块（按段落边界、不切碎、超长段硬切）
  B. 笔记落盘 + 台账（**处理完立即记账**，中断不丢进度 —— 10-15 踩到的坑）
  C. 增量：没变化 → 不重抽、不重蒸、**0 次 LLM 调用**；文件改了 → 只重蒸它自己
  D. 抽取器版本变化 → 强制重蒸
  E. 检索：BM25 命中相关主题、主题加权、无关问题不命中
  F. 触发匹配：必须以 `@名字` 开头、名字按群可配、只 @ 不说话不触发
  G. 生成：强制引用要求进 prompt、**自检发现越界 → 删句/兜底**
  H. 自检失败时不假装通过（grounded=None）、空回答走兜底
  I. 资料为空时的兜底
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-kbn-suite")
shutil.rmtree(SC, ignore_errors=True)
SC.mkdir(parents=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer import kb_notes as N  # noqa: E402
from consumer import kb_answer as A  # noqa: E402
from consumer import kb_extract as EX  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


DOCS = SC / "docs"
DOCS.mkdir(parents=True)
DATA = SC / "data"
GROUP = "测试群"

# 造两份资料：一份讲"回头克"，一份讲"三合局"
(DOCS / "回头克笔记.md").write_text(
    "# 回头克\n\n## 回头克判断\n"
    "测短期事遇动爻发动回头克，必须看动变的旺衰：动爻较旺则依然有用，变爻较旺则动爻可能变废。\n\n"
    "## 忧患卦原则\n所有忧患卦中，格局趋势大于旺衰吉凶。\n",
    encoding="utf-8")
(DOCS / "三合局笔记.txt").write_text(
    "三合局的条件\n\n"
    "寅午戌三合火局需要三者俱全；缺一个就不成局，只能论半合。\n"
    "申子辰三合水局同理。\n",
    encoding="utf-8")

print("[A] chunk_text 分块")
text = "\n".join(f"第{i}段内容" + "汉" * 50 for i in range(40))
parts = N.chunk_text(text, limit=500)
check("超长文本被分块", len(parts) > 1, f"{len(parts)} 块")
check("每块不超过上限太多", all(len(p) <= 700 for p in parts),
      str(max(len(p) for p in parts)))
check("短文本不分块", len(N.chunk_text("很短", limit=500)) == 1)
check("空文本返回空", N.chunk_text("") == [])
long_para = "汉" * 1200
lp = N.chunk_text(long_para, limit=500)
check("单段超长被硬切", len(lp) >= 3 and all(len(p) <= 500 for p in lp), str(len(lp)))

print("\n[B] 蒸馏落盘 + 台账即时记账（中断不丢进度）")
calls = {"n": 0}


def fake_distill(text_in, src, llm, timeout_s=240.0, on_call=None):
    calls["n"] += 1
    return f"## {Path(src).stem}要点\n- 这是 {src} 的蒸馏结果。", 1


N.distill_text = fake_distill            # 桩：不打网络
rep = N.distill_group(DOCS, GROUP, DATA, llm={"fake": True}, dry_run=False)
check("两份资料都蒸了", len(rep.new) == 2 and rep.notes_written == 2, str(rep.new))
check("桩调用 2 次", calls["n"] == 2, str(calls["n"]))
root = N.kb_root(DATA, GROUP)
check("台账已落盘", (root / "_sources.json").is_file())
check("索引已生成", (root / "_index.md").is_file())
ents = N.load_sources(root)
check("台账两条记录", len(ents) == 2, str(list(ents)))
check("每条都有笔记文件名", all(e.note_file for e in ents.values()))
check("每条都有指纹", all(e.fingerprint.get("size") for e in ents.values()))
check("文字稿也已落盘", (root / "text").is_dir()
      and len(list((root / "text").glob("*.txt"))) == 2)
idx = (root / "_index.md").read_text(encoding="utf-8")
check("索引里含两份资料名", "回头克笔记" in idx and "三合局笔记" in idx)

print("\n[C] 增量：没变化就不重蒸（0 次调用）")
before = calls["n"]
rep2 = N.distill_group(DOCS, GROUP, DATA, llm={"fake": True}, dry_run=False)
check("**未变化 → 不重蒸**", not rep2.new and not rep2.updated and len(rep2.unchanged) == 2,
      f"new={rep2.new} upd={rep2.updated} unch={len(rep2.unchanged)}")
check("**0 次 LLM 调用**（省钱的关键）", calls["n"] == before, f"{before} → {calls['n']}")
# 只改一个文件 → 只重蒸它
(DOCS / "三合局笔记.txt").write_text(
    "三合局的条件\n\n寅午戌三合火局需要三者俱全；缺一个只能论半合。\n新增：巳酉丑三合金局。\n",
    encoding="utf-8")
rep3 = N.distill_group(DOCS, GROUP, DATA, llm={"fake": True}, dry_run=False)
check("**只重蒸变化的那个文件**", rep3.updated == ["三合局笔记.txt"] and not rep3.new,
      str(rep3.updated))
check("只多花 1 次调用", calls["n"] == before + 1, str(calls["n"]))

print("\n[D] 抽取器版本变化 → 强制重蒸")
ents = N.load_sources(root)
for e in ents.values():
    e.fingerprint["extractor_version"] = EX.EXTRACTOR_VERSION - 1
N.save_sources(root, ents)
before = calls["n"]
rep4 = N.distill_group(DOCS, GROUP, DATA, llm={"fake": True}, dry_run=False)
check("版本变化触发重蒸", len(rep4.updated) == 2, str(rep4.updated))

print("\n[E] 检索（BM25 + 主题加权）")
secs = A.load_sections(GROUP, DATA)
check("载入到片段", len(secs) >= 2, str(len(secs)))
hits = A.retrieve("寅午戌三合局需要什么条件", secs, top_k=3)
check("相关问题命中三合局", hits and "三合" in (hits[0].topic + hits[0].text),
      hits[0].topic if hits else "(无命中)")
hits2 = A.retrieve("回头克要看动变旺衰吗", secs, top_k=3)
check("相关问题命中回头克", hits2 and "回头克" in (hits2[0].topic + hits2[0].text),
      hits2[0].topic if hits2 else "(无命中)")
check("命中带来源（出处可追溯）", bool(hits[0].source) and bool(hits[0].citation))
hits3 = A.retrieve("今天中午吃什么比较好", secs, top_k=3)
check("无关问题不命中或低分", (not hits3) or hits3[0].score < 0.5,
      f"{hits3[0].score:.2f}" if hits3 else "(无命中)")

print("\n[F] 触发匹配（必须以 @名字 开头；名字按群可配）")
st = A.strip_trigger
check("@小易 开头 → 剥出问题", st("@小易 寅午戌能三合吗", "小易") == "寅午戌能三合吗")
check("带 U+2005 也能剥", st("@小易\u2005问题内容", "小易") == "问题内容")
check("标点分隔也能剥", st("@小易，问题内容", "小易") == "问题内容")
check("**按群换名字**（@小助）", st("@小助 问题内容", "小助") == "问题内容")
check("**名字不匹配就不触发**", st("@小助 问题内容", "小易") is None)
check("**不在开头不触发**（引用别人的话）", st("大家说@小易 是啥", "小易") is None)
check("没有 @ 不触发", st("问题内容", "小易") is None)
check("**只 @ 不说话不触发**（避免空问题白花调用）", st("@小易", "小易") is None)
check("只有空格也不触发", st("@小易   ", "小易") is None)

print("\n[G] 生成 + 自检（越界必删）")


def make_llm(gen: str, chk: str, recorder: dict):
    def _call(prompt, llm, timeout_s=120.0):
        recorder.setdefault("prompts", []).append(prompt)
        return chk if "事实核查" in prompt else gen
    return _call


rec: dict = {}
A_answer_call = make_llm(
    "按资料，寅午戌三合需要三者俱全，缺一个只能论半合。〔来源：三合局笔记.txt「三合局要点」〕",
    "通过", rec)
real_call = None
import consumer.summarize as S  # noqa: E402
real_call = S.call_llm
S.call_llm = A_answer_call
res = A.answer_question("寅午戌三合局需要什么条件", GROUP, DATA, self_check=True)
check("生成了回答", bool(res.answer) and "三合" in res.answer, res.answer[:60])
check("prompt 里含强制引用要求", "〔来源：" in res.prompt or "必须标出处" in res.prompt)
check("prompt 里含资料片段", "寅午戌" in res.prompt)
check("自检通过 → grounded=True", res.grounded is True)
check("两次调用（生成+自检）", res.llm_calls == 2, str(res.llm_calls))
check("带免责尾", "以老师讲解为准" in res.answer, res.answer[-40:])
# 越界：自检报"无依据"，应把那句删掉
rec2: dict = {}
S.call_llm = make_llm(
    "按资料，三合需要三者俱全。〔来源：三合局笔记.txt「三合局要点」〕"
    "另外老师说这个卦主会在三天内发财。",
    "无依据\n另外老师说这个卦主会在三天内发财。", rec2)
res2 = A.answer_question("寅午戌三合局需要什么条件", GROUP, DATA, self_check=True)
check("**越界内容被删掉**", "三天内发财" not in res2.answer, res2.answer[:80])
check("自检标记为未通过", res2.grounded is False)
check("保留了有依据的部分", "三者俱全" in res2.answer)
check("越界内容被留证", "三天内发财" in res2.dropped, res2.dropped[:60])
# 全是越界 → 兜底
rec3: dict = {}
S.call_llm = make_llm("老师明天会讲新卦例。", "无依据\n老师明天会讲新卦例。", rec3)
res3 = A.answer_question("寅午戌三合局需要什么条件", GROUP, DATA, self_check=True)
check("**整体越界 → 走兜底话术**", "未涉及" in res3.answer, res3.answer[:60])

print("\n[H] 自检异常 / 空回答")
rec4: dict = {}


def boom(prompt, llm, timeout_s=120.0):
    if "事实核查" in prompt:
        raise RuntimeError("模拟自检失败")
    return "按资料，三合需三者俱全。〔来源：三合局笔记.txt「要点」〕"


S.call_llm = boom
res4 = A.answer_question("寅午戌三合局需要什么条件", GROUP, DATA, self_check=True)
check("自检失败**不假装通过**（grounded=None）", res4.grounded is None, str(res4.grounded))
check("自检失败仍保留回答（不白费）", "三者俱全" in res4.answer, res4.answer[:60])
S.call_llm = lambda *a, **k: ""
res5 = A.answer_question("寅午戌三合局需要什么条件", GROUP, DATA, self_check=True)
check("空回答 → 兜底话术", "未涉及" in res5.answer, res5.answer[:60])
S.call_llm = real_call

print("\n[I] 空资料库与无关问题")
empty_root = N.kb_root(DATA, "空群")
res6 = A.answer_question("随便问问", "空群", DATA, self_check=False)
check("空资料库 → 明确提示", "资料库为空" in res6.answer or "未涉及" in res6.answer,
      res6.answer[:50])
res7 = A.answer_question("今天中午吃什么", GROUP, DATA, self_check=False)
check("无关问题 → 不编造，走兜底", "未涉及" in res7.answer, res7.answer[:60])
check("无关问题不调 LLM（0 次）", res7.llm_calls == 0, str(res7.llm_calls))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("知识库蒸馏/台账/检索/生成/自检 验证通过")
