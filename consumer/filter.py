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
    case_insensitive: bool = True  # 默认大小写不敏感，更符合实际聊天场景

    @classmethod
    def from_yaml(cls, raw: dict) -> "FilterConfig":
        return cls(
            groups=[str(g) for g in raw.get("groups", [])],
            senders=[str(s) for s in raw.get("senders", [])],
            keywords=[str(k) for k in raw.get("keywords", [])],
            case_insensitive=bool(raw.get("case_insensitive", True)),
        )


class Filter:
    def __init__(self, cfg: FilterConfig):
        self.case_insensitive = cfg.case_insensitive
        norm = (lambda s: s.casefold()) if cfg.case_insensitive else (lambda s: s)
        self.groups = {norm(g) for g in cfg.groups}
        self.senders = {norm(s) for s in cfg.senders}
        # 关键词保留原大小写列表（小写版单独存），按原样回写命中原因
        self.keywords = cfg.keywords
        self._norm_keywords = [norm(k) for k in cfg.keywords if k]

    def match(self, *, group_name: str, sender: str, content: str) -> tuple[bool, str]:
        """返回 (是否入库, 命中原因)。

        匹配优先级（任一命中即入库）：
        1. 群白名单为空（=全部群） 或 群名在白名单
        2. 发送人在白名单（非空时）
        3. 关键词命中（子串匹配，行为与原版一致）
        """
        norm = (lambda s: s.casefold()) if self.case_insensitive else (lambda s: s)
        g = norm(group_name)
        s = norm(sender)
        c = norm(content)

        # 1. 群白名单
        if not self.groups or g in self.groups:
            return True, "group_match"

        # 2. 发送人白名单
        if self.senders and s in self.senders:
            return True, "sender_match"

        # 3. 关键词命中
        for kw, kw_norm in zip(self.keywords, self._norm_keywords):
            if kw and kw_norm in c:
                return True, f"keyword:{kw}"

        return False, "no_match"