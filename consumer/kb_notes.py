"""群知识库：把抽取出的**文字稿**蒸馏成**带出处的笔记**（可增量、可人工审阅）。

产物布局（`<数据根>/kb/<群>/`）
--------------------------------
```
_sources.json     指纹台账：文件 → (指纹, 抽取字数, 文本稿路径, 蒸馏到哪份笔记, 笔记 sha)
_index.md         主题索引（检索入口；也是给人看的目录）
<原文件名>.md     该文件的笔记（每个知识点都带〔来源〕）
text/<原文件名>.txt  抽取出的原始文字稿（供复查、也是增量抽取的缓存）
```

为什么**按原文件分笔记**而不是按主题合并
--------------------------------------
* 可追溯：某个结论来自哪份资料一目了然；
* 增量安全：某文件更新时**只重写它自己那份笔记**，不会破坏别的文件贡献的内容；
* 可人工修：你想改哪份就改哪份，不会被下次蒸馏覆盖（蒸馏只追加/替换自己那块）。

增量策略（关键）
----------------
* `fingerprint_changed()` 判定文件是否需要重抽文字；
* 需要重抽 → 重抽 + **重新蒸馏该文件** → **替换**它那份笔记（整份重写，最简单也最安全）；
* 没变 → 完全跳过（不花额度）。
* `kb.auto_distill_time` 到点 / 页面按钮 / CLI 都可触发；**没变化时不调 LLM**。

写文件前的安全约定
------------------
* `_index.md` 与笔记里都带"**由工具生成**"标记，避免被误当人工文件；
* 蒸馏失败**逐文件记录**，不影响其它文件；失败的文件**保留上一版笔记**（不清空）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from consumer import kb_extract as EX

log = logging.getLogger("consumer.kb_notes")

#: 每块文字的目标长度（字符）。中文 1 字 ≈ 0.6~1 token，1.2 万字约 1~1.5 万 token，
#: 留足输出空间（蒸馏结果通常比原文短很多）。
CHUNK_CHARS = 12000

#: 单文件文字超过这个长度才分块（否则一次蒸完）
SINGLE_PASS_CHARS = 20000

#: 蒸馏提示词。要求：按主题归纳、保留可检索的关键词、**每条带出处**、不编造。
DISTILL_PROMPT = """你是资料整理助手。下面是一份学习资料的正文（第 {part}/{total} 部分），来源文件：`{src}`。

请把它整理成**结构化的知识点笔记**，供后续问答检索使用。

要求：
1. **按主题分组**，每组用 `## 主题名` 作为小标题（主题名要能当检索关键词，
   例如"用神取法""回头克""三合局条件"），不要用"第一部分"这种无信息量的标题。
2. 每个知识点写成 `- ` 开头的条目，**尽量保留原文的关键术语、口诀、数字、条件**；
   口诀/断语请原样引用，不要改写。
3. **不要编造**：只写这段资料里确实有的内容。资料里没讲清的，就写"资料未展开"，
   绝不补充你自己的知识。
4. 去掉与知识无关的内容（页眉页脚、主讲人姓名重复、目录页码、图片说明、
   寒暄与闲聊）。
5. 篇幅：压缩到原文的 20%~40%，宁可精炼，不要复述。
6. 直接输出 Markdown（从 `## ` 开始），不要任何开场白、总结语或"以下是"。

资料正文：
{body}
"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class SourceEntry:
    """一个资料文件在台账里的记录。"""
    rel: str                                  # 相对资料目录的路径
    fingerprint: dict = field(default_factory=dict)
    chars: int = 0
    method: str = ""
    text_file: str = ""                       # 文字稿相对路径（kb/<群>/text/...）
    note_file: str = ""                       # 笔记相对路径
    note_sha: str = ""
    distilled_at: str = ""
    warning: str = ""

    def to_json(self) -> dict:
        return {"fingerprint": self.fingerprint, "chars": self.chars, "method": self.method,
                "text_file": self.text_file, "note_file": self.note_file,
                "note_sha": self.note_sha, "distilled_at": self.distilled_at,
                "warning": self.warning}

    @classmethod
    def from_json(cls, rel: str, d: dict) -> "SourceEntry":
        return cls(rel=rel, fingerprint=d.get("fingerprint") or {},
                   chars=int(d.get("chars") or 0), method=str(d.get("method") or ""),
                   text_file=str(d.get("text_file") or ""),
                   note_file=str(d.get("note_file") or ""),
                   note_sha=str(d.get("note_sha") or ""),
                   distilled_at=str(d.get("distilled_at") or ""),
                   warning=str(d.get("warning") or ""))


