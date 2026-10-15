"""把标定里这位改回「可师」（用户 10-15 要求）。

背景
----
`senders` 里两个 wxid 都写着 `name: 可贞` / `name_prev: 可师` —— 说明**原标注名是可师**，
被"自动标定"流程（`wechat_offline.py`）用微信数据库里的昵称覆盖成了「可贞」。
用户明确：这个人在弟子群里该叫「可师」（与 `周少彬`(name_prev=周师) 的称谓一致）。

做法
----
把 `name` 设为「可师」，并保留一个回退点 `name_prev = 可贞`。
**只动这两个 wxid，其它字段原样保留**；改前先做 `.bak-<时间戳>` 备份。

用法（默认仅预演，加 --apply 才写）：
    python tools/rename_sender.py            # 看会改什么
    python tools/rename_sender.py --apply    # 真正写入
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TARGET_PAIRS = [("25984982465418055@openim", "可师"),
                ("wxid_81sc5zsfs4vj22", "可师")]


def main() -> int:
    ap = argparse.ArgumentParser(description="把标定里的显示名改掉（带备份）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    ap.add_argument("--to", default="可师", help="目标名字（默认 可师）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    import paths
    lab_path = paths.data_root() / "data" / "labels.json"
    if not lab_path.is_file():
        print(f"[x] 找不到标定文件: {lab_path}")
        return 1

    lab = json.loads(lab_path.read_text(encoding="utf-8"))
    senders = lab.get("senders") or {}
    changes = []
    for wxid, _ in TARGET_PAIRS:
        ent = senders.get(wxid)
        if ent is None:
            print(f"  [跳过] {wxid} 不在标定里")
            continue
        old = ent.get("name") or ""
        if old == args.to:
            print(f"  [已是最新] {wxid}: {old}")
            continue
        changes.append((wxid, old, args.to))

    print(f"  标定文件: {lab_path}")
    print(f"  计划改动 {len(changes)} 处：")
    for wxid, old, new in changes:
        print(f"    {wxid}: {old!r} -> {new!r}（旧名存入 name_prev）")

    if not changes:
        print("  无需改动")
        return 0
    if not args.apply:
        print("\n  预演结束。确认无误后加 --apply 真正写入。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = lab_path.with_name(f"{lab_path.name}.bak-{stamp}")
    shutil.copy2(lab_path, bak)
    print(f"  已备份: {bak.name}")

    for wxid, old, new in changes:
        ent = senders[wxid]
        ent["name"] = new
        ent["name_prev"] = old          # 留回退点
    lab_path.write_text(json.dumps(lab, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  已写入 {len(changes)} 处改动")

    # 回读校验
    back = json.loads(lab_path.read_text(encoding="utf-8"))
    ok = all((back["senders"].get(w) or {}).get("name") == args.to for w, _, _ in changes)
    print(f"  回读校验: {'OK' if ok else '失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
