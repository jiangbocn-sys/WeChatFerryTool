"""当日归档信息 → LLM 归纳总结。

设计要点
--------
* 只总结**进入归档**的消息（重点群 ∩ 重点成员/敏感关键词），与 `digest.py` 同一套判定
  （复用 `_in_digest` / `_exclude_types` / `_render_content`），所以归档里有什么就总结什么。
* 提交给 LLM 的文本**逐条带时间、发送人、类型、内容**；图片/视频是占位符（无 OCR），
  语音用转写结果。
* 数据库以 **只读** 方式打开（`mode=ro`），不需要写权限。

用法
----
    # 只看将要发给 LLM 的 prompt（不联网、不花钱）
    python -m consumer.summarize --date 2026-10-05 --print-prompt

    # 真正调用 LLM，结果写到 reports/summary-YYYY-MM-DD.md
    python -m consumer.summarize --date 2026-10-05

Web 端（`web/app.py` 的 /digest 页）复用这里的 `collect()` / `build_prompt()` / `summarize()`：
「预览 prompt」只调前两个（纯本地），「生成当日总结」才会真的联网烧 token。
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from consumer.digest import (  # noqa: E402
    _TYPE_LABEL,
    _exclude_types,
    _in_digest,
    _load_labels,
    _monitored_groups,
    _render_content,
    _summary_hint,
)
from paths import data_root  # noqa: E402

log = logging.getLogger("consumer.summarize")

PROJECT_DIR = data_root()
LABELS_PATH = PROJECT_DIR / "data" / "labels.json"

PROMPT_TEMPLATE = """你是一个群聊内容整理助手。

下面是「{date}」的群聊发言记录（已按时间排序，格式为 `[时间] 发送人（类型）：内容`）。
说明：「图片」「视频」是占位符——说明该时刻有人发了图片/视频，但没有可提取的文字描述；
「语音转文本」是语音消息的自动转写结果，可能存在识别误差。

请根据讨论的内容，总结核心要点，按顺序逐条输出总结内容。

要求：
1. 按时间顺序逐条输出总结要点，每条一行，以「- 」开头
2. 输入记录已按时间排序、且每条都标注了时刻，请据此梳理讨论的先后与进展；
   输出**不需要**逐条标注时间
3. 保留对话中出现的关键信息：人名、时间、数字、金额、结论、待办事项
4. 同一话题的连续发言合并为一条要点，不要逐句复述原文
5. 不要编造记录中没有的信息；转写明显有误时标注「（转写存疑）」
6. 忽略寒暄、表情与无实质内容的发言
{extra_rules}
发言记录：
{body}

请开始总结（只输出总结内容，不要重复贴回原文）："""

#: 有"每群总结提示"时追加的规则段（{hints} 由 build_prompt 填）
HINTS_RULE = """7. **下面「本群总结提示」是群主对总结口径的要求，优先级高于上面的通用要求**：
   按提示决定该保留什么、该剔除什么（提示里点名的内容算正文，提示之外的噪音直接丢掉，
   不要写进总结）；若提示与本记录的实际情况不符，以记录为准并照常总结
"""


def _hints_block(hints: list[tuple[str, str]]) -> str:
    """拼"每群总结提示"规则段（(群显示名, 提示) 列表）。无提示则返回空串。

    注意：这里返回的内容会填进模板的 `{extra_rules}` —— 它必须**同时**
    包含"第 7 条要求"和提示清单（踩过：只返回清单会导致规则说明丢失）。
    """
    if not hints:
        return ""
    rule = ("7. **下面「本群总结提示」是群主对本群总结口径的要求，优先级高于上面的通用要求**："
            "按提示决定该保留什么、该剔除什么（提示里点名的内容算正文；提示之外的噪音直接丢掉、"
            "不要写进总结）；若提示与本记录的实际情况不符，以记录为准并照常总结")
    lines = [rule, "", "本群总结提示："]
    for gname, hint in hints:
        lines.append(f"- 【{gname}】{hint}")
    return "\n".join(lines) + "\n"



# ---------------------------------------------------------------------------
def _load_rows(db_path: Path, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
    """只读打开，取当天全部消息（筛选交给 _in_digest）。"""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT group_name, sender, content, msg_type, received_at, transcript
            FROM messages
            WHERE received_at >= ? AND received_at < ?
            ORDER BY received_at ASC
            """,
            (start_ts, end_ts),
        ).fetchall()
    finally:
        conn.close()


