"""群知识库：**检索 + 生成 + 自检**（回答严格限定在资料内容里）。

链路
----
```
问题 → ① 本地检索（关键词 + BM25，从笔记里选 top-k 段）
     → ② 生成（只喂选中的片段，强制标 〔来源〕）
     → ③ **第二遍自检**（拿"回答 + 片段"再问一次：有没有片段里查不到的内容？）
     → ④ 有越界 → 删掉那些句子 / 整条兜底；查不到 → 明确回"资料未涉及"
```

为什么必须做第 ③ 步
------------------
大模型天生会"补全"。只靠提示词写"不要编造"**不可靠**（本项目在总结功能上反复验证过）。
所以生成之后**再花一次调用**独立校验，这是"只用资料回答"能落地的关键。

检索为什么用**确定性关键词 + BM25** 而不是向量库
--------------------------------------------
* 零新依赖、零额外费用；
* **可解释**：能列出"命中了哪几段、为什么"（知识库问答里这比召回率更重要）；
* 出错可人工修（改笔记标题/加关键词），向量库出错只能干瞪眼。
召回不够时再叠加 embedding，不冲突。
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from consumer.kb_notes import kb_root, load_sources

log = logging.getLogger("consumer.kb_answer")

#: 回答后固定追加的免责尾（用户同意带）。可在配置里关掉。
DISCLAIMER = "（据资料整理，如有出入以老师讲解为准）"

#: 单段片段最多喂多少字（太长会挤掉其它片段）
MAX_SEG_CHARS = 2500
#: 默认检索段数
DEFAULT_TOP_K = 5
#: 低于这个相关度认为"没命中"
MIN_SCORE = 0.15


# ---------------------------------------------------------------------------
# 中文分词（不引入 jieba：用 n-gram + 停用词，够用且零依赖）
# ---------------------------------------------------------------------------

_STOP = set("的了是在和与及也就都而及其这那有你我他她它们个之对于把被为以"
            "什么怎么如何请问一下可以吗呢吧啊呀哦嗯请问请教老师群友问题")


def _tokens(text: str) -> list[str]:
    """粗分词：单个汉字 + 相邻二字组（bigram），去掉停用词与标点。

    bigram 对中文术语很有效（"回头克""用神""旬空"都是 2~3 字），
    且无需词典 —— 检索场景够用，还避免了 jieba 这类新依赖。
    """
    text = (text or "").lower()
    # 只保留中文、字母、数字
    chars = re.findall(r"[\u4e00-\u9fff]|[a-z0-9]+", text)
    uni = [c for c in chars if c not in _STOP]
    out = list(uni)
    # 汉字 bigram（跳过被停用词隔开的位置）
    han = "".join(c for c in chars if len(c) == 1)
    for i in range(len(han) - 1):
        bg = han[i:i + 2]
        if bg[0] in _STOP or bg[1] in _STOP:
            continue
        out.append(bg)
    return out


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Hit:
    """一个检索命中的片段。"""
    note_file: str          # 笔记文件名（对外显示的"来源"）
    source: str             # 原始资料文件（更准确的出处）
    topic: str              # `## 主题`
    text: str
    score: float

    @property
    def citation(self) -> str:
        return f"{self.source}「{self.topic}」" if self.topic else self.source


@dataclass
class AnswerResult:
    question: str = ""
    group: str = ""
    hits: list[Hit] = field(default_factory=list)
    prompt: str = ""
    answer: str = ""
    raw_answer: str = ""
    grounded: bool | None = None       # 自检结论（None = 没做自检）
    dropped: str = ""                  # 自检删掉的内容（留证）
    llm_calls: int = 0
    from_kb: bool = True               # False = 资料里没有 → 走了兜底话术


# ---------------------------------------------------------------------------
# 载入笔记
# ---------------------------------------------------------------------------

@dataclass
class NoteSection:
    note_file: str
    source: str
    topic: str
    body: str


_H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)


def load_sections(group: str, data_root: Path, kb_dir: str | None = None) -> list[NoteSection]:
    """把该群所有笔记切成 `## 主题` 段（`kb_dir` 用于解析 `kb.root` 共享笔记）。"""
    root = kb_root(data_root, group, kb_dir)
    entries = load_sources(root)
    out: list[NoteSection] = []
    for rel, ent in entries.items():
        if not ent.note_file:
            continue
        p = root / ent.note_file
        if not p.is_file():
            continue
        try:
            md = p.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("笔记读取失败 %s: %s", p, e)
            continue
        # 去掉文件头注释与 <sub> 元信息
        md = re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL)
        md = re.sub(r"^<sub>.*?</sub>\s*$", "", md, flags=re.M)
        marks = list(_H2.finditer(md))
        if not marks:
            body = md.strip()
            if body:
                out.append(NoteSection(ent.note_file, rel, "", body))
            continue
        for i, m in enumerate(marks):
            start = m.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(md)
            body = md[start:end].strip()
            if body:
                out.append(NoteSection(ent.note_file, rel, m.group(1).strip(), body))
    return out


# ---------------------------------------------------------------------------
# 检索（BM25）
# ---------------------------------------------------------------------------

def retrieve(question: str, sections: list[NoteSection], top_k: int = DEFAULT_TOP_K
             ) -> list[Hit]:
    """BM25 检索。**主题命中加权**（主题就是知识点的名字，权重高）。"""
    if not sections:
        return []
    q_tokens = _tokens(question)
    if not q_tokens:
        return []
    docs: list[tuple[list[str], list[str]]] = []      # (正文tokens, 主题tokens)
    for s in sections:
        docs.append((_tokens(s.body), _tokens(s.topic)))
    n = len(docs)
    avgdl = sum(len(d[0]) for d in docs) / max(1, n)
    df: Counter = Counter()
    for d, _t in docs:
        for t in set(d):
            df[t] += 1
    k1, b = 1.5, 0.75
    q_set = list(dict.fromkeys(q_tokens))
    scored: list[tuple[float, int]] = []
    for idx, (dtok, ttok) in enumerate(docs):
        tf = Counter(dtok)
        ttf = Counter(ttok)
        dl = len(dtok) or 1
        score = 0.0
        for t in q_set:
            f = tf.get(t, 0)
            if f:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
            # 主题命中：强加权（命中主题通常就是这个知识点）
            if ttf.get(t):
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += 2.0 * idf * (ttf[t] * (k1 + 1)) / (ttf[t] + k1)
        if score > 0:
            scored.append((score, idx))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:max(1, top_k)]
    best = top[0][0] or 1.0
    hits: list[Hit] = []
    for score, idx in top:
        s = sections[idx]
        text = s.body
        if len(text) > MAX_SEG_CHARS:
            text = text[:MAX_SEG_CHARS] + "\n…（本段过长，已截断）"
        hits.append(Hit(note_file=s.note_file, source=s.source, topic=s.topic,
                        text=text, score=score / best))
    return hits


# ---------------------------------------------------------------------------
# 生成 + 自检
# ---------------------------------------------------------------------------

ANSWER_PROMPT = """你是学习群的**助教**，负责依据群内资料回答问题。

