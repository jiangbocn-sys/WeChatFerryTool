"""内存验证第二步：把命中的 SILK / 图片段**完整 dump 出来**，确认是真数据。

第一步（scan_wechat_memory.py）已在微信主进程内存中找到明文 SILK 头与 JPEG/PNG 头。
本脚本进一步：
  1. 定位 SILK 头后，按 silk 结构估算长度并把整段 dump 到文件；
  2. 尝试用 pysilk 解码成 WAV —— **能解出 PCM 才算真的拿到语音**；
  3. 图片则 dump 出 JPEG/PNG（按 EOI/IEND 结束符截断），验证能被识别。

只读内存；输出的样本放在 `.wft-memdump/`（不在账号目录，也不提交）。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import re
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from tools.scan_wechat_memory import (MEMORY_BASIC_INFORMATION, MEM_COMMIT, PAGE_GUARD,  # noqa: E402
                                      PAGE_NOACCESS, PROCESS_QUERY_INFORMATION,
                                      PROCESS_VM_READ, READABLE, k32)

OUT = Path(r"D:\projects\.wft-memdump")
SILK_MAGIC = b"\x02#!SILK"


def open_proc(pid: int):
    return k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)


def iter_regions(h, max_bytes: int):
    """产出可读内存区 (base, size)，按调用方预算截断。"""
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    budget = 0
    while budget < max_bytes:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                  ctypes.sizeof(mbi)):
            break
        base = ctypes.cast(mbi.BaseAddress, ctypes.c_void_p).value or 0
        size = mbi.RegionSize
        if size == 0:
            break
        if (mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD)
                and mbi.Protect not in (0, PAGE_NOACCESS)
                and (mbi.Protect & 0xFF) in READABLE):
            yield base, size
            budget += size
        addr = base + size
        if addr > 0x7FFFFFFFFFFF:
            break


def read_mem(h, addr: int, n: int) -> bytes:
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got))
    return buf.raw[:got.value] if ok and got.value else b""


def silk_len(buf: bytes) -> int:
    """估算 silk 总长度：silk 头 10 字节 + 每 20ms 帧(前 2 字节为长度)累加。

    silk v3 文件结构：`#!SILK_V3` 头后，每个 packet 前 2 字节小端 = 该包长度。
    """
    i = 10                       # 跳过 \x02#!SILK_V3
    total = i
    while i + 2 <= len(buf):
        n = struct.unpack_from("<H", buf, i)[0]
        if n == 0 or i + 2 + n > len(buf):
            break
        i += 2 + n
        total = i
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--max-mb", type=int, default=2048)
    ap.add_argument("--want-silk", type=int, default=5)
    ap.add_argument("--want-img", type=int, default=3)
    args = ap.parse_args()

    procs = ([args.pid] if args.pid
             else [p for p, _n in __import__("tools.scan_wechat_memory", fromlist=["x"])
                   .find_processes("Weixin")])
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    got_silk = got_img = 0
    for pid in procs:
        h = open_proc(pid)
        if not h:
            print(f"[{pid}] 打不开（err={ctypes.get_last_error()}）")
            continue
        print(f"\n[{pid}] 扫描…")
        try:
            for base, size in iter_regions(h, args.max_mb * 1024 * 1024):
                if got_silk >= args.want_silk and got_img >= args.want_img:
                    break
                # 只读区间的头部与可能有数据的部分；为省内存按 4 MB 分块
                off = 0
                while off < size:
                    n = min(4 << 20, size - off)
                    chunk = read_mem(h, base + off, n)
                    if not chunk:
                        break
                    if got_silk < args.want_silk:
                        i = chunk.find(SILK_MAGIC)
                        while i >= 0 and got_silk < args.want_silk:
                            # 多读一点，保证整段在里面
                            more = read_mem(h, base + off + i, 1 << 20)
                            if len(more) >= 12:
                                ln = silk_len(more)
                                if ln > 20:
                                    p = OUT / f"silk_{pid}_{base + off + i:X}.silk"
                                    p.write_bytes(more[:ln])
                                    print(f"  🎯 SILK {ln} B -> {p.name}")
                                    got_silk += 1
                            i = chunk.find(SILK_MAGIC, i + 1)
                    if got_img < args.want_img:
                        for magic, end, ext in ((b"\xff\xd8\xff", b"\xff\xd9", "jpg"),
                                                (b"\x89PNG\r\n\x1a\n", b"IEND\xaeB`\x82", "png")):
                            j = chunk.find(magic)
                            if j < 0:
                                continue
                            more = read_mem(h, base + off + j, 4 << 20)
                            k = more.find(end, len(magic))
                            if k > 0:
                                p = OUT / f"img_{pid}_{base + off + j:X}.{ext}"
                                p.write_bytes(more[:k + len(end)])
                                print(f"  🖼  {ext.upper()} {k + len(end)} B -> {p.name}")
                                got_img += 1
                            break
                    off += n
        finally:
            k32.CloseHandle(h)
        if got_silk >= args.want_silk and got_img >= args.want_img:
            break

    print(f"\n=== dump 结果：SILK {got_silk} 个 / 图片 {got_img} 个 → {OUT} ===")
    # 用 pysilk 验证 SILK 是否真能解码
    try:
        import pysilk
    except ImportError:
        print("（没装 pysilk，跳过解码验证）")
        pysilk = None
    if pysilk:
        for p in sorted(OUT.glob("*.silk")):
            wav = p.with_suffix(".wav")
            try:
                with open(p, "rb") as fi, open(wav, "wb") as fo:
                    pysilk.decode(fi, fo, 24000)
                print(f"  ✅ {p.name} 解码成功 -> {wav.name} ({wav.stat().st_size} B PCM)")
            except Exception as e:  # noqa: BLE001
                print(f"  ❌ {p.name} 解码失败: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
