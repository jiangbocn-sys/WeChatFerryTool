"""推送模块：把重要消息推到 Bark（iOS 推送神器）。

失败不阻塞主流程，只记日志。
"""
import logging
from dataclasses import dataclass

import requests


log = logging.getLogger(__name__)


@dataclass
class BarkConfig:
    server: str   # 例如 https://api.day.app
    key: str


class Notifier:
    def __init__(self, cfg: BarkConfig | None):
        self.cfg = cfg

    def push(self, *, title: str, body: str, group: str | None = None) -> bool:
        if not self.cfg or not self.cfg.key:
            return False
        if self.cfg.key == "REPLACE_ME":
            log.warning("Bark key 未配置，跳过推送")
            return False

        url = f"{self.cfg.server.rstrip('/')}/{self.cfg.key}"
        payload = {
            "title": title[:60],
            "body": body[:200],
            "group": "wechatferry",
            "level": "time-sensitive",
            "icon": "https://cdn-icons-png.flaticon.com/512/124/124034.png",
        }
        if group:
            payload["group"] = f"wechatferry-{group[:20]}"

        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.ok:
                return True
            log.warning("Bark 推送失败: %s %s", r.status_code, r.text[:200])
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("Bark 推送异常: %s", e)
            return False