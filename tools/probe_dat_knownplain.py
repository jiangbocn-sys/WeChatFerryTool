"""探针 11（决定性）：用"已知明文"反推 `.dat` 的加密方式。

已知明文：用户从 PC 微信另存的 JPEG（489,072 B，正好等于消息 XML 的 length）
已知密文：msg\attach\...\Img\ddece4375134faa154cd693fe53a6d48.dat（60,624 B）
已知 key 材料：消息 XML 的 aeskey = c8bd25997eb553482d4fc7816f9846bd

判据：JPEG 以 FF D8 FF E0 … 开头、FF D9 结尾；明文长度已知 → 可以直接算
"密文偏移 ↔ 明文偏移"的对齐关系与每字节 XOR 值。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PLAIN = Path(r"D:\projects\wx-saved\微信图片_20261007112501_1034_1.jpg")
DAT_DIR = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg\attach"
               r"\dd12d75e3cf9932ab2a6743476e0d35d\2026-10\Img")
STEM = "ddece4375134faa154cd693fe53a6d48"
AESKEY = bytes.fromhex("c8bd25997eb553482d4fc7816f9846bd")

plain = PLAIN.read_bytes()
enc = (DAT_DIR / f"{STEM}.dat").read_bytes()
print(f"明文 {len(plain)} B   密文 {len(enc)} B   差 = {len(enc) - len(plain)}")
print(f"明文头 16: {plain[:16].hex(' ')}")
print(f"密文头 24: {enc[:24].hex(' ')}")
print(f"明文尾 16: {plain[-16:].hex(' ')}")
print(f"密文尾 16: {enc[-16:].hex(' ')}")

# 1) 试各种"密文偏移 - 明文偏移"的对齐，看 XOR 值是否恒定
print("\n=== 1. 对齐分析：固定 (密文偏移 - 明文偏移) = d，看 XOR 值集合 ===")
best = []
for d in range(0, 64):
    if d + 64 > len(enc):
        continue
    ks = {enc[d + i] ^ plain[i] for i in range(48)}
    best.append((len(ks), d, sorted(ks)[:4]))
best.sort()
for n, d, k in best[:5]:
    print(f"   d={d:<3} 48 字节里不同 XOR 值 {n} 个   例: {[hex(x) for x in k]}")

# 2) 如果存在 d 使 XOR 恒定 → 用更大窗口确认
n, d, _ = best[0]
if n <= 4:
    win = min(4096, len(plain), len(enc) - d)
    ks = {enc[d + i] ^ plain[i] for i in range(win)}
    print(f"\n   → d={d} 用 {win} 字节窗口复检：不同 XOR 值 {len(ks)} 个")
    if len(ks) <= 8:
        print(f"     XOR 值: {[hex(x) for x in sorted(ks)]}")
        if len(ks) == 1:
            print(f"     🎉 恒定 key = {hex(list(ks)[0])} → 整段单字节 XOR（偏移 {d}）")

# 3) 明文头部与密文头部做逐字节 XOR（看有没有固定关系）
print("\n=== 2. 头部逐字节 XOR（密文[i] ^ 明文[i]，i=0..31）===")
for i in range(0, 32, 8):
    row = [f"{enc[i+j] ^ plain[i+j]:02x}" for j in range(8)]
    print(f"   i={i:<3} {' '.join(row)}")

# 4) AES-ECB(key=aeskey) 解出的头部 与 明文头部 的 XOR
print("\n=== 3. AES-ECB(aeskey) 解出的前 16 字节 vs 明文前 16 字节 ===")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aes128 import aes128_ecb_decrypt  # noqa: E402
for off in (0, 15, 16, 32):
    seg = enc[off:]
    seg = seg[: len(seg) - len(seg) % 16]
    dec = aes128_ecb_decrypt(AESKEY, seg[:64])
    x = bytes(a ^ b for a, b in zip(dec, plain))
    print(f"   off={off:<3} dec={dec[:16].hex(' ')}")
    print(f"          ^明文={x[:16].hex(' ')}  不同值 {len(set(x))} 个")

# 5) 长度关系：密文 - 明文 = ?
print(f"\n=== 4. 长度关系 ===\n   密文-明文 = {len(enc)-len(plain)}（若为 0/16/32 等小值，说明只有头尾开销）")
hdr = (DAT_DIR / f"{STEM}_t.dat").read_bytes()[:16]
print(f"   _t.dat 头 16: {hdr.hex(' ')}")
print(f"   .dat  头 16: {enc[:16].hex(' ')}")
