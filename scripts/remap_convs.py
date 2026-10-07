"""把历史归档里 '(未知会话:<hash前8位>)' 的行，用最新已知会话映射补正为真实会话 id/名称。

场景：语音归档时该会话还没被识别（对方从未发过消息，无法反推 wxid）；
后续对方回了消息 → 其 wxid 进入数据库 → md5 可反查 → 跑本脚本即可补正。

用法：
  python scripts/remap_convs.py --dry-run   # 预览
  python scripts/remap_convs.py             # 执行
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")

import yaml


def build_map(conn: sqlite3.Connection) -> dict[str, str]:
    """hash 前 8 位 -> 会话 id/名称。

    手工映射 `voice.conv_overrides` 的键**同时接受完整 32 位与 8 位前缀**
    （2026-10-07：以前只截前 8 位用，填 8 位前缀的用户会被静默忽略）。
    """
    ids: set[str] = set()
    ids |= {r[0] for r in conn.execute("SELECT DISTINCT group_name FROM messages")}
    ids |= {r[0] for r in conn.execute("SELECT DISTINCT sender FROM messages")}
    try:
        labels = json.loads((PROJECT_DIR / "data" / "labels.json").read_text(encoding="utf-8"))
        ids |= set((labels.get("groups") or {}).keys())
        ids |= set((labels.get("senders") or {}).keys())
    except Exception:  # noqa: BLE001
        pass
    m = {hashlib.md5(i.encode()).hexdigest()[:8]: i for i in ids if i}
    cfg = yaml.safe_load(open(PROJECT_DIR / "config.yaml", encoding="utf-8"))
    overrides = (cfg.get("voice") or {}).get("conv_overrides") or {}
    for h, v in overrides.items():
        k = str(h).strip()
        if not k:
            continue
        m.setdefault(k[:8], str(v))      # 32 位或 8 位都归一到前 8 位
        m.setdefault(k, str(v))          # 原样也留一份（对照用）
    return m


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(PROJECT_DIR / "data" / "messages.db")
    m = build_map(conn)
    rows = conn.execute(
        "SELECT msg_id, group_name FROM messages WHERE group_name LIKE '(未知会话:%'"
    ).fetchall()

    fixed = 0
    for msg_id, gname in rows:
        prefix = gname[len("(未知会话:"):-1]
        if prefix in m:
            newname = m[prefix]
            print(f"{msg_id}: {gname} -> {newname}")
            if not args.dry_run:
                conn.execute("UPDATE messages SET group_name=? WHERE msg_id=?", (newname, msg_id))
            fixed += 1
    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"{'[dry-run] ' if args.dry_run else ''}可补正 {fixed} / 共 {len(rows)} 条未知会话记录")


if __name__ == "__main__":
    main()