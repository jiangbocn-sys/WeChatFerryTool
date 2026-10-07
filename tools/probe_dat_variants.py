"""探针 12：全盘扫描 .dat，找出与你保存的原图（489,072 B）对应的变体。

思路：每个 `.dat` 头部 offset 10 起的 2 字节小端 = 该文件的**明文大小**，
直接和已知明文的长度（以及其它尺寸）比对；命中就是"明文-密文"配对，可以反推算法。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WX = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e")
TARGETS = {489072: "你保存的原图", 4700: "缩略图(cdnthumblength)", 46944: "中图(_b.dat 声明)",
           60593: "hevc_mid_size", 144803: "11:47 那张(实际大小)"}

dats = [p for p in WX.rglob("*.dat") if p.is_file()]
print(f"扫描 {len(dats)} 个 .dat\n")
rows = []
for p in dats:
    try:
        with open(p, "rb") as f:
            h = f.read(16)
    except OSError:
        continue
    if len(h) < 16 or h[:6] != bytes([0x07, 0x08, 0x56, 0x32, 0x08, 0x07]):
        continue
    declared = int.from_bytes(h[10:12], "little")
    rows.append((declared, p.stat().st_size, p))

print("=== 目标尺寸命中 ===")
for t, label in TARGETS.items():
    hits = [(d, sz, p) for d, sz, p in rows if d == t]
    print(f"  声明={t:<8} ({label}): {len(hits)} 个", [f"{p.name}(实际{sz})" for _, sz, p in hits[:3]])

print("\n=== 声明大小 vs 实际大小的关系（统计前 200 个）===")
diffs = Counter()
for d, sz, p in rows[:200]:
    if d:
        diffs[sz - d] += 1
for diff, n in diffs.most_common(8):
    print(f"  实际-声明 = {diff:>8}: {n} 个")

print("\n=== ddece4375134faa154cd693fe53a6d48 的所有变体（各目录）===")
stem = "ddece4375134faa154cd693fe53a6d48"
for p in WX.rglob(f"{stem}*"):
    if p.is_file():
        b = p.read_bytes()[:16]
        dec = int.from_bytes(b[10:12], "little") if len(b) >= 16 else -1
        print(f"  {p.stat().st_size:>9} B  声明={dec:>8}  {p.relative_to(WX)}")

print("\n=== 顺带：其它 69 个会话里的 .dat 变体命名规律 ===")
suffixes = Counter()
for _, _, p in rows:
    n = p.name
    suffixes[n[n.find("_"):] if "_" in n else "(无后缀)"] += 1
for s, n in suffixes.most_common(6):
    print(f"  {s:<12} {n} 个")
