"""交互式标定工具：给 wxid/roomid 标定显示名 + 标记"重点关注"。

用法：
  python scripts/annotate.py            # 进入交互模式
  python scripts/annotate.py --list     # 只列出当前所有标定（不交互）
  python scripts/annotate.py --reset    # 清空所有标定

存储：data/labels.json
格式：
  {
    "groups": {
      "<roomid>": {"name": "<显示名>", "important": true|false}
    },
    "senders": {
      "<wxid>": {"name": "<显示名>", "important": true|false}
    }
  }
"""
import argparse
import json
import sqlite3
import sys
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


def save_labels(labels: dict) -> None:
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LABELS_PATH.write_text(
        json.dumps(labels, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def discover_ids_from_db() -> tuple[set, set]:
    """从 SQLite 数据库返回 (所有 roomid, 所有 wxid)。"""
    if not DB_PATH.exists():
        return set(), set()
    conn = sqlite3.connect(DB_PATH)
    try:
        groups = {r[0] for r in conn.execute("SELECT DISTINCT group_name FROM messages").fetchall()}
        senders = {r[0] for r in conn.execute("SELECT DISTINCT sender FROM messages").fetchall()}
    finally:
        conn.close()
    return groups, senders


def is_private(group_name: str) -> bool:
    return "@chatroom" not in group_name


def display_label(entry: dict | None) -> str:
    if not entry:
        return ""
    name = entry.get("name", "")
    important = "★" if entry.get("important") else " "
    return f"[{important}{name}]" if name else f"[{important}?]"


def list_labels(labels: dict) -> None:
    print("=" * 60)
    print("群标定 (groups)")
    print("=" * 60)
    for k, v in sorted(labels["groups"].items()):
        print(f"  {k}  {display_label(v)}")
    if not labels["groups"]:
        print("  (空)")
    print()
    print("=" * 60)
    print("联系人标定 (senders)")
    print("=" * 60)
    for k, v in sorted(labels["senders"].items()):
        print(f"  {k}  {display_label(v)}")
    if not labels["senders"]:
        print("  (空)")


def prompt_one(item_kind: str, key: str, current: dict | None) -> dict | None:
    """让用户为单个 key 设置标定。返回 None 表示删除。"""
    print()
    print(f"--- {item_kind}: {key} ({'私聊' if is_private(key) else '群'}) ---")
    cur_name = (current or {}).get("name", "")
    cur_important = (current or {}).get("important", False)
    print(f"当前: name='{cur_name}' important={cur_important}")

    raw = input("显示名（直接回车=不变，输入 '-' = 删除标定）: ").strip()
    if raw == "-":
        return None
    name = raw if raw else cur_name

    raw_imp = input(f"是否重点关注？(y/n，回车='{'y' if cur_important else 'n'}') ").strip().lower()
    if raw_imp == "":
        important = cur_important
    else:
        important = raw_imp in ("y", "yes", "1", "true")

    return {"name": name, "important": important}


def interactive() -> None:
    labels = load_labels()
    groups, senders = discover_ids_from_db()

    print(f"数据库里有 {len(groups)} 个 group_name，{len(senders)} 个 sender。")
    print("操作：依次为每个 ID 设置显示名 + 是否重点关注。")
    print("完成后输入 'q' 退出（已设置的会保存）。")
    print()

    dirty = False

    # 群
    print("\n========== 群 ==========")
    for g in sorted(groups):
        cur = labels["groups"].get(g)
        new = prompt_one("群", g, cur)
        if new is None:
            labels["groups"].pop(g, None)
        elif new:
            labels["groups"][g] = new
        dirty = True
        if input("(回车继续 / 'q' 退出并保存): ").strip().lower() == "q":
            break

    # 联系人
    print("\n========== 联系人 ==========")
    for s in sorted(senders):
        cur = labels["senders"].get(s)
        new = prompt_one("联系人", s, cur)
        if new is None:
            labels["senders"].pop(s, None)
        elif new:
            labels["senders"][s] = new
        dirty = True
        if input("(回车继续 / 'q' 退出并保存): ").strip().lower() == "q":
            break

    if dirty:
        save_labels(labels)
        print(f"\n已保存到 {LABELS_PATH}")
    else:
        print("\n无改动。")


def main() -> None:
    force_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="只列出当前标定（不交互）")
    parser.add_argument("--reset", action="store_true", help="清空所有标定")
    args = parser.parse_args()

    if args.reset:
        save_labels({"groups": {}, "senders": {}})
        print(f"已清空 {LABELS_PATH}")
        return

    if args.list:
        labels = load_labels()
        list_labels(labels)

        groups, senders = discover_ids_from_db()
        unlabeled_groups = sorted(groups - set(labels["groups"].keys()))
        unlabeled_senders = sorted(senders - set(labels["senders"].keys()))
        if unlabeled_groups or unlabeled_senders:
            print()
            print("=" * 60)
            print("未标定的 ID（数据库里见过但还没起名字）")
            print("=" * 60)
            for g in unlabeled_groups:
                kind = "私聊" if is_private(g) else "群"
                print(f"  [{kind}] {g}")
            for s in unlabeled_senders:
                print(f"  [人] {s}")
        return

    interactive()


if __name__ == "__main__":
    main()
