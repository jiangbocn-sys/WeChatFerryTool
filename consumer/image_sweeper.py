"""图片巡检器：抢存微信缓存里的**明文缩略图**（聊天图片能直接读！）。

关键发现（2026-10-08 实测）
--------------------------
微信会在缓存里放**明文 JPEG 缩略图**：

    cache\\<年-月>\\Message\\<会话md5>\\Thumb\\<序号>_<秒级时间戳>_thumb.jpg
       头 = ff d8 ff e0（标准 JPEG），7 KB ~ 163 KB，实测含 500×500 的真聊天图
    另：ImageTemp\\<n>_<ts>_mid_temp_convert（图片转换中间产物）

* 文件名带**秒级时间戳** → 与库里消息 `received_at` 按"会话 md5 + 时间接近"配对，
  实测差值 **9 秒**即可对上。
* ⚠️ **微信会清理**：实测同一批文件几分钟内就消失 → **必须"出现即抢"**
  （常驻巡检，和语音 `VoiceTemp` 一个套路）。

设计
----
* 常驻线程，每 `interval_s` 秒扫一遍所有会话的 `Thumb`/`ImageTemp`；
* 发现即 `copy2` 到 `<data>/voices/../images/` 暂存区（按 `(会话md5, ts)` 记账，幂等）；
* 供上层用 `store.message_near()` 反查对应图片消息，命名成 `<msg_id>.jpg` 存到
  `data/images/`，这样**总结时能按消息直接引用图片**；
* 只读微信缓存 + 写自己的数据目录，不改微信数据。

用法::

    sw = ImageSweeper(wechat_dir, data_root / "data" / "images")
    sw.start()                      # 常驻
    for conv_hash, ts, path in sw.pending():
        mid = store.message_near(conv_id, ts, msg_type=3, tol_s=90)
        ...
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger("consumer.image_sweeper")

# 明文媒体所在的缓存子目录 → 扩展名
CACHE_DIRS = {"Thumb": ".jpg", "ImageTemp": ".img"}


class ImageSweeper:
    """后台持续扫 `Thumb`/`ImageTemp`，把明文缩略图抢存下来。"""

    def __init__(self, account_dir: Path, out_dir: Path, interval_s: float = 3.0,
                 keep_s: float = 3600.0):
        self.account_dir = Path(account_dir)
        self.cache_dir = self.account_dir / "cache"
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.interval_s = max(1.0, float(interval_s))
        self.keep_s = keep_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # (会话md5, 秒级时间戳) -> (暂存路径, 抢存时刻)
        self._saved: dict[tuple[str, int], tuple[Path, float]] = {}
        self._seen_src: set[str] = set()
        self.stats = {"scans": 0, "saved": 0, "missed": 0}

    # ---- 生命周期 ----
    def start(self) -> bool:
        if not self.cache_dir.is_dir():
            log.warning("ImageSweeper: cache 目录不存在 %s", self.cache_dir)
            return False
        self._thread = threading.Thread(target=self._loop, name="image-sweeper", daemon=True)
        self._thread.start()
        log.info("ImageSweeper 已启动（每 %.1fs 扫 %s 的 %s，抢存明文缩略图到 %s）",
                 self.interval_s, self.cache_dir, "/".join(CACHE_DIRS), self.out_dir)
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:  # noqa: BLE001
                log.debug("ImageSweeper 扫描异常", exc_info=True)
            self._stop.wait(self.interval_s)

    # ---- 单次扫描 ----
    def scan_once(self) -> int:
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
                for sub, ext in CACHE_DIRS.items():
                    d = cdir / sub
                    if not d.is_dir():
                        continue
                    try:
                        files = list(d.iterdir())
                    except OSError:
                        continue
                    for f in files:
                        if not f.is_file():
                            continue
                        ts = self._ts_of(f.name)
                        if ts is None:
                            continue
                        try:
                            st = f.stat()
                        except OSError:
                            continue          # 已被微信删掉
                        key = f"{f}|{st.st_mtime_ns}"
                        if key in self._seen_src:
                            continue
                        try:
                            # ⚠️ 微信的临时文件常常**没有扩展名**（如 `<n>_<ts>_hd_temp`、
                            # `<n>_<ts>_thumb_temp`），它们在内容上仍是 JPEG。
                            # 若原样保留 `.img` 之类，后续 PIL 会打不开（踩过）→ 统一成 .jpg。
                            ext_out = f.suffix if f.suffix.lower() in (".jpg", ".jpeg", ".png") else ".jpg"
                            dest = self.out_dir / f"{cdir.name}_{ts}{ext_out}"
                            shutil.copy2(f, dest)
                        except OSError:
                            self.stats["missed"] += 1
                            continue
                        self._seen_src.add(key)
                        with self._lock:
                            self._saved[(cdir.name, ts)] = (dest, now)
                        saved += 1
                        self.stats["saved"] += 1
                        log.info("ImageSweeper 抢到明文图片: %s (%d B) 会话=%s…",
                                 f.name, st.st_size, cdir.name[:12])
        self._prune(now)
        return saved

    @staticmethod
    def _ts_of(name: str) -> int | None:
        """从 `<序号>_<秒级时间戳>_thumb.jpg` 或 `<序号>_<秒级时间戳>_mid_temp_convert`
        里取出秒级时间戳；取不到返回 None。"""
        parts = name.split("_")
        for p in parts[1:3]:                  # 时间戳通常在第二段
            if p.isdigit() and len(p) >= 9:
                return int(p)
        return None

    def _prune(self, now: float) -> None:
        """按**抢存时刻**清理过期暂存文件（不能用源文件 mtime —— 可能是旧的）。"""
        if self.keep_s <= 0:
            return
        with self._lock:
            items = list(self._saved.items())
        for (h, ts), (p, at) in items:
            if now - at > self.keep_s:
                with self._lock:
                    self._saved.pop((h, ts), None)
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    # ---- 供上层配对 ----
    def pending(self) -> list[tuple[str, int, Path]]:
        """已抢到、还没被取走的明文图片：[(会话md5, 秒级ts, 路径)]。"""
        with self._lock:
            return [(h, ts, p) for (h, ts), (p, _at) in self._saved.items()]

    def take(self, conv_hash: str, ts: int, tol_s: int = 0) -> Path | None:
        """取走一条（精确 ts 或容差内最近的），并从 pending 移除。

        ⚠️ 遍历 `pending()` 时**不要**用它 —— 它可能返回并移除**另一个**文件
        （同会话里时间最接近的那个），导致多个条目抢同一个文件、正文被跳过。
        那种场合用 `take_exact(entry_path)`（踩过：图片配对曾因此一张都没归档）。
        """
        best: tuple[int, tuple[str, int]] | None = None
        with self._lock:
            for k in list(self._saved):
                h, kts = k
                if h != conv_hash:
                    continue
                d = abs(kts - int(ts))
                if d <= tol_s and (best is None or d < best[0]):
                    best = (d, k)
            if best is None:
                return None
            p, _at = self._saved.pop(best[1])
        return p

    def take_exact(self, path: Path) -> Path | None:
        """按**确切路径**取走某条已抢到的文件（配对循环遍历 `pending()` 时用这个）。"""
        target = Path(path)
        with self._lock:
            for k, (p, _at) in list(self._saved.items()):
                if p == target:
                    self._saved.pop(k, None)
                    return p
        return None