@dataclass
class DistillReport:
    """一次蒸馏的结果（CLI / 页面都要能如实展示）。"""
    group: str = ""
    new: list[str] = field(default_factory=list)          # 新增并蒸馏的文件
    updated: list[str] = field(default_factory=list)      # 内容变化并重蒸的文件
    unchanged: list[str] = field(default_factory=list)    # 跳过（没变化）
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # 空内容/不支持
    llm_calls: int = 0
    chunks: int = 0
    notes_written: int = 0
    dry_run: bool = False
    plan_calls: int = 0                                   # 预演时的预估调用次数

    @property
    def changed(self) -> list[str]:
        return self.new + self.updated


# ---------------------------------------------------------------------------
# 路径与台账
# ---------------------------------------------------------------------------

def _kb_dir_map(data_root: Path) -> dict[str, str]:
    """资料目录名 → 实际路径（扫 `配置文件所在目录/kb_dirs.txt`，一行一个目录）。

    这一层是为了**多群共用一份资料只蒸馏一次**：真实路径里可能有 `D:\\...` 这种
    在目录名里不合法/不好看的字符，所以用"目录名"当笔记根名，
    再用这个映射把"目录名 → 真实路径"记下来，避免同名不同路径撞车。
    """
    out: dict[str, str] = {}
    for p in _kb_dir_map_candidates(data_root):
        if not p.is_file():
            continue
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                d = Path(line)
                out[d.name] = str(d)
        except OSError:
            continue
        break
    return out


def _kb_dir_map_candidates(data_root: Path) -> list[Path]:
    cands: list[Path] = []
    try:
        import paths
        cands.append(Path(paths.config_path()).parent / "kb_dirs.txt")
    except Exception:  # noqa: BLE001
        pass
    cands.append(Path(data_root) / "kb" / "_dirs.txt")
    return cands


def _auto_slug(kb_dir: str | Path, data_root: Path) -> str:
    """资料目录 → 笔记根名（默认取目录名；重名冲突时加短哈希区分）。

    用户 2026-10-15 明确要求：**多个群用同一个资料目录，就不该再单独蒸馏一遍**。
    所以笔记根**由资料目录决定**，与群名无关 —— 同一个目录天然共用一套笔记，
    **零配置**（不需要在 config.yaml 里手工写映射）。
    """
    d = Path(kb_dir)
    name = (d.name or "kb").strip() or "kb"
    # 与另一个同名但**不同路径**的目录冲突时加短哈希（8 位），保证不会串。
    # ⚠️ 只有"记录里的那个目录**当前仍然存在**"才算真冲突：
    #    否则临时目录/已删除目录的历史记录会让新目录被误判成重名而加哈希
    #    （2026-10-15 踩到：上一次回归留下的 `同名` 记录把新目录顶掉了）。
    known = _kb_dir_map(data_root)
    other = known.get(name)
    if other:
        op = Path(other)
        try:
            conflict = op.is_dir() and str(op) != str(d)
        except OSError:
            conflict = False
        if conflict:
            h = hashlib.sha1(str(d).lower().encode("utf-8")).hexdigest()[:8]
            name = f"{name}-{h}"
    return name


def _remember_kb_dir(data_root: Path, kb_dir: str | Path, slug: str) -> None:
    """把「目录名 → 真实路径」记下来（供下次判断是否重名冲突）。"""
    import paths
    try:
        p = Path(paths.config_path()).parent / "kb_dirs.txt"
    except Exception:  # noqa: BLE001
        return
    try:
        cur = set()
        if p.is_file():
            cur = {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.startswith("#")}
        line = str(Path(kb_dir))
        if line not in cur:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        log.debug("记录 kb_dirs 失败: %s", e)


