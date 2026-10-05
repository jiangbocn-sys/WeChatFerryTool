"""自动回复模块。

工作流：
1. Consumer 把通过 filter 的消息传给 replier
2. replier 检查模板匹配 + scope 匹配 + 限速
3. 全部 OK 时随机延迟 N 秒后调 HookClient.send_text
4. 实际发送成功后调 RateLimiter.record_reply
   并通过 sent_tracker 记录（用于标记 direction=out）

注意：
- 默认 test_only=true：只记录不发送，方便先观察行为
- 实际启用前请确认限速参数（防封号）
- use_llm=true 时 response 字段变成 LLM 人设/指令；生成失败降级到该字段原文
"""
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from consumer.hook_client import HookClient, HookError
from consumer.ratelimit import RateLimiter, RateLimitConfig

try:
    from consumer.sent_tracker import SentTracker
except ImportError:  # noqa: BLE001
    SentTracker = None  # type: ignore[assignment,misc]

try:
    from openai import OpenAI  # 复用 minimax / DeepSeek 兼容 SDK
except ImportError:  # noqa: BLE001
    OpenAI = None  # type: ignore[assignment]


log = logging.getLogger(__name__)


@dataclass
class ReplyTemplate:
    name: str
    trigger: str               # 触发关键词
    match_type: str            # substring | exact | regex
    response: str              # 固定回复内容；use_llm=True 时变成 LLM 人设/指令
    scope_groups: list[str] = field(default_factory=list)   # 空 = 不限群
    scope_senders: list[str] = field(default_factory=list)  # 空 = 不限人
    test_only: bool = True     # 默认只记录不发
    use_llm: bool = False      # True 时用 LLM 动态生成回复（response 字段作为人设）


@dataclass
class ReplyConfig:
    enabled: bool = False
    templates: list[ReplyTemplate] = field(default_factory=list)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_s: float = 30.0
    history_count: int = 10  # 生成回复时附带的最近对话条数（0=不带）


