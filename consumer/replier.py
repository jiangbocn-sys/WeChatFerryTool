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
    # LLM 不可用（未配 key / 调用失败 / 超时）时发送的固定文案。
    # 必须与 response 分开：use_llm=True 时 response 是**人设指令**，
    # 直接发出去会"破功"，所以不能拿它兜底。
    fallback: str = ""


@dataclass
class KbConfig:
    """群知识库问答的**独立**参数（与自动回复模板互不影响）。

    用户 2026-10-15 定的口径：
      * 每群**各自** 2 分钟一条（`per_group_cooldown_s=120`）
      * 连续提问**排队**（上限 10，同人去重）
      * 回复前随机 **5~10 秒**延迟
      * 默认 `test_only=True` 先观察
      * 触发：消息**以 `@<虚拟号>` 开头**（名字**按群可配**）
    """
    enabled: bool = False
    cooldown_s: int = 120             # 每群两次回答最小间隔
    delay_min_s: int = 5
    delay_max_s: int = 10
    max_queue: int = 10               # 每群最多排队条数
    daily_limit: int = 200            # 全局日限（防失控）
    dedup_window_s: int = 120         # 同人重复提问去重窗口
    self_check: bool = True           # 回答后做第二遍自检
    top_k: int = 5                    # 检索片段数
    test_only: bool = True            # 只记日志不发（默认开）
    #: 是否允许**自己发**的消息触发（默认 True）。
    #: 为什么默认允许：知识库必须**显式** `@虚拟号` 开头才命中（不是泛匹配），
    #: 不存在"自问自答刷屏"的风险；而实测中用户就是**用自己的号在群里 @小易 测试**
    #: （2026-10-15 踩到：那条消息 `sender=本人` → 被 `if not is_self` 整段跳过，
    #: 表现为"@了完全没反应"）。自动回复模板仍**不**对自发消息生效（保持原样）。
    allow_self: bool = True
    disclaimer: str = "（据资料整理，如有出入以老师讲解为准）"


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
    kb: KbConfig = field(default_factory=KbConfig)
    #: 群 → 虚拟号名（`labels.groups.<gid>.kb_at`）。触发用，按群可配。
    kb_trigger_by_group: dict[str, str] = field(default_factory=dict)
    #: 群 → 是否开启知识库问答（`labels.groups.<gid>.kb_on`）
    kb_on_by_group: dict[str, bool] = field(default_factory=dict)
    #: 群 → 资料目录（`labels.groups.<gid>.kb_dir`）。用于解析 `kb.root` 共享笔记。
    kb_dir_by_group: dict[str, str] = field(default_factory=dict)


