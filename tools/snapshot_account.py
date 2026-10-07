"""对账号数据目录做"文件元信息快照/对比"，用来定位某个操作会在本地产生什么文件。

只记录 (路径, 大小, mtime)，不读内容、不写账号目录 → 很轻，也不影响微信。
用法：
    python tools/snapshot_account.py save     # 存基线
    python tools/snapshot_account.py diff     # 与基线对比，列出 新增/修改/删除
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from paths import wechat_files_root  # noqa: E402

STATE = Path(__file__).resolve().parent / ".account_snapshot.json"


def account_dir() -> Path | None:
    root = wechat_files_root()
    if not root:
        return None
    subs = [d for d in root.iterdir() if d.is_dir() and d.name not in ("all_users", "Backup")]
    if not subs:
        return None
    subs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return subs[0]


def scan(base: Path) -> dict[str, list]:
    out: dict[str, list] = {}
    for p in base.rglob("*"):
        try:
            if not p.is_file():
                continue
            st = p.stat()
        except OSError:
            continue
        out[str(p.relative_to(base))] = [st.st_size, int(st.st_mtime)]
    return out


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("save", "diff"):
        print(__doc__)
        return 2
    base = account_dir()
    if base is None:
        print("找不到微信账号目录")
        return 2
    print(f"账号目录: {base}")
    cur = scan(base)
    print(f"文件数: {len(cur)}")

    if sys.argv[1] == "save":
        STATE.write_text(json.dumps({"base": str(base), "files": cur}, ensure_ascii=False),
                         encoding="utf-8")
        print(f"基线已保存: {STATE}")
        return 0

    if not STATE.is_file():
        print("还没有基线，先跑 save")
        return 2
    old = json.loads(STATE.read_text(encoding="utf-8"))
    if old.get("base") != str(base):
        print(f"⚠️ 基线是另一个目录的: {old.get('base')}")
    of = old.get("files") or {}

    added = sorted(set(cur) - set(of))
    removed = sorted(set(of) - set(cur))
    changed = sorted(k for k in set(cur) & set(of) if cur[k] != of[k])

    print(f"\n新增 {len(added)} / 修改 {len(changed)} / 删除 {len(removed)}")
    if added:
        print("\n=== 新增 ===")
        for k in added:
            print(f"  {cur[k][0]:>9} B  {k}")
    if changed:
        print("\n=== 修改 ===")
        for k in changed:
            print(f"  {of[k][0]:>9} -> {cur[k][0]:>9} B  {k}")
    if removed:
        print("\n=== 删除 ===")
        for k in removed:
            print(f"  {of[k][0]:>9} B  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
