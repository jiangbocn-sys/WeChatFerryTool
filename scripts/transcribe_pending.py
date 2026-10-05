"""批量补转写：把 data/voices/ 里已存档但未成功转写的语音重新走一遍 ASR。

用法：
  python scripts/transcribe_pending.py           # 补转所有 [待转写]/[解码失败]/空 的语音
  python scripts/transcribe_pending.py --limit 10

适用场景：Mac whisper 服务当时不在线 / 未配置，之后服务就绪了统一补转。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.stdout.reconfigure(encoding="utf-8")

import yaml

from consumer.asr import ASRClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    cfg = yaml.safe_load(open(PROJECT_DIR / "config.yaml", encoding="utf-8"))
    asr_cfg = cfg.get("asr") or {}
    asr = ASRClient(
        url=str(asr_cfg.get("url") or ""),
        timeout_s=float(asr_cfg.get("timeout_s", 60)),
        enabled=bool(asr_cfg.get("enabled", True)),
    )
    if not asr.ready:
        print("asr.url 未配置，无法补转。请先在 config.yaml 里填 Mac 服务地址。")
        sys.exit(1)

    voices_dir = PROJECT_DIR / "data" / "voices"
    db_path = PROJECT_DIR / "data" / "messages.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT msg_id, transcript FROM messages
        WHERE msg_type = 34
          AND (transcript IS NULL OR transcript = '' OR transcript LIKE '[待转写]%' OR transcript LIKE '[解码失败]%')
        ORDER BY id DESC LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    ok = fail = skip = 0
    for r in rows:
        wav = voices_dir / f"{r['msg_id']}.wav"
        if not wav.exists():
            skip += 1
            continue
        print(f"转写 {r['msg_id']} ... ", end="", flush=True)
        text = asr.transcribe(wav)
        if text:
            conn.execute("UPDATE messages SET transcript = ? WHERE msg_id = ?", (text, r["msg_id"]))
            conn.commit()
            print(f"OK: {text[:50]}")
            ok += 1
        else:
            print("失败")
            fail += 1
    conn.close()
    print(f"\n完成：成功 {ok}，失败 {fail}，无 wav 文件跳过 {skip}")


if __name__ == "__main__":
    main()