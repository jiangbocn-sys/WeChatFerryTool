"""补转写：把**还留在微信 VoiceTemp 里**的语音文件，配对回消息并转写。

为什么需要它（2026-10-07 实测）
------------------------------
当天 32 条语音里只有 9 条转写成功；`未捕获到语音文件` 16 条、`[解码失败]` 1 条、空 6 条。
排查结论：**ASR（whisper）服务是好的**（`/health` 200），问题在**抓取**——
`VoiceTap` 的 `wait_for()` 只有 20 秒窗口，而语音文件是微信**按需落盘**的
（要等你在微信里点一下播放，或过一会儿才出现），于是超时放弃。
好消息：这些文件往往**还在** `VoiceTemp` 里。

配对原理
--------
`<cache>/<月>/Message/<会话md5>/VoiceTemp/<序号>_<秒级时间戳>`：
  * 目录名 = `md5(conv_id)` → 能反查是哪个会话（用 messages 表 + labels 建映射）
  * 文件名后缀 = 该语音的秒级时间戳 → 和库里 `msg_type=34` 消息的 `received_at` 配对

用法：
    python tools/backfill_voices.py --dry-run       # 只看能配对多少（不写库、不联网）
    python tools/backfill_voices.py                 # 真补（需要 ASR 服务在）
    python tools/backfill_voices.py --tolerance 90  # 放宽配对容差（秒，默认 60）
"""
from __future__ import annotations

import argparse
import hashlib
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
from consumer.voice_tap import _looks_like_voice_bin  # noqa: E402
from paths import config_path, data_root, wechat_files_root  # noqa: E402

PROJECT_DIR = data_root()


def main() -> int:
    ap = argparse.ArgumentParser(description="把 VoiceTemp 里残留的语音配对回消息并转写")
    ap.add_argument("--dry-run", action="store_true", help="只配对，不写库不联网")
    ap.add_argument("--tolerance", type=int, default=60, help="时间戳配对容差（秒）")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    db = PROJECT_DIR / "data" / "messages.db"
    if not db.is_file():
        print(f"找不到消息库: {db}")
        return 2
    acc_root = wechat_files_root()
    if not acc_root:
        print("找不到微信数据目录（xwechat_files）")
        return 2
    print(f"微信数据根: {acc_root}")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # 待补的语音消息（未转写/捕获失败/解码失败/空）
    pend = conn.execute(
        """SELECT msg_id, group_name, received_at, transcript FROM messages
           WHERE msg_type = 34
             AND (transcript IS NULL OR transcript = '' OR transcript LIKE '[未捕获%'
                  OR transcript LIKE '[解码失败]%' OR transcript LIKE '[待转写]%')
           ORDER BY received_at DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()
    print(f"库里待补语音: {len(pend)} 条")
    if not pend:
        conn.close()
        return 0

    # conv_id → md5（用于把 VoiceTemp 目录名映射回会话）
    gids = {r["group_name"] for r in pend if r["group_name"]}
    want_hash = {hashlib.md5(g.encode("utf-8")).hexdigest(): g for g in gids}
    print(f"涉及会话: {len(want_hash)} 个")

    # 扫 VoiceTemp：按 conv 收集候选文件（名 → (路径, 时间戳)）
    cands: dict[str, list[tuple[Path, int]]] = {}
    for account in [d for d in acc_root.iterdir() if d.is_dir()]:
        cache = account / "cache"
        if not cache.is_dir():
            continue
        for month in cache.iterdir():
            msgdir = month / "Message"
            if not msgdir.is_dir():
                continue
            for cdir in msgdir.iterdir():
                gid = want_hash.get(cdir.name)
                vd = cdir / "VoiceTemp"
                if gid is None or not vd.is_dir():
                    continue
                for f in vd.iterdir():
                    if not f.is_file() or not _looks_like_voice_bin(f.name):
                        continue
                    try:
                        ts = int(f.name.split("_")[-1])
                        size = f.stat().st_size
                    except (OSError, ValueError):
                        continue
                    cands.setdefault(gid, []).append((f, ts))
                    print(f"  候选 [{gid[:20]}] {f.name}  {size} B")

    # 配对：同会话、时间戳差 <= tolerance
    pairs: list[tuple[sqlite3.Row, Path]] = []
    used: set[Path] = set()
    for r in pend:
        gid = r["group_name"]
        if not gid:
            continue
        best: tuple[int, Path] | None = None
        for f, ts in cands.get(gid, []):
            if f in used:
                continue
            d = abs(ts - int(r["received_at"] or 0))
            if d <= args.tolerance and (best is None or d < best[0]):
                best = (d, f)
        if best:
            used.add(best[1])
            pairs.append((r, best[1]))
    print(f"\n可配对: {len(pairs)} / {len(pend)}")
    for r, f in pairs:
        print(f"  msg={r['msg_id']:<22} {r['group_name'][:18]:<20} "
              f"差值={abs(int(f.name.split('_')[-1]) - int(r['received_at'] or 0)):>3}s  <- {f.name}")

    if args.dry_run:
        print("\n（--dry-run：未解码、未调 ASR、未写库）")
        conn.close()
        return 0
    if not pairs:
        conn.close()
        return 0

    import yaml
    cfg = yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
    asr_cfg = cfg.get("asr") or {}
    if not asr_cfg.get("enabled") or not asr_cfg.get("url"):
        print("ASR 未启用/未配置（config.yaml 的 asr.enabled / asr.url）")
        conn.close()
        return 2
    asr = ASRClient(url=asr_cfg["url"], timeout_s=float(asr_cfg.get("timeout_s") or 120))
    import shutil
    voices = PROJECT_DIR / "data" / "voices"
    voices.mkdir(parents=True, exist_ok=True)

    ok = fail = 0
    for r, src in pairs:
        dest = voices / f"{r['msg_id']}{src.suffix or '.bin'}"
        try:
            shutil.copy2(src, dest)
        except OSError as e:
            print(f"  复制失败 {src.name}: {e}")
            fail += 1
            continue
        wav = dest.with_suffix(".wav")
        if not decode_to_wav(dest, wav):
            print(f"  {r['msg_id']} 解码失败")
            fail += 1
            continue
        text = asr.transcribe(wav)
        if text:
            conn.execute("UPDATE messages SET transcript = ? WHERE msg_id = ?", (text, r["msg_id"]))
            conn.commit()
            print(f"  {r['msg_id']} OK: {text[:60]}")
            ok += 1
        else:
            print(f"  {r['msg_id']} ASR 返回空")
            fail += 1
    print(f"\n完成：成功 {ok}，失败 {fail}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
