"""全局语音文件监听（事件驱动 + 句柄防删）。

比轮询可靠得多的原理：
1. watchdog（ReadDirectoryChangesW）监听 <微信数据>/cache 全树，
   VoiceTemp 里任何文件「创建」瞬间回调（延迟 <10ms）
2. 回调里立即 os.open 拿句柄——Python 默认不带 FILE_SHARE_DELETE，
   持有句柄期间 Windows 会阻止微信删除该文件，保证完整读取
3. 读全量（大小稳定）后存到 data/voices/raw/<conv_hash>_<ms>.bin
4. 捕获事件写入内存队列，供 main 里按 会话hash+时间 与消息事件匹配

覆盖两类场景：
- 收到语音（有消息事件）→ 匹配后解码/转写/关联 msg_id
- 自己发出的语音（DLL 不推消息事件）→ 仍会捕获为 raw 文件（孤儿语音）
"""
import hashlib
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)


def conv_hash(conv_id: str) -> str:
    return hashlib.md5(conv_id.encode("utf-8")).hexdigest()


def _looks_like_voice_bin(name: str) -> bool:
    """判断 VoiceTemp 里的一个文件名**像不像微信的语音文件**。

    实测真实语音文件名形如 `1050_1791344098`（`<序号>_<秒级时间戳>`，**没有扩展名**，10~30 KB）。
    同一目录里还会有**别的程序**留下的临时文件 —— 例如百度网盘同步的
    `4_1791363695.baiduyun.uploading.cfg`（数字前缀一模一样，光看时间戳会误判）。
    所以必须排除"带扩展名"的：微信语音不带扩展名。
    """
    if not name or name.startswith("."):
        return False
    if "." in name:                      # 有扩展名 → 不是微信语音（.cfg/.tmp/.dat… 都排掉）
        return False
    head, _, tail = name.partition("_")
    return head.isdigit() and tail.isdigit() and len(tail) >= 9


class _Handler(FileSystemEventHandler):
    def __init__(self, tap: "VoiceTap"):
        self.tap = tap

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.parent.name != "VoiceTemp":
            return
        self.tap._capture(p)


