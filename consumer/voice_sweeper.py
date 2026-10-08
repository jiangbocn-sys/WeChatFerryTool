"""常驻语音巡检器：**主动、持续**把微信 `VoiceTemp` 里短暂出现的语音抢存下来。

为什么要它（2026-10-08 实测教训）
--------------------------------
用户观察到：早上 11 条语音只有 1 条转写成功，而**那条他既没播放、群窗口也没打开**。
说明语音文件是**微信自动写进 `VoiceTemp`** 的（收到就写或后台预取），只是**存留极短**。

而现有两条捕获路径都是**被动等**、命中率很低：
  * `VoiceTap`（watchdog 监听文件创建）—— 2026-10-08 一整天 **0 次命中**；
  * `capture_voice()`（消息到达后轮询 20 秒）—— 撞运气，11 条中 1 条。

本模块换个思路：**不管消息事件，后台持续扫所有会话的 `VoiceTemp`**，
文件一出现就立刻复制到暂存区（`<data>/voices/swept/`），并记录
`(会话md5, 秒级时间戳, 暂存路径)`。之后由上层按"时间戳 ↔ 库里 pending 语音消息"
配对补转写 —— 这样既不依赖消息事件，也不受 20 秒窗口限制。

设计要点
--------
* **抢存优先**：发现即 `shutil.copy2`（微信随时会删）。
* **不误抓**：复用 `voice_tap._looks_like_voice_bin()`（微信语音无扩展名；
  排除百度网盘 `*.baiduyun.uploading.cfg` 这类噪音）。
* **幂等**：已抢过的文件按 `(路径, mtime_ns)` 记账，不重复复制。
* **省 IO**：只想扫"最近改过的月目录"，且单个 VoiceTemp 目录里文件很少。
* 只读微信目录 + 写自己的暂存区，不改微信数据。

用法（由 `consumer.main.Consumer` 在启用语音时以后台线程启动）::

    sweeper = VoiceSweeper(wechat_dir, data_root / "data" / "voices" / "swept")
    sweeper.start()          # 常驻线程
    ...
    sweeper.stop()

    # 把已抢到的语音按时间戳配对给"还没转写"的语音消息
    for conv_hash, ts, path in sweeper.pending():
        ...
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from consumer.voice_tap import _looks_like_voice_bin

log = logging.getLogger("consumer.voice_sweeper")


class VoiceSweeper:
    """后台持续扫 `VoiceTemp` 抢存语音。"""

    def __init__(self, account_dir: Path, out_dir: Path, interval_s: float = 2.0,
                 keep_s: float = 3600.0):
        self.account_dir = Path(account_dir)
        self.cache_dir = self.account_dir / "cache"
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.interval_s = max(0.5, float(interval_s))
        self.keep_s = keep_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # (会话md5, 秒级时间戳) -> (暂存文件路径, 抢存时刻)
        # ⚠️ 必须单独记"抢存时刻"：源文件的 mtime 可能很旧（例如昨天遗留的文件今天才被扫到），
        #    若按源 mtime 算存活时间会被 _prune 立刻删掉（踩过）。
        self._swept: dict[tuple[str, int], tuple[Path, float]] = {}
        self._seen_src: set[str] = set()
        self.stats = {"scans": 0, "saved": 0, "missed": 0}

    # ---- 生命周期 ----
    def start(self) -> bool:
        if not self.cache_dir.is_dir():
            log.warning("VoiceSweeper: cache 目录不存在 %s", self.cache_dir)
            return False
        self._thread = threading.Thread(target=self._loop, name="voice-sweeper", daemon=True)
        self._thread.start()
        log.info("VoiceSweeper 已启动（每 %.1fs 扫一遍 %s，抢存到 %s）",
                 self.interval_s, self.cache_dir, self.out_dir)
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:  # noqa: BLE001
                log.debug("VoiceSweeper 扫描异常", exc_info=True)
            self._stop.wait(self.interval_s)

    # ---- 单次扫描 ----
    def scan_once(self) -> int:
        """扫一遍所有 VoiceTemp，抢存新文件；返回本次抢到的数量。"""
        self.stats["scans"] += 1
        saved = 0
        now = time.time()
        try:
            months = [d for d in self.cache_dir.iterdir() if d.is_dir()]
        except OSError:
            return 0
        for month in months:
            msgdir = month / "Message"
            if not msgdir.is_dir():
                continue
            try:
                convs = [d for d in msgdir.iterdir() if d.is_dir()]
            except OSError:
                continue
            for cdir in convs:
                vd = cdir / "VoiceTemp"
                if not vd.is_dir():
                    continue
                try:
                    files = list(vd.iterdir())
                except OSError:
                    continue
                for f in files:
                    if not _looks_like_voice_bin(f.name):
                        continue
                    try:
                        st = f.stat()
                    except OSError:
                        continue          # 已被微信删掉
                    key = f"{f}|{st.st_mtime_ns}"
                    if key in self._seen_src:
                        continue
                    # 抢存（越快越好）
                    try:
                        dest = self.out_dir / f"{cdir.name}_{f.name}"
                        shutil.copy2(f, dest)
                    except OSError:
                        self.stats["missed"] += 1
                        continue
                    self._seen_src.add(key)
                    try:
                        ts = int(f.name.split("_")[-1])
                    except (ValueError, IndexError):
                        continue
                    with self._lock:
                        self._swept[(cdir.name, ts)] = (dest, now)
                    saved += 1
                    self.stats["saved"] += 1
                    log.info("VoiceSweeper 抢到语音: %s (%d B) 会话=%s… ts=%d",
                             f.name, st.st_size, cdir.name[:12], ts)
        self._prune(now)
        return saved

    def _prune(self, now: float) -> None:
        """清掉超过 keep_s 未被取走的暂存文件与记账（避免无限增长）。

        按**抢存时刻**计时（不是源文件 mtime —— 源文件可能很旧，见 __init__ 注释）。
        """
        if self.keep_s <= 0:
            return
        with self._lock:
            items = list(self._swept.items())
        for (h, ts), (p, swept_at) in items:
            if now - swept_at > self.keep_s:
                with self._lock:
                    self._swept.pop((h, ts), None)
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    # ---- 供上层配对 ----
    def take(self, conv_hash: str, since_ts: int, tol_s: int = 30) -> Path | None:
        """取一条与 `since_ts`（秒级）最接近、且还没被取走的语音文件。

        配对依据：VoiceTemp 文件名后缀 = 该语音的秒级时间戳，
        与库里消息的 `received_at` 通常只差几秒。
        """
        best: tuple[int, tuple[str, int]] | None = None
        with self._lock:
            for k in list(self._swept):
                h, ts = k
                if h != conv_hash:
                    continue
                d = abs(ts - int(since_ts))
                if d <= tol_s and (best is None or d < best[0]):
                    best = (d, k)
            if best is None:
                return None
            p, _at = self._swept.pop(best[1])
        log.info("VoiceSweeper 配对: 差值 %ds -> %s", best[0], p.name)
        return p

    def pending(self) -> list[tuple[str, int, Path]]:
        """列出已抢到、还没被取走的语音：[(会话md5, 秒级ts, 路径)]。"""
        with self._lock:
            return [(h, ts, p) for (h, ts), (p, _at) in self._swept.items()]

    def take_exact(self, path: Path) -> Path | None:
        """按**确切路径**取走某条已抢到的文件。

        配对循环遍历 `pending()` 时**必须**用这个，不要用 `take(会话, ts)` ——
        后者可能返回并移除同会话里时间最接近的**另一个**文件，
        导致多个条目抢同一个文件、正文被跳过（在图片配对里踩过这个坑）。
        """
        target = Path(path)
        with self._lock:
            for k, (p, _at) in list(self._swept.items()):
                if p == target:
                    self._swept.pop(k, None)
                    return p
        return None
