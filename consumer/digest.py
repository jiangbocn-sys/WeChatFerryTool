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

PROJECT_DIR = Path(__file__).resolve().parent.parent

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

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT group_name, sender, content, msg_type, received_at, transcript, direction, score
        FROM messages
        WHERE received_at >= ? AND received_at < ? AND priority >= 1
        ORDER BY received_at ASC
        """,
        (start_ts, end_ts),
    ).fetchall()
    conn.close()

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


def _render_content(m: sqlite3.Row) -> str:
    msg_type = m["msg_type"]
    content = (m["content"] or "").strip()
    if msg_type == 1:
        return content.replace("\n", " ")
    if msg_type == 34:  # 语音
        trans = (m["transcript"] or "").strip()
        if trans and not trans.startswith("[待转写]") and not trans.startswith("[解码失败]"):
            return f"[语音] {trans}"
        # 解析时长
        import re
        mm = re.search(r'voicelength="(\d+)"', content)
        secs = round(int(mm.group(1)) / 1000) if mm else 0
        return f"[语音 ~{secs}s]（未转写）"
    # 其他类型
    label = _TYPE_LABEL.get(msg_type, f"type={msg_type}")
    # 49 类尝试提取 title
    if msg_type == 49:
        import re
        tm = re.search(r"<title>([^<]{0,60})</title>", content)
        if tm:
            return f"[{label}] {tm.group(1)}"
    return f"[{label}]"


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