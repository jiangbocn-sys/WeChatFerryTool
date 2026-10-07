"""系统测试：现有 keys 到底还能打开哪些微信库（判断密钥是否失效）。

动机：想读微信"语音转文字"的结果（它写在 message_0.db 里），但单库试读失败。
需要先分清是"密钥全失效"还是"只有 message_0.db 的密钥轮换了"。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer import wechat_offline as wo  # noqa: E402


def main() -> int:
    dbdir = wo.find_db_dir()
    if not dbdir:
        print("找不到微信 db 目录")
        return 2
    print(f"库目录: {dbdir}")

    keys = wo.key_candidates()
    print(f"口令候选: {len(keys)} 个\n")

    # 挑几个代表性的库
    dbs: list[Path] = []
    for pat in ("**/message_*.db", "**/contact.db", "**/session.db", "**/media_*.db",
                "**/head_image.db", "**/*.db"):
        for p in sorted(dbdir.glob(pat)):
            if p.name.endswith(("-wal", "-shm")) or p in dbs:
                continue
            dbs.append(p)
        if len(dbs) >= 6:
            break

    for db in dbs[:6]:
        print(f"=== {db.relative_to(dbdir)}  ({db.stat().st_size} B) ===")
        work = wo.make_workdir()
        snap = wo.snapshot(db, work)
        ok_key = None
        tried = 0
        for k in keys:
            tried += 1
            conn = None
            try:
                conn = wo.open_db(snap, [k])
                if conn is not None:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                    ok_key = (k, n)
                    conn.close()
                    break
            except Exception:  # noqa: BLE001
                pass
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
        if ok_key:
            print(f"  ✅ 打开成功（试了 {tried}/{len(keys)} 个 key，{ok_key[1]} 张表）")
        else:
            print(f"  ❌ {len(keys)} 个 key 全部失败")
        print()

    # keys 的时间范围
    import json
    import time
    kp = wo.keys_path_default()
    recs = []
    for line in kp.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    if recs:
        ts = [r.get("ts") or 0 for r in recs]
        print(f"keys 采集时间: {time.strftime('%Y-%m-%d %H:%M', time.localtime(min(ts)))}"
              f" ~ {time.strftime('%Y-%m-%d %H:%M', time.localtime(max(ts)))}"
              f"（共 {len(recs)} 条）")
        print(f"现在时间:     {time.strftime('%Y-%m-%d %H:%M')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
