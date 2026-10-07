"""探针 8（路线 B）：在本地缓存里找"明文 JPEG ↔ 加密 .dat"的配对。

判据：
  1. `.dat` 头部 offset 10 起的 2 字节小端 = 明文文件大小（实测 _t 对应 cdnthumblength，
     无后缀对应 hevc_mid_size）→ 可与某个 .jpg 的大小对上；
  2. 落盘时间接近（同一条消息的两个副本）；
  3. 尺寸可比（用 PIL 读 .jpg 的宽高，与消息 XML 里的 cdnthumb* 无关，只做辅助）。

找到配对后，把 (密文, 明文) 一块打出来，就能反推密钥/块边界。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg")
print("扫描 msg 目录下的 .dat 与 .jpg ...")
dats: list[Path] = [p for p in BASE.rglob("*.dat") if p.is_file()]
jpegs: list[Path] = [p for p in BASE.rglob("*.jpg") if p.is_file()]
print(f"  .dat {len(dats)} 个；.jpg {len(jpegs)} 个\n")


def dat_size(p: Path) -> int:
    """头部 offset 10 起的 2 字节小端（= 明文大小）。"""
    try:
        with open(p, "rb") as f:
            b = f.read(16)
        if len(b) < 16 or b[:6] != bytes([0x07, 0x08, 0x56, 0x32, 0x08, 0x07]):
            return -1
        return int.from_bytes(b[10:12], "little")
    except OSError:
        return -1


jpg_by_size: dict[int, list[Path]] = defaultdict(list)
for j in jpegs:
    jpg_by_size[j.stat().st_size].append(j)

print("=== 按大小配对（.dat 头部声明的大小 == .jpg 实际大小）===")
pairs = []
for d in dats:
    sz = dat_size(d)
    if sz <= 0:
        continue
    for j in jpg_by_size.get(sz, []):
        dt = abs(d.stat().st_mtime - j.stat().st_mtime)
        pairs.append((d, j, sz, dt))
pairs.sort(key=lambda x: x[3])
print(f"  找到 {len(pairs)} 对（按时间差排序，取最接近的 5 对）：")
for d, j, sz, dt in pairs[:5]:
    print(f"    Δt={dt:>7.1f}s  size={sz:>8}  dat={d.name[:40]}")
    print(f"                       jpg={j.name[:40]}  ({j.parent.name[:28]})")

if pairs:
    d, j, sz, dt = pairs[0]
    enc = d.read_bytes()
    plain = j.read_bytes()
    print(f"\n=== 取最匹配的一对做字节分析 ===")
    print(f"  密文(.dat) {d.name}  {len(enc)} B")
    print(f"  明文(.jpg) {j.name}  {len(plain)} B")
    print(f"  密文头 32: {enc[:32].hex(' ')}")
    print(f"  明文头 32: {plain[:32].hex(' ')}")
    body = enc[16:]
    print(f"  密文数据段(off=16) {len(body)} B；明文 {len(plain)} B；差 {len(body) - len(plain)}")
    if len(body) == len(plain):
        print("  → 长度相等！数据段与明文逐字节相同？", body == plain)
        if body != plain:
            ks = {body[i] ^ plain[i] for i in range(min(64, len(body)))}
            print(f"  → 前 64 字节的 XOR 值集合: {[hex(k) for k in sorted(ks)][:8]}")
    # 也试 off=15
    body15 = enc[15:]
    print(f"  密文数据段(off=15) {len(body15)} B；与明文等长? {len(body15) == len(plain)}")
    if len(body15) == len(plain) and body15 != plain:
        ks = {body15[i] ^ plain[i] for i in range(min(64, len(body15)))}
        print(f"  → off=15 前 64 字节 XOR 值集合: {[hex(k) for k in sorted(ks)][:8]}")

print("\n=== 附：所有 .dat 的（声明大小, 实际大小, 文件名）前 12 条 ===")
for d in dats[:12]:
    sz = dat_size(d)
    real = d.stat().st_size
    print(f"  {d.name[:46]:<48} 声明={sz:>9}  实际={real:>9}  落盘={datetime.fromtimestamp(d.stat().st_mtime):%m-%d %H:%M:%S}")
