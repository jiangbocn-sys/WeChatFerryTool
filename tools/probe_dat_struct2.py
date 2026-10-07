"""探针 4：确定 `.dat` 的"头多长 + 从哪开始 XOR"。

已知事实（前面实测）：
  * `_t.dat` 尾部用 key=0xF9 解出来是 `… FF DB … FF C4 … FF D9`（标准 JPEG 段结构）
    → 数据段是**单字节 XOR**、长度不变；
  * 但头部 0~40 偏移里找不到 `FF D8 FF`（SOI）→ 头部区域的语义还没定。

本脚本用"多文件一致性"定位：
  1. 对每个 `.dat` 用尾部反推 key（要求 raw[-2:]+key == FF D9）
  2. 打印不同候选偏移处 XOR 后的字节，找哪个偏移**恒定**出现 FF D8 FF
  3. 对比同类文件的头 16 字节，看哪些字节是常量、哪些随文件变化
  4. 用 65 个明文 .jpg 的头部/尾部做对照
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg")
IMG_DIRS = sorted(p for p in (BASE / "attach").glob("*/2026-10/Img") if p.is_dir())
print(f"图片目录 {len(IMG_DIRS)} 个\n")

# 收集若干 .dat（优先 _t.dat，它们多且小）
files: list[Path] = []
for d in IMG_DIRS:
    files.extend(sorted(d.glob("*_t.dat"))[:3])
    files.extend(sorted(d.glob("*.dat"))[:2])
print(f"样本 {len(files)} 个\n")

print("=== 每个文件：尾部反推 key + 各偏移 XOR 后的前 4 字节 ===")
offsets = [6, 8, 10, 12, 14, 15, 16, 18, 20, 24, 32]
head_hits: Counter = Counter()
for f in files[:12]:
    raw = f.read_bytes()
    keys = [k for k in range(256) if raw[-2] ^ k == 0xFF and raw[-1] ^ k == 0xD9]
    k = keys[0] if len(keys) == 1 else None
    print(f"\n  {f.name[:44]:<46} {len(raw):>8} B  key={hex(k) if k is not None else keys}")
    if k is None:
        continue
    for off in offsets:
        seg = bytes(b ^ k for b in raw[off:off + 4])
        mark = "  ←FFD8FF!" if seg[:3] == b"\xff\xd8\xff" else ""
        print(f"    off={off:<3} XOR后={seg.hex(' ')}{mark}")
        if seg[:3] == b"\xff\xd8\xff":
            head_hits[off] += 1

print("\n=== 哪个偏移恒定出现 JPEG SOI ===")
for off, n in head_hits.most_common():
    print(f"  off={off}: {n} 次")

print("\n=== 头 16 字节的逐字节一致性（多文件）===")
heads = [f.read_bytes()[:16] for f in files[:12] if f.stat().st_size >= 16]
if heads:
    for i in range(16):
        vals = [h[i] for h in heads]
        c = Counter(vals)
        common, cnt = c.most_common(1)[0]
        flag = "常量" if cnt == len(vals) else f"变化({len(c)}种)"
        print(f"  byte[{i:>2}] 最常见 {common:#04x}  {flag}   全部: {[hex(v) for v in vals[:8]]}")

print("\n=== 明文 JPEG 对照（msg/video/*.jpg 的头 16 字节）===")
for f in sorted((BASE / "video").glob("*.jpg"))[:3]:
    b = f.read_bytes()[:16]
    print(f"  {f.name[:40]:<42} {b.hex(' ')}")
