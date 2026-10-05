"""语音捕获测试工具：实时监听 VoiceTemp，抓取+解码出现的语音文件。

用法：
  python scripts/voice_watch.py                # 监听全部会话的 VoiceTemp
  python scripts/voice_watch.py 49366798260@chatroom   # 只看指定会话

测试方法：在微信里播放一条历史语音（或让别人发新语音），本脚本会：
  1. 发现 VoiceTemp 里新出现的文件 → 复制到 data/voices/
  2. 尝试 silk 解码 → data/voices/*.wav
  3. 打印文件大小、解码结果
"""
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

sys.stdout.reconfigure(encoding="utf-8")

import hashlib

from consumer.voice import decode_to_wav, find_wechat_files_dir

import yaml


def all_voicetemps(account_dir: Path) -> list[Path]:
    out = []
    cache = account_dir / "cache"
    if not cache.is_dir():
        return out
    for month_dir in sorted(cache.iterdir(), reverse=True):
        msg_dir = month_dir / "Message"
        if not msg_dir.is_dir():
            continue
        for conv in msg_dir.iterdir():
            vt = conv / "VoiceTemp"
            if vt.is_dir():
                out.append(vt)
    return out


def main() -> None:
    cfg = yaml.safe_load(open(PROJECT_DIR / "config.yaml", encoding="utf-8"))
    self_wxid = (cfg.get("hook", {}).get("self_wxid") or "").strip()

    account_dir = find_wechat_files_dir(self_wxid)
    if not account_dir:
        print("找不到微信数据目录！")
        sys.exit(1)
    print(f"微信账号数据目录: {account_dir}")

    filter_conv = sys.argv[1] if len(sys.argv) > 1 else None
    if filter_conv:
        want_hash = hashlib.md5(filter_conv.encode()).hexdigest()
        print(f"只监听会话 {filter_conv} (md5={want_hash})")
        vts = [vt for vt in all_voicetemps(account_dir) if vt.parent.name == want_hash]
    else:
        vts = all_voicetemps(account_dir)
        print(f"监听全部 {len(vts)} 个 VoiceTemp 目录")

    seen: dict[str, float] = {}
    dest_dir = PROJECT_DIR / "data" / "voices"
    dest_dir.mkdir(parents=True, exist_ok=True)

    print("开始轮询（每 0.5 秒），Ctrl+C 停止...")
    print("→ 现在去微信里播放一条语音消息，观察这里的变化！")
    try:
        while True:
            for vt in vts:
                try:
                    files = [f for f in vt.iterdir() if f.is_file()]
                except OSError:
                    continue
                for f in files:
                    key = str(f)
                    if key in seen:
                        continue
                    seen[key] = time.time()
                    size = f.stat().st_size
                    print(f"\n[发现] {f}")
                    print(f"       大小: {size} bytes  首字节: {f.read_bytes()[:16]!r}")
                    # 复制出来
                    dest = dest_dir / f"{f.stem}{f.suffix or '.bin'}"
                    dest.write_bytes(f.read_bytes())
                    print(f"       已复制: {dest}")
                    # 尝试解码
                    wav = dest.with_suffix(".wav")
                    ok = decode_to_wav(dest, wav)
                    print(f"       silk 解码: {'成功 -> ' + str(wav) if ok else '失败'}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n停止。")


if __name__ == "__main__":
    main()