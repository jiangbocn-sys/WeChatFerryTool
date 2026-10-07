"""探针 5：`.dat` 的头部到底多长、SOI(FF D8 FF) 在哪个偏移。

已知：
  * 所有 `_t.dat` 的尾 key 都是 0xF9 → 尾部是单字节 XOR 的 JPEG；
  * 头部（约前 15 字节）XOR 后各文件一致 → 是固定格式头，不是图像数据；
  * 但 off=16 处 XOR 后不是 FFD8FF。

本脚本：对每个文件扫全部偏移找 SOI / JPEG 段标志，并统计"SOI 偏移 - 文件长度"的关系。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg")
files: list[Path] = []
for d in sorted(p for p in (BASE / "attach").glob("*/2026-10/Img") if p.is_dir()):
    files.extend(sorted(d.glob("*_t.dat"))[:4])
files = [f for f in files if f.stat().st_size > 32]
print(f"样本 {len(files)} 个\n")

print("=== 每个文件：XOR 后 SOI(FF D8 FF) 出现的偏移 ===")
head_lens: Counter = Counter()
for f in files:
    raw = f.read_bytes()
    keys = [k for k in range(256) if raw[-2] ^ k == 0xFF and raw[-1] ^ k == 0xD9]
    if len(keys) != 1:
        print(f"  {f.name[:44]:<46} key 不唯一 {keys}")
        continue
    k = keys[0]
    soi = [off for off in range(0, 64) if raw[off] ^ k == 0xFF
           and raw[off + 1] ^ k == 0xD8 and raw[off + 2] ^ k == 0xFF]
    # 也找 JPEG 段标志（FF C0/C4/DB/DA）出现的位置
    segs = [off for off in range(0, 64)
            if raw[off] ^ k == 0xFF and (raw[off + 1] ^ k) in (0xC0, 0xC4, 0xDB, 0xDA, 0xE0, 0xEE)]
    print(f"  {f.name[:40]:<42} {len(raw):>8}B key={k:#04x} SOI@{soi} 段标志@{segs[:6]}")
    if soi:
        head_lens[soi[0]] += 1

print("\n=== SOI 偏移分布 ===")
for off, n in head_lens.most_common():
    print(f"  off={off}: {n} 次")

# 取一个文件，把 XOR 后的前 64 字节十六进制打出来（人工看结构）
if files:
    f = files[0]
    raw = f.read_bytes()
    k = [x for x in range(256) if raw[-2] ^ x == 0xFF and raw[-1] ^ x == 0xD9][0]
    dec = bytes(b ^ k for b in raw[:64])
    print(f"\n=== {f.name} 前 64 字节（XOR {k:#04x} 后）===")
    print("  原始:", raw[:64].hex(" "))
    print("  解密:", dec.hex(" "))
    print("  ASCII:", "".join(chr(b) if 32 <= b < 127 else "." for b in dec))
    # 全文件里找 SOI 和 JPEG 段
    full = bytes(b ^ k for b in raw)
    for name, magic in (("SOI FF D8 FF", b"\xff\xd8\xff"), ("EOI FF D9", b"\xff\xd9"),
                        ("SOF0 FF C0", b"\xff\xc0"), ("DQT FF DB", b"\xff\xdb"),
                        ("DHT FF C4", b"\xff\xc4"), ("SOS FF DA", b"\xff\xda"),
                        ("APP0 FF E0", b"\xff\xe0"), ("RIFF", b"RIFF"),
                        ("ftyp", b"ftyp"), ("PNG", b"\x89PNG")):
        idx = full.find(magic)
        print(f"    全文件找 {name:<12}: {'@' + str(idx) if idx >= 0 else '未找到'}")