**唯一依据**：下面提供的「资料片段」。你**不许使用片段之外的知识**，也不许推测或补全。

要求：
1. 用**第三人称**（"资料中讲…""按资料，…"），**不要**用"我""老师"的口吻，不要冒充讲课人。
2. 每个结论后面必须标出处，格式：`〔来源：<资料文件名>「<主题>」〕`。
3. 如果片段里**没有**能回答这个问题的内容，就只回一句：
   `资料中未涉及这个问题，建议请教老师。` —— 不要勉强作答，不要编造。
4. 简洁：3~6 句或 2~5 条要点；不要复述整段资料，不要客套、不要开场白。
5. 直接输出回答正文。若提到卦例条件/口诀，**原样引用**，不要改写术语。

【资料片段】
{context}

【问题】
{question}

【回答】"""

CHECK_PROMPT = """下面是一次"依据资料回答问题"的结果。请做**事实核查**。

【资料片段】（回答**只能**依据这些内容）
{context}

【问题】
{question}

【待核查的回答】
{answer}

请逐句判断：回答里的内容，是否都能在上述资料片段里找到依据？
* 能推断 / 能直接读到 → 有依据
* 资料片段里没有、属于模型自己补充的知识或猜测 → **无依据**

如果有无依据的内容，请输出：
```
无依据
<把无依据的那些句子原样抄出来，每句一行>
```

