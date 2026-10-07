"""对照实验：语音转写错字，用哪种提示方式让 LLM 推理得最好？

三方对照（同一份发言记录，只换规则段）：
  A. 无规则：不提转写错字的事（基线）
  B. 固定纠错表：把同音对照表直接列出来（10-07 最初的写法）
  C. 上下文推断：教它"读得通就原样采信、读不通才按上下文改"（10-07 改后的写法）

关键靶子：记录里**故意保留**若干"长得像术语错例、但在原语境里完全正确"的词
（尘土/神经/缝合/暗洞/事要…），用来检验 B 是否会**过度纠偏**、C 是否更克制。

用法（会真实调用 LLM、消耗额度）：
    python tools/experiment_asr_fix.py --dry-run      # 只打印将提交的 prompt 长度
    python tools/experiment_asr_fix.py                # 真跑三方对照
    python tools/experiment_asr_fix.py --date 2026-10-07
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer import summarize as S  # noqa: E402
from paths import data_root  # noqa: E402

PROJECT_DIR = data_root()

# ---------------------------------------------------------------------------
# 三种规则段（只替换 prompt 里第 7/8 条的位置）
RULE_NONE = ""

RULE_TABLE = """7. **语音转写的同音字词要纠错**（重要）：标记为「语音转文本」的内容是自动识别的，
   常有同音错字。当某个词在上下文里读不通、但很像某个**术语/专名**时，请按上下文改成正确写法，
   并**按改正后的意思理解**。已知的真实例子（持续补充）：
   申金/神经、世爻/事要、辰土/尘土、六爻/六要、动爻/动要、旺相/王相、
   干支/干枝、纳甲/那甲、卦爻/挂要、应爻/映要、入墓/入木、逢合/缝合、
   旬空/寻空、太极/太急、方位/方为、罗盘/罗判。斜杠左边是正确术语。
   不确定时按最符合上下文的写法处理，并在该条要点后用「（转写存疑）」标注。
8. 不要因为上述纠错而编造记录里没有的信息；纠正范围仅限"读不通的同音词"。"""

RULE_INFER = """7. **语音转写的错字要按上下文推断，不要机械替换**（重要）：标记为「语音转文本」的内容是
   自动识别的，同音字/近音字错得很常见。处理原则：
   · 先按**上下文**判断某个词在这里是否读得通 —— 读得通就**原样采信**，绝不因为"它长得像
     某个术语"就改掉（很多词在别的语境里本来就是正确的，乱改会制造新错误）；
   · 只有当某个词在本句/本话题里**明显读不通**、而换成某个同音术语后整段逻辑才通顺时，
     才按上下文改成那个术语，并**按改后的意思理解**；
   · 判断依据优先级：本群/本话题的用词习惯 > 说话人前后几句的表述 > 常识；
   · 拿不准就保留原词，并在该条要点后标「（转写存疑）」。
   以下是**历史上确实出现过的同音错例**，仅作"可疑信号"参考，**不是替换表**：
   神经（可能是申金）、事要（可能是世爻）、尘土（可能是辰土）、六要（可能是六爻）、
   动要（可能是动爻）、干枝（可能是干支）、那甲（可能是纳甲）、挂要（可能是卦爻）、
   映要（可能是应爻）、入木（可能是入墓）、缝合（可能是逢合）、寻空（可能是旬空）、
   太急（可能是太极）、方为（可能是方位）、逢河（可能是逢合）、日成（可能是日辰）、
   暗洞（可能是暗动）。这些词在别的语境里可能完全正确 —— 只有读不通时才考虑。
