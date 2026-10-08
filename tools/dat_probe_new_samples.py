"""攻 .dat 第一步：把新样本（同一张图的三档变体）摸清楚 + 全盘找明文。

2026-10-08 的新样本（南京路特坦群，12:22/12:29 下载）：
    stem `d0b2151882ea319ea5de237956b90a26`:
        msg\\attach\\...\\Img\\<stem>_t.dat      2,957 B   （缩略）
        msg\\attach\\...\\Img\\<stem>.dat       19,214 B   （中图）
        msg\\attach\\...\\Img\\<stem>_h.dat    548,688 B   （高清）
        cache\\...\\Bubble\\<other>_b.dat       68,219 B
    stem `174d9c5ca4dbe033753187978f8c9e59`:
        _t.dat 4,905 B / .dat 70,212 B / _h.dat 无 / _b.dat 45,543 B

本脚本：
  1. 打印每个变体的头部字段（头 16 字节 + 声明大小 + 变体字节）与载荷统计；
  2. 全盘（微信账号目录）搜"能打开的图片文件"，看这张图有没有明文副本留在某处；
  3. 统计载荷的熵/重复度，判断加密类型（流密码 vs 分组）。
只读。
"""
from __future__ import annotations

import glob
import math
import os
import struct
import sys
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e")
STEMS = ["d0b2151882ea319ea5de237956b90a26", "174d9c5ca4dbe033753187978f8c9e59"]


def describe(p: Path) -> dict:
    b = p.read_bytes()
    head = b[:16]
    declared = struct.unpack_from("<H", b, 10)[0] if len(b) >= 12 else 0
    body = b[16:]
    # 熵
    cnt = Counter(body[:65536])
    ent = -sum((v / len(body[:65536])) * math.log2(v / len(body[:65536]))
               for v in cnt.values()) if body else 0
    # 8 字节块重复度（ECB 特征）
    blocks = Counter(body[i:i + 8] for i in range(0, min(len(body), 65536) - 8, 8))
    top = blocks.most_common(1)[0] if blocks else (b"", 0)
    return dict(path=p, size=len(b), head=head, declared=declared,
                variant=head[15], body_len=len(body), entropy=ent,
                top_block=top, dup_ratio=top[1] / max(1, len(blocks)))


def main() -> int:
    print("=== 新样本变体一览 ===")
    samples = []
    for stem in STEMS:
        for pat in (f"msg/attach/*/2026-10/Img/{stem}*.dat",
                    f"cache/2026-10/Message/*/Bubble/{stem}*.dat"):
            samples += [Path(x) for x in glob.glob(str(BASE / pat))]
    samples = sorted(set(samples), key=lambda p: p.stat().st_size)
    for p in samples:
        d = describe(p)
        print(f"  {p.name:<50} {d['size']:>9} B  声明={d['declared']:>7} "
              f"差={d['size'] - d['declared']:>6}  变体=0x{d['variant']:02x}  "
              f"熵={d['entropy']:.2f}  重复8B块={d['dup_ratio'] * 100:.2f}%")
        print(f"      头: {d['head'].hex(' ')}")
    if not samples:
        print("  （没找到样本，可能已被清理）")
        return 1

    print("\n=== 全盘找这张图的明文副本（可打开的图片文件）===")
    found = 0
    try:
        from PIL import Image
    except ImportError:
        print("  （没有 PIL）")
        return 0
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".heic", ".dat", ""}
    for p in BASE.rglob("*"):
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if p.suffix.lower() not in exts:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size < 20000 or st.st_size > 20_000_000:
            continue
        # 只看今天有改动的
        import time
        if time.time() - st.st_mtime > 86400:
            continue
        try:
            with Image.open(p) as im:
                im.load()
                size = f"{im.size[0]}x{im.size[1]}"
            print(f"  ✅ 明文图 {size} {im.format} {st.st_size} B  {p.relative_to(BASE)}")
            found += 1
        except Exception:  # noqa: BLE001
            continue
        if found > 40:
            break
    print(f"  → 今天改动过的明文图片共 {found} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