如果**全部有依据**，只输出两个字：
```
通过
```"""


def _context_block(hits: list[Hit]) -> str:
    parts: list[str] = []
    for i, h in enumerate(hits, 1):
        parts.append(f"[片段{i}] 来源：{h.source}「{h.topic}」\n{h.text}")
    return "\n\n".join(parts)


def answer_question(question: str, group: str, data_root: Path, *,
                    top_k: int = DEFAULT_TOP_K, llm: dict | None = None,
                    self_check: bool = True, disclaimer: str = DISCLAIMER,
                    kb_dir: str | None = None,
                    verbose: bool = False, timeout_s: float = 120.0) -> AnswerResult:
    """用知识库回答问题（**不发送**，只返回结果）。

    `kb_dir`：该群的资料目录（用于 `kb.root` 映射，让多群共享同一套笔记）。
    """
    res = AnswerResult(question=question, group=group)
    sections = load_sections(group, data_root, kb_dir)
    if not sections:
        res.answer = "资料库为空（还没蒸馏过资料），建议先导入资料。"
        res.from_kb = False
        return res
    hits = retrieve(question, sections, top_k=top_k)
    if not hits or hits[0].score < MIN_SCORE:
        res.from_kb = False
        res.answer = "资料中未涉及这个问题，建议请教老师。" + (f"\n{disclaimer}" if disclaimer else "")
        return res
    res.hits = hits

    if llm is None:
        from consumer.summarize import _llm_cfg
        llm = _llm_cfg()
    from consumer.summarize import call_llm, strip_think

    context = _context_block(hits)
    prompt = ANSWER_PROMPT.format(context=context, question=question)
    res.prompt = prompt
    try:
        raw = strip_think(call_llm(prompt, llm, timeout_s=timeout_s))
        res.llm_calls += 1
    except Exception as e:  # noqa: BLE001
        log.warning("知识库生成失败: %s", e)
        res.from_kb = False
        res.answer = "（回答生成失败，请稍后再试）"
        return res
    res.raw_answer = raw
    answer = (raw or "").strip()

    # 空回答 → 当没查到
    if not answer:
        res.from_kb = False
        res.answer = "资料中未涉及这个问题，建议请教老师。"
        if disclaimer:
            res.answer += f"\n{disclaimer}"
        return res

    # ---- 第二遍自检 ----
    if self_check:
        try:
            chk = strip_think(call_llm(
                CHECK_PROMPT.format(context=context, question=question, answer=answer),
                llm, timeout_s=timeout_s))
            res.llm_calls += 1
            verdict = (chk or "").strip()
            if "无依据" in verdict:
                res.grounded = False
                bad = [ln.strip("-• \t") for ln in verdict.splitlines()
                       if ln.strip() and "无依据" not in ln]
                res.dropped = "；".join(bad)[:500]
                # 把无依据的句子从回答里删掉
                kept = answer
                for b in bad:
                    if len(b) >= 4 and b in kept:
                        kept = kept.replace(b, "")
                kept = re.sub(r"\n{2,}", "\n", kept).strip()
                if len(kept) < 10:
                    res.from_kb = False
                    res.answer = "资料中未涉及这个问题，建议请教老师。"
                    if disclaimer:
                        res.answer += f"\n{disclaimer}"
                    return res
                answer = kept
            else:
                res.grounded = True
        except Exception as e:  # noqa: BLE001
            # 自检失败：**不能当作通过**，但也不该丢掉已有回答 → 标记未知
            log.warning("知识库自检失败（结论未知）: %s", e)
            res.grounded = None

    res.answer = answer + (f"\n{disclaimer}" if disclaimer else "")
    return res


# ---------------------------------------------------------------------------
# 触发匹配（供 replier 用）
# ---------------------------------------------------------------------------

def strip_trigger(content: str, name: str) -> str | None:
    """消息是否以 `@<name>` 开头？是则返回剥掉前缀后的问题，否则 None。

    用户 2026-10-15 定的规则：**必须以 `@名字` 开头才回答**，
    名字**按群可配**（不同群可能用不同的虚拟号名）。

    ⚠️ 虚拟号在群里**不存在**，微信不会生成 `wxid`，
    所以只能按**文本前缀**匹配；手动打的 `@小易 问题` 后面通常跟空格或标点。
    """
    if not content or not name:
        return None
    s = content.strip()
    # 允许前缀带不可见字符（微信点选成员会带 U+2005）
    pat = re.compile(r"^@\s*" + re.escape(name.strip()) + r"[\s\u2005\u00a0]*[:：,，。.!！?？-]*\s*")
    m = pat.match(s)
    if not m:
        return None
    q = s[m.end():].strip()
    # ⚠️ 只 @ 不说话（`@小易` 后面啥也没有）→ 视为没触发。
    #    否则会拿空问题去检索、白花一次 LLM 调用，甚至发出空回答（2026-10-15 实测踩到）。
    if not q:
        return None
    return q
