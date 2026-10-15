"""把"归档用的详细总结"提炼成"适合发到群里的成品稿"（2026-10-12 用户要求）。

用户要求："发送到群内的总结不需要包含聊天记录"。

为什么要二次提炼
----------------
归档总结（`summary-<日期>-<群名>.md`）是给**用户自己看的**，规则要求它保留人名/时间/数字/
待办等关键信息，所以读起来会比较贴近聊天内容。**直接把它发进群**就会像在复述聊天记录。

所以发群前再走一次 LLM，把详细稿**提炼成群内成品**：
  * ✗ 不复述、不引用具体聊天内容（不出现"某某说…"这类逐条转述）
  * ✗ 不带时间戳、不带逐条发言
  * ✓ 按主题归纳当天讨论了什么、形成了什么结论、有什么待办
  * ✓ 保留有用的结论性信息（卦例要点、作业安排、结论），但**不点名引用原话**

⚠️ 这是**额外一次 LLM 调用**（可用 `digest.send_to_group.refine: false` 关掉，
关掉就直接发归档稿 —— 那会带较多聊天细节）。

失败不致命：提炼失败/为空时**回退用原文**（宁可发详细稿，也不要漏发），并记日志。
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("consumer.group_summary")

#: 提炼提示词。刻意**不把原始聊天记录**再喂一遍 —— 只喂归档总结，
#: 既省额度，也避免模型又被聊天细节带回去逐条复述。
REFINE_PROMPT = """下面是一份**供内部归档使用的**群聊总结稿。请把它改写成**可以直接发到群里的成品**。

改写要求（重要）：
1. **不要包含聊天记录**：不要逐条转述谁说了什么，不要写"某某说/某某问/某某回复"，
   不要引用具体发言原话，不要带时间戳。要的是**归纳**，不是记录。
2. 按**主题**归纳当天讨论的内容：聊了哪几件事、得出了什么结论/共识、有什么待办或安排。
3. 站在"给群里所有人看"的角度写：客观、简洁、不评价个人，不透露谁问得多/谁被点名。
4. 保留**结论性、知识性**的要点（例如课程讲到的方法与要点、确定下来的时间与安排）。
   但把与人名绑定的细节去掉，改写成一般性表述。
5. 格式：每条一行，以「- 」开头；3~8 条为宜；总长尽量不超过 500 字。
   不要加标题、不要加落款、不要写"以下是总结"。
6. 若原稿信息很少，就只写这一两句，不要为凑条数而扩写或编造。
7. 不要输出任何与改写无关的解释或客套话。
8. **⚠️ 原稿里如果有一行以「提醒：本时段」开头的语音提醒（形如
   `提醒：本时段 ★某某 于 HH:MM 起发了 N 条语音（内容未获取），请回群自行查看`），
   必须原样保留在改写结果的最后一行、一个字都不要改**：
   它的作用正是告诉群里的人"还有这些语音没拿到内容、请自己回去听"，
   属于**对读者有用的信息**，不在"要剔除的聊天记录"之列。
   注意：第 5 条"总长不超过 500 字"**不限制这一行**，不要为了压字数把它删掉。

原稿：
{summary}

请直接输出改写后的成品（只输出要点，每行以「- 」开头）："""


#: 语音提醒行的识别正则。
#: ⚠️ 必须**宽容**：模型经常改措辞（漏掉"请回群自行查看"、写成"回群自行查看"、
#: 漏掉前导 `- `），一旦匹配不上，兜底就会**再补一条** → 正文里出现两条提醒行
#: （2026-10-15 实测 `岳章形家风水班.md` 里就同时有 ★可贞 与 ★周少彬 两条）。
#: 这里只要求以「提醒：本时段」开头，到行尾为止都算这一行。
REMINDER_RE = re.compile(r"^[ \t]*[-*]?[ \t]*提醒：本时段[^\n]*", re.M)


def extract_reminder(text: str) -> str:
    """从文本里取出语音提醒行（规范化成 `- 提醒：…` 一行）。取不到返回空串。"""
    m = REMINDER_RE.search(text or "")
    if not m:
        return ""
    line = m.group(0).strip()
    while line[:1] in ("-", "*"):
        line = line[1:].strip()
    return "- " + line


def ensure_reminder(refined: str, source: str, want: str = "") -> str:
    """**程序保证**提炼结果里有语音提醒行，并**规范成 `want` 的确切文本**。

    2026-10-13 实测问题：归档稿里有提醒行，但提炼时被模型当噪音删掉了
    （1071 字 → 343 字，提醒行整行消失），于是**发进群的内容里没有提醒**。
    2026-10-15 又发现第二层问题：模型在总结里只写了句含糊的
    「提醒：本时段 有语音，请回群自行查看」（没有时间、没有人名），提炼会原样留下。

    所以统一用**程序生成的确切文本**（`digest.reminder_line`）：有就替换、没有就补上。
    `want` 传空时退回"从 source 里取"的行为。
    """
    from consumer.digest import replace_reminder        # 局部 import 避免循环依赖
    want = want or extract_reminder(source)
    if not want:
        return refined
    return replace_reminder(refined, want)


def refine_for_group(summary: str, llm_cfg: dict, date_str: str = "",
                     group_name: str = "", timeout_s: float = 180.0) -> str:
    """用一次 LLM 调用把归档总结提炼成群内成品稿。

    失败时返回空串（调用方应回退用原文）。
    """
    src = (summary or "").strip()
    if not src:
        return ""
    from consumer.summarize import call_llm, strip_think
    prompt = REFINE_PROMPT.replace("{summary}", src)
    try:
        out = strip_think(call_llm(prompt, llm_cfg, timeout_s=timeout_s))
    except Exception as e:  # noqa: BLE001
        log.warning("总结提炼失败（将回退用原稿）: %s: %s", type(e).__name__, str(e)[:120])
        return ""
    out = (out or "").strip()
    if not out:
        log.warning("总结提炼返回空（将回退用原稿）")
        return ""
    # 模型偶尔会把原稿整段带回来：那种情况视为提炼失败（否则等于没提炼）
    if out == src:
        log.warning("总结提炼结果与原稿完全相同（视为未提炼，回退用原稿）")
        return ""
    # ⚠️ 兜底：语音提醒行必须留下（实测模型会把它当噪音删掉 → 发进群里就没有提醒了）
    if extract_reminder(src) and not extract_reminder(out):
        log.info("提炼把语音提醒行删掉了，已按程序兜底补回")
        out = ensure_reminder(out, src)
    return out
