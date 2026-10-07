"""探针 10：把"最后一类高强度假设"验掉。

之前只试过 AES-128-ECB(key=XML.aeskey)。还没试的：
  A. AES-128-CBC(key=aeskey, IV=头部前 16 字节)
  B. AES-128-ECB(key=cdnthumburl 的十六进制前 16 字节)
  C. AES-128-ECB(key=文件内部某 16 字节)
  D. 上述各种 + 尾部 XOR 0x37 / 0xF9
  E. aeskey 的 hex → 当 password 走 KDF（SHA256 前 16 字节 / MD5）

纯 Python AES-128 实现（已过 FIPS-197 自检）。只读本地文件。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_img_aes import _expand_key, _decrypt_block, aes128_ecb_decrypt  # noqa: E402

BASE = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg")
IMG = sorted((BASE / "attach").glob("dd12d75e*/2026-10/Img"))[0]
STEM = "ddece4375134faa154cd693fe53a6d48"
IMG_XML_KEY = "c8bd25997eb553482d4fc7816f9846bd"
CDN_URL = ("305f020100044b304902010002041fd287e002032f4f560204997ac2dc02046ac5bb8c0424"
           "66356432626438362d336634302d343564362d393535342d6539303163353466656134620204"
           "05290a02020100040d004c4ff2000000000000000000")
MAGICS = {b"\xff\xd8\xff": "JPEG", b"\x89PNG": "PNG", b"RIFF": "WEBP", b"BM": "BMP"}


def kind(b: bytes) -> str | None:
    if b.startswith(b"RIFF") and b[8:12] == b"WEBP":
        return "WEBP"
    for m, n in MAGICS.items():
        if b.startswith(m):
            return n
    return None


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    rk = _expand_key(key)
    prev = iv
    out = bytearray()
    for i in range(0, len(data) - len(data) % 16, 16):
        blk = data[i:i + 16]
        dec = _decrypt_block(blk, rk)
        out.extend(bytes(a ^ b for a, b in zip(dec, prev)))
        prev = blk
    return bytes(out)


raw = (IMG / f"{STEM}_t.dat").read_bytes()
print(f"文件 {STEM}_t.dat {len(raw)} B")
print("head16:", raw[:16].hex(" "))

keys = {
    "xml.aeskey": bytes.fromhex(IMG_XML_KEY),
    "head[0:16]": raw[:16],
    "head[16:32]": raw[16:32],
    "cdnurl[0:16]": bytes.fromhex(CDN_URL)[:16],
    "md5(aeskey)": hashlib.md5(IMG_XML_KEY.encode()).digest(),
    "sha256(aeskey)[:16]": hashlib.sha256(IMG_XML_KEY.encode()).digest()[:16],
}
ivs = {"head[0:16]": raw[:16], "zero": bytes(16), "cdnurl[0:16]": bytes.fromhex(CDN_URL)[:16]}

print("\n=== ECB：换 key ===")
for kn, k in keys.items():
    for off in (0, 15, 16):
        seg = raw[off:]
        seg = seg[: len(seg) - len(seg) % 16]
        if len(seg) < 16:
            continue
        dec = aes128_ecb_decrypt(k, seg[:512])
        k2 = kind(dec)
        tail = aes128_ecb_decrypt(k, seg[-64:])
        if k2 or kind(tail) or off == 16:
            print(f"  key={kn:<20} off={off:<3} 前8={dec[:8].hex(' ')}  {('✅' + k2) if k2 else ''}")

print("\n=== CBC：换 IV ===")
for kn, k in keys.items():
    for ivn, iv in ivs.items():
        seg = raw[16:]
        seg = seg[: len(seg) - len(seg) % 16]
        if len(seg) < 32:
            continue
        dec = aes_cbc_decrypt(k, iv, seg[:512])
        k2 = kind(dec)
        if k2:
            print(f"  ✅ key={kn} iv={ivn} → {k2}")
print("  （无 ✅ 表示都不对）")

print("\n=== 各种 AES 结果再 XOR 0x37/0xF9 ===")
for kn, k in keys.items():
    seg = raw[16:]
    seg = seg[: len(seg) - len(seg) % 16]
    dec = aes128_ecb_decrypt(k, seg[:256])
    for x in (0x37, 0xF9):
        d2 = bytes(b ^ x for b in dec)
        if kind(d2):
            print(f"  ✅ key={kn} off=16 + XOR {x:#04x} → {kind(d2)}")
print("  （无 ✅ 表示都不对）")

print("\n=== 直接看 cdnthumburl 解出来的字节（它可能是明文/或含 IV）===")
u = bytes.fromhex(CDN_URL)
print("  cdnurl 前 32:", u[:32].hex(" "))
print("  cdnurl 后 32:", u[-32:].hex(" "))
print("  里面可见的 ASCII:", "".join(chr(b) if 32 <= b < 127 else "." for b in u))