class Replier:
    def __init__(
        self,
        cfg: ReplyConfig,
        hook: HookClient,
        rate_limiter: RateLimiter,
        labels_path: Path | None = None,
        sent_tracker: "SentTracker | None" = None,
        history_fn: "Callable[[str, int], list[dict]] | None" = None,
        state_path: Path | None = None,
    ):
        self.cfg = cfg
        self.hook = hook
        self.rl = rate_limiter
        self.labels_path = labels_path
        self.sent_tracker = sent_tracker
        self.history_fn = history_fn  # (group_name, limit) -> 最近消息列表（时间倒序）
        # 知识库用**独立**的限速状态文件（与模板回复互不干扰）
        self._kb_state_path = (state_path or (rate_limiter.state_path)).with_name(
            (state_path or rate_limiter.state_path).stem + "_kb.json")
        self._timers: list[threading.Timer] = []  # 防止 GC
        # 冷却期内的触发排队（不丢弃）
        self.MAX_QUEUE_PER_GROUP = 3
        self._queues: dict[str, deque] = {}
        self._queue_timers: dict[str, threading.Timer] = {}
        self._q_lock = threading.Lock()
        # ---- 知识库问答：独立限速 / 队列 / 去重 ----
        self._kb_rl = RateLimiter(
            self._kb_state_path,
            RateLimitConfig(per_group_cooldown_s=int(cfg.kb.cooldown_s),
                            global_daily_limit=int(cfg.kb.daily_limit),
                            min_delay_s=int(cfg.kb.delay_min_s),
                            max_delay_s=int(cfg.kb.delay_max_s)),
        )
        self._kb_q: dict[str, deque] = {}       # 群 → [(sender, question, ts)]
        self._kb_timer: dict[str, threading.Timer] = {}
        self._kb_lock = threading.Lock()
        self._kb_seen: deque = deque(maxlen=200)   # (群, 发送人, 问题指纹, 时间)
        self._kb_data_root: Path | None = None
        self._kb_ask = None                     # 延迟注入 kb_answer.answer_question

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
                    fallback=str(t.get("fallback", "") or ""),
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
        # ---- 群知识库问答（独立子段 `replies.kb`）----
        kr = raw.get("kb") or {}
        cfg.kb = KbConfig(
            enabled=bool(kr.get("enabled", False)),
            cooldown_s=int(kr.get("cooldown_s", 120)),
            delay_min_s=int(kr.get("delay_min_s", 5)),
            delay_max_s=int(kr.get("delay_max_s", 10)),
            max_queue=int(kr.get("max_queue", 10)),
            daily_limit=int(kr.get("daily_limit", 200)),
            dedup_window_s=int(kr.get("dedup_window_s", 120)),
            self_check=bool(kr.get("self_check", True)),
            top_k=int(kr.get("top_k", 5)),
            test_only=bool(kr.get("test_only", True)),
            allow_self=bool(kr.get("allow_self", True)),
            disclaimer=str(kr.get("disclaimer")
                           or "（据资料整理，如有出入以老师讲解为准）"),
        )
        obj = cls(cfg, hook, RateLimiter(state_path, rl_cfg), labels_path=labels_path,
                  sent_tracker=sent_tracker, history_fn=history_fn, state_path=state_path)
        if labels_path:
            obj._load_kb_group_fields(labels_path)
        return obj

    # ---- 知识库：按群的开关与虚拟号名 ----

    def _load_kb_group_fields(self, labels_path: Path) -> None:
        """从 labels.json 读**按群**的 `kb_on` / `kb_at`（页面可改，热重载可用）。"""
        try:
            data = json.loads(labels_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        groups = data.get("groups") or {}
        self.cfg.kb_trigger_by_group = {
            str(g): str((v or {}).get("kb_at") or "").strip()
            for g, v in groups.items()
            if str((v or {}).get("kb_at") or "").strip()
        }
        self.cfg.kb_on_by_group = {
            str(g): bool((v or {}).get("kb_on"))
            for g, v in groups.items() if (v or {}).get("kb_on")
        }
        # 资料目录也要带上：多群可能通过 `kb.root` 映射共享同一套笔记，
        # 只按群名找笔记会"查无资料"（2026-10-15 踩到）。
        self.cfg.kb_dir_by_group = {
            str(g): str((v or {}).get("kb_dir") or "").strip()
            for g, v in groups.items() if str((v or {}).get("kb_dir") or "").strip()
        }

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
                # LLM 失败：优先用模板自带的固定兜底；没配才用内置极简文案。
                # 绝不回退到 response —— use_llm=True 时那是人设，发出去会破功。
                llm_failed = True
                fb = (template.fallback or "").strip()
                if fb:
                    log.warning("LLM 生成失败，改用模板固定兜底（persona=%s）", template.name)
                    text = fb
                else:
                    log.warning("LLM 生成失败且模板未配 fallback，使用内置兜底（persona=%s）",
                                template.name)
                    text = "[自动回复：稍后再聊]"
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
        with self._kb_lock:
            for t in self._kb_timer.values():
                t.cancel()
            self._kb_timer.clear()
            self._kb_q.clear()

    # =====================================================================
    # 群知识库问答（独立于上面的模板回复）
    # =====================================================================

    def reload_kb_group_fields(self) -> None:
        """页面改了按群的开关/虚拟号名后，热重载（不重启 app）。"""
        if self.labels_path:
            self._load_kb_group_fields(self.labels_path)

    def kb_trigger_name(self, group_name: str) -> str:
        """该群的虚拟号名（没配就用全局默认 `小易`）。"""
        return (self.cfg.kb_trigger_by_group.get(group_name) or "小易").strip()

    def kb_enabled_for(self, group_name: str) -> bool:
        """该群是否开了知识库问答。

        **三道闸都要开**（2026-10-15 修）：
          1. `replies.enabled` —— 自动回复总开关。app 启动时**默认强制关闭**它
             （`app/main.py` 的"默认策略"，托盘菜单可开）。
             知识库**必须尊重同一个闸**：否则页面显示"自动回复:关"，
             知识库却还在群里真实发言 —— 这种不一致迟早出事。
          2. `replies.kb.enabled` —— 知识库总闸。
          3. 该群的 `kb_on` —— 按群开关。

        全开才算开：避免"总闸一开所有群都被答"，也避免绕过 app 的默认静默策略。
        """
        if not self.cfg.enabled:
            return False
        if not self.cfg.kb.enabled:
            return False
        return bool(self.cfg.kb_on_by_group.get(group_name))

    def maybe_reply_kb(self, *, group_name: str, sender: str, content: str,
                       is_self: bool = False) -> bool:
        """知识库问答入口。返回 True 表示**已接管**这条消息（调用方就不用再走模板匹配）。

        流程：触发匹配 → 去重 → 限速判定 → 直接答 or 排队。

        `is_self=True`（自己发的消息）：默认**也允许触发**，因为知识库要求
        显式 `@虚拟号` 开头 —— 这是"人主动问助教"，不是自问自答刷屏。
        实测用户就是用自己的号在群里 @小易 测试的（2026-10-15）。
        可用 `replies.kb.allow_self: false` 关掉。
        """
        if is_self and not self.cfg.kb.allow_self:
            return False
        if not self.kb_enabled_for(group_name):
            return False
        name = self.kb_trigger_name(group_name)
        try:
            from consumer.kb_answer import strip_trigger
        except Exception as e:  # noqa: BLE001
            log.warning("知识库模块不可用: %s", e)
            return False
        q = strip_trigger(content, name)
        if q is None:
            # ⚠️ 诊断日志（2026-10-15 加）：**只在看起来像想触发、但没匹配上时**记一条，
            #    否则群消息量大会刷屏。排查"@了却没反应"时这条是关键线索 ——
            #    能看到"名字对不对、是不是没在开头、是不是只 @ 没说话"。
            if content.lstrip().startswith("@") or ("@" + name) in content:
                log.info("知识库未触发：群=%s 期望前缀=@%s 实际=%r（原因：%s）",
                         group_name, name, content[:60],
                         "不在消息开头" if ("@" + name) in content
                         else "名字不匹配或只是@没说内容")
            return False
        # 去重：同群、同人、相似问题，在一个窗口内只答一次
        fp = self._kb_fp(q)
        now = time.time()
        with self._kb_lock:
            for g, s, f, ts in list(self._kb_seen):
                if g == group_name and s == sender and f == fp \
                        and now - ts < self.cfg.kb.dedup_window_s:
                    log.info("知识库：重复提问已忽略（群=%s 人=%s）", group_name, sender)
                    return True
            self._kb_seen.append((group_name, sender, fp, now))

        if self.cfg.kb.test_only:
            log.info("🧪 [知识库·测试模式] 群=%s 提问=%s 问题=%s",
                     group_name, sender, q[:80])
            return True

        allowed, reason, wait_s = self._kb_rl.can_reply(group_name)
        if allowed:
            delay = self._kb_rl.pick_delay()
            log.info("📚 计划回答 群=%s 延迟=%ds 问题=%s", group_name, delay, q[:60])
            self._schedule_kb(group_name, delay, sender, q)
            return True
        if reason.startswith("group_cooldown"):
            with self._kb_lock:
                dq = self._kb_q.setdefault(group_name, deque())
                if len(dq) >= self.cfg.kb.max_queue:
                    dropped = dq.popleft()        # 丢最早的（已过时）
                    log.warning("知识库队列已满，丢弃最早一条：群=%s 问题=%s",
                                group_name, str(dropped[1])[:40])
                dq.append((sender, q, now))
                qlen = len(dq)
            log.info("知识库冷却中（还需 %ds），已排队第 %d 条：群=%s 问题=%s",
                     wait_s, qlen, group_name, q[:40])
            self._schedule_kb(group_name, max(1.0, wait_s + 1), None, None)
            return True
        log.info("知识库不回答（不可排队）：群=%s 原因=%s", group_name, reason)
        return True

    @staticmethod
    def _kb_fp(q: str) -> str:
        """问题指纹：去空白/标点后取前 40 字（用于"同一问题"去重）。"""
        s = re.sub(r"[\s\u2005\u00a0，,。.！!？?；;：:、]+", "", q or "")
        return s[:40]

    def _schedule_kb(self, group_name: str, after_s: float,
                     sender: str | None, question: str | None) -> None:
        """安排一次知识库回答（直接答 or 排空队列）。"""
        if sender is not None and question is not None:
            t = threading.Timer(max(0.1, after_s), self._kb_answer_one,
                                args=(group_name, sender, question))
            t.daemon = True
            t.start()
            self._timers.append(t)
            return
        with self._kb_lock:
            cur = self._kb_timer.get(group_name)
            if cur is not None and cur.is_alive():
                return
            t = threading.Timer(max(1.0, after_s), self._kb_drain, args=(group_name,))
            t.daemon = True
            self._kb_timer[group_name] = t
            t.start()

    def _kb_drain(self, group_name: str) -> None:
        """冷却结束后逐条回答排队的提问（每条重新等冷却，保持 2 分钟间隔）。"""
        while True:
            with self._kb_lock:
                dq = self._kb_q.get(group_name)
                if not dq:
                    self._kb_timer.pop(group_name, None)
                    return
            allowed, reason, wait_s = self._kb_rl.can_reply(group_name)
            if not allowed:
                if reason.startswith("group_cooldown"):
                    time.sleep(min(max(wait_s, 1), 60))
                    continue
                with self._kb_lock:
                    dropped = len(self._kb_q.pop(group_name, []) or [])
                    self._kb_timer.pop(group_name, None)
                log.warning("知识库日限已满，丢弃排队 %d 条：群=%s", dropped, group_name)
                return
            with self._kb_lock:
                dq = self._kb_q.get(group_name)
                if not dq:
                    self._kb_timer.pop(group_name, None)
                    return
                sender, q, _ts = dq.popleft()
                remain = len(dq)
            log.info("知识库排队出队回答：群=%s（剩余 %d）", group_name, remain)
            self._kb_answer_one(group_name, sender, q)

    def _kb_answer_one(self, group_name: str, sender: str, question: str) -> None:
        """真正查资料 + 生成 + 自检 + 发送（失败只记日志，绝不发兜底噪音）。"""
        data_root = self._kb_data_root
        if data_root is None:
            try:
                import paths
                data_root = paths.data_root()
            except Exception:  # noqa: BLE001
                log.error("知识库：拿不到数据根，放弃回答")
                return
        try:
            if self._kb_ask is None:
                from consumer.kb_answer import answer_question as _aq
                self._kb_ask = _aq
            res = self._kb_ask(question, group_name, data_root,
                               top_k=self.cfg.kb.top_k,
                               self_check=self.cfg.kb.self_check,
                               kb_dir=self.cfg.kb_dir_by_group.get(group_name) or None,
                               disclaimer=self.cfg.kb.disclaimer)
        except Exception as e:  # noqa: BLE001
            log.error("知识库回答失败（不发兜底噪音）：群=%s 问题=%s err=%s",
                      group_name, question[:40], e)
            return
        text = (res.answer or "").strip()
        if not text:
            log.warning("知识库：空回答，未发送（群=%s 问题=%s）", group_name, question[:40])
            return
        # 群里带上提问人的 @ 前缀（复用现有 _resolve_at）
        at = self._resolve_at(sender)
        if at and "@chatroom" in group_name and not text.lstrip().startswith("@"):
            text = at + text
        try:
            result = self.hook.send_text(group_name, text)
            log.info("📚 知识库已回答 群=%s ret=%s 来源=%d 段 字数=%d",
                     group_name, result, len(res.hits), len(text))
            self._kb_rl.record_reply(group_name)
            if self.sent_tracker is not None:
                self.sent_tracker.record(group_name, text)
            self._kb_audit(group_name, sender, question, res, text)
        except HookError as e:
            log.error("知识库发送失败: %s", e)

    def _kb_audit(self, group_name: str, sender: str, question: str,
                  res, text: str) -> None:
        """审计：问题 / 命中来源 / 回答 / 自检结论，写 `data/kb_qa.jsonl`。

        为什么要审计：`send_text` 的返回值**恒为 ret=0**（项目里已踩过），
        发送成功与否无法从返回值判断；出问题只能靠这份日志回查。
        """
        try:
            import paths
            p = paths.data_root() / "data" / "kb_qa.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "group": group_name,
                "asker": sender,
                "question": question,
                "hits": [{"source": h.source, "topic": h.topic, "score": round(h.score, 3)}
                         for h in (res.hits or [])],
                "grounded": res.grounded,
                "dropped": (res.dropped or "")[:300],
                "llm_calls": res.llm_calls,
                "from_kb": res.from_kb,
                "answer": text,
            }
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            log.debug("知识库审计写入失败: %s", e)
