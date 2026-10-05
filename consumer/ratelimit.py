"""自动回复限速器。

约束（防封号）：
- 每群每 N 秒最多 1 条回复（默认 600 秒 = 10 分钟）
- 全局每天最多 K 条回复（默认 30 条）
- 回复前随机延迟 min~max 秒（默认 60~300 秒）

状态持久到 data/reply_state.json，重启不丢失。
线程安全（consumer 在主线程跑，目前不需要，但加锁更稳）。
"""
import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path


log = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    per_group_cooldown_s: int = 600        # 每群两次回复最小间隔（秒）
    global_daily_limit: int = 30            # 全局每天最多回复条数
    min_delay_s: int = 60                   # 回复前最小延迟
    max_delay_s: int = 300                  # 回复前最大延迟


class RateLimiter:
    """简单的状态持久化限速器。"""

    def __init__(self, state_path: Path, cfg: RateLimitConfig):
        self.state_path = state_path
        self.cfg = cfg
        self._lock = threading.Lock()
        self._state = self._load()

    # ---- 状态持久化 ----

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {"day": "", "daily_count": 0, "groups": {}}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"day": "", "daily_count": 0, "groups": {}}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    # ---- 查询 ----

    def can_reply(self, group_name: str) -> tuple[bool, str, int]:
        """返回 (allowed, reason, suggested_wait_seconds)。"""
        with self._lock:
            now = int(time.time())
            # 1) 全局每日上限
            today = self._today()
            if self._state.get("day") != today:
                # 新一天，重置计数
                self._state["day"] = today
                self._state["daily_count"] = 0
            if self._state.get("daily_count", 0) >= self.cfg.global_daily_limit:
                return (False, f"global_daily_limit={self.cfg.global_daily_limit} 已达上限", 86400)

            # 2) 每群冷却
            grp = self._state.setdefault("groups", {}).get(group_name, {})
            last_ts = grp.get("last_reply_ts", 0)
            elapsed = now - last_ts
            if elapsed < self.cfg.per_group_cooldown_s:
                wait = self.cfg.per_group_cooldown_s - elapsed
                return (False, f"group_cooldown 还需 {wait}s", wait)

            return (True, "ok", 0)

    def record_reply(self, group_name: str) -> None:
        """记录一次回复（必须在实际发送成功后调用）。"""
        with self._lock:
            now = int(time.time())
            today = self._today()
            if self._state.get("day") != today:
                self._state["day"] = today
                self._state["daily_count"] = 0
            self._state["daily_count"] = self._state.get("daily_count", 0) + 1
            self._state.setdefault("groups", {})[group_name] = {"last_reply_ts": now}
            self._save()

    def pick_delay(self) -> int:
        """回复前随机延迟秒数。"""
        lo, hi = self.cfg.min_delay_s, self.cfg.max_delay_s
        if hi <= lo:
            return lo
        return random.randint(lo, hi)
