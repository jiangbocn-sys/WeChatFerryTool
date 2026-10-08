"""实时验证：等一条新的图片消息 → 巡检器抢到 → 配对归档成 <msg_id>.jpg。

用法（运行中的 app 会自己抢）：
    python tools/verify_image_pipeline.py            # 等 3 分钟
    python tools/verify_image_pipeline.py --minutes 5

它只做三件事：
  1. 记录当前 messages 表里 type=3 的最大 id 与 data/images 现有文件；
  2. 轮询等待：出现**新的 type=3 消息** 且 `data/images/<msg_id>.jpg` 落地；
  3. 用 PIL 校验落地的图能打开，并打印尺寸。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from paths import data_root  # noqa: E402

ROOT = data_root()
DB = ROOT / "data" / "messages.db"
IMGDIR = ROOT / "data" / "images"


def newest_images() -> dict[str, int]:
    out = {}
    if DB.is_file():
        conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
        try:
            for mid, ts in conn.execute(
                    "SELECT msg_id, received_at FROM messages WHERE msg_type=3 "
                    "ORDER BY received_at DESC LIMIT 5"):
                out[str(mid)] = int(ts or 0)
        finally:
            conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=3.0)
    args = ap.parse_args()

    before = newest_images()
    have_before = {p.stem for p in IMGDIR.glob("*.jpg")} if IMGDIR.is_dir() else set()
    print(f"开始前：库里最近 {len(before)} 条图片消息；data/images 已有 {len(have_before)} 张")
    print(f"等待新图片消息（最多 {args.minutes} 分钟）… 请在微信里让群里发一张**图片**")

    deadline = time.time() + args.minutes * 60
    seen_new: set[str] = set()
    while time.time() < deadline:
        cur = newest_images()
        new = [m for m in cur if m not in before]
        for mid in new:
            seen_new.add(mid)
            p = IMGDIR / f"{mid}.jpg"
            if p.is_file():
                try:
                    from PIL import Image
                    with Image.open(p) as im:
                        im.load()
                        size = f"{im.size[0]}x{im.size[1]}"
                except Exception as e:  # noqa: BLE001
                    size = f"打开失败 {type(e).__name__}"
                print(f"  🎯 新图片已归档：msg={mid}  文件={p.name}  "
                      f"大小={p.stat().st_size} B  尺寸={size}")
                return 0
            else:
                print(f"  · 新图片消息 {mid} 已入库，等巡检器抢图…")
        time.sleep(5)

    still = {p.stem for p in IMGDIR.glob("*.jpg")} if IMGDIR.is_dir() else set()
    print(f"\n超时。等待期间新图片消息 {len(seen_new)} 条，"
          f"data/images 增加了 {len(still - have_before)} 张")
    if seen_new and not (still - have_before):
        print("→ 有新图片消息但没归档：说明**抢图或配对**还有问题，需要查日志")
    elif not seen_new:
        print("→ 期间没有新图片消息（需要有人在群里发图）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
