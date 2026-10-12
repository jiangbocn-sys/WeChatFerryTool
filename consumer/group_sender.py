"""把每日总结发到群（用户可选，2026-10-10 需求）。

⚠️ **这是本项目里唯一会主动往群里发言的功能**（自动回复是另一条独立链路）。
**两条链路完全独立、互不影响**（用户明确要求"不同功能线，不要互相限制"）：
本模块**不读** `replies.*`（自动回复的 enabled / templates / rate_limit 都不是它的条件），
也不依赖 replier 的任何状态；关掉自动回复照样发，反过来关掉本功能也不影响自动回复。
两者只共用最底层的 `HookClient.send_text` 客户端（就像都用同一个 hook 一样），
各自有各自的开关与限速。

所以设计上以"保守、可关、可审计"为第一原则：

1. **唯一开关就是每群勾选**：在「🎯 监控规则」页勾选"把总结发到群内"
   （`labels.json` 里 `groups.<gid>.send_summary`，默认 **false**）。
   没有全局总闸 —— 想停某个群就取消它的勾选。
   （`config.yaml` 的 `digest.send_to_group` 段**只放调参**：`dry_run` / `chunk_chars` /
   `min_interval_s`；**没有 enabled 这个键**，是否发送完全由每群勾选决定。）
2. **只发本群自己的总结**：绝不把别的群的内容发进来（`send_summary_mode:
   per_group` 时每群一份文件；`combined` 模式下**不会发**，因为合并稿含其它群内容）。
3. **长度切分**：微信单条文本框有上限，按 `chunk_chars` 切段发送，段间可加序号。
4. **限速**：每次发送之间 `min_interval_s` 秒，降低风控概率。
5. **审计**：每发一条都写 `data/summary_sent.jsonl`，并调 `SentTracker.record()`
   让它记进"自己发出的消息"（避免回流成新消息）。
6. **失败不致命**：任何异常只记日志；归档/总结/抓取都不受影响。

只做"发送"这一件事，文案清洗与切分是纯函数、可单测。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("consumer.group_sender")


# ---- 文案清洗（纯函数，便于单测） ----

def md_to_plain(text: str) -> str:
    """把总结的 Markdown 清成适合微信显示的纯文本。

    只做**保守**处理（微信不渲染 Markdown，留着 `**` 反而难看）：
      * 去掉行首 `- ` / `* ` 列表符号 → 保留为「· 」
      * 去掉 `**粗体**` / `*斜体*` / `__粗体__` 的标记符
      * 去掉行首 `#` 标题符
      * 去掉 ``` 代码围栏
      * 去掉表格分隔行（|---|）
    绝不改动正文里的中文引号、括号、数字。
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        # 表格分隔行（|---|---|）→ 丢掉，避免一堆横线
        if stripped and re.fullmatch(r"\|?[\s:|-]+\|?", stripped) and "-" in stripped:
            continue
        if stripped.startswith("```"):
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)          # 标题符
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1· ", line)         # 列表符
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)            # 粗体
        line = re.sub(r"__(.+?)__", r"\1", line)                # 粗体（下划线式）
        line = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", line)  # 斜体
        line = re.sub(r"`([^`]+)`", r"\1", line)                # 行内代码
        out.append(line)
    # 折叠 3 个以上连续空行
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunk_text(text: str, limit: int = 1800) -> list[str]:
    """把长文本切成不超过 `limit` 字的若干段（尽量在空行/换行处切，其次硬切）。

    微信单条文本框有上限，超长会发送失败或只发出前半截。
    """
    text = (text or "").strip()
    if not text:
        return []
    limit = max(200, int(limit))
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        # 优先在空行处断，其次换行，再其次句末标点，最后硬切
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = max(window.rfind(c) for c in "。！？；")
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest.strip():
        chunks.append(rest.strip())
    return [c for c in chunks if c]


def make_messages(date_str: str, group_name: str, summary: str,
                  limit: int = 1800, header: bool = True) -> list[str]:
    """把一份总结变成"要依次发出的若干条消息"。

    首条带抬头（日期 + 群名 + 说明），便于群里的人知道这是什么。
    """
    body = md_to_plain(summary)
    parts = chunk_text(body, limit=limit)
    if not parts:
        return []
    msgs: list[str] = []
    if header:
        msgs.append(f"【{date_str} 群聊总结】{group_name}\n（由工具自动汇总，仅供参考）")
    if len(parts) == 1:
        msgs.append(parts[0])
    else:
        for i, p in enumerate(parts, 1):
            msgs.append(f"（{i}/{len(parts)}）\n{p}")
    return msgs


# ---- 发送 ----

class SummarySender:
    """按开关把总结发到群，并写审计日志。"""

    def __init__(self, hook, data_root: Path, cfg: dict | None = None,
                 tracker=None):
        self.hook = hook
        self.data_root = Path(data_root)
        self.cfg = cfg or {}
        self.tracker = tracker
        self.audit_path = self.data_root / "data" / "summary_sent.jsonl"

    # -- 开关 --
    @staticmethod
    def enabled_for_group(labels: dict, gid: str) -> bool:
        """该群是否勾选了"把总结发到群内"（默认关）。**这是唯一的发送开关。**"""
        ent = ((labels or {}).get("groups") or {}).get(gid) or {}
        return bool(ent.get("send_summary", False))

    # -- 审计 --
    def _audit(self, gid: str, n: int, ok: int, err: str = "") -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": int(time.time()),
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "group": gid, "messages": n, "sent": ok, "error": err[:200],
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def send_group(self, gid: str, msgs: list[str], dry_run: bool = False) -> int:
        """把 `msgs` 依次发给 `gid`，返回成功条数。失败只记日志。"""
        interval = float(self.cfg.get("min_interval_s", 1.5))
        sent = 0
        for i, m in enumerate(msgs):
            if dry_run:
                log.info("[演练] 将发往 %s 第 %d/%d 条（%d 字）：%s",
                         gid, i + 1, len(msgs), len(m), m[:40].replace("\n", " "))
                sent += 1
                continue
            try:
                self.hook.send_text(gid, m)
            except Exception as e:  # noqa: BLE001
                log.warning("总结发送失败 group=%s 第 %d 条: %s: %s",
                            gid, i + 1, type(e).__name__, str(e)[:120])
                self._audit(gid, len(msgs), sent, f"{type(e).__name__}: {e}")
                return sent
            sent += 1
            # 记进"自己发出的消息"，避免回流成新消息被再次归档
            try:
                if self.tracker is not None:
                    self.tracker.record(gid, m)
            except Exception:  # noqa: BLE001
                pass
            if interval > 0 and i + 1 < len(msgs):
                time.sleep(interval)
        self._audit(gid, len(msgs), sent)
        return sent