def _type_label(msg_type: int) -> str:
    if msg_type == 1:
        return "文本"
    if msg_type == 34:
        return "语音转文本"
    return _TYPE_LABEL.get(msg_type, f"type={msg_type}")


def build_lines(rows: list[sqlite3.Row], labels: dict) -> dict[str, list[str]]:
    """按会话分组，逐条生成 `[HH:MM] 发送人（类型）：内容`。

    返回 **{群 id: [行, ...]}**（2026-10-07 起用 id 而不是显示名 —— 因为"每群总结提示"
    是按 id 存在 labels 里的，用显示名会查不到；渲染时名字用 `labels` 兜底）。

    ⚠️ **必须把"监控名单"一起传进 `_in_digest`** —— 否则归档（`generate_digest`
    传了 monitored）与总结（这里没传）的选取范围会不一致：归档里有的消息，
    总结可能看不到（或反之）。这是 10-07 测试逮到的真 bug。
    """
    exclude = _exclude_types()
    monitored = _monitored_groups()
    out: dict[str, list[str]] = {}

    for r in rows:
        if not _in_digest(r, labels, exclude, monitored):
            continue
        ts = int(r["received_at"] or 0)
        hhmm = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"
        gkey = r["group_name"] or "?"
        skey = r["sender"] or "?"

        gname = ((labels.get("groups") or {}).get(gkey) or {}).get("name") or gkey
        sname = ((labels.get("senders") or {}).get(skey) or {}).get("name") or skey

        body = _render_content(r).replace("\n", " ").strip()
        out.setdefault(gkey, []).append(
            f"[{hhmm}] {sname}（{_type_label(int(r['msg_type'] or 0))}）：{body}"
        )
    return out


def gid_for_name(gname: str, labels: dict | None = None) -> str:
    """显示名 → 群 id（找不到就原样返回）。

    兼容旧调用：`build_prompt` 以前收的是 `{群显示名: [...]}`，现在收 `{群 id: [...]}`，
    两种都认。
    """
    if labels:
        for gid, ent in (labels.get("groups") or {}).items():
            if ((ent or {}).get("name") or "") == gname:
                return gid
    return gname


def name_for_gid(gid: str, labels: dict | None = None) -> str:
    """群 id → 显示名（没标定就返回 id）。

    用于 per_group 的**文件名**和 prompt 里的群标题 —— `build_lines()` 从 10-07 起
    以群 id 为键（因为"每群总结提示"是按 id 存的），所以这里要显式换回人看的名字，
    否则文件名会变成 `summary-<日期>-50279811726@chatroom.md`。
    """
    if labels:
        ent = (labels.get("groups") or {}).get(gid) or {}
        nm = str(ent.get("name") or "").strip()
        if nm:
            return nm
    return gid


def build_prompt(date_str: str, grouped: dict[str, list[str]],
                 labels: dict | None = None) -> str:
    """把分组后的记录拼成 prompt。

    `grouped` 的键可以是**群 id**（新）或**群显示名**（旧调用点，会用 labels 反查 id）。
    每个群的「总结提示」（`labels.groups.<id>.summary_hint`）会作为独立段落一起提交，
    让模型按群主的口径决定保留什么、剔除什么噪音。
    """
    if labels is None:
        try:
            labels = _load_labels(LABELS_PATH)
        except Exception:  # noqa: BLE001
            labels = {}
    groups_meta = labels.get("groups") or {}

    blocks: list[str] = []
    hints: list[tuple[str, str]] = []
    for key, lines in grouped.items():
        gid = key if key in groups_meta else gid_for_name(key, labels)
        ent = groups_meta.get(gid) or {}
        gname = str(ent.get("name") or key)
        hint = _summary_hint(gid, labels)
        if hint:
            hints.append((gname, hint))
        blocks.append(f"### 群：{gname}（{len(lines)} 条）")
        blocks.extend(lines)
        blocks.append("")
    # 注意用 replace 而不是 str.format：body 里含 `{`／`}`（XML/JSON 片段）会炸 format
    return (PROMPT_TEMPLATE
            .replace("{date}", date_str)
            .replace("{extra_rules}", _hints_block(hints))
            .replace("{body}", "\n".join(blocks).strip()))