def kb_root(data_root: Path, group: str, kb_dir: str | Path | None = None) -> Path:
    """某群的笔记根目录。

    **有 `kb_dir` 时按资料目录定根**（多个群用同一目录 → 自动共用一套笔记）；
    没给 `kb_dir` 时才退回按群名（兼容旧调用与手工实验）。
    """
    from consumer.summarize import safe_name       # 复用已有的文件名安全化
    if kb_dir:
        slug = _auto_slug(kb_dir, data_root)
        _remember_kb_dir(data_root, kb_dir, slug)
        return Path(data_root) / "kb" / safe_name(slug, fallback="kb")
    return Path(data_root) / "kb" / safe_name(group, fallback="group")


def _sources_path(root: Path) -> Path:
    return root / "_sources.json"


def load_sources(root: Path) -> dict[str, SourceEntry]:
    p = _sources_path(root)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("台账读取失败（当作空台账重来）: %s", e)
        return {}
    out: dict[str, SourceEntry] = {}
    for rel, d in (raw.get("sources") or {}).items():
        try:
            out[rel] = SourceEntry.from_json(rel, d or {})
        except Exception as e:  # noqa: BLE001
            log.warning("台账条目解析失败，忽略 %s: %s", rel, e)
    return out


def save_sources(root: Path, entries: dict[str, SourceEntry]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "_note": "本文件由工具生成（consumer/kb_notes.py）—— 记录资料指纹与蒸馏进度，请勿手改",
        "extractor_version": EX.EXTRACTOR_VERSION,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": {k: v.to_json() for k, v in sorted(entries.items())},
    }
    tmp = _sources_path(root).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_sources_path(root))          # 原子替换，避免写一半丢台账


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _match_kb_dir(entry_root: Path, candidates: list[Path]) -> Path | None:
    """把一份台账匹配到它的资料目录（按文件名 + 指纹，可靠且不靠猜）。

    匹配依据：台账里记了每个文件的 `size`（必要时再比 sha256）；
    资料目录里存在**同名同大小**的文件 → 认定为该目录。
    这样即使目录名与笔记目录名不同（例如笔记叫"弟子群"、目录也叫"弟子群"但换了盘符）
    也能对上；对不上就返回 None（迁移时只报告、不乱动）。
    """
    entries = load_sources(entry_root)
    if not entries:
        return None
    best: tuple[int, Path] | None = None
    for cand in candidates:
        if not cand.is_dir():
            continue
        score = 0
        for rel, ent in entries.items():
            f = cand / rel
            if not f.is_file():
                continue
            want = int((ent.fingerprint or {}).get("size") or -1)
            try:
                if want >= 0 and f.stat().st_size == want:
                    score += 1
            except OSError:
                continue
        if score and (best is None or score > best[0]):
            best = (score, cand)
    return best[1] if best else None


def migrate_to_dir_keyed(data_root: Path, kb_base_dirs: list[Path],
                         apply: bool = False) -> list[str]:
    """把"按群名"的旧笔记目录改成"按资料目录"命名（多群共用一份资料不再重复蒸馏）。

    返回给 CLI/页面显示的说明行。`apply=False` 时只报告计划（不动文件）。
    """
    from consumer.summarize import safe_name
    root = Path(data_root) / "kb"
    out: list[str] = []
    if not root.is_dir():
        return ["（没有 kb 目录，无需迁移）"]
    # 候选资料目录：各资料根下的一级/二级子目录（够用；层级更深时人工配置）
    candidates: list[Path] = []
    for base in kb_base_dirs:
        if not base.is_dir():
            continue
        candidates.append(base)
        for p in sorted(base.iterdir()):
            if p.is_dir():
                candidates.append(p)
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        src = _match_kb_dir(d, candidates)
        if src is None:
            out.append(f"⏭️  {d.name}：**匹配不到资料目录**（保留原样，需要人工确认）")
            continue
        target_name = safe_name(_auto_slug(src, data_root), fallback="kb")
        if target_name == d.name:
            _remember_kb_dir(data_root, src, target_name)
            out.append(f"✅ {d.name}：已符合「按资料目录」命名（{src}）")
            continue
        target = root / target_name
        if target.exists():
            out.append(f"⚠️  {d.name} → {target_name} 目标已存在，**跳过**（请人工合并）")
            continue
        if apply:
            try:
                d.rename(target)
                _remember_kb_dir(data_root, src, target_name)
                out.append(f"📦 {d.name} → {target_name}（资料目录 {src}）")
            except OSError as e:
                out.append(f"❌ {d.name} → {target_name} 失败：{e}")
        else:
            out.append(f"📦 计划：{d.name} → {target_name}（资料目录 {src}）")
    return out


