"""LLM 评分模块。

用 OpenAI 兼容协议（DeepSeek / qwen 都兼容）调用，对单条消息打分 1~5：
5 = 必须立即处理（合同/付款/截止日）
4 = 值得关注（涉及重要决策/客户）
3 = 普通业务消息
2 = 闲聊倾向
1 = 噪声（表情、签到）

返回 (score, reason)。
"""
import re
import time
from dataclasses import dataclass


_SYSTEM_PROMPT = """你是一名微信群消息的"信息助理"。用户疲于应付过多群消息，希望你帮他标记出真正重要的内容。

评分标准（1~5）：
- 5：必须立即处理。包含付款/合同/截止日/客户紧急请求/明确 @用户 等。
- 4：值得关注。包含报价、关键决策、项目进度更新、明显是用户关心的项目话题。
- 3：普通业务消息。不紧急但与工作相关。
- 2：闲聊倾向。讨论题外话题。
- 1：噪声。表情、签到、灌水、无意义回复。

输出格式（严格遵守，不要多余内容）：
SCORE: <1-5>
REASON: <不超过 30 字的中文理由>
"""


@dataclass
class ScorerConfig:
    base_url: str
    api_key: str
    model: str


class Scorer:
    def __init__(self, cfg: ScorerConfig):
        self.cfg = cfg
        self._client = None  # lazy init，避免 import 期就要求 openai

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI  # 延迟导入
            self._client = OpenAI(
                base_url=self.cfg.base_url,
                api_key=self.cfg.api_key,
                timeout=15.0,          # 单次 LLM 调用最长 15s
                max_retries=0,         # 重试我们自己控制
            )
        return self._client

    def score(self, *, group_name: str, sender: str, content: str) -> tuple[int, str]:
        """返回 (1-5 的整数评分, 简短理由)。

        失败/未配置时返回 (3, "<前缀>: <原因>")，前缀为 "score_skipped" 或 "score_failed"。
        调用方可通过判断 reason 前缀来决定是否触发推送，
        避免 LLM 故障时反而批量推送中性分。
        """
        if not self.cfg.api_key or self.cfg.api_key == "REPLACE_ME":
            return 3, "score_skipped: no api_key"

        user_msg = (
            f"群名：{group_name}\n"
            f"发送人：{sender}\n"
            f"消息内容：\n{content[:1500]}"
        )
        last_err: Exception | None = None
        for attempt in range(3):  # 最多 3 次（含首次）
            try:
                client = self._get_client()
                resp = client.chat.completions.create(
                    model=self.cfg.model,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.0,
                    # 不设 max_tokens 限制：按实际用量计费，模型写完自然停
                )
                text = self._strip_think(resp.choices[0].message.content or "")
                return self._parse(text)
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))  # 1.5s, 3s
        # 评分失败不阻塞主流程，但 reason 明确标识为不可推送
        return 3, f"score_failed: {type(last_err).__name__ if last_err else 'Unknown'}"

    @staticmethod
    def _strip_think(text: str) -> str:
        """去掉推理模型的  thinking... 推理块（含被截断未闭合的情况）。"""
        text = re.sub(r" thinking.*?\s*", "", text, flags=re.DOTALL)
        if " thinking" in text:
            text = text[: text.index(" thinking")]
        return text.strip()

    @staticmethod
    def _parse(text: str) -> tuple[int, str]:
        score = 3
        reason = "parse_failed"
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("SCORE:"):
                try:
                    n = int(line.split(":", 1)[1].strip().split()[0])
                    if 1 <= n <= 5:
                        score = n
                except (ValueError, IndexError):
                    pass
            elif line.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()[:60]
        return score, reason