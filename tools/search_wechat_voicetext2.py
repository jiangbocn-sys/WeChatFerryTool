"""最终判定：微信本地库是否保存了「语音转文字」的结果。

做法（不依赖任何易碎的输出/排序）：
  1. 打开 message_0.db / message_fts.db / media_0.db / message_resource.db / weclaw.db；
  2. 把**所有表**的**所有文本/BLOB 列**解压后拼成一个大字符串，搜已知语音内容；
  3. 同时单独统计"语音行（local_type=34）的 message_content 是否含转写文本"。
只读快照。
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer import wechat_offline as wo  # noqa: E402

try:
    import zstandard as zstd
except ImportError:
    zstd = None

# 已知语音转写内容（我们自己转出来的）+ 你刚用微信转文字的那条可能含的词
PROBES = ["神经暗洞", "来生事", "逢河", "赤窑", "找箱子", "行李箱"]
OUT = Path(r"D:\projects\.wft-voicesearch.txt")


def unz(b) -> str:
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    if isinstance(b, int):
        return ""                      # 数值列（create_time 等）当作无文本
    if not b:
        return ""
    if isinstance(b, (bytes, bytearray)) and zstd is not None and bytes(b[:4]) == b"\x28\xb5\x2f\xfd":
        try:
            return zstd.ZstdDecompressor().decompressobj().decompress(bytes(b)).decode(
                "utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
    if isinstance(b, (bytes, bytearray)):
        try:
            return bytes(b).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
    return str(b)


def open_db_first_key(db: Path):
    for i, k in enumerate(wo.key_candidates()):
        try:
            work = wo.make_workdir()
            snap = wo.snapshot(db, work)
            conn = wo.open_db(snap, [k])
        except Exception:  # noqa: BLE001
            continue
        if conn is None:
            continue
        try:
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            return conn, i
        except Exception:  # noqa: BLE001
            continue
    return None, None


def main() -> int:
    log = []
    dbdir = wo.find_db_dir()
    msgdir = dbdir / "message"
    files = sorted(p for p in msgdir.glob("*.db") if not p.name.endswith(("-wal", "-shm")))
    files.append(dbdir / "weclaw.db")

    grand_hits = 0
    for db in files:
        if not db.is_file():
            continue
        conn, ki = open_db_first_key(db)
        log.append(f"\n### {db.name} (key #{ki})")
        if conn is None:
            log.append("  打不开")
            continue
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        log.append(f"  表数 {len(tables)}；逐个搜列内容…")
        hits = 0
        voice_rows_with_text = 0
        for t in tables:
            try:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
            except sqlite3.Error:
                continue
            if not cols:
                continue
            try:
                rows = conn.execute(
                    f'SELECT {", ".join(chr(34)+c+chr(34) for c in cols)} FROM "{t}" LIMIT 5000'
                ).fetchall()
            except sqlite3.Error:
                continue
            for r in rows:
                # 语音行：看有没有非 XML 的额外文本
                joined = " ".join(unz(v) for v in r)
                if "<voicemsg" in joined:
                    # voicemsg 之后若还有别的内容（转写）就能看出来
                    tail = joined.split("<voicemsg", 1)[0]
                    if tail.strip():
                        voice_rows_with_text += 1
                for p in PROBES:
                    if p in joined:
                        i = joined.find(p)
                        ctx = joined[max(0, i - 70):i + 90].replace("\n", " ")
                        log.append(f"  ✅ [{t}] 命中 {p!r}: …{ctx}…")
                        hits += 1
                        break
        if hits == 0:
            log.append("  （未命中任何探针）")
        log.append(f"  语音行里 <voicemsg> 之外还有文本的行数: {voice_rows_with_text}")
        grand_hits += hits
        conn.close()

    log.append(f"\n=== 总命中 {grand_hits} ===")
    text = "\n".join(log)
    OUT.write_text(text, encoding="utf-8")
    print(text[-3000:])
    print(f"\n（完整输出已写入 {OUT}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
