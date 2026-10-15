"""知识库 CLI：抽取 / 蒸馏 / 问答预演 / 状态查看。

四个子命令（都可安全预演）：

  extract   只抽文字，报告每个文件抽了多少字、哪些失败/跳过（**不调 LLM**）
  distill   增量蒸馏成笔记。**默认预演**，加 --yes 才真调 LLM 并写文件
  ask       用知识库回答一个问题（打印回答 + 命中来源 + 自检结果，**不发送**）
  status    看台账：已蒸馏哪些文件、字数、上次蒸馏时间

用法：
    python tools/kb.py extract  --group 弟子群 --dir "D:\\JiangBo\\Documents\\wechatdocs\\弟子群"
    python tools/kb.py distill  --group 弟子群 --dir "..."          # 预演
    python tools/kb.py distill  --group 弟子群 --dir "..." --yes    # 真跑
    python tools/kb.py ask      --group 弟子群 --q "@小易 寅午戌能三合吗"
    python tools/kb.py status   --group 弟子群
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))


def _out(s: str = "") -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    print(s)


def _data_root() -> Path:
    import paths
    return paths.data_root()


def cmd_extract(a) -> int:
    from consumer import kb_extract as EX
    rep = EX.extract_dir(Path(a.dir), tuple(a.ext.split(",")))
    _out(f"\n资料目录: {a.dir}")
    _out(f"  成功 {len(rep.ok)} 个，合计 {rep.total_chars:,} 字"
         f"（失败 {len(rep.failed)}、跳过 {len(rep.skipped)}）")
    for e in rep.ok:
        w = f"  ⚠️ {e.warning}" if e.warning else ""
        _out(f"    ✅ [{e.method:<9}] {e.chars:>8,} 字  {e.path.name}{w}")
    for p, w in rep.warnings:
        _out(f"    ⚠️  {p.name}: {w}")
    for p, r in rep.skipped:
        _out(f"    ⏭️  跳过 {p.name}: {r}")
    for p, r in rep.failed:
        _out(f"    ❌ 失败 {p.name}: {r}")
    return 0 if not rep.failed else 1


def cmd_distill(a) -> int:
    from consumer.kb_notes import distill_group, summarize_report
    llm = {}
    if a.yes:
        from consumer.summarize import _llm_cfg, llm_ready
        if not llm_ready():
            _out("❌ 未配置 LLM（设置页填 base_url 与模型名后再来）")
            return 2
        llm = _llm_cfg()
    rep = distill_group(Path(a.dir), a.group, _data_root(), llm,
                        allow_ext=tuple(a.ext.split(",")),
                        dry_run=not a.yes,
                        progress=(_out if a.verbose else None))
    _out()
    _out(summarize_report(rep))
    if not a.yes and rep.plan_calls:
        _out(f"\n⚠️ 以上是**预演**：确认无误后加 --yes 真正蒸馏（将调用 LLM {rep.plan_calls} 次）")
    return 0 if not rep.failed else 1


def cmd_ask(a) -> int:
    from consumer.kb_answer import answer_question
    res = answer_question(a.q, a.group, _data_root(), top_k=a.top_k, verbose=True)
    _out()
    _out("=" * 70)
    _out(f"问题：{res.question}")
    _out(f"命中片段：{len(res.hits)} 段")
    for h in res.hits:
        _out(f"  - 〔{h.note_file}〕{h.topic}（相关度 {h.score:.2f}）")
    _out(f"LLM 调用：{res.llm_calls} 次")
    if res.grounded is not None:
        _out(f"自检：{'通过（无越界内容）' if res.grounded else '**发现越界内容，已裁剪/兜底**'}")
    if res.dropped:
        _out(f"  自检删掉的内容：{res.dropped[:200]}")
    _out("-" * 70)
    _out("回答：")
    _out(res.answer or "（无）")
    return 0


def cmd_status(a) -> int:
    from consumer.kb_notes import kb_root, load_sources
    root = kb_root(_data_root(), a.group)
    entries = load_sources(root)
    _out(f"\n群「{a.group}」知识库: {root}")
    if not entries:
        _out("  （台账为空：还没蒸馏过）")
        return 0
    done = [e for e in entries.values() if e.note_file]
    _out(f"  资料 {len(entries)} 份（已蒸馏 {len(done)}）")
    for rel, e in sorted(entries.items()):
        flag = "✅" if e.note_file else "⏳"
        warn = f"  ⚠️ {e.warning}" if e.warning else ""
        _out(f"    {flag} {rel}: {e.chars:,} 字 → {e.note_file or '（未蒸馏）'}"
             f"  上次 {e.distilled_at or '-'}{warn}")
    idx = root / "_index.md"
    if idx.is_file():
        _out(f"  索引: {idx}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="群知识库工具（抽取 / 蒸馏 / 问答预演 / 状态）")
    ap.add_argument("--verbose", action="store_true", help="打印每一步细节")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract", help="只抽文字（不调 LLM）")
    p.add_argument("--dir", required=True)
    p.add_argument("--ext", default=",".join(
        (".txt", ".md", ".csv", ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx")))
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("distill", help="增量蒸馏（默认预演）")
    p.add_argument("--group", required=True, help="群名（笔记按群存放）")
    p.add_argument("--dir", required=True, help="该群的资料目录")
    p.add_argument("--yes", action="store_true", help="真正调用 LLM 并写文件")
    p.add_argument("--ext", default=",".join(
        (".txt", ".md", ".csv", ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx")))
    p.set_defaults(func=cmd_distill)

    p = sub.add_parser("ask", help="用知识库回答问题（不发送到微信）")
    p.add_argument("--group", required=True)
    p.add_argument("--q", required=True, help="问题（可带 @小易 前缀，会被剥掉）")
    p.add_argument("--top-k", type=int, default=5, help="检索多少段")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("status", help="看台账")
    p.add_argument("--group", required=True)
    p.set_defaults(func=cmd_status)

    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return int(a.func(a) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
