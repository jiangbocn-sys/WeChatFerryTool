"""事件驱动语音捕获 v2（watchdog）—— 秒级/毫秒级捕获 VoiceTemp 里闪现的语音文件。

相比 v1（0.5s 轮询）的关键改进：
1. watchdog = ReadDirectoryChangesW 事件驱动，文件「创建」瞬间回调（延迟 <10ms）
2. 拿到事件后立即 os.open（Python 默认不带 FILE_SHARE_DELETE）——
   持有句柄期间 Windows 会阻止微信删除该文件，保证能完整读取
3. 读全量（等大小稳定）后复制到 data/voices/raw/

用法：
  python scripts/voice_watch2.py     # 测试：抓到就复制并打印
"""
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.stdout.reconfigure(encoding="utf-8")

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from consumer.voice import find_wechat_files_dir


def build_reverse_map() -> dict[str, str]:
    """md5(会话) -> 会话 id 的反查表（从 DB + labels 收集所有已知会话）。"""
    ids: set[str] = set()
    try:
        conn = sqlite3.connect(PROJECT_DIR / "data" / "messages.db")
        ids |= {r[0] for r in conn.execute("SELECT DISTINCT group_name FROM messages")}
        ids |= {r[0] for r in conn.execute("SELECT DISTINCT sender FROM messages")}
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        import json
        labels = json.loads((PROJECT_DIR / "data" / "labels.json").read_text(encoding="utf-8"))
        ids |= set((labels.get("groups") or {}).keys())
        ids |= set((labels.get("senders") or {}).keys())
    except Exception:  # noqa: BLE001
        pass
    return {hashlib.md5(i.encode()).hexdigest(): i for i in ids if i}


class VoiceHandler(FileSystemEventHandler):
    def __init__(self, dest_dir: Path, reverse_map: dict[str, str]):
        self.dest_dir = dest_dir
        self.reverse_map = reverse_map
        self.seen: set[str] = set()

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        # 只要 VoiceTemp 目录里的直接子文件
        if p.parent.name != "VoiceTemp":
            return
        if str(p) in self.seen:
            return
        self.seen.add(str(p))

        t0 = time.time()
        conv_hash = p.parent.parent.name
        conv_id = self.reverse_map.get(conv_hash, f"(未知会话 {conv_hash[:12]}…)")

        # 立即抢开句柄（阻止微信删除）；文件可能还没写完/被锁，重试
        fd = None
        for _ in range(100):  # 最多约 1 秒
            try:
                fd = os.open(str(p), os.O_RDONLY | os.O_BINARY)
                break
            except (FileNotFoundError, PermissionError, OSError):
                time.sleep(0.01)
        if fd is None:
            print(f"[{time.strftime('%H:%M:%S')}] 发现 {p.name} 但抢不到句柄（已消失）")
            return

        try:
            # 读全量：等大小连续两次一致
            data = b""
            stable = 0
            while stable < 2:
                os.lseek(fd, 0, 0)
                buf = b""
                while True:
                    chunk = os.read(fd, 1 << 16)
                    if not chunk:
                        break
                    buf += chunk
                if buf and len(buf) == len(data):
                    stable += 1
                else:
                    stable = 0
                data = buf
                time.sleep(0.02)

            stamp = int(time.time() * 1000)
            dest = self.dest_dir / f"{conv_hash}_{stamp}.bin"
            dest.write_bytes(data)
            dt_ms = int((time.time() - t0) * 1000)
            print(f"\n[{time.strftime('%H:%M:%S')}] ✅ 捕获! 会话={conv_id}")
            print(f"    文件: {p.name}  大小: {len(data)} bytes  耗时: {dt_ms}ms")
            print(f"    首字节: {data[:16]!r}")
            print(f"    已存: {dest}")
        finally:
            os.close(fd)


def main() -> None:
    cfg = yaml.safe_load(open(PROJECT_DIR / "config.yaml", encoding="utf-8"))
    self_wxid = (cfg.get("hook", {}).get("self_wxid") or "").strip()
    account_dir = find_wechat_files_dir(self_wxid)
    if not account_dir:
        print("找不到微信数据目录")
        sys.exit(1)

    cache_dir = account_dir / "cache"
    dest_dir = PROJECT_DIR / "data" / "voices" / "raw"
    dest_dir.mkdir(parents=True, exist_ok=True)
    reverse_map = build_reverse_map()

    print(f"微信数据目录: {account_dir}")
    print(f"监听: {cache_dir} （递归，事件驱动）")
    print(f"已知会话反查表: {len(reverse_map)} 条")
    print("→ 现在发送或播放一条语音，观察捕获！")

    handler = VoiceHandler(dest_dir, reverse_map)
    observer = Observer()
    observer.schedule(handler, str(cache_dir), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()