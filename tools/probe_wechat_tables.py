"""列出微信 message_0.db / message_fts.db 的全部表名，定位「语音转文字」存哪。

背景：语音行的 message_content 解压后只有 <voicemsg .../>（不含转写），
所以转写结果一定在别处 —— 可能是独立表，或 message_fts.db（全文索引）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer import wechat_offline as wo  # noqa: E402


def open_db_first_key(db: Path):
    for i, k in enumerate(wo.key_candidates()):
        work = wo.make_workdir()
        snap = wo.snapshot(db, work)
        conn = wo.open_db(snap, [k])
        if conn is not None:
            return conn, i
    return None, None


def main() -> int:
    dbdir = wo.find_db_dir()
    for rel in ("message/message_0.db", "message/message_fts.db",
                "message/message_resource.db"):
        db = dbdir / rel
        if not db.is_file():
            print(f"\n### {rel}: 不存在")
            continue
        conn, ki = open_db_first_key(db)
        print(f"\n### {rel}  (key #{ki})")
        if conn is None:
            print("   打不开")
            continue
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")]
        print(f"   共 {len(names)} 个对象：")
        for n in names:
            try:
                c = conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
            except Exception:  # noqa: BLE001
                c = "?"
            mark = ""
            if any(k in n.lower() for k in ("voice", "trans", "asr", "audio", "speech", "text")):
                mark = "   <<< 可能相关"
            print(f"     {n:<46} {c}{mark}")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