# ---------------------------------------------------------------------------
def _llm_cfg() -> dict:
    """复用 config.yaml 的 llm 段（与评分/自动回复同一份配置）。"""
    try:
        import yaml
        import paths
        cfg = yaml.safe_load(paths.config_path().read_text(encoding="utf-8")) or {}
        return cfg.get("llm") or {}
    except Exception:  # noqa: BLE001
        return {}


def call_llm(prompt: str, llm: dict, timeout_s: float = 180.0) -> str:
    base_url = str(llm.get("base_url") or "").strip()
    model = str(llm.get("model") or "").strip()
    # 本地模型（Ollama 等）不需要 key，但 SDK 要求非空，给个占位
    api_key = str(llm.get("api_key") or "").strip() or "not-needed"
    if not base_url:
        raise RuntimeError("未配置 LLM：请在 Web 管理页的初始化/设置里填 base_url 与模型名")
    if not model:
        raise RuntimeError("未配置 LLM 模型名（model）")

    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.choices[0].message.content or "").strip()


def strip_think(text: str) -> str:
    """去掉推理模型的 `<think>…</think>` 推理块（含被截断、未闭合的情况）。

    实测 MiniMax-M3 会把整段思考过程一起返回（2026-10-06 真实调用），
    `scorer.py` / `replier.py` 早就各自剥过一遍 —— 总结这条链路上原先漏了，
    结果报告里混进一大段英文推理。这里补上，并保证 `<think>` 之后即使没有闭标签
    也能拿到正文。
    """
    import re
    text = text or ""
    out = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    if "<think>" in out:
        # 未闭合：真实模型会先写推理、后写正文（正文不在 think 块内）→ 优先取 think 之后的内容
        after = out.split("<think>", 1)[1]
        if "</think>" not in after and after.strip():
            out = re.sub(r"<think>.*", "", out, flags=re.DOTALL)
        else:
            out = out[: out.index("<think>")]
    return out.strip()


def collect(date_str: str, db_path: Path | None = None) -> tuple[dict[str, list[str]], int] | None:
    """取某天**进入归档**的发言，返回 `({群名: [行, ...]}, 总条数)`。

    数据库不存在返回 None（Web 端据此提示"先让 consumer 跑起来"）。
    纯本地、不联网，Web 的「预览 prompt」用的就是这个。
    """
    day = datetime.strptime(date_str, "%Y-%m-%d")
    start_ts = int(day.timestamp())
    end_ts = int((day + timedelta(days=1)).timestamp())

    db_file = db_path or (PROJECT_DIR / "data" / "messages.db")
    if not db_file.is_file():
        return None

    labels = _load_labels(PROJECT_DIR / "data" / "labels.json")
    rows = _load_rows(db_file, start_ts, end_ts)
    grouped = build_lines(rows, labels)
    return grouped, sum(len(v) for v in grouped.values())


@dataclass
class SummarizeResult:
    """一次总结的结果摘要（Web 与 CLI 共用）。

    `group` = 这份总结覆盖的会话名；合并模式下为空串。
    """
    date: str
    path: Path
    total: int
    conversations: int
    prompt: str
    content: str
    grouped: dict[str, list[str]] = field(default_factory=dict)
    group: str = ""

    @property
    def chars(self) -> int:
        return len(self.prompt)