def collect_kb_dirs(data_root: Path, groups: list[tuple[str, str]]) -> list[Path]:
    """从 [(群名, kb_dir)] 收集去重后的资料目录（用于迁移时当候选）。"""
    out: list[Path] = []
    seen: set[str] = set()
    for _name, d in groups:
        if not d:
            continue
        p = Path(d)
        k = str(p).lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def note_name_for(rel: str) -> str:
    """资料文件 → 笔记文件名（放平到笔记根，逗号/斜杠都换掉）。"""
    base = rel.replace("\\", "/")
    base = re.sub(r"\.(pdf|docx?|pptx?|xlsx?|txt|md|csv)$", "", base, flags=re.I)
    base = base.replace("/", "__")
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip(" .")
    return (base or "note") + ".md"


def text_name_for(rel: str) -> str:
    base = rel.replace("\\", "/").replace("/", "__")
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip(" .")
    return (base or "doc") + ".txt"


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------

def chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """按段落边界切块（尽量不切断句子）；单块不超过 limit 太多。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n"):
        plen = len(para) + 1
        if size + plen > limit and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        # 单段本身就超长 → 硬切
        if plen > limit:
            if buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            for i in range(0, len(para), limit):
                chunks.append(para[i:i + limit])
            continue
        buf.append(para)
        size += plen
    if buf:
        chunks.append("\n".join(buf))
    return chunks


# ---------------------------------------------------------------------------
# 蒸馏
# ---------------------------------------------------------------------------

def distill_text(text: str, src: str, llm: dict, timeout_s: float = 240.0,
                 on_call=None) -> tuple[str, int]:
    """把一份文字稿蒸馏成笔记。返回 `(markdown, 调用次数)`。

    `on_call`：每调用一次 LLM 前回调（用于实时报告进度与预估）。
    """
    from consumer.summarize import call_llm, strip_think
    parts = chunk_text(text)
    if not parts:
        return "", 0
    if len(parts) == 1:
        prompt = DISTILL_PROMPT.format(part=1, total=1, src=src, body=parts[0])
        if on_call:
            on_call(1)
        out = strip_think(call_llm(prompt, llm, timeout_s=timeout_s))
        return _clean_note(out), 1
    # 多块：逐块蒸馏再拼（避免一次 prompt 过大）
    pieces: list[str] = []
    calls = 0
    for i, body in enumerate(parts, 1):
        prompt = DISTILL_PROMPT.format(part=i, total=len(parts), src=src, body=body)
        if on_call:
            on_call(1)
        out = strip_think(call_llm(prompt, llm, timeout_s=timeout_s))
        calls += 1
        cleaned = _clean_note(out)
        if cleaned:
            pieces.append(cleaned)
    return "\n\n".join(pieces), calls


_H_PREFIX = re.compile(r"^#{1,3}\s*")


def _clean_note(md: str) -> str:
    """清理模型输出：去掉开场白、统一标题层级、压掉多余空行。"""
    lines = (md or "").strip().split("\n")
    out: list[str] = []
    for ln in lines:
        s = ln.rstrip()
        if not out and s.strip() and not s.lstrip().startswith("#") and not s.lstrip().startswith("-"):
            # 首行的"以下是…"之类客套直接丢
            if re.match(r"^(以下是|下面|好的|收到|这是)", s.strip()):
                continue
        out.append(s)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def note_markdown(src_rel: str, body: str, chars_src: int, when: str | None = None) -> str:
    """包一层文件头（出处、生成时间、压缩比），方便人工审阅与追溯。"""
    when = when or datetime.now().strftime("%Y-%m-%d %H:%M")
    ratio = f"{len(body) / chars_src:.0%}" if chars_src else "-"
    return (f"<!-- 由工具生成（consumer/kb_notes.py），请勿手改：下次蒸馏会覆盖本文件 -->\n"
            f"# {Path(src_rel).name}\n\n"
            f"<sub>来源：`{src_rel}` ｜ 蒸馏时间：{when} ｜ "
            f"原文字数：{chars_src:,} ｜ 笔记字数：{len(body):,}（{ratio}）</sub>\n\n"
            f"{body}\n")


def build_index(group: str, entries: dict[str, SourceEntry], root: Path) -> str:
    """生成 `_index.md`：主题索引（从笔记的 `## ` 标题汇总）。"""
    rows: list[tuple[str, str, list[str]]] = []
    for rel, ent in sorted(entries.items()):
        topics: list[str] = []
        if ent.note_file:
            np = root / ent.note_file
            if np.is_file():
                try:
                    topics = [m.group(1).strip()
                              for m in re.finditer(r"^##\s+(.+)$", np.read_text(encoding="utf-8"),
                                                   re.M)]
                except OSError:
                    topics = []
        rows.append((rel, ent.note_file, topics))
    lines = [f"<!-- 由工具生成（consumer/kb_notes.py），请勿手改 -->",
             f"# {group} · 知识库索引", "",
             f"<sub>资料 {len(rows)} 份 ｜ 生成时间 "
             f"{datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ 抽取器版本 {EX.EXTRACTOR_VERSION}</sub>", ""]
    for rel, nf, topics in rows:
        lines.append(f"## {Path(rel).name}")
        lines.append(f"<sub>`{rel}` → `{nf}`</sub>" if nf else f"<sub>`{rel}`（未蒸馏）</sub>")
        if topics:
            lines.append("")
            for t in topics:
                lines.append(f"- {t}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def distill_group(kb_dir: Path, group: str, data_root: Path, llm: dict,
                  allow_ext=None, dry_run: bool = False,
                  progress=None) -> DistillReport:
    """对一个资料目录做「抽取 + 增量蒸馏」。`dry_run=True` 只报告计划、不调 LLM。

    `progress`：`(msg: str) -> None` 回调，用于 CLI/页面实时显示。
    """
    from consumer.summarize import safe_name
    allow = tuple(allow_ext or EX.DEFAULT_EXT)
    root = kb_root(data_root, group, kb_dir)   # 按资料目录定根 → 多群共用一份笔记
    rep = DistillReport(group=group, dry_run=dry_run)
    entries = load_sources(root)

    def say(msg: str) -> None:
        log.info("%s", msg)
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    files = EX.scan_files(Path(kb_dir), allow)
    if not files:
        say(f"[{group}] 资料目录为空或没有可用文件：{kb_dir}")

    def persist() -> None:
        """**每个文件处理完立即落台账**（中断/被杀也不丢已完成的进度）。

        ⚠️ 2026-10-15 踩到：原来只在整轮结束才写 `_sources.json`，
        进程被中断后台杀掉后，已蒸馏的 2 个文件**完全没记账** →
        下次重跑会把它们**再蒸一遍（白花额度）**，而且"资料增加后增量再蒸馏"
        这个核心承诺直接失效。所以改成即时落盘。
        """
        try:
            save_sources(root, entries)
        except OSError as e:
            log.warning("台账落盘失败（本轮仍继续）: %s", e)

    seen: set[str] = set()
    for f in files:
        rel = str(f.relative_to(kb_dir))
        seen.add(rel)
        ent = entries.get(rel) or SourceEntry(rel=rel)
        try:
            fp = EX.file_fingerprint(f)
        except OSError as e:
            rep.failed.append((rel, f"读文件失败: {e}"))
            persist()
            continue
        if not EX.fingerprint_changed(ent.fingerprint, fp):
            rep.unchanged.append(rel)
            continue
        # --- 抽取 ---
        try:
            ex = EX.extract_file(f)
        except EX.ExtractError as e:
            rep.failed.append((rel, str(e)))
            persist()
            continue
        except Exception as e:  # noqa: BLE001
            rep.failed.append((rel, f"{type(e).__name__}: {e}"))
            persist()
            continue
        if not ex.text.strip():
            rep.skipped.append((rel, ex.warning or "抽取结果为空"))
            ent.fingerprint = fp
            ent.chars = 0
            ent.warning = ex.warning
            entries[rel] = ent
            persist()
            continue
        # --- 分块预估 / 真蒸 ---
        parts = chunk_text(ex.text)
        rep.chunks += len(parts)
        rep.plan_calls += len(parts)
        was_new = ent.note_file == "" and not ent.distilled_at
        if dry_run:
            (rep.new if was_new else rep.updated).append(rel)
            continue
        say(f"[{group}] 蒸馏 {rel}（{ex.chars:,} 字 → {len(parts)} 块）")
        try:
            body, calls = distill_text(ex.text, rel, llm,
                                       on_call=lambda n: None)
        except Exception as e:  # noqa: BLE001
            rep.failed.append((rel, f"蒸馏失败: {type(e).__name__}: {e}"))
            persist()
            continue
        rep.llm_calls += calls
        if not body.strip():
            rep.failed.append((rel, "蒸馏结果为空（模型只返回了推理？）"))
            persist()
            continue
        # --- 落地：文字稿 + 笔记 ---
        note_rel = note_name_for(rel)
        try:
            (root / "text").mkdir(parents=True, exist_ok=True)
            text_rel = str(Path("text") / text_name_for(rel))
            (root / text_rel).write_text(ex.text, encoding="utf-8")
            md = note_markdown(rel, body, ex.chars)
            (root / note_rel).write_text(md, encoding="utf-8")
        except OSError as e:
            rep.failed.append((rel, f"写文件失败: {e}"))
            persist()
            continue
        ent.fingerprint = fp
        ent.chars = ex.chars
        ent.method = ex.method
        ent.text_file = text_rel
        ent.note_file = note_rel
        ent.note_sha = sha256_text(body)
        ent.distilled_at = datetime.now().isoformat(timespec="seconds")
        ent.warning = ex.warning
        entries[rel] = ent
        rep.notes_written += 1
        (rep.new if was_new else rep.updated).append(rel)
        persist()                      # ← 立即记账，防止中断丢进度
        say(f"[{group}] ✅ {rel} → {note_rel}")

    # 台账里已被删除的文件：标记但不删笔记（人工可能还想留着）
    for rel, ent in list(entries.items()):
        if rel not in seen:
            ent.warning = (ent.warning + " ｜ " if ent.warning else "") + "源文件已不在资料目录"
            entries[rel] = ent

    if not dry_run:
        try:
            save_sources(root, entries)
            (root / "_index.md").write_text(build_index(group, entries, root), encoding="utf-8")
        except OSError as e:
            rep.failed.append(("_index.json/_sources.json", f"写台账失败: {e}"))
    return rep


def summarize_report(rep: DistillReport) -> str:
    """把报告拼成一段人可读文本（CLI 与页面共用）。"""
    head = "【预演】" if rep.dry_run else ""
    lines = [f"{head}群「{rep.group}」：新增 {len(rep.new)}、更新 {len(rep.updated)}、"
             f"未变化 {len(rep.unchanged)}、跳过 {len(rep.skipped)}、失败 {len(rep.failed)}"]
    if rep.dry_run:
        lines.append(f"  预计 LLM 调用：{rep.plan_calls} 次（{rep.chunks} 块）；"
                     f"预演不写文件、不调用模型")
    else:
        lines.append(f"  LLM 调用：{rep.llm_calls} 次；写出笔记 {rep.notes_written} 份")
    for k, lab in ((rep.new, "新增"), (rep.updated, "更新")):
        for r in k:
            lines.append(f"  {lab}: {r}")
    for r, why in rep.skipped:
        lines.append(f"  ⏭️  跳过: {r} —— {why}")
    for r, why in rep.failed:
        lines.append(f"  ❌ 失败: {r} —— {why}")
    return "\n".join(lines)
