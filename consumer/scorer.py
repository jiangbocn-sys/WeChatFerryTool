"""LLM 评分模块。

用 OpenAI 兼容协议（DeepSeek / qwen 都兼容）调用，对单条消息打分 1~5：
5 = 必须立即处理（合同/付款/截止日）
4 = 值得关注（涉及重要决策/客户）
3 = 普通业务消息
2 = 闲聊倾向
1 = 噪声（表情、签到）

返回 (score, reason)。
"""
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
            self._client = OpenAI(base_url=self.cfg.base_url, api_key=self.cfg.api_key)
        return self._client

    def score(self, *, group_name: str, sender: str, content: str) -> tuple[int, str]:
        """返回 (1-5 的整数评分, 简短理由)。失败时返回 (3, "score_failed")。"""
        if not self.cfg.api_key or self.cfg.api_key == "REPLACE_ME":
            return 3, "score_skipped: no api_key"

        user_msg = (
            f"群名：{group_name}\n"
            f"发送人：{sender}\n"
            f"消息内容：\n{content[:1500]}"
        )
        try:
            client = self._get_client()
            resp = client.chat.completions.create(
                model=self.cfg.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=120,
            )
            text = resp.choices[0].message.content.strip()
            return self._parse(text)
        except Exception as e:  # noqa: BLE001
            # 评分失败不阻塞主流程，给中性分
            return 3, f"score_failed: {type(e).__name__}"

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