class VoiceTap:
    def __init__(self, account_dir: Path, out_dir: Path):
        self.cache_dir = account_dir / "cache"
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._captures: deque[tuple[str, Path, float]] = deque(maxlen=100)  # (conv_hash, path, epoch)
        self._seen: set[str] = set()
        # 已被某条消息取走的捕获路径（防止多条消息共享同一个文件 → 转写全错）
        self._claimed: set[str] = set()
        self._observer: Optional[Observer] = None

    # ---- 认领（消息事件匹配后调用；或孤儿处理完成后调用）----
    def claim(self, path: Path) -> None:
        """标记已处理：文件改名为 .bin.claimed（持久化，防重启重复处理）。"""
        try:
            if path.exists():
                path.rename(path.with_name(path.name + ".claimed"))
        except OSError:
            pass

    def unclaimed_files(self, older_than_s: float = 90.0) -> list[Path]:
        """列出 N 秒前捕获、还没被认领的语音文件（供孤儿处理）。"""
        out: list[Path] = []
        now = time.time()
        for f in self.out_dir.glob("*.bin"):
            try:
                if now - f.stat().st_mtime >= older_than_s:
                    out.append(f)
            except OSError:
                continue
        return sorted(out)

    # ---- 生命周期 ----
    def start(self) -> bool:
        if not self.cache_dir.is_dir():
            log.warning("VoiceTap: cache 目录不存在 %s", self.cache_dir)
            return False
        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(self.cache_dir), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        log.info("VoiceTap 已启动（事件驱动监听 %s）", self.cache_dir)
        return True

    def stop(self) -> None:
        if self._observer:
            try:
                self._observer.stop()
            except Exception:  # noqa: BLE001
                pass

    # ---- 捕获 ----
    def _capture(self, p: Path) -> None:
        key = str(p)
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)

        # 立即抢句柄（阻止微信删除）
        fd = None
        for _ in range(100):  # ~1s
            try:
                fd = os.open(str(p), os.O_RDONLY | os.O_BINARY)
                break
            except OSError:
                time.sleep(0.01)
        if fd is None:
            log.warning("VoiceTap: 发现 %s 但抢不到句柄（已消失）", p.name)
            return

        try:
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

            h = p.parent.parent.name  # 会话 md5
            now = time.time()
            dest = self.out_dir / f"{h}_{int(now * 1000)}.bin"
            dest.write_bytes(data)
            with self._lock:
                self._captures.append((h, dest, now))
            log.info("VoiceTap 捕获语音: 会话hash=%s… size=%d -> %s", h[:12], len(data), dest.name)
        except OSError as e:
            log.warning("VoiceTap 读取失败 %s: %s", p.name, e)
        finally:
            os.close(fd)

    # ---- 查询（供消息事件匹配） ----
    def recent(self, h: str, since: float) -> Optional[Path]:
        """取该会话 `since` 之后、**尚未被取走**的最新捕获。

        ⚠️ 取走后会记账（`_claimed`），同一文件**不会再给第二条消息** ——
        否则短时间内到达的多条语音会全部配到同一个文件上，转写结果全错
        （2026-10-08 实测：三条**时长/aeskey 都不同**的语音被转写成同一段文字，
         根因就是这个"窗口内返回最新文件但不消费"的语义）。
        """
        with self._lock:
            for ch, path, ts in reversed(self._captures):
                if ch == h and ts >= since and path.exists():
                    key = str(path)
                    if key in self._claimed:
                        continue
                    self._claimed.add(key)
                    return path
        return None

    def wait_for(self, h: str, since: float, timeout_s: float = 20.0) -> Optional[Path]:
        """等这个会话出现**可用的新捕获**（含 since 之后已捕获、且未被取走的）。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            p = self.recent(h, since)
            if p:
                return p
            time.sleep(0.2)
        return None

    def scan_temp(self, h: str, since: float) -> Optional[Path]:
        """**直接扫盘**：在 `<cache>/<月>/Message/<会话md5>/VoiceTemp/` 里找 mtime >= since 的文件。

        为什么需要它（2026-10-07 实测）：`wait_for` 只认**事件已经捕获过**的文件，窗口 20 秒。
        实际会遇到两种漏抓：
          * 语音文件**迟落盘**（20 秒后才出现）—— 那时 `wait_for` 已经放弃了；
          * watchdog 的事件**漏掉/来不及**（同一条语音的多个文件、目录刚被创建等）。
        这两种情况下文件其实**还在 VoiceTemp 里**（实测事后来看还有未认领的文件），
        所以超时后主动扫一遍目录能把它们捞回来。

        返回最新匹配的文件；没有则 None。**不抢句柄、不认领** —— 由调用方决定（通常紧接着
        调 `claim()`），保证与事件路径的行为一致。
        """
        if not h:
            return None
        found: list[tuple[float, Path]] = []
        try:
            months = [d for d in self.cache_dir.iterdir() if d.is_dir()]
        except OSError:
            return None
        for month in months:
            vd = month / "Message" / h / "VoiceTemp"
            if not vd.is_dir():
                continue
            try:
                for f in vd.iterdir():
                    if not f.is_file() or not _looks_like_voice_bin(f.name):
                        continue
                    try:
                        mt = f.stat().st_mtime
                    except OSError:
                        continue
                    if mt >= since:
                        found.append((mt, f))
            except OSError:
                continue
        if not found:
            return None
        found.sort(key=lambda t: t[0])
        return found[-1][1]

    def _conv_hashes(self) -> set[str]:
        """缓存里出现过的会话 md5（用于"这条语音是哪个会话"的兜底匹配）。"""
        out: set[str] = set()
        try:
            for month in self.cache_dir.iterdir():
                msgdir = month / "Message"
                if not msgdir.is_dir():
                    continue
                for d in msgdir.iterdir():
                    if (d / "VoiceTemp").is_dir():
                        out.add(d.name)
        except OSError:
            pass
        return out
