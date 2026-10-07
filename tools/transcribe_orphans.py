"""补转写：把 `voices/raw/*.bin`（孤儿语音，自己发出的）解码并转写，回填 messages.transcript。

为什么需要它（2026-10-07 实测）：
  * `scripts/transcribe_pending.py` 找的是 `voices/<msg_id>.wav` —— 那是**收到的语音**的落盘名；
    孤儿语音（自己发出的）落在 `voices/raw/<会话hash>_<毫秒>.bin`，并在落库时用
    `msg_id = vt-<stem>`；两者命名不同，所以补转脚本对孤儿语音永远 skip。
  * 本工具按 `vt-<stem>` 找到对应消息行，解码 raw 里的 .bin → wav → ASR → 回填。

只读 raw 里的语音文件 + 写自己的消息库（`transcript` 列）；不联网（除非调 ASR 服务）。
用法：
    python tools/transcribe_orphans.py --dry-run     # 只看会处理哪些
    python tools/transcribe_orphans.py               # 真跑（需要 ASR 服务可用）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer.asr import ASRClient  # noqa: E402
from consumer.voice import decode_to_wav  # noqa: E402
from paths import data_root  # noqa: E402

PROJECT_DIR = data_root()


def main() -> int:
    ap = argparse.ArgumentParser(description="补转孤儿语音（voices/raw/*.bin → transcript）")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不转写、不写库")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    db = PROJECT_DIR / "data" / "messages.db"
    raw_dir = PROJECT_DIR / "data" / "voices" / "raw"
    if not db.is_file():
        print(f"找不到消息库: {db}")
        return 2
    if not raw_dir.is_dir():
        print(f"找不到语音目录: {raw_dir}")
        return 2

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # 待处理：转录为空 或 待转写 的孤儿语音
    rows = conn.execute(
        """SELECT msg_id, transcript, received_at, group_name FROM messages
           WHERE msg_id LIKE 'vt-%'
             AND (transcript IS NULL OR transcript = '' OR transcript LIKE '[待转写]%')
           ORDER BY received_at DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()
    print(f"待处理的孤儿语音: {len(rows)} 条")

    # 用 stem 找 raw 里的 .bin（可能带 .claimed 后缀）
    def find_bin(stem: str) -> Path | None:
        for cand in (raw_dir / f"{stem}.bin", raw_dir / f"{stem}.bin.claimed"):
            if cand.is_file():
                return cand
        return None

    todo: list[tuple[sqlite3.Row, Path]] = []
    for r in rows:
        stem = str(r["msg_id"])[len("vt-"):]
        p = find_bin(stem)
        mark = "✅ 有文件" if p else "❌ 文件不在（无法补转）"
        print(f"  {r['msg_id'][:40]:<42} {mark}")
        if p:
            todo.append((r, p))

    if not todo:
        print("\n没有可补转的语音（文件都不在了）。")
        conn.close()
        return 0
    print(f"\n可补转: {len(todo)} 条")
    if args.dry_run:
        print("（--dry-run：未解码、未调 ASR、未写库）")
        conn.close()
        return 0

    import yaml
    import paths
    cfg = yaml.safe_load(paths.config_path().read_text(encoding="utf-8")) or {}
    asr_cfg = cfg.get("asr") or {}
    if not asr_cfg.get("enabled") or not asr_cfg.get("url"):
        print("ASR 未启用/未配置（config.yaml 的 asr.enabled / asr.url），无法补转。")
        conn.close()
        return 2
    asr = ASRClient(url=asr_cfg["url"], timeout_s=float(asr_cfg.get("timeout_s") or 120))

    ok = fail = 0
    for r, bin_path in todo:
        stem = str(r["msg_id"])[len("vt-"):]
        wav = raw_dir / f"{stem}.wav"
        if not decode_to_wav(bin_path, wav):
            print(f"  {r['msg_id'][:40]} 解码失败")
            fail += 1
            continue
        text = asr.transcribe(wav)
        if text:
            conn.execute("UPDATE messages SET transcript = ? WHERE msg_id = ?",
                         (text, r["msg_id"]))
            conn.commit()
            print(f"  {r['msg_id'][:40]} OK: {text[:60]}")
            ok += 1
        else:
            print(f"  {r['msg_id'][:40]} ASR 返回空")
            fail += 1
    print(f"\n完成：成功 {ok}，失败 {fail}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
