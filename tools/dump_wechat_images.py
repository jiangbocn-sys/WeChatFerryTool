"""从微信进程内存里提取**能打开的**图片（A 方案：图片落地）。

已验证的事实（2026-10-07）：
  * 微信主进程内存里有**明文 JPEG/PNG**（scan_wechat_memory.py 命中 94/14 处）；
  * dump 出来的两张能正常打开（132x132、160x156）→ 说明"读内存取图"可行；
  * hook 的 `Decode_Pic` 是**空壳**（返回 success 但不产出文件），不能用。

本脚本做得更严谨：
  1. 找 JPEG/PNG 头后**按结束符截断**（JPEG 用 FFD9，PNG 用 IEND），避免截断/拼接；
  2. 用 PIL **逐个校验**，只保留能真正打开的；
  3. 按分辨率/大小去重，输出到 .wft-memimages/。

只读内存；不写进程、不改微信数据。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from tools.scan_wechat_memory import (PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,  # noqa: E402
                                      find_processes, k32)
from tools.dump_wechat_memory_media import iter_regions, read_mem  # noqa: E402

OUT = Path(r"D:\projects\.wft-memimages")
JPEG_SOI = b"\xff\xd8\xff"
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def validate(path: Path) -> tuple[int, int, int] | None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()
            return im.size[0], im.size[1], len(path.read_bytes())
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--max-mb", type=int, default=2048)
    ap.add_argument("--max-images", type=int, default=40)
    ap.add_argument("--min-size", type=int, default=2000, help="小于此字节数的丢弃")
    args = ap.parse_args()

    procs = ([args.pid] if args.pid else [p for p, _n in find_processes("Weixin")])
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    seen: set[str] = set()
    kept = tried = 0
    for pid in procs:
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            print(f"[{pid}] 打不开（err={ctypes.get_last_error()}）")
            continue
        print(f"[{pid}] 扫描…")
        try:
            for base, size in iter_regions(h, args.max_mb * 1024 * 1024):
                if kept >= args.max_images:
                    break
                off = 0
                while off < size and kept < args.max_images:
                    n = min(8 << 20, size - off)
                    chunk = read_mem(h, base + off, n)
                    if not chunk:
                        break
                    for magic, end, ext in ((JPEG_SOI, b"\xff\xd9", "jpg"),
                                            (PNG_SIG, b"IEND\xaeB`\x82", "png")):
                        start = 0
                        while kept < args.max_images:
                            i = chunk.find(magic, start)
                            if i < 0:
                                break
                            start = i + len(magic)
                            # 多读一段以保证拿到结束符（跨区边界时）
                            more = read_mem(h, base + off + i, 8 << 20)
                            j = more.find(end, len(magic))
                            if j < 0:
                                continue
                            blob = more[:j + len(end)]
                            if len(blob) < args.min_size:
                                continue
                            tried += 1
                            tmp = OUT / f"cand_{pid}_{base + off + i:X}.{ext}"
                            tmp.write_bytes(blob)
                            info = validate(tmp)
                            if not info:
                                tmp.unlink(missing_ok=True)
                                continue
                            w, hh, sz = info
                            digest = hashlib.md5(blob).hexdigest()[:8]
                            if digest in seen:
                                tmp.unlink(missing_ok=True)
                                continue
                            seen.add(digest)
                            final = OUT / f"{w}x{hh}_{digest}.{ext}"
                            tmp.rename(final)
                            kept += 1
                            print(f"  ✅ {w}x{hh}  {sz} B  -> {final.name}")
                    off += n
        finally:
            k32.CloseHandle(h)
        if kept >= args.max_images:
            break

    print(f"\n=== 提取到 {kept} 张可用图片（候选 {tried} 个）→ {OUT} ===")
    if kept == 0:
        print("提示：微信可能尚未把图片载入内存（在微信里滚动浏览该群后再试）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
