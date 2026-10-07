"""探针 6：全文件扫描 + 分类，找出 .dat 真正的明文格式与头部长度。

关键改动：不再只看头部 64 字节，而是**对全文件**找各种已知魔数；
并把"尾部 key 不唯一"的文件列为可疑（可能是明文 / 别的格式）。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg")
files: list[Path] = []
for d in sorted(p for p in (BASE / "attach").glob("*/2026-10/Img") if p.is_dir()):
    files.extend(sorted(d.glob("*_t.dat"))[:3])
    files.extend(sorted(d.glob("*.dat"))[:2])
files = [f for f in files if f.stat().st_size > 32]
print(f"样本 {len(files)}\n")

MAGICS = {
    "JPEG": b"\xff\xd8\xff", "PNG": b"\x89PNG", "GIF": b"GIF8", "BMP": b"BM",
    "WEBP/RIFF": b"RIFF", "MP4/ftyp": b"ftyp", "HEIC": b"ftypheic",
    "ZLIB": b"\x78\x9c", "GZIP": b"\x1f\x8b", "ZSTD": b"\x28\xb5\x2f\xfd",
    "TIFF": b"II*\x00",
}

print("=== A. 头部 15 字节之后的原始字节（未 XOR）看是否本来就是明文 ===")
plain_hits = Counter()
for f in files[:20]:
    raw = f.read_bytes()
    for off in (15, 16):
        seg = raw[off:off + 12]
        for name, m in MAGICS.items():
            if seg.startswith(m):
                plain_hits[f"{name}@{off}"] += 1
print("  明文魔数命中:", dict(plain_hits) or "无")

print("\n=== B. 对每个文件用 0xF9 XOR 全文件，扫描魔数 ===")
xor_hits = Counter()
detail = []
for f in files:
    raw = f.read_bytes()
    dec = bytes(b ^ 0xF9 for b in raw)
    found = {}
    for name, m in MAGICS.items():
        i = dec.find(m)
        if i >= 0:
            found[name] = i
    for name, off in found.items():
        xor_hits[name] += 1
    detail.append((f.name, len(raw), found))
for name, n in xor_hits.most_common():
    print(f"  {name}: {n} 个文件命中")
print("  样例:", [(d[0][:28], d[2]) for d in detail[:6] if d[2]][:4])

print("\n=== C. 用尾部反推的 key 全文件扫描（只对 key 唯一的文件）===")
uniq_hits = Counter()
for f in files:
    raw = f.read_bytes()
    keys = [k for k in range(256) if raw[-2] ^ k == 0xFF and raw[-1] ^ k == 0xD9]
    if len(keys) != 1:
        continue
    k = keys[0]
    dec = bytes(b ^ k for b in raw)
    for name, m in MAGICS.items():
        i = dec.find(m)
        if i >= 0:
            uniq_hits[f"{name}"] += 1
print(" ", dict(uniq_hits) or "无")

print("\n=== D. 尾部 key 不唯一的文件（可疑：明文/别的格式）===")
odd = 0
for f in files:
    raw = f.read_bytes()
    keys = [k for k in range(256) if raw[-2] ^ k == 0xFF and raw[-1] ^ k == 0xD9]
    if len(keys) != 1:
        odd += 1
        if odd <= 5:
            print(f"  {f.name[:44]:<46} {len(raw):>8}B keys={[hex(k) for k in keys]}")
            print(f"      head: {raw[:24].hex(' ')}")
            print(f"      tail: {raw[-12:].hex(' ')}")
print(f"  共 {odd} / {len(files)} 个")

print("\n=== E. 头 16 字节的逐字节一致性（全部样本）===")
heads = [f.read_bytes()[:16] for f in files if f.stat().st_size >= 16]
for i in range(16):
    vals = [h[i] for h in heads]
    c = Counter(vals)
    common, cnt = c.most_common(1)[0]
    print(f"  byte[{i:>2}] 最常见 {common:#04x} ({cnt}/{len(vals)})  变化{len(c)}种")
