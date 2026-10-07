"""内存验证：微信进程的**可读内存**里有没有"解密后的"语音/图片数据。

目的（2026-10-07 用户批准）：判断"自研 DLL + 内存截获"这条路是否可行。
  * 若内存里能找到 SILK 音频（`\\x02#!SILK`），说明微信确实在某处持有明文音频
    → 内存截获/dump 路线成立，值得投入写 DLL；
  * 若找不到，则连自研 DLL 也很难拿到，应停止在这条路上投入。
顺带也搜图片头（JPEG/PNG），验证"图片解密后的明文是否在内存里"。

**只读**：只调用 OpenProcess(PROCESS_QUERY_INFORMATION|PROCESS_VM_READ) + VirtualQueryEx
+ ReadProcessMemory，绝不 WriteProcessMemory、不挂起进程、不改任何内存/文件。
用法：
    python tools/scan_wechat_memory.py                 # 扫描所有 Weixin 进程
    python tools/scan_wechat_memory.py --pid 1234      # 只扫指定进程
    python tools/scan_wechat_memory.py --max-mb 4096   # 每进程最多读多少 MB（默认 2 GB）
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import re
import sys
import time
from ctypes import wintypes
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = {0x02, 0x04, 0x20, 0x40}      # RO / RW / EXEC_RO / EXEC_RW


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("BaseAddress", wt.LPVOID),
                ("AllocationBase", wt.LPVOID),
                ("AllocationProtect", wt.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD),
                ("Protect", wt.DWORD),
                ("Type", wt.DWORD)]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, wt.LPCVOID,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, wt.LPCVOID, wt.LPVOID,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CloseHandle.argtypes = [wt.HANDLE]

# 特征签名：(名字, 字节, 说明)
SIGS: list[tuple[str, bytes, str]] = [
    ("SILK", b"\x02#!SILK", "微信语音（silk）明文头 —— 关键目标"),
    ("SILK2", b"#!SILK", "silk 明文头（无前导 02）"),
    ("AMR", b"#!AMR", "AMR 音频头"),
    ("JPEG", b"\xff\xd8\xff\xe0", "JPEG（JFIF）"),
    ("JPEG2", b"\xff\xd8\xff\xdb", "JPEG（量化表开头）"),
    ("PNG", b"\x89PNG\r\n\x1a\n", "PNG"),
    ("GIF", b"GIF89a", "GIF"),
    ("ZSTD", b"\x28\xb5\x2f\xfd", "zstd 压缩块（微信新库用它压消息）"),
]


def find_processes(pattern: str = "Weixin") -> list[tuple[int, str]]:
    """列出进程（按名字模糊匹配）：用 tasklist，避免依赖第三方库。"""
    import subprocess
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                         capture_output=True, text=True, encoding="gbk", errors="replace")
    procs = []
    for line in (out.stdout or "").splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and re.search(pattern, parts[0], re.I):
            try:
                procs.append((int(parts[1]), parts[0]))
            except ValueError:
                continue
    return procs


def scan_pid(pid: int, name: str, max_bytes: int, ctx: int = 64) -> dict:
    res = {"pid": pid, "name": name, "read": 0, "regions": 0, "hits": []}
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        res["error"] = f"OpenProcess 失败 err={ctypes.get_last_error()}（多半权限不够，需管理员）"
        return res
    try:
        mbi = MEMORY_BASIC_INFORMATION()
        addr = 0
        buf = ctypes.create_string_buffer(1 << 20)      # 1 MB 缓冲
        got = ctypes.c_size_t(0)
        while res["read"] < max_bytes:
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
                off = 0
                while off < size and res["read"] < max_bytes:
                    n = min(len(buf), size - off)
                    if k32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n,
                                             ctypes.byref(got)) and got.value:
                        chunk = buf.raw[:got.value]
                        res["read"] += got.value
                        for sname, sig, _desc in SIGS:
                            start = 0
                            while True:
                                i = chunk.find(sig, start)
                                if i < 0:
                                    break
                                lo = max(0, i - 8)
                                res["hits"].append({
                                    "sig": sname,
                                    "addr": base + off + i,
                                    "preview": chunk[lo:i + ctx].hex(),
                                })
                                start = i + 1
                                if len(res["hits"]) > 500:      # 上限，防爆
                                    break
                    else:
                        break
                    off += got.value or n
            res["regions"] += 1
            addr = base + size
            if addr > 0x7FFFFFFFFFFF:
                break
    finally:
        k32.CloseHandle(h)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--max-mb", type=int, default=2048, help="每进程最多读 MB（默认 2048）")
    ap.add_argument("--pattern", default="Weixin")
    args = ap.parse_args()

    procs = [(args.pid, "?")] if args.pid else find_processes(args.pattern)
    if not procs:
        print("没找到微信进程（--pattern 默认 Weixin）")
        return 2
    print(f"目标进程 {len(procs)} 个：{procs}")
    print(f"每进程读取上限: {args.max_mb} MB\n")

    total = {name: 0 for name, _s, _d in SIGS}
    for pid, name in procs:
        t0 = time.time()
        r = scan_pid(pid, name, args.max_mb * 1024 * 1024)
        dt = time.time() - t0
        if r.get("error"):
            print(f"[{pid} {name}] {r['error']}")
            continue
        counts: dict[str, int] = {}
        for h in r["hits"]:
            counts[h["sig"]] = counts.get(h["sig"], 0) + 1
        print(f"[{pid} {name}] 读 {r['read'] / 1024 / 1024:.0f} MB / {r['regions']} 区，"
              f"耗时 {dt:.1f}s")
        if not counts:
            print("    没有命中任何特征")
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            desc = next(d for n, _s, d in SIGS if n == k)
            print(f"    ✅ {k:<6} {v:>5} 处   ({desc})")
            total[k] = total.get(k, 0) + v
        # 打印前几个 SILK 命中的上下文（关键证据）
        for h in [x for x in r["hits"] if x["sig"].startswith("SILK")][:3]:
            print(f"       SILK @0x{h['addr']:X}: {h['preview'][:120]}…")

    print("\n=== 汇总 ===")
    for k, v in sorted(total.items(), key=lambda kv: -kv[1]):
        if v:
            print(f"  {k:<6} {v}")
    silk = total.get("SILK", 0) + total.get("SILK2", 0)
    print()
    if silk:
        print("🎯 内存里找到 SILK 明文音频 → 内存截获/DLL 路线**有可行性**")
    else:
        print("❌ 内存里没找到 SILK 明文音频 → 自研 DLL 也很难拿到语音，建议停止这条路线")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
