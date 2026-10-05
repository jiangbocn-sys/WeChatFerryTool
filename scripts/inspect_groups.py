"""分析 SQLite 数据库，反推需要监控的群/联系人。

用法：
  python scripts/inspect_groups.py            # 显示统计 + 生成 YAML 骨架（不写 config.yaml）
  python scripts/inspect_groups.py --write   # 把生成的 groups/senders 段写入 config.yaml

数据来源：data/messages.db 里已有的消息记录。
对于没有任何消息记录的群（本工具捕获不到），无法列出——这是 DLL 没暴露群枚举接口的代价。
"""
import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

# 强制 stdout 用 utf-8，避免 Windows 控制台 gbk 编码报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "data" / "messages.db"
CONFIG_PATH = PROJECT_DIR / "config.yaml"


def is_private(group_name: str) -> bool:
    return "@chatroom" not in group_name


def analyze(db_path: Path) -> tuple[dict, dict]:
    """返回 (groups_stats, senders_stats)。

    groups_stats: {group_name: {msgs, senders: {sender: count}, last_seen}}
    senders_stats: {sender: {msgs, groups: {group_name: count}}}
    """
    if not db_path.exists():
        sys.exit(f"数据库不存在: {db_path}（先跑 consumer 让它写入）")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT group_name, sender, content, score FROM messages ORDER BY id DESC"
    ).fetchall()
    conn.close()

    groups: dict[str, dict] = defaultdict(lambda: {"msgs": 0, "senders": Counter(), "last": ""})
    senders: dict[str, dict] = defaultdict(lambda: {"msgs": 0, "groups": Counter()})

    for r in rows:
        g = r["group_name"]
        s = r["sender"]
        groups[g]["msgs"] += 1
        groups[g]["senders"][s] += 1
        # content 用作 last seen 标记
        groups[g]["last"] = (r["content"] or "")[:30]
        senders[s]["msgs"] += 1
        senders[s]["groups"][g] += 1

    return dict(groups), dict(senders)


def print_report(groups: dict, senders: dict) -> None:
    print("=" * 70)
    print("群/会话流量统计（按消息数降序）")
    print("=" * 70)
    for g, info in sorted(groups.items(), key=lambda kv: -kv[1]["msgs"]):
        kind = "私聊" if is_private(g) else "群"
        top = ", ".join(f"{s}({c})" for s, c in info["senders"].most_common(3))
        print(f"\n[{kind}] {g}  ({info['msgs']} 条)")
        print(f"  主要发送人: {top}")
        print(f"  最近: {info['last']}")

    print()
    print("=" * 70)
    print("发送人统计（按消息数降序，前 20）")
    print("=" * 70)
    for s, info in list(sorted(senders.items(), key=lambda kv: -kv[1]["msgs"]))[:20]:
        groups_str = ", ".join(f"{g}({c})" for g, c in info["groups"].most_common(3))
        print(f"  {s}: {info['msgs']} 条  [{groups_str}]")


def build_filter_yaml(groups: dict, senders: dict, top_n_groups: int = 5, top_n_senders: int = 10) -> str:
    """生成 filter 段骨架 YAML。"""
    sorted_groups = sorted(groups.items(), key=lambda kv: -kv[1]["msgs"])
    sorted_senders = sorted(senders.items(), key=lambda kv: -kv[1]["msgs"])

    # 选 top-N 群（私聊也归入 groups 段，因为私聊 wxid 在 filter 里就是 group_name）
    selected_groups = [g for g, _ in sorted_groups[:top_n_groups]]
    selected_senders = [s for s, _ in sorted_senders[:top_n_senders]]

    lines = ["filter:"]
    lines.append("  # 自动生成骨架，请按需调整（删除不想监控的项）")
    lines.append(f"  groups:  # {len(selected_groups)} 个 top 候选")
    for g in selected_groups:
        kind = "私聊" if is_private(g) else "群"
        cnt = groups[g]["msgs"]
        lines.append(f'    - "{g}"  # {kind}，{cnt} 条')
    lines.append("  senders:  # {} 个 top 候选".format(len(selected_senders)))
    for s in selected_senders:
        cnt = senders[s]["msgs"]
        lines.append(f'    - "{s}"  # {cnt} 条')
    lines.append("  keywords:")
    lines.append('    - "REPLACE_KEYWORD"')
    lines.append("  case_insensitive: true")
    return "\n".join(lines)


def write_to_config(yaml_block: str) -> None:
    """简单替换 config.yaml 里的 filter 段（找 `filter:` 行到下一个顶层 key 之间）。"""
    if not CONFIG_PATH.exists():
        sys.exit(f"config.yaml 不存在: {CONFIG_PATH}")
    text = CONFIG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # 找 filter: 行
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("filter:"):
            start = i
            break
    if start is None:
        sys.exit("config.yaml 里没找到 filter: 段")

    # 找下一个顶层 key（不以空白开头、不以 - 开头）
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln and not ln[0].isspace() and not ln.lstrip().startswith("-"):
            end = i
            break

    new_lines = lines[:start] + [yaml_block + "\n", "\n"] + lines[end:]
    # 清理多余空行
    cleaned: list[str] = []
    prev_blank = False
    for ln in new_lines:
        blank = (ln.strip() == "")
        if blank and prev_blank:
            continue
        cleaned.append(ln)
        prev_blank = blank

    CONFIG_PATH.write_text("".join(cleaned), encoding="utf-8")
    print(f"\n已写入 {CONFIG_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="把生成的 filter 段写入 config.yaml")
    parser.add_argument("--top-groups", type=int, default=5)
    parser.add_argument("--top-senders", type=int, default=10)
    args = parser.parse_args()

    groups, senders = analyze(DB_PATH)
    print_report(groups, senders)

    yaml_block = build_filter_yaml(groups, senders, args.top_groups, args.top_senders)
    print("\n" + "=" * 70)
    print("生成的 filter 段骨架（可手动调整或用 --write 写入）")
    print("=" * 70)
    print(yaml_block)

    if args.write:
        write_to_config(yaml_block)


if __name__ == "__main__":
    main()