@dataclass
class SummarizeRun:
    """一次"生成当日总结"的整体结果：`per_group` 模式下会有多份文件。"""
    date: str
    mode: str
    results: list[SummarizeResult] = field(default_factory=list)
    total: int = 0
    errors: list[str] = field(default_factory=list)

    # ---- 兼容旧调用点（以前只返回单个 SummarizeResult）----
    @property
    def path(self) -> Path | None:
        return self.results[0].path if self.results else None

    @property
    def paths(self) -> list[Path]:
        return [r.path for r in self.results]

    @property
    def files(self) -> list[str]:
        return [r.path.name for r in self.results]

    @property
    def content(self) -> str:
        """各群正文拼起来（页面预览用）。"""
        return "\n\n".join(f"## {r.group or '（合并）'}\n\n{r.content}" for r in self.results)

    @property
    def conversations(self) -> int:
        return len(self.results)

    @property
    def chars(self) -> int:
        return sum(r.chars for r in self.results)


def summarize_mode() -> str:
    """当日总结方式（`config.yaml` 的 `digest.summarize_mode`）。

    * `per_group`（默认）—— 每个会话单独一份总结 `summary-<日期>-<群名>.md`，每群一次 LLM 调用
    * `combined`        —— 所有会话合并成一份 `summary-<日期>.md`，只调一次 LLM
    """
    try:
        import paths
        import yaml
        cfg = yaml.safe_load(paths.config_path().read_text(encoding="utf-8")) or {}
        v = str((cfg.get("digest") or {}).get("summarize_mode") or "").strip().lower()
        if v in ("per_group", "combined"):
            return v
    except Exception:  # noqa: BLE001
        pass
    return "per_group"


def safe_name(text: str, fallback: str = "group") -> str:
    """把会话名变成合法文件名：去掉 Windows 非法字符、压缩空白、限长。"""
    import re
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text or "")).strip(" .")
    s = re.sub(r"\s+", " ", s)
    s = s[:60].strip(" .")
    return s or fallback


def _write_summary_file(path: Path, date_str: str, group: str, total: int, summary: str,
                        llm_cfg: dict, grouped: dict[str, list[str]], run_mode: str,
                        labels: dict | None = None) -> None:
    head = [
        f"# 当日归档总结 · {date_str}" + (f" · {group}" if group else ""),
        "",
        (f"> 会话：**{group}**（{total} 条归档消息）" if group
         else f"> 基于 {total} 条归档消息（{len(grouped)} 个会话）"),
        f"> 由大模型归纳生成；模型：{llm_cfg.get('model') or '?'} @ {llm_cfg.get('base_url') or '?'}",
        f"> 总结方式：{'每群单独一份' if run_mode == 'per_group' else '所有群合并一份'}"
        f"（`digest.summarize_mode: {run_mode}`）",
        "",
        summary,
        "",
        "---",
        "",
        "## 附：提交给模型的原始记录",
        "",
    ]
    for gid, lines in grouped.items():
        head.append(f"### {name_for_gid(gid, labels)}")
        head.extend(lines)
        head.append("")
    path.write_text("\n".join(head), encoding="utf-8")


def llm_ready() -> bool:
    """LLM 是否配好了（有 base_url 与 model）。用于定时任务判断"要不要顺手总结"。"""
    cfg = _llm_cfg()
    return bool(str(cfg.get("base_url") or "").strip() and str(cfg.get("model") or "").strip())


