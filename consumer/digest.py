"""每日重点发言归档。

生成 Markdown 报告：当天所有 priority>=1 的消息（= 重点群的重点成员 / 重要联系人 / 重要群），
按 群 → 发送人 分组，按时间排序；语音消息优先显示转写文本（transcript）。

用法（命令行）：
  python -m consumer.digest                  # 生成今天的
  python -m consumer.digest --date 2026-10-02
  python -m consumer.digest --date 2026-10-02 --output reports/
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from paths import data_root

PROJECT_DIR = data_root()

log = logging.getLogger("consumer.digest")

_TYPE_LABEL = {
    3: "图片",
    34: "语音",
    43: "视频",
    47: "表情",
    49: "链接/文件",
    50: "通话",
    51: "系统",
    10000: "系统",
    10002: "撤回",
}


def _load_labels(labels_path: Path) -> dict:
    try:
        return json.loads(labels_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"groups": {}, "senders": {}}


def _display(labels: dict, kind: str, key: str) -> str:
    entry = (labels.get(kind) or {}).get(key) or {}
    name = (entry.get("name") or "").strip()
    return name or key


def generate_digest(
    date_str: str,
    db_path: Path | None = None,
    labels_path: Path | None = None,
    out_dir: Path | None = None,
) -> Path:
    """生成指定日期的归档 Markdown，返回文件路径。"""
    db_path = db_path or (PROJECT_DIR / "data" / "messages.db")
    labels_path = labels_path or (PROJECT_DIR / "data" / "labels.json")
    out_dir = out_dir or (PROJECT_DIR / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)

    day = datetime.strptime(date_str, "%Y-%m-%d")
    start_ts = int(day.timestamp())
    end_ts = int((day + timedelta(days=1)).timestamp())

    labels = _load_labels(labels_path)

    exclude_types = _exclude_types()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    raw_rows = conn.execute(
        """
        SELECT group_name, sender, content, msg_type, received_at, transcript, direction, score, priority
        FROM messages
        WHERE received_at >= ? AND received_at < ?
        ORDER BY received_at ASC
        """,
        (start_ts, end_ts),
    ).fetchall()
    conn.close()

    # 选取规则见 _in_digest：监控名单里的群（未设重点人）= 整群归档；
    # 设了重点关注人 = 只归档这些人。与"入库闸门"解耦，改完可直接重跑当天归档。
    monitored = _monitored_groups()
    try:
        rows = [r for r in raw_rows if _in_digest(r, labels, exclude_types, monitored)]
    except Exception:  # noqa: BLE001
        log.exception("归档选取异常，退回 priority>=1 旧规则")
        rows = [r for r in raw_rows if int(r["priority"] or 0) >= 1]

    # 按 群 → 发送人 分组
    grouped: dict[str, dict[str, list[sqlite3.Row]]] = {}
    for r in rows:
        grouped.setdefault(r["group_name"], {}).setdefault(r["sender"], []).append(r)

    lines: list[str] = []
    lines.append(f"# 每日重点发言归档 · {date_str}")
    lines.append("")
    total = sum(len(v) for g in grouped.values() for v in g.values())
    lines.append(f"共 {total} 条重点消息，涉及 {len(grouped)} 个会话、"
                 f"{sum(len(v) for v in grouped.values())} 位成员。")
    lines.append("")

    if not rows:
        lines.append("_（当天没有重点消息）_")
    for gkey, senders in grouped.items():
        gname = _display(labels, "groups", gkey)
        lines.append(f"## {gname}")
        lines.append(f"<sub>会话 ID: {gkey}</sub>")
        lines.append("")
        for skey, msgs in senders.items():
            sname = _display(labels, "senders", skey)
            lines.append(f"### {sname}")
            lines.append("")
            for m in msgs:
                t = datetime.fromtimestamp(m["received_at"]).strftime("%H:%M")
                content = _render_content(m)
                lines.append(f"- **{t}** {content}")
            lines.append("")

    path = out_dir / f"digest-{date_str}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("归档已生成: %s（%d 条重点消息）", path, total)
    return path


def _is_star(row, labels: dict) -> bool:
    """这条发言是不是"重点关注"（重点群的成员 / ★重点群 / 全局重点联系人）。

    10-07 起重点人**不再被过滤**，而是渲染时行首加 `★`，让 LLM 自己加权。
    """
    gname = row["group_name"] or ""
    sender = row["sender"] or ""
    g = (labels.get("groups") or {}).get(gname) or {}
    s = (labels.get("senders") or {}).get(sender) or {}
    if s.get("important"):
        return True
    if g.get("important"):
        return True
    focus = [m for m in (g.get("focus_members") or []) if m]
    return bool(focus) and sender in focus


def _render_content(m: sqlite3.Row) -> str:
    """归档里每条消息的单行摘要。

    2026-10-07 起统一走 `consumer/cards.py`：图片/视频/链接/引用/文件不再只显示
    `[图片]` `[链接]`，而是带上尺寸、大小、标题、被引用的人与原话等 —— web 浏览页
    与归档用的是**同一个解析器**，不会两处不一致。
    """
    from consumer import cards as cards_mod   # 局部 import：避免 consumer 内部循环依赖

    try:
        return cards_mod.summary_line(
            m["msg_type"], m["content"] or "",
            transcript=(m["transcript"] or ""),
        )
    except Exception:  # noqa: BLE001
        # 解析器出问题也绝不能影响归档生成
        label = _TYPE_LABEL.get(m["msg_type"], f"type={m['msg_type']}")
        return f"[{label}]"


def _exclude_types() -> set[int]:
    """归档排除的消息类型（噪音闸门）。

    默认 = **表情(47) + 系统(51, 10000) + 撤回(10002)** —— 见 `config.yaml` 的
    `digest.exclude_types`。注意 10-07 起**入库闸门是入库闸门、这里是归档闸门**：
    入库侧可能挡掉更多类型（视频/图片/名片…），若某个类型没入库，这里也自然没有它。
    """
    try:
        import yaml
        import paths
        cfg = yaml.safe_load(paths.config_path().read_text(encoding="utf-8")) or {}
        v = (cfg.get("digest") or {}).get("exclude_types")
        if v is not None:
            return {int(x) for x in v}
    except Exception:  # noqa: BLE001
        pass
    return {3, 47, 51, 10000}   # 图片(无法交给模型) / 表情 / 系统 / 系统


def _kw_haystack(row) -> str:
    """关键词匹配用的文本（每群的"敏感关键词"命中判定用）。

    **不能只搜 content**：语音正文只在 transcript 列；图片/视频的 content 是 XML，
    有意义的文字在属性/子标签里；**引用回复**的关键信息在被引用的原话里
    （`<refermsg><content>`）—— 2026-10-07 起统一用 cards.summary_line() 兜底，
    它会把 title/des/引用内容/文件名/位置名都拼出来。
    """
    import re
    xml = row["content"] or ""
    parts = [xml, row["transcript"] or ""]
    if "<" in xml:
        for pat in (r"<title>(.*?)</title>", r'\btitle="([^"]{1,80})"',
                    r'\bdes="([^"]{1,120})"', r"<content>(.*?)</content>",
                    r'\bpoiname="([^"]{1,80})"'):
            parts.extend(m.group(1) for m in re.finditer(pat, xml, re.S))
    try:
        from consumer import cards as cards_mod
        parts.append(cards_mod.summary_line(row["msg_type"], xml,
                                            transcript=(row["transcript"] or "")))
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(parts).lower()


def _summary_hint(gid: str, labels: dict | None = None) -> str:
    """取某个群的"总结提示"（`labels.groups.<gid>.summary_hint`，用户在标定页填）。

    用途：当日总结时把这段提示**一起提交给 LLM**，让它知道该怎么总结这个群、
    哪些内容算噪音要剔除（例如"只关心报价与交期，忽略寒暄和表情包"）。
    空/缺失返回空串；本函数只读内存里的 labels，不碰网络。
    """
    if labels is None:
        labels = _load_labels()
    ent = (labels.get("groups") or {}).get(gid) or {}
    return str(ent.get("summary_hint") or "").strip()


def _monitored_groups() -> set[str] | None:
    """config.yaml 的 `filter.groups`（= 总设置页里勾选的"监控的群"）。

    返回 None 表示**没有配置监控名单**（= 不限制，按旧的 labels 规则走）。
    这条很重要：设置了监控群以后，归档范围就与监控名单一致了 —— 否则会出现
    "加进监控却在归档里看不到"的困惑。
    """
    try:
        import paths
        import yaml
        cfg = yaml.safe_load(paths.config_path().read_text(encoding="utf-8")) or {}
        v = (cfg.get("filter") or {}).get("groups")
        if v:
            return {str(x).strip() for x in v if str(x).strip()}
    except Exception:  # noqa: BLE001
        pass
    return None


def _in_digest(row, labels: dict, exclude_types: set[int],
               monitored: set[str] | None = None) -> bool:
    """归档选取规则。

    **用户决定（2026-10-07，第二版）**：归档/总结要的是**上下文**，不是"只有重点人说的话"。
    只给重点人的发言会让 LLM 看不到"谁在问、别人怎么接"，因果链和代词全丢。
    所以现在：

      * 群在**监控名单**（`filter.groups`）里 → **整群对话都进归档**（不再按重点人过滤）；
      * **重点人/★重点群不再过滤，而是"标记"** —— 渲染时行首加 `★`（见 `_render_content`），
        并在 prompt 里告诉模型"★ 是重点关注的人"，由它自己在总结时加权；
      * 群**不在**监控名单，但设了敏感关键词 → 只有命中关键词的行进归档（保留这条补充来源）；
      * 私聊 → 只有"全局重点联系人"才进（私聊没有群上下文可言，维持原样）；
      * 类型闸门（`exclude_types`）依旧先生效（表情/系统等噪音先挡掉）。

    `monitored=None`（没配监控名单）时退回旧规则：靠 labels 里的 important/focus/keywords 判定。
    """
    if int(row["msg_type"] or 0) in exclude_types:
        return False

    gname = row["group_name"] or ""
    sender = row["sender"] or ""
    g = (labels.get("groups") or {}).get(gname) or {}
    s = (labels.get("senders") or {}).get(sender) or {}

    focus = [m for m in (g.get("focus_members") or []) if m]
    kws = [str(k).strip() for k in (g.get("keywords") or []) if str(k).strip()]
    monitored_hit = (monitored is None) or (gname in monitored)
    is_group = gname.endswith("@chatroom")

    # 私聊：只有"全局重点联系人"才进归档
    if not is_group:
        return bool(s.get("important"))

    # ① 监控名单里的群 → 整群对话（上下文优先，重点人靠 ★ 标记而不是过滤）
    if monitored_hit and monitored is not None:
        return True

    # ② 没配监控名单时的旧规则：标记重点 / 配了重点成员 / 配了敏感关键词
    if not (g.get("important") or focus or kws):
        return False
    if focus:
        # 没有监控名单时的兜底：只归档重点成员 + 全局重点联系人（原行为）
        return sender in focus or bool(s.get("important"))
    if g.get("important"):
        return True
    if kws and any(k.lower() in _kw_haystack(row) for k in kws):
        return True
    return bool(s.get("important"))



def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--output", default=None, help="输出目录（默认项目内 reports/）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = Path(args.output) if args.output else None
    path = generate_digest(args.date, out_dir=out)
    print(f"已生成: {path}")


if __name__ == "__main__":
    main()