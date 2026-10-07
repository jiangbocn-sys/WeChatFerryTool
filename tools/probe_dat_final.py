"""探针 13：最后一组尝试 —— 重复密钥 XOR / 明文内含 / 已知明文对齐。

手上有一对"疑似同源"的数据：
  明文：用户从微信另存的 489,072 B JPEG（原图，来自 CDN）
  密文：cache\...\Bubble\ddece4375134faa154cd693fe53a6d48_b.dat（47,999 B）
另外还有同一条消息的 `.dat`（60,624 B，声明 59,569）与 `_t.dat`（4,731 B，声明 3,676）。

尝试：
  A. 用 aeskey 的 16 字节做**重复密钥 XOR**（各种偏移）
  B. 明文 JPEG 的片段是否**原样**出现在任一密文里（可能只有部分被加密）
  C. 已知明文对齐：把明文前缀按各种偏移与密文 XOR，看 XOR 值是否有周期性
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PLAIN = Path(r"D:\projects\wx-saved\微信图片_20261007112501_1034_1.jpg")
WX = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e")
STEM = "ddece4375134faa154cd693fe53a6d48"
AESKEY = bytes.fromhex("c8bd25997eb553482d4fc7816f9846bd")

plain = PLAIN.read_bytes()
cands: dict[str, Path] = {}
for p in WX.rglob(f"{STEM}*"):
    if p.is_file() and p.suffix == ".dat":
        cands[p.name] = p
print("候选密文:", {k: v.stat().st_size for k, v in cands.items()})

MAGICS = [b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8"]

print("\n=== A. aeskey 做重复密钥 XOR ===")
for name, p in cands.items():
    enc = p.read_bytes()
    for off in (0, 15, 16, 32):
        seg = enc[off:off + 64]
        dec = bytes(seg[i] ^ AESKEY[i % 16] for i in range(len(seg)))
        if any(dec.startswith(m) for m in MAGICS):
            print(f"  ✅ {name} off={off} 重复密钥 XOR 命中")
    # 也试 0xF9 / 0x37 的重复
    for k in (0xF9, 0x37):
        for off in (0, 15, 16):
            seg = enc[off:off + 32]
            if any(bytes(b ^ k for b in seg).startswith(m) for m in MAGICS):
                print(f"  ✅ {name} off={off} XOR {k:#04x} 命中")
print("  （无输出 = 都不中）")

print("\n=== B. 明文片段是否原样出现在密文里 ===")
for name, p in cands.items():
    enc = p.read_bytes()
    hits = []
    for probe_len, label in ((16, "16B"), (32, "32B")):
        seg = plain[:probe_len]
        i = enc.find(seg)
        if i >= 0:
            hits.append(f"{label}@ {i}")
    # 也反向：密文片段出现在明文里
    for off in (16, 32):
        seg = enc[off:off + 16]
        j = plain.find(seg)
        if j >= 0:
            hits.append(f"密文片段@{off} → 明文@{j}")
    print(f"  {name}: {hits or '无'}")

print("\n=== C. 已知明文对齐的周期性分析 ===")
# 用 _b.dat（体积最接近原图）与明文前缀
p = cands.get(f"{STEM}_b.dat")
if p:
    enc = p.read_bytes()
    for d in (0, 16, 32, 64):
        ks = [enc[d + i] ^ plain[i] for i in range(256)]
        # 找周期性：检查 16/32 字节周期
        periodic = None
        for period in (8, 16, 32, 64):
            if all(ks[i] == ks[i % period] for i in range(len(ks))):
                periodic = period
                break
        print(f"  d={d:<3} XOR 值前 8: {[hex(k) for k in ks[:8]]}  不同 {len(set(ks))} 个"
              f"  周期={periodic}")
else:
    print("  没有 _b.dat")

print("\n=== D. 头部 16 字节之后是否就是密文（长度=明文-固定开销） ===")
for name, p in cands.items():
    enc = p.read_bytes()
    declared = int.from_bytes(enc[10:12], "little")
    print(f"  {name:<40} 实际 {len(enc):>7}  声明 {declared:>7}  "
          f"内容(off=16) {len(enc)-16:>7}  声明/内容 = {declared/(len(enc)-16):.3f}")