def summarize(date_str: str, print_prompt: bool = False, out_dir: Path | None = None,
              mode: str | None = None) -> SummarizeRun | None:
    """生成当日总结。没配置 LLM 就抛 RuntimeError；当天没有归档消息返回 None。

    默认**每个群一份**（`digest.summarize_mode: per_group`）；设为 `combined` 则合并成一份。
    `print_prompt=True` 时**不调用 LLM**，只打印 prompt 并返回 None（CLI 老行为）。
    """
    collected = collect(date_str)
    if collected is None:
        raise SystemExit(f"找不到数据库: {PROJECT_DIR / 'data' / 'messages.db'}")
    grouped, total = collected
    if not total:
        log.warning("当天没有进入归档的消息（检查 labels.json 的重点群/重点成员/敏感关键词）")
        return None

    run_mode = (mode or summarize_mode()).strip().lower()
    if run_mode not in ("per_group", "combined"):
        run_mode = "per_group"

    if print_prompt:
        labels = _load_labels(LABELS_PATH)
        if run_mode == "per_group":
            for gid, lines in grouped.items():
                gname = name_for_gid(gid, labels)
                print(f"\n########## {gname}（{len(lines)} 条）##########")
                print(build_prompt(date_str, {gid: lines}, labels))
        else:
            print(build_prompt(date_str, grouped))
        return None

    llm_cfg = _llm_cfg()
    labels = _load_labels(LABELS_PATH)
    out_dir = out_dir or (PROJECT_DIR / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 每群一份 vs 合并一份
    chunks: list[tuple[str, dict[str, list[str]], int]] = []
    if run_mode == "per_group":
        for gid, lines in grouped.items():
            chunks.append((gid, {gid: lines}, len(lines)))
    else:
        chunks.append(("", grouped, total))

    run = SummarizeRun(date=date_str, mode=run_mode, total=total)
    used: set[str] = set()
    for gid, sub_grouped, sub_total in chunks:
        gname = name_for_gid(gid, labels) if gid else ""
        prompt = build_prompt(date_str, sub_grouped, labels)
        log.info("[%s] %d 条，prompt %d 字", gname or "合并", sub_total, len(prompt))
        try:
            summary = strip_think(call_llm(prompt, llm_cfg))
            if not summary:
                raise RuntimeError("LLM 返回了空内容（模型只输出了推理过程？）")
        except Exception as e:  # noqa: BLE001
            # 一个群失败不影响其它群：记下来继续
            log.warning("「%s」总结失败：%s", gname or "合并", e)
            run.errors.append(f"{gname or '合并'}: {type(e).__name__}: {e}")
            continue

        if gid:
            # 没标定时用群 id（剥掉 @chatroom 尾巴让文件名干净些：g4@chatroom → g4）
            label = gname if gname != gid else (
                gid[:-len("@chatroom")] if gid.endswith("@chatroom") else gid)
            base = f"summary-{date_str}-{safe_name(label, fallback='group')}"
        else:
            base = f"summary-{date_str}"
        name = f"{base}.md"
        n = 2
        # 只在**本次运行内**避免重名（同一天重跑应当覆盖同名文件，保持幂等）
        while name in used:
            name = f"{base}-{n}.md"
            n += 1
        used.add(name)
        path = out_dir / name
        _write_summary_file(path, date_str, gname, sub_total, summary, llm_cfg, sub_grouped, run_mode,
                            labels)
        log.info("总结已写入: %s", path)
        run.results.append(SummarizeResult(
            date=date_str, path=path, total=sub_total, conversations=len(sub_grouped),
            prompt=prompt, content=summary, grouped=sub_grouped, group=gname,
        ))

    if not run.results:
        raise RuntimeError("；".join(run.errors) or "所有会话的总结都失败了")
    return run


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description="当日归档 → LLM 归纳总结")
    ap.add_argument("--date", default=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                    help="日期（默认昨天）")
    ap.add_argument("--print-prompt", action="store_true",
                    help="只打印将发给 LLM 的 prompt，不联网")
    ap.add_argument("--out", default=None, help="输出目录（默认 reports/）")
    ap.add_argument("--mode", default=None, choices=["per_group", "combined"],
                    help="总结方式：per_group=每个群一份（默认，读 config 的 digest.summarize_mode）；combined=全部合并一份")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = Path(args.out) if args.out else None
    res = summarize(args.date, print_prompt=args.print_prompt, out_dir=out, mode=args.mode)
    if res:
        # 只打摘要：结果里带着 prompt/grouped，直接 print 会刷满屏
        print(f"方式: {res.mode}   共 {res.total} 条 / {len(res.results)} 份文件")
        for r in res.results:
            print(f"  已生成: {r.path}")
            print(f"    {r.group or '（合并）'}：{r.total} 条 · prompt {r.chars} 字 · 总结 {len(r.content)} 字")
        for e in res.errors:
            print(f"  ⚠ 失败: {e}")


if __name__ == "__main__":
    main()