8. 不要因为上述推断而编造记录里没有的信息；不推断没把握的词，改与不改都要能自圆其说。"""

VARIANTS = [("A 无规则", RULE_NONE), ("B 固定纠错表", RULE_TABLE), ("C 上下文推断", RULE_INFER)]

# 靶子：这些词在本实验的语境里**都是正确的**，看谁会被乱改
TRAPS = {
    "尘土": "正常词义（扬尘/尘土）",
    "神经": "正常词义（神经紧张/别神经）",
    "缝合": "正常词义（伤口缝合）",
    "暗洞": "正常词义（山洞/暗洞）",
    "事要": "正常词义（这事要紧）",
    "方为": "正常词义（方为妥当）",
    "入木": "正常词义（入木三分）",
    "日成": "正常词义（当日成交/日成）",
}


def build_variant_prompt(date_str: str, grouped, labels, rule: str) -> str:
    """用当前模板生成 prompt，但把"第 7/8 条"整段换成指定规则。"""
    base = S.build_prompt(date_str, grouped, labels)
    start = base.find("7. ")
    end = base.find("\n\n发言记录：")
    if start == -1 or end == -1 or end < start:
        return base
    # 无规则时，把 6 后面直接接"发言记录"
    if not rule:
        return base[:start].rstrip() + base[end:]
    return base[:start] + rule + base[end:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-07")
    ap.add_argument("--group", default="49366798260@chatroom", help="只跑这个群（默认易青岚乙巳年弟子群）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    db = PROJECT_DIR / "data" / "messages.db"
    labels_path = PROJECT_DIR / "data" / "labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.is_file() else {}

    # 取该群当天 +- 一段时间的**全部入库消息**（比归档范围更宽，给足上下文）
    day = datetime.strptime(args.date, "%Y-%m-%d")
    lo = int((day - timedelta(hours=6)).timestamp())
    hi = int((day + timedelta(hours=18)).timestamp())
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT group_name, sender, content, msg_type, received_at, transcript
           FROM messages WHERE group_name = ? AND received_at BETWEEN ? AND ?
           ORDER BY received_at""",
        (args.group, lo, hi),
    ).fetchall()
    conn.close()
    print(f"取到 {len(rows)} 条（{args.group}，{args.date} 前后）")

    # 用归档渲染函数把消息变成行（含 ★ 重点标记）
    from consumer.digest import _exclude_types, _in_digest, _is_star, _render_content
    from consumer.digest import _TYPE_LABEL
    excl, mon = _exclude_types(), None
    lines = []
    for r in rows:
        if int(r["msg_type"] or 0) in excl:
            continue
        ts = int(r["received_at"] or 0)
        hhmm = datetime.fromtimestamp(ts).strftime("%H:%M")
        gname = ((labels.get("groups") or {}).get(r["group_name"]) or {}).get("name") or r["group_name"]
        sname = ((labels.get("senders") or {}).get(r["sender"]) or {}).get("name") or r["sender"]
        star = "★" if _is_star(r, labels) else ""
        lines.append(f"[{hhmm}]{star}{sname}（{_TYPE_LABEL.get(int(r['msg_type'] or 0), '?')}）："
                     f"{_render_content(r).replace(chr(10), ' ').strip()}")
    grouped = {args.group: lines}
    print(f"提交给 LLM 的记录行数: {len(lines)}")
    print(f"其中含语音转写的行: {sum(1 for l in lines if '语音' in l)}")

    prompts = {}
    for name, rule in VARIANTS:
        p = build_variant_prompt(args.date, grouped, labels, rule)
        prompts[name] = p
        print(f"  [{name}] prompt {len(p)} 字")
    if args.dry_run:
        print("\n（--dry-run：未调用 LLM）")
        out = Path(args.out) if args.out else (PROJECT_DIR / "reports" / f"asr-exp-{args.date}-prompts.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"prompt 已写出: {out}")
        return 0

    llm = S._llm_cfg()
    if not (llm.get("base_url") and llm.get("model")):
        print("未配置 LLM（config.yaml 的 llm.base_url / model），无法实验")
        return 2
    print(f"\n开始调用 LLM：{llm.get('model')} @ {llm.get('base_url')}")
    results = {}
    for name, _ in VARIANTS:
        print(f"  → {name} …", end="", flush=True)
        try:
            txt = S.strip_think(S.call_llm(prompts[name], llm))
            results[name] = txt
            print(f" OK（{len(txt)} 字）")
        except Exception as e:  # noqa: BLE001
            results[name] = f"[调用失败] {type(e).__name__}: {e}"
            print(f" 失败: {type(e).__name__}")

    out = Path(args.out) if args.out else (PROJECT_DIR / "reports" / f"asr-exp-{args.date}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"date": args.date, "group": args.group,
                               "n_lines": len(lines), "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写出: {out}")

    print("\n" + "=" * 70)
    print("靶子检查：这些词在原语境里**都是正确的**，被改掉就是过度纠偏")
    print("=" * 70)
    for name, _ in VARIANTS:
        body = results.get(name, "")
        hit = [w for w in TRAPS if w in body]
        print(f"  [{name}] 保留的正常词: {hit if hit else '（一个都没保留）'}")
    print()
    for name, _ in VARIANTS:
        print("=" * 70)
        print(f"### {name}")
        print("=" * 70)
        print(results.get(name, ""))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
