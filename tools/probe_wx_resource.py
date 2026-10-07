"""探针：微信的 hardlink.db / message_resource.db 能否解密、里面有没有"消息 → 本地文件"映射。

只读：把库（含 -wal）快照到临时目录再用已采口令解密，不写微信数据。
用法：python tools\probe_wx_resource.py
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = Path(r"D:\projects\WeChatFerryTool")
WXDB = Path(r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\db_storage")
TARGET_MSG = "5439543700231738180"        # 11:25 那张图（易青岚乙巳年弟子群）
LOCAL_STEM = "ddece4375134faa154cd693fe53a6d48"   # 本地 .dat 文件名（去掉后缀）

sys.path.insert(0, str(PROJ))
from consumer import wechat_offline as wo  # noqa: E402

keys = wo.key_candidates()
print(f"口令候选 {len(keys)} 个；keys 文件 = {wo.keys_path_default()}")
for k in keys[:3]:
    print("   例:", str(k)[:70])
print()

work = wo.make_workdir() / "resprobe"
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True, exist_ok=True)

for rel in ("hardlink/hardlink.db", "message/message_resource.db"):
    db = WXDB / rel
    print(f"=== {rel} ===")
    if not db.exists():
        print("   不存在\n")
        continue
    snap = wo.snapshot(db, work)
    conn = wo.open_db(snap, keys)
    if conn is None:
        print("   ✗ 口令都打不开\n")
        continue
    tabs = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print("   ✅ 打开成功；表:", tabs)
    for t in tabs:
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"   - {t} ({cnt} 行): {cols}")
        except Exception as e:  # noqa: BLE001
            print(f"   - {t}: 读列失败 {e}")
    # 找映射：按消息 id 或本地文件名查
    for t in tabs:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        # 打印一行样本，看内容长什么样
        try:
            sample = conn.execute(f"SELECT * FROM {t} LIMIT 2").fetchall()
            for s in sample[:1]:
                print(f"   样本 {t}:", [str(x)[:70] for x in s])
        except Exception:  # noqa: BLE001
            pass
        for needle, label in ((TARGET_MSG, "11:25 的消息 id"), (LOCAL_STEM, "本地文件名")):
            for c in cols:
                try:
                    rows = conn.execute(
                        f"SELECT * FROM {t} WHERE CAST({c} AS TEXT) LIKE ? LIMIT 2",
                        (f"%{needle}%",)).fetchall()
                    if rows:
                        print(f"   🎯 {t}.{c} 命中「{label}」{len(rows)} 行：")
                        for r in rows:
                            print("      ", [str(x)[:70] for x in r])
                except Exception:  # noqa: BLE001
                    pass
    conn.close()
    print()

shutil.rmtree(work, ignore_errors=True)
