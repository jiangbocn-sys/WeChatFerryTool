"""针对性内存扫描：只找**大图**（真聊天图片）与**可解码音频**，判断常驻扫描是否可用。

2026-10-07 首次扫描的教训：命中 94 个 JPEG / 14 个 PNG，但筛出来的多是
UI 素材（emoji 面板 768×1392）、气泡背景、头像（64×64/128×128）——
所以"能读内存"不等于"能拿到聊天图"。本脚本按**尺寸与体积**严格过滤：

  图片：最短边 >= 200 且 >= 20 KB（聊天图/截图基本满足；头像/图标/表情会被排除）
  音频：定位所有 `#!SILK` 起点，按"2 字节长度 + payload"重建，**用 pysilk 实解**才算数

只读内存；样本写 .wft-deepscan/。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import struct
import sys
import wave
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from tools.dump_wechat_memory_media import iter_regions, read_mem  # noqa: E402
from tools.scan_wechat_memory import (PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,  # noqa: E402
                                      find_processes, k32)

OUT = Path(r"D:\projects\.wft-deepscan")
SILK = b"#!SILK"
JPEG = b"\xff\xd8\xff"
PNG = b"\x89PNG\r\n\x1a\n"


def img_ok(blob: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(BytesIO(blob)) as im:
            im.load()
            return im.size
    except Exception:  # noqa: BLE001
        return None


def silk_try(payload: bytes) -> bytes | None:
    """把 payload 当"2 字节长度 + 数据"的帧序列重建，返回能解出的 PCM。"""
    out = bytearray()
    i = 0
    frames = 0
    while i + 2 <= len(payload):
        n = struct.unpack_from("<H", payload, i)[0]
        if n == 0 or n > 1024 or i + 2 + n > len(payload):
            break
        out += payload[i + 2:i + 2 + n]
        i += 2 + n
        frames += 1
    if frames < 20:
        return None
    try:
        import pysilk
        fo = BytesIO()
        pysilk.decode(BytesIO(bytes(out)), fo, 24000)
        pcm = fo.getvalue()
        return pcm if len(pcm) > 4000 else None
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-mb", type=int, default=1024)
    ap.add_argument("--min-side", type=int, default=200)
    ap.add_argument("--min-bytes", type=int, default=20000)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    procs = [p for p, _n in find_processes("Weixin")]
    print(f"扫描 {len(procs)} 个进程，过滤条件：最短边>={args.min_side} 且 >={args.min_bytes} B\n")
    seen: set[str] = set()
    big_imgs: list[tuple[int, int, int, Path]] = []
    audio: list[tuple[float, Path]] = []
    for pid in procs:
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            continue
        print(f"[{pid}] …")
        try:
            for base, size in iter_regions(h, args.max_mb * 1024 * 1024):
                off = 0
                while off < size:
                    n = min(8 << 20, size - off)
                    chunk = read_mem(h, base + off, n)
                    if not chunk:
                        break
                    # --- 图片 ---
                    for magic, end, ext in ((JPEG, b"\xff\xd9", "jpg"),
                                            (PNG, b"IEND\xaeB`\x82", "png")):
                        start = 0
                        while True:
                            i = chunk.find(magic, start)
                            if i < 0:
                                break
                            start = i + len(magic)
                            more = read_mem(h, base + off + i, 12 << 20)
                            j = more.find(end, len(magic))
                            if j < 0:
                                continue
                            blob = more[:j + len(end)]
                            if len(blob) < args.min_bytes:
                                continue
                            d = hashlib.md5(blob).hexdigest()[:8]
                            if d in seen:
                                continue
                            size_wh = img_ok(blob)
                            if not size_wh or min(size_wh) < args.min_side:
                                continue
                            seen.add(d)
                            p = OUT / f"{size_wh[0]}x{size_wh[1]}_{d}.{ext}"
                            p.write_bytes(blob)
                            big_imgs.append((size_wh[0], size_wh[1], len(blob), p))
                            print(f"  🖼 大图 {size_wh[0]}x{size_wh[1]} {len(blob)} B -> {p.name}")
                    # --- 音频 ---
                    k = 0
                    while True:
                        i = chunk.find(SILK, k)
                        if i < 0:
                            break
                        k = i + 5
                        payload = read_mem(h, base + off + i + 9, 1 << 20)   # 跳过 #!SILK_V3
                        pcm = silk_try(payload)
                        if not pcm:
                            continue
                        d = hashlib.md5(pcm).hexdigest()[:8]
                        if d in seen:
                            continue
                        seen.add(d)
                        wav = OUT / f"audio_{d}.wav"
                        with wave.open(str(wav), "wb") as w:
                            w.setnchannels(1)
                            w.setsampwidth(2)
                            w.setframerate(24000)
                            w.writeframes(pcm)
                        secs = len(pcm) / 2 / 24000
                        audio.append((secs, wav))
                        print(f"  🔊 音频 {secs:.1f}s ({len(pcm)} B PCM) -> {wav.name}")
                    off += n
        finally:
            k32.CloseHandle(h)

    print(f"\n=== 结果 ===")
    print(f"  大图（最短边>={args.min_side}）: {len(big_imgs)} 张")
    for w, hh, sz, p in sorted(big_imgs, key=lambda t: -t[0] * t[1])[:10]:
        print(f"    {w}x{hh}  {sz:>9} B  {p.name}")
    print(f"  可解码音频: {len(audio)} 段")
    for secs, p in sorted(audio, reverse=True)[:5]:
        print(f"    {secs:.1f}s  {p.name}")
    print(f"\n样本目录: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
