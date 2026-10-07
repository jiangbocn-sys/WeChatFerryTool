"""探针 7：对 `.dat` 的数据段做穷尽式格式判定。

已知头部结构（181 个样本）：前 15 字节 07 08 56 32 08 07 00 04 00 00 <2字节长度> 00 00 01，
第 16 字节是变体标识。数据段不是简单单字节 XOR 的 JPEG，所以逐个试：

  A. 对所有 256 个单字节 key：XOR 后是不是 JPEG/PNG/WebP/zstd/zlib
  B. zlib / zstd / gzip 直接解（原始、以及跳过 15/16 字节）
  C. 数据段长度是否 16 的倍数（AES 无填充的特征）
  D. 用消息 XML 的 aeskey 做 AES-128-ECB（整段 + 跳过 15/16/31/32），看头尾
  E. 是否存在"重复周期"（异或流密钥的迹象）
"""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_img_aes import aes128_ecb_decrypt  # noqa: E402

BASE = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg")
IMG = sorted((BASE / "attach").glob("dd12d75e*/2026-10/Img"))[0]
STEM = "ddece4375134faa154cd693fe53a6d48"
AESKEY = bytes.fromhex("c8bd25997eb553482d4fc7816f9846bd")

MAGICS = {b"\xff\xd8\xff": "JPEG", b"\x89PNG": "PNG", b"GIF8": "GIF", b"BM": "BMP",
          b"RIFF": "WEBP", b"\x28\xb5\x2f\xfd": "ZSTD", b"ftyp": "MP4"}

for fname in (f"{STEM}_t.dat", f"{STEM}.dat"):
    raw = (IMG / fname).read_bytes()
    print(f"=== {fname}  {len(raw)} B ===")
    print(f"  head: {raw[:24].hex(' ')}")
    body_default = raw[16:]
    print(f"  数据段(off=16) 长度 {len(body_default)}，mod16 = {len(body_default) % 16}")
    print(f"  数据段(off=15) 长度 {len(raw) - 15}，mod16 = {(len(raw) - 15) % 16}")

    print("  A. 暴力单字节 XOR（对 off=15/16 的数据段）:")
    hit = False
    for off in (15, 16):
        seg = raw[off:off + 8]
        for k in range(256):
            d = bytes(b ^ k for b in seg)
            for m, name in MAGICS.items():
                if d.startswith(m):
                    print(f"     ✅ off={off} key={k:#04x} → {name}")
                    hit = True
    if not hit:
        print("     （无命中）")

    print("  B. 直接解压尝试:")
    for off in (0, 6, 15, 16):
        seg = raw[off:]
        for name, fn in (("zlib", lambda b: zlib.decompress(b)),
                         ("gzip", lambda b: __import__("gzip").decompress(b)),
                         ("zstd", lambda b: __import__("zstandard").ZstdDecompressor().decompress(b, max_output_size=1 << 22))):
            try:
                out = fn(seg)
                print(f"     ✅ off={off} {name} 解开 {len(out)} B，前 8: {out[:8].hex(' ')}")
                break
            except Exception:  # noqa: BLE001
                pass
    else:
        print("     （三种都解不开）")

    print("  D. AES-128-ECB（aeskey 来自消息 XML）:")
    for off in (0, 15, 16, 31, 32):
        seg = raw[off:]
        n = len(seg) - (len(seg) % 16)
        if n < 16:
            continue
        dec = aes128_ecb_decrypt(AESKEY, seg[:n])
        kind = next((nm for m, nm in MAGICS.items() if dec.startswith(m)), None)
        print(f"     off={off:<3} 前8={dec[:8].hex(' ')} 后8={dec[-8:].hex(' ')} {('✅ ' + kind) if kind else ''}")

    print("  E. 重复周期检测（前 512 字节里找 8 字节重复块）:")
    seg = raw[16:16 + 512]
    blocks = [seg[i:i + 8] for i in range(0, len(seg) - 8, 8)]
    dup = len(blocks) - len(set(blocks))
    print(f"     8 字节块 {len(blocks)} 个，重复 {dup} 个")
    print()