class Replier:
    def __init__(
        self,
        cfg: ReplyConfig,
        hook: HookClient,
        rate_limiter: RateLimiter,
        labels_path: Path | None = None,
        sent_tracker: "SentTracker | None" = None,
        history_fn: "Callable[[str, int], list[dict]] | None" = None,
    ):
        self.cfg = cfg
        self.hook = hook
        self.rl = rate_limiter
        self.labels_path = labels_path
        self.sent_tracker = sent_tracker
        self.history_fn = history_fn  # (group_name, limit) -> 最近消息列表（时间倒序）
        self._timers: list[threading.Timer] = []  # 防止 GC
        # 冷却期内的触发排队（不丢弃）
        self.MAX_QUEUE_PER_GROUP = 3
        self._queues: dict[str, deque] = {}
        self._queue_timers: dict[str, threading.Timer] = {}
        self._q_lock = threading.Lock()

    @classmethod
    def from_yaml(cls, raw: dict, hook: HookClient, state_path: Path,
                  llm_base_url: str = "", llm_api_key: str = "", llm_model: str = "",
                  labels_path: Path | None = None,
                  sent_tracker: "SentTracker | None" = None,
                  history_fn: "Callable[[str, int], list[dict]] | None" = None) -> "Replier":
        """从 yaml 段构造 Replier。

        llm_* 参数由 main.py 从 cfg['llm'] 段传入；use_llm 模板要靠这些参数生成回复。
        labels_path 用于查发送人标定的显示名（管理平台标注的名字）。
        sent_tracker 用于标记我们发出去的消息（direction=out）。
        """
        rl_cfg = RateLimitConfig(
            per_group_cooldown_s=int((raw.get("rate_limit") or {}).get("per_group_cooldown_s", 600)),
            global_daily_limit=int((raw.get("rate_limit") or {}).get("global_daily_limit", 30)),
            min_delay_s=int((raw.get("rate_limit") or {}).get("min_delay_s", 60)),
            max_delay_s=int((raw.get("rate_limit") or {}).get("max_delay_s", 300)),
        )
        templates: list[ReplyTemplate] = []
        for t in raw.get("templates") or []:
            try:
                templates.append(ReplyTemplate(
                    name=str(t.get("name") or t.get("trigger", "?")),
                    trigger=str(t.get("trigger", "")),
                    match_type=str(t.get("match_type", "substring")),
                    response=str(t.get("response", "")),
                    scope_groups=[str(g) for g in (t.get("scope") or {}).get("groups") or []],
                    scope_senders=[str(s) for s in (t.get("scope") or {}).get("senders") or []],
                    test_only=bool(t.get("test_only", True)),
                    use_llm=bool(t.get("use_llm", False)),
                ))
            except Exception as e:  # noqa: BLE001
                log.warning("解析模板失败，跳过: %s (err=%s)", t, e)

        cfg = ReplyConfig(
            enabled=bool(raw.get("enabled", False)),
            templates=templates,
            rate_limit=rl_cfg,
            llm_base_url=llm_base_url or "https://api.deepseek.com/v1",
            llm_api_key=llm_api_key or "",
            llm_model=llm_model or "deepseek-chat",
            history_count=int(raw.get("history_count", 10)),
        )
        return cls(cfg, hook, RateLimiter(state_path, rl_cfg), labels_path=labels_path,
                   sent_tracker=sent_tracker, history_fn=history_fn)

    def _resolve_at(self, sender_wxid: str) -> str:
        """查 labels 找发送人的标定显示名，返回 `@<name>\u2005` 用于 @ 提及。

        返回空串表示没找到。\u2005 (FOUR-PER-EM SPACE) 是 WeChat 群 @ 的标准格式。
        """
        if not self.labels_path or not sender_wxid:
            return ""
        try:
            with self.labels_path.open(encoding="utf-8") as f:
                data = json.load(f)
            entry = (data.get("senders") or {}).get(sender_wxid) or {}
            name = (entry.get("name") or "").strip()
            if name:
                return f"@{name}\u2005"
            return f"@{sender_wxid}\u2005"
        except Exception as e:  # noqa: BLE001
            log.warning("读取 labels 失败: %s", e)
            return f"@{sender_wxid}\u2005"

    def _sender_names(self) -> dict[str, str]:
        """labels.json 的 senders 名字映射（wxid -> 显示名）。"""
        if not self.labels_path:
            return {}
        try:
            with self.labels_path.open(encoding="utf-8") as f:
                data = json.load(f)
            return {
                k: (v.get("name") or "")
                for k, v in (data.get("senders") or {}).items()
                if v.get("name")
            }
        except Exception:  # noqa: BLE001
            return {}

    def _build_history(self, group_name: str, sender: str, content: str) -> str:
        """构造「最近对话记录」文本块（最多 history_count 条，时间正序）。

        - 文本消息取原文；语音取转写文本（无转写显示 [语音]）
        - 图片/系统等噪声不进入上下文
        - 排除当前待回复的这条（延迟发送时它可能已入库）
        """
        if self.history_fn is None or self.cfg.history_count <= 0:
            return ""
        try:
            rows = self.history_fn(group_name, self.cfg.history_count + 5) or []
        except Exception as e:  # noqa: BLE001
            log.debug("获取对话历史失败: %s", e)
            return ""
        rows = sorted(rows, key=lambda m: m.get("received_at") or 0)
        names = self._sender_names()
        lines: list[str] = []
        cur = content.strip()
        for m in rows:
            mt = m.get("msg_type")
            txt = (m.get("content") or "").strip()
            if mt == 34:  # 语音
                tr = (m.get("transcript") or "").strip()
                txt = f"[语音] {tr}" if tr and not tr.startswith("[") else "[语音]"
            elif mt != 1:
                continue
            if not txt:
                continue
            if txt == cur and (m.get("sender") or "") == sender:
                continue
            who = names.get(m.get("sender") or "", m.get("sender") or "")
            lines.append(f"{who}: {txt[:100]}")
        lines = lines[-self.cfg.history_count:]
        if not lines:
            return ""
        return "最近的对话记录（按时间顺序）：\n" + "\n".join(lines) + "\n\n"

    # ---- 核心 ----

    def maybe_reply(self, *, group_name: str, sender: str, content: str) -> None:
        """主入口：consumer 拿到一条消息后调用。

        限速策略：
        - 群冷却期内 → 入队等待（不丢弃，每群最多排 MAX_QUEUE_PER_GROUP 条）
        - 日限满 → 丢弃（无法排队）
        """
        if not self.cfg.enabled:
            return
        template = self._match_template(group_name, sender, content)
        if template is None:
            return

        if template.test_only:
            delay = self.rl.pick_delay()
            log.info(
                "🧪 [测试模式] 将回复 群=%s 发送人=%s 模板=%s 延迟=%ds 内容=%s",
                group_name, sender, template.name, delay, template.response[:80],
            )
            return

        allowed, reason, wait_s = self.rl.can_reply(group_name)
        if allowed:
            delay = self.rl.pick_delay()
            log.info("📨 计划回复 群=%s 模板=%s 延迟=%ds", group_name, template.name, delay)
            timer = threading.Timer(
                delay,
                self._do_send,
                args=(template, group_name, sender, content),
            )
            timer.daemon = True
            timer.start()
            self._timers.append(timer)  # 防止被 GC 回收
            return

        if reason.startswith("group_cooldown"):
            with self._q_lock:
                q = self._queues.setdefault(group_name, deque())
                if len(q) >= self.MAX_QUEUE_PER_GROUP:
                    log.warning(
                        "⛔ 排队已满（%d 条），丢弃本条：群=%s 模板=%s",
                        self.MAX_QUEUE_PER_GROUP, group_name, template.name,
                    )
                    return
                q.append((template, sender, content))
                qlen = len(q)
            log.info(
                "⏳ 冷却期内（还需 %ds），已排队第 %d 条：群=%s 模板=%s",
                wait_s, qlen, group_name, template.name,
            )
            self._schedule_queue_drain(group_name, wait_s + 1)
            return

        log.info("回复被限速（不可排队）：模板=%s 群=%s 原因=%s", template.name, group_name, reason)

    # ---- 排队回复 ----

    def _schedule_queue_drain(self, group_name: str, after_s: float) -> None:
        """确保该群有一个排队清空 worker（已存在则不重复起）。"""
        with self._q_lock:
            t = self._queue_timers.get(group_name)
            if t is not None and t.is_alive():
                return
            timer = threading.Timer(max(1.0, after_s), self._drain_queue, args=(group_name,))
            timer.daemon = True
            self._queue_timers[group_name] = timer
            timer.start()

    def _drain_queue(self, group_name: str) -> None:
        """worker：冷却结束后逐条发送排队的回复（每条重新等冷却，保持间隔）。"""
        while True:
            with self._q_lock:
                q = self._queues.get(group_name)
                if not q:
                    self._queue_timers.pop(group_name, None)
                    return
            allowed, reason, wait_s = self.rl.can_reply(group_name)
            if not allowed:
                if reason.startswith("group_cooldown"):
                    time.sleep(min(max(wait_s, 1), 60))
                    continue
                # 日限满：清空该群队列
                with self._q_lock:
                    dropped = len(self._queues.pop(group_name, []) or [])
                    self._queue_timers.pop(group_name, None)
                log.warning("日限已满，丢弃队列 %d 条：群=%s", dropped, group_name)
                return
            with self._q_lock:
                q = self._queues.get(group_name)
                if not q:
                    self._queue_timers.pop(group_name, None)
                    return
                template, sender, content = q.popleft()
                remaining = len(q)
            log.info("⏳ 排队回复出队发送：群=%s 模板=%s（剩余 %d）", group_name, template.name, remaining)
            self._do_send(template, group_name, sender, content)

    def _do_send(self, template: ReplyTemplate, group_name: str, sender: str, content: str) -> None:
        """延迟后真正执行：可能调 LLM 生成 → 发送。

        LLM 失败时绝不能发 persona 原文（破功），改发安全兜底。
        自动加 @<sender> 前缀（用 labels 里的标定名）。
        """
        text: Optional[str] = None
        llm_failed = False
        if template.use_llm:
            text = self._generate_reply(template, group_name, sender, content)
            if text is None:
                # LLM 失败：兜底（避免 persona 泄露破功）
                log.warning("LLM 生成失败，使用安全兜底（persona=%s）", template.name)
                llm_failed = True
                text = "[自动回复：稍后再聊]"  # 极简兜底，不解释原因（避免破功）
        else:
            text = template.response

        # @ 提及只在群里需要；私聊不加（避免 "@xxx 你好" 这种怪格式）
        is_group = "@chatroom" in group_name
        at = self._resolve_at(sender)
        # 1. 替换占位符 {at}
        if at and "{at}" in text:
            text = text.replace("{at}", at)
        # 2. 群里还没 @sender 时自动加前缀（私聊跳过）
        elif at and is_group and not text.lstrip().startswith("@"):
            text = at + text

        try:
            result = self.hook.send_text(group_name, text)
            log.info("✅ 已发送回复 群=%s ret=%s 内容=%s", group_name, result, text[:80])
            self.rl.record_reply(group_name)
            # 记录到自己发出去的消息（用于 main 标记 direction=out）
            if self.sent_tracker is not None:
                self.sent_tracker.record(group_name, text)
        except HookError as e:
            log.error("❌ 发送失败: %s", e)

    def _generate_reply(self, template: ReplyTemplate, group_name: str, sender: str, content: str) -> Optional[str]:
        """用 LLM 动态生成回复。失败返回 None。"""
        if OpenAI is None:
            log.error("openai SDK 不可用，无法生成 LLM 回复")
            return None
        if not self.cfg.llm_api_key or self.cfg.llm_api_key in ("REPLACE_ME", ""):
            log.error("LLM api_key 未配置，无法生成回复")
            return None
        try:
            client = OpenAI(
                base_url=self.cfg.llm_base_url,
                api_key=self.cfg.llm_api_key,
                timeout=self.cfg.llm_timeout_s,
                max_retries=0,
            )
            persona = template.response.strip() or "你是一个微信群里的助手，请用 1-2 句话自然回复。"
            is_group = "@chatroom" in group_name
            at = self._resolve_at(sender)
            if is_group and at:
                at_rule = "- 直接从正文开始写回复，不要包含任何 @ 提及（系统会自动补 @ 前缀）"
                scene = f"群聊中有人 @ 了你（发送人 wxid: {sender}）。"
            else:
                at_rule = "- 这是私聊，直接从正文开始，不要加任何 @ 前缀"
                scene = f"有人给你发了条私聊消息（发送人 wxid: {sender}）。"
            system_prompt = (
                f"{persona}\n\n"
                "规则：\n"
                "- 用中文，自然口语风格\n"
                "- 不要重复自我介绍（已经聊过）\n"
                "- 回答必须真实、有依据；不确定或无法核实的信息（如实时天气、新闻、具体数字）要明确说不知道或建议对方查证，严禁编造\n"
                "- 直接输出回复文本，不要任何 <think> 推理过程或元描述\n"
                "- 如果内容无关/骚扰，可以简短礼貌地表示稍后回复\n"
                f"{at_rule}"
            )
            # 附上最近对话上下文
            history_block = self._build_history(group_name, sender, content)
            user_msg = f"{scene}\n{history_block}他的消息内容：\n{content[:500]}"
            resp = client.chat.completions.create(
                model=self.cfg.llm_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.7,
                # 不设 max_tokens 限制：按实际用量计费，模型写完自然停
                # （设上限只会在推理偶发变长时造成截断，反而引发兜底/误报）
            )
            # 调试：记录完整响应结构
            try:
                finish_reason = resp.choices[0].finish_reason
            except Exception:  # noqa: BLE001
                finish_reason = "?"
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                # 可能是 content_filter 拦截，或模型就是返回空
                log.warning(
                    "LLM 返回空内容，finish_reason=%s 完整响应=%s",
                    finish_reason,
                    resp.model_dump_json()[:500],
                )
                return None
            # 去掉可能的引号包裹
            if text.startswith('"') and text.endswith('"'):
                text = text[1:-1]
            # 去掉 <think>...</think> 推理块（minimax 等推理模型会带）
            text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
            # 未闭合的  thinking（推理被截断）→ 从  thinking 起全部丢弃
            if " thinking" in text:
                text = text[: text.index(" thinking")]
            text = text.strip()
            if not text:
                log.warning(
                    "LLM 内容去掉推理块后为空（token 可能被推理耗尽），finish_reason=%s 原始内容=%s",
                    finish_reason,
                    (resp.choices[0].message.content or "")[:300],
                )
                return None
            return text
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 生成异常: %s", e)
            return None

    def _match_template(
        self, group_name: str, sender: str, content: str
    ) -> ReplyTemplate | None:
        """按顺序匹配，返回第一个命中的模板。"""
        for t in self.cfg.templates:
            if not self._match_scope(t, group_name, sender):
                continue
            if not self._match_trigger(t, content):
                continue
            return t
        return None

    def _match_scope(self, t: ReplyTemplate, group_name: str, sender: str) -> bool:
        if t.scope_groups and group_name not in t.scope_groups:
            return False
        if t.scope_senders and sender not in t.scope_senders:
            return False
        return True

    def _match_trigger(self, t: ReplyTemplate, content: str) -> bool:
        if t.match_type == "exact":
            return content.strip() == t.trigger
        if t.match_type == "regex":
            try:
                return re.search(t.trigger, content) is not None
            except re.error as e:
                log.warning("模板 %s 正则表达式错误: %s", t.name, e)
                return False
        # 默认 substring
        return t.trigger in content

    def shutdown(self) -> None:
        """停止所有 pending 定时器（含排队 worker）。"""
        for t in self._timers:
            t.cancel()
        with self._q_lock:
            for t in self._queue_timers.values():
                t.cancel()
            self._queue_timers.clear()
            self._queues.clear()
