"""消息浏览器：分页浏览数据库消息，自动套用标定显示真实名字。

用法：
  python scripts/browse.py                  # 默认最近 30 条
  python scripts/browse.py --limit 100      # 最近 100 条
  python scripts/browse.py --important      # 只看"重点关注"群/联系人
  python scripts/browse.py --group 493...   # 按群过滤
  python scripts/browse.py --sender wxid_x  # 按发送人过滤
  python scripts/browse.py --keyword 报价   # 按内容关键词过滤
  python scripts/browse.py --interactive    # 交互翻页模式

标定数据来源：data/labels.json
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "data" / "messages.db"
LABELS_PATH = PROJECT_DIR / "data" / "labels.json"


def force_utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def load_labels() -> dict:
    if not LABELS_PATH.exists():
        return {"groups": {}, "senders": {}}
    try:
        return json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"groups": {}, "senders": {}}


def lookup_name(labels: dict, kind: str, key: str) -> tuple[str, bool]:
    """返回 (display_name, is_important)。key 不存在时返回 ("", False)。"""
    entry = labels.get(kind, {}).get(key)
    if not entry:
        return ("", False)
    return (entry.get("name", "") or "", bool(entry.get("important")))


def fetch_messages(args, labels) -> list[dict]:
    if not DB_PATH.exists():
        sys.exit(f"数据库不存在: {DB_PATH}")

    sql = "SELECT id, msg_id, group_name, sender, content, msg_type, received_at, score, score_reason, pushed FROM messages WHERE 1=1"
    params: list = []
    if args.important:
        # 重要群/重要联系人 — 用 JSON 字段不实际存在于 schema 中，所以客户端过滤
        pass
    if args.group:
        sql += " AND group_name = ?"
        params.append(args.group)
    if args.sender:
        sql += " AND sender = ?"
        params.append(args.sender)
    if args.keyword:
        sql += " AND content LIKE ?"
        params.append(f"%{args.keyword}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit or 30)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    out = []
    for r in rows:
        rec = dict(r)
        if args.important:
            g_name, g_imp = lookup_name(labels, "groups", rec["group_name"])
            s_name, s_imp = lookup_name(labels, "senders", rec["sender"])
            if not (g_imp or s_imp):
                continue
        out.append(rec)
    return out


def fmt_ts(ts: int | None) -> str:
    if not ts:
        return "未知时间"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_score(rec: dict) -> str:
    s = rec.get("score")
    reason = rec.get("score_reason") or ""
    if s is None:
        return "未评分"
    if "score_failed" in reason:
        return f"评分失败 ({reason[:30]})"
    if "score_skipped" in reason:
        return f"跳过评分 ({reason[:30]})"
    return f"score={s} ({reason[:30]})"


def render_message(rec: dict, labels: dict) -> str:
    g_name, g_imp = lookup_name(labels, "groups", rec["group_name"])
    s_name, s_imp = lookup_name(labels, "senders", rec["sender"])

    g_display = g_name or rec["group_name"]
    s_display = s_name or rec["sender"]

    g_flag = "★" if g_imp else " "
    s_flag = "★" if s_imp else " "

    content = rec["content"] or ""
    if len(content) > 200:
        content = content[:200] + "..."

    pushed = "📤已推" if rec["pushed"] else " "

    lines = [
        f"[{fmt_ts(rec['received_at'])}] {pushed} {g_flag}群:{g_display}  {s_flag}人:{s_display}",
        f"  {fmt_score(rec)}",
        f"  {content}",
    ]
    return "\n".join(lines)


def print_batch(records: list[dict], labels: dict) -> None:
    if not records:
        print("(没有匹配的消息)")
        return
    print(f"--- {len(records)} 条消息 ---\n")
    for r in records:
        print(render_message(r, labels))
        print()


def interactive_mode(initial_records: list[dict], labels: dict, args, total_count: int) -> None:
    """简化版交互翻页：每页 N 条，输入 n=下一页, q=退出。"""
    page_size = args.limit if args.limit else 30
    records = initial_records
    pos = 0
    while pos < len(records):
        batch = records[pos:pos + page_size]
        print_batch(batch, labels)
        pos += page_size
        if pos >= len(records):
            break
        cmd = input(f"[{pos}/{len(records)}] n=下一页 / q=退出: ").strip().lower()
        if cmd == "q":
            break


def main() -> None:
    force_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30, help="最多显示条数（默认 30）")
    parser.add_argument("--important", action="store_true", help="只看标定为重点关注的群/联系人")
    parser.add_argument("--group", help="按群 roomid 过滤")
    parser.add_argument("--sender", help="按发送人 wxid 过滤")
    parser.add_argument("--keyword", help="按消息内容关键词过滤")
    parser.add_argument("--interactive", "-i", action="store_true", help="翻页模式")
    args = parser.parse_args()

    labels = load_labels()
    records = fetch_messages(args, labels)

    if args.interactive:
        interactive_mode(records, labels, args, len(records))
    else:
        print_batch(records, labels)


if __name__ == "__main__":
    main()
