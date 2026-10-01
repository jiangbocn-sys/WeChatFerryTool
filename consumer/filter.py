"""消息过滤模块。

判定一条消息是否应该入库：
- 群白名单（只监听配置的群）
- 发送人白名单（只关心某些人）
- 关键词命中（任一命中即入库）

任何一条命中规则 → 入库。
"""
from dataclasses import dataclass


@dataclass
class FilterConfig:
    groups: list[str]
    senders: list[str]
    keywords: list[str]

    @classmethod
    def from_yaml(cls, raw: dict) -> "FilterConfig":
        return cls(
            groups=[str(g) for g in raw.get("groups", [])],
            senders=[str(s) for s in raw.get("senders", [])],
            keywords=[str(k) for k in raw.get("keywords", [])],
        )


class Filter:
    def __init__(self, cfg: FilterConfig):
        self.groups = set(cfg.groups)
        self.senders = set(cfg.senders)
        self.keywords = cfg.keywords

    def match(self, *, group_name: str, sender: str, content: str) -> tuple[bool, str]:
        """返回 (是否入库, 命中原因)。

        匹配优先级（任一命中即入库）：
        1. 群白名单为空（=全部群） 或 群名在白名单
        2. 发送人在白名单（非空时）
        3. 关键词命中
        """
        # 1. 群白名单
        if not self.groups or group_name in self.groups:
            return True, "group_match"

        # 2. 发送人白名单
        if self.senders and sender in self.senders:
            return True, "sender_match"

        # 3. 关键词命中
        for kw in self.keywords:
            if kw and kw in content:
                return True, f"keyword:{kw}"

        return False, "no_match"