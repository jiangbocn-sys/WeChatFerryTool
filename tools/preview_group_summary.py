"""零风险预演：生成本群今天的总结，并展示"发进群时会被整理成什么样"。

**不发送任何消息**（只调 LLM 生成 + 本地渲染）。
用法：
    python tools/preview_group_summary.py --group 49366798260@chatroom
    python tools/preview_group_summary.py --date 2026-10-12 --no-llm   # 只看渲染样式
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer.group_sender import make_messages  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="49366798260@chatroom")
    ap.add_argument("--date", default="")
    ap.add_argument("--no-llm", action="store_true",
                    help="不调 LLM，用占位正文，只看渲染/切分样式")
    ap.add_argument("--chunk", type=int, default=1800)
    ap.add_argument("--no-refine", action="store_true",
                    help="不做发群前的二次提炼（直接看归档稿会被发成什么样）")
    args = ap.parse_args()

    from datetime import datetime
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    if args.no_llm:
        body = ("- **今日要点一**：老师讲解了某卦例的取用神思路，重点在申金与辰土的刑冲关系。\n"
                "- 要点二：学员提问「面试能否通过」，老师答：世爻旺相、应爻生世，可成。\n"
                "- 要点三：作业与后续安排 —— 明晚继续讲纳甲。\n")
        name = "（示例群）"
    else:
        from consumer import summarize as S
        labels = S._load_labels(S.LABELS_PATH)
        name = S.name_for_gid(args.group, labels)
        print(f"正在生成「{name}」{date_str} 的总结（会调一次 LLM）…")
        res = S.summarize(date_str)
        if res is None:
            print("  当天没有进入归档的消息（该群今天没有内容？）")
            return 1
        hit = None
        for r in res.results:
            if args.group in (r.grouped or {}):
                hit = r
                break
        if hit is None:
            print(f"  今天没有该群的总结。产出的有："
                  f"{[list((r.grouped or {}).keys()) for r in res.results]}")
            return 1
        body = hit.content
        print(f"  已生成（{hit.total} 条消息）→ {hit.path.name}")
        print()
        print("=" * 68)
        print("【归档稿】（给你自己看的详细版，存档在 reports/）")
        print("=" * 68)
        print(body)

    # 发群前的二次提炼（与线上 `Consumer._send_summaries_to_groups` 同一逻辑）
    if not args.no_llm and not args.no_refine:
        from consumer.group_summary import refine_for_group
        from consumer.summarize import _llm_cfg
        print()
        print("正在提炼成群内成品稿（再调一次 LLM）…")
        refined = refine_for_group(body, _llm_cfg(), date_str=date_str, group_name=name)
        if refined:
            print(f"  ✅ 提炼成功（{len(body)} 字 → {len(refined)} 字）")
            body = refined
        else:
            print("  ⚠️ 提炼失败，线上会回退用归档原稿")

    msgs = make_messages(date_str, name, body, limit=args.chunk)
    print()
    print("=" * 68)
    print(f"下面是把这份总结发进群时会依次发出的 {len(msgs)} 条消息：")
    print("=" * 68)
    for i, m in enumerate(msgs, 1):
        print(f"\n----- 第 {i}/{len(msgs)} 条（{len(m)} 字）-----")
        print(m)
    print()
    print("=" * 68)
    print("⚠️ 以上只是本地预演，**没有发送任何消息**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
