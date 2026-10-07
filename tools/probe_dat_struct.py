"""探针 3：把微信 4.x `.dat`（头 07 08 56 32）的结构拆清楚。

思路（全是可验证的判据，不靠猜）：
  1. 头部 3 行的字节用多种整数格式解一遍，看哪个等于"明文文件大小"
     （大小可由 `_t.dat` 的解密结果反推：它一定 ≤ 压缩后大小）
  2. 用 JPEG 头 FF D8 FF 反推 XOR key：k = raw[off] ^ 0xFF
     同时用 JPEG 尾 FF D9 校验：raw[-1] ^ k == 0xD9
  3. 如果 key 一致 → 说明 [off, end) 全是单字节 XOR；再验证整段首尾
  4. 明文 JPEG（msg\\video\\*.jpg）拿来做"已知明文"对照

只读本地文件。用法：python tools\probe_dat_struct.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg")
hits = list((BASE / "attach").glob("dd12d75e*/2026-10/Img"))
IMG = hits[0] if hits else None
print("图片目录:", IMG, "\n")

STEM = "ddece4375134faa154cd693fe53a6d48"
AESKEY = bytes.fromhex("c8bd25997eb553482d4fc7816f9846bd")

for fname in (f"{STEM}_t.dat", f"{STEM}.dat"):
    f = IMG / fname
    raw = f.read_bytes()
    print(f"=== {fname}  {len(raw)} B ===")
    head = raw[:32]
    print("  head:", head.hex(" "))
    # ① 头部各 4 字节窗口，按 LE/BE 解
    for off in (6, 8, 10, 12, 14):
        win = raw[off:off + 4]
        if len(win) < 4:
            continue
        le = int.from_bytes(win, "little")
        be = int.from_bytes(win, "big")
        tag = ""
        if len(raw) - 15 == le or len(raw) - 15 == be:
            tag = "  ← 等于「总长-15」"
        print(f"    off={off:<3} {win.hex(' ')}  LE={le:<12} BE={be:<12}{tag}")
    # ② 用 JPEG 头反推 XOR key（FF D8 FF）
    print("  -- 用 FF D8 FF 反推 key --")
    for off in range(0, 40):
        k = raw[off] ^ 0xFF
        if raw[off + 1] ^ k == 0xD8 and raw[off + 2] ^ k == 0xFF:
            tail_ok = raw[-1] ^ k == 0xD9
            print(f"    ✅ off={off} key={k:#04x} 尾标记校验={'通过' if tail_ok else '不通过'}")
            body = raw[off:]
            if all(b ^ k == b for b in b""):
                pass
            dec = bytes(b ^ k for b in body)
            print(f"       解出前 16: {dec[:16].hex(' ')}")
            print(f"       解出后 16: {dec[-16:].hex(' ')}")
    # ③ 头 15 字节之后，直接看是不是"每个字节 XOR 同一个 key"：
    #    用尾部 FF D9 唯一确定的 key（若存在）
    print("  -- 尾部反推 key --")
    for k in range(256):
        if raw[-2] ^ k == 0xFF and raw[-1] ^ k == 0xD9:
            print(f"    尾部给出 key={k:#04x}")
    print()
