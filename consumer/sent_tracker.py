"""跟踪 consumer 通过 HookClient.send_text 发送出去的消息。

目的：让 consumer 能区分"收到的"和"自己发的"消息。
原理：发送时记一笔，收到时查最近 5 分钟内同群同内容是否匹配。
"""
import json
import logging
import time
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


class SentTracker:
    """简单的"最近自己发过什么"追踪器，状态持久化到文件（防重启丢）。"""

    def __init__(self, state_path: Path, window_s: int = 300):
        self.state_path = state_path
        self.window_s = window_s
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self.state_path.exists():
            return []
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            # 过滤掉超过 2x window 的旧条目
            cutoff = int(time.time()) - self.window_s * 2
            return [e for e in data if isinstance(e, dict) and e.get("ts", 0) > cutoff]
        except Exception as e:  # noqa: BLE001
            log.warning("读 sent_log 失败: %s", e)
            return []

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self._entries, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("写 sent_log 失败: %s", e)

    def record(self, group: str, content: str) -> None:
        """记录一次发送（consumer 调 HookClient.send_text 后调用）。"""
        now = int(time.time())
        self._entries.append({
            "group": group,
            "content_prefix": content[:200],
            "ts": now,
        })
        # 清理过期
        cutoff = now - self.window_s * 2
        self._entries = [e for e in self._entries if e["ts"] > cutoff]
        self._save()

    def is_self_sent(self, group: str, content: str, ts: Optional[int] = None) -> bool:
        """检查刚收到的消息是不是我们自己发出去的。

        匹配规则：相同 group_name + 内容前缀一致 + 时间在 window_s 内。
        """
        now = ts or int(time.time())
        cutoff = now - self.window_s
        cp = content[:200]
        for e in self._entries:
            if e.get("group") != group:
                continue
            if e.get("ts", 0) < cutoff:
                continue
            if e.get("content_prefix") == cp:
                return True
        return False

    def match_any(self, content: str, ts: Optional[int] = None) -> Optional[str]:
        """按内容匹配任意记录，返回当时的发送目标（group）。

        用途：私聊里 DLL 只报"消息作者"，不报"发给了谁"；
        但我们自己（replier）发出的消息有记录，可以据此把回声消息
        重新归属到正确的会话，让私聊历史能拼出双向对话。
        """
        now = ts or int(time.time())
        cutoff = now - self.window_s
        cp = content[:200]
        for e in reversed(self._entries):
            if e.get("ts", 0) < cutoff:
                continue
            if e.get("content_prefix") == cp:
                return e.get("group")
        return None
