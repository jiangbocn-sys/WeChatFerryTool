"""探针 2：微信 4.x `.dat`（头 07 08 56 32）到底怎么解 —— 用已知明文判据反推。

关键判据：
  * JPEG 以 FF D8 FF 开头、以 FF D9 结尾；
  * 若尾部是"单字节 XOR"，则由 raw[-2:] 能唯一反推 XOR key（要求两个等式同解）；
  * 若是 AES 在前，则对整块 AES 解密后再 XOR。
把所有组合试一遍，命中就打印出来。

只读本地文件。用法：python tools\probe_img_aes2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_img_aes import aes128_ecb_decrypt  # noqa: E402  （复用纯 Python AES）

BASE = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg\attach")
hits = list(BASE.glob("dd12d75e*/2026-10/Img"))
IMG = hits[0] if hits else None
print("图片目录:", IMG)
if not IMG:
    sys.exit(1)

AESKEY = bytes.fromhex("c8bd25997eb553482d4fc7816f9846bd")   # 11:25 消息 XML 里的 aeskey
MAGICS = {b"\xff\xd8\xff": "JPEG", b"\x89PNG": "PNG", b"GIF8": "GIF", b"RIFF": "WEBP"}


def kind(b: bytes) -> str | None:
    if b.startswith(b"RIFF") and b[8:12] == b"WEBP":
        return "WEBP"
    for m, n in MAGICS.items():
        if b.startswith(m):
            return n
    return None


for fname in ("ddece4375134faa154cd693fe53a6d48_t.dat",
              "ddece4375134faa154cd693fe53a6d48.dat"):
    f = IMG / fname
    if not f.exists():
        print(f"  {fname}: 不存在")
        continue
    raw = f.read_bytes()
    print(f"\n=== {fname}  {len(raw)} B ===")
    print(f"  head : {raw[:32].hex(' ')}")
    print(f"  tail : {raw[-16:].hex(' ')}")

    # ① 由 JPEG 尾标记反推单字节 XOR key
    cands = []
    for k in range(256):
        if raw[-2] ^ k == 0xFF and raw[-1] ^ k == 0xD9:
            cands.append(k)
    print(f"  由尾部反推的 XOR key 候选: {[hex(c) for c in cands]}")

    # ② 各种偏移 + （XOR 可选）+ AES
    for off in (0, 4, 6, 8, 10, 12, 15, 16):
        body = raw[off:]
        # 先试"只 XOR"
        for k in cands or [0x37]:
            out = bytes(b ^ k for b in body)
            if kind(out):
                print(f"  ✅ off={off} 纯 XOR key={k:#x} → {kind(out)}")
        # 再试"AES 后 XOR" / "XOR 后 AES"
        n = len(body) - (len(body) % 16)
        if n < 16:
            continue
        dec = aes128_ecb_decrypt(AESKEY, body[:n])
        if kind(dec):
            print(f"  ✅ off={off} 纯 AES → {kind(dec)}")
        for k in cands or [0x37]:
            out = bytes(b ^ k for b in dec)
            if kind(out):
                print(f"  ✅ off={off} AES+XOR key={k:#x} → {kind(out)}")
        out2 = aes128_ecb_decrypt(AESKEY, bytes(b ^ 0x37 for b in body[:n]))
        if kind(out2):
            print(f"  ✅ off={off} XOR+AES(0x37) → {kind(out2)}")
    # ③ 头 15 字节之后直接看是否明文
    for off in (13, 14, 15):
        seg = raw[off:off + 8]
        print(f"  off={off} 原始字节: {seg.hex(' ')}")
