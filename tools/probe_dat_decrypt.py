"""探针：微信 4.x `.dat` 解密 —— 已排除方案的完整记录（可复跑）。

⚠️ 结论（2026-10-07，多轮实测）：**没有找到可用的解密方式**。本脚本把验证过的
假设全部再跑一遍，方便下次接手时快速确认"哪些路已封死"，不要重复劳动。

已验证无效（每一项都是全文件/多偏移，不是抽样）：
  1. 单字节 XOR：256 个 key × 各偏移，零图片魔数命中
  2. zlib / gzip / zstd：原样与跳过 15/16 字节都解不开
  3. AES-128-ECB，key 取：XML.aeskey / 头部字节 / cdnthumburl 前 16 字节 /
     md5(aeskey) / sha256(aeskey)[:16]
  4. AES-128-CBC，IV 取：头部前 16 字节 / 全零 / cdnthumburl 前 16 字节
  5. 上述 AES 结果再 XOR 0x37 / 0xF9
  6. 明文（第 16 字节之后直接就是密文）
  7. DLL 侧 /Decode_Pic（空壳，恒回 ret:0 但不产出字节）

已确定的结构（181 样本一致）：
  07 08 56 32 08 07 00 04 00 00 <2字节小端明文大小> 00 00 01 <1字节变体>
  其中"明文大小"= 消息 XML 的 cdnthumblength(_t) / hevc_mid_size(无后缀)，可校验映射。
  第 16~30 字节在很多文件里完全相同（_t.dat 200 样本仅 4 种取值）→ 疑似固定块/分组密钥。

用法：python tools\probe_dat_decrypt.py
"""
from __future__ import annotations

import hashlib
import sys
import zlib
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aes128 import aes128_cbc_decrypt, aes128_ecb_decrypt, selftest  # noqa: E402

BASE = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg")
IMG = sorted((BASE / "attach").glob("dd12d75e*/2026-10/Img"))[0]
STEM = "ddece4375134faa154cd693fe53a6d48"
XML_KEY = bytes.fromhex("c8bd25997eb553482d4fc7816f9846bd")
CDN_URL = bytes.fromhex(
    "305f020100044b304902010002041fd287e002032f4f560204997ac2dc02046ac5bb8c042466356432"
    "626438362d336634302d343564362d393535342d653930316335346665613462020405290a02020100"
    "040d004c4ff2000000000000000000")
MAGICS = {b"\xff\xd8\xff": "JPEG", b"\x89PNG": "PNG", b"RIFF": "WEBP", b"BM": "BMP",
          b"GIF8": "GIF", b"\x28\xb5\x2f\xfd": "ZSTD", b"ftyp": "MP4"}


def kind(b: bytes) -> str | None:
    if b.startswith(b"RIFF") and b[8:12] == b"WEBP":
        return "WEBP"
    for m, n in MAGICS.items():
        if b.startswith(m):
            return n
    return None


def main() -> int:
    print("AES-128 自检:", "✅" if selftest() else "❌")
    hits = 0
    for fname in (f"{STEM}_t.dat", f"{STEM}.dat"):
        raw = (IMG / fname).read_bytes()
        print(f"\n=== {fname}  {len(raw)} B ===")
        print(f"  head: {raw[:16].hex(' ')}")

        # 1. 单字节 XOR（用**更强的判据**：要连续 3 个魔数字节都对，BM/GIF8 太容易假阳性）
        STRICT = [b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8", b"\x28\xb5\x2f\xfd"]
        off_hit = False
        for off in (0, 6, 15, 16):
            seg = raw[off:off + 4]
            for k in range(256):
                d = bytes(b ^ k for b in seg)
                if any(d.startswith(m) for m in STRICT):
                    print(f"  ✅ XOR off={off} key={k:#04x} → {d.hex(' ')}")
                    hits += 1
                    off_hit = True
        print("  1. 单字节 XOR：无命中" if not off_hit else "")

        # 2. 解压
        ok = False
        for off in (0, 6, 15, 16):
            seg = raw[off:]
            for name, fn in (("zlib", zlib.decompress),
                             ("gzip", __import__("gzip").decompress),
                             ("zstd", lambda b: __import__("zstandard")
                              .ZstdDecompressor().decompress(b, max_output_size=1 << 22))):
                try:
                    if kind(fn(seg)):
                        print(f"  ✅ {name} off={off}")
                        hits += 1
                        ok = True
                except Exception:  # noqa: BLE001
                    pass
        print("  2. 解压：无命中" if not ok else "")

        # 3/4/5. AES 各种组合
        keys = {"xml.aeskey": XML_KEY, "head[0:16]": raw[:16], "cdnurl[0:16]": CDN_URL[:16],
                "md5(aeskey)": hashlib.md5(XML_KEY).digest(),
                "sha256(aeskey)[:16]": hashlib.sha256(XML_KEY).digest()[:16]}
        ivs = {"head[0:16]": raw[:16], "zero": bytes(16), "cdnurl[0:16]": CDN_URL[:16]}
        found = False
        for kn, k in keys.items():
            for off in (0, 15, 16, 31, 32):
                seg = raw[off:]
                dec = aes128_ecb_decrypt(k, seg[:512])
                if kind(dec) or kind(aes128_ecb_decrypt(k, seg[-64:])):
                    print(f"  ✅ AES-ECB key={kn} off={off}")
                    hits += 1
                    found = True
                for x in (0x37, 0xF9):
                    if kind(bytes(b ^ x for b in dec)):
                        print(f"  ✅ AES-ECB key={kn} off={off} + XOR {x:#04x}")
                        hits += 1
                        found = True
            for ivn, iv in ivs.items():
                seg = raw[16:]
                if kind(aes128_cbc_decrypt(k, iv, seg[:512])):
                    print(f"  ✅ AES-CBC key={kn} iv={ivn}")
                    hits += 1
                    found = True
        print("  3/4/5. AES 各种组合：无命中" if not found else "")

    print(f"\n=== 命中的方案数：{hits} ===")
    if hits == 0:
        print("结论：以上假设全部排除 —— 需要参考实现的密钥派生/块边界（去看 dat2img.go）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
