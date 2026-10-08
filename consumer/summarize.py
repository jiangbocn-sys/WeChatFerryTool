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
import re
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
    _is_star,
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
**发送人前面带 `★` 的是群主重点关注的人**（他们的话往往更关键，但不代表其它人的发言可以忽略——
恰恰相反，别人的提问与回应是理解上下文的关键）。

请根据讨论的内容，总结核心要点，按顺序逐条输出总结内容。

要求：
1. 按时间顺序逐条输出总结要点，每条一行，以「- 」开头
2. 输入记录已按时间排序、且每条都标注了时刻，请据此梳理讨论的先后与进展；
   输出**不需要**逐条标注时间
3. 保留对话中出现的关键信息：人名、时间、数字、金额、结论、待办事项；
   **带 ★ 的人是重点关注对象**，其发言要优先覆盖，但梳理因果时必须结合其它人的提问与回应
4. 同一话题的连续发言合并为一条要点，不要逐句复述原文；
   照原样保留该群的**领域用词**（各群关心的东西不同，不要替换成你熟悉的其它领域词汇）
5. 不要编造记录中没有的信息；转写明显有误时标注「（转写存疑）」
6. 忽略寒暄、表情与无实质内容的发言
7. **语音转写的错字要按上下文推断，不要机械替换**（重要）：标记为「语音转文本」的内容是
   自动识别的，同音字/近音字错得很常见。处理原则：
   · 先按**上下文**判断某个词在这里是否读得通 —— 读得通就**原样采信**，绝不因为"它长得像
     某个术语"就改掉（很多词在别的语境里本来就是正确的，乱改会制造新错误）；
   · 只有当某个词在本句/本话题里**明显读不通**、而换成某个同音词后整段逻辑才通顺时，
     才按上下文改成那个词，并**按改后的意思理解**；
   · **以该群自己的领域为准**：本群是做什么的、在聊什么，就用它那一行的常用说法
     （专业术语、内部简称、人名/地名），不要往别的领域猜；
   · 判断依据优先级：本群/本话题的用词习惯 > 说话人前后几句的表述 > 常识；
   · 拿不准就保留原词，并在该条要点后标「（转写存疑）」。
   以下是**历史上确实出现过的同音错例**，只是"可疑信号"的举例，**不是替换表**，
   也**不代表所有群都关心这些领域**（换个群这些词可能完全不相干）：
   神经（可能是申金）、事要（可能是世爻）、尘土（可能是辰土）、六要（可能是六爻）、
   动要（可能是动爻）、干枝（可能是干支）、那甲（可能是纳甲）、挂要（可能是卦爻）、
   映要（可能是应爻）、入木（可能是入墓）、缝合（可能是逢合）、寻空（可能是旬空）、
   太急（可能是太极）、方为（可能是方位）、逢河（可能是逢合）、日成（可能是日辰）、
   暗洞（可能是暗动）。这些词在别的语境里可能完全正确 —— 只有读不通时才考虑。
8. 不要因为上述推断而编造记录里没有的信息；不推断没把握的词，改与不改都要能自圆其说。
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
    rule = ("9. **下面「本群总结提示」是群主对本群总结口径的要求，优先级高于上面的通用要求**："
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
            SELECT msg_id, group_name, sender, content, msg_type, received_at, transcript
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
    # 图片理解：一次性取本批图片消息的描述（带缓存；仅在设置页开启时才调模型）
    try:
        from consumer.digest import image_descriptions
        img_ids = [str(r["msg_id"]) for r in rows
                   if int(r["msg_type"] or 0) == 3 and r["msg_id"]]
        descs = image_descriptions(img_ids) if img_ids else {}
    except Exception:  # noqa: BLE001
        descs = {}

    for r in rows:
        if not _in_digest(r, labels, exclude, monitored):
            continue
        ts = int(r["received_at"] or 0)
        hhmm = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"
        gkey = r["group_name"] or "?"
        skey = r["sender"] or "?"

        gname = ((labels.get("groups") or {}).get(gkey) or {}).get("name") or gkey
        sname = ((labels.get("senders") or {}).get(skey) or {}).get("name") or skey

        body = _render_content(r, descs.get(str(r["msg_id"]), "")).replace("\n", " ").strip()
        # 重点人/重点群**不再被过滤**，而是打 ★ 标记交给模型加权（10-07 用户确认）
        star = "★" if _is_star(r, labels) else ""
        out.setdefault(gkey, []).append(
            f"[{hhmm}]{star}{sname}（{_type_label(int(r['msg_type'] or 0))}）：{body}"
        )
    return out


def _safe_print(text: str) -> None:
    """打印可能含任意字符的文本（prompt 里会带用户的提示文本、群名、emoji…）。

    Windows 控制台默认 GBK：直接 print 会在遇到 `⚠️`/`↔` 这类字符时抛
    `UnicodeEncodeError`（10-07 实测 `--print-prompt` 因此崩过）。
    这里逐层降级：UTF-8 容错 → ASCII 替换 —— **任何情况下都不让打印把功能搞崩**。
    """
    try:
        print(text)
        return
    except UnicodeEncodeError:
        pass
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
        print(text)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        print(text.encode("ascii", "replace").decode("ascii"))
    except Exception:  # noqa: BLE001
        pass


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


# ---------------------------------------------------------------------------
# 阶段（跨天，指定会话）总结 —— 2026-10-07 新增
# ---------------------------------------------------------------------------
def fmt_range(start: str, end: str) -> str:
    """把起止日期显示成人看的样子：同日 → `2026-10-07`；跨天 → `2026-10-05 ~ 2026-10-07`。"""
    return start if start == end else f"{start} ~ {end}"


def collect_group_range(gid: str, start: str, end: str,
                        db_path: Path | None = None) -> tuple[list[str], dict] | None:
    """取**指定会话**在 [start, end]（含两端整天）内的消息，渲染成归档那种行。

    与每日总结的区别（刻意如此）：
      * **只按会话取**（用户明确指定了一个群），不像 `_in_digest` 那样按监控名单/重点人过滤 ——
        阶段复盘往往就是想看"这个群这几天到底聊了什么"，包括"路人"的话。
      * 时间范围是**日期闭区间**（含 end 那天整天），不是单日。
      * 仍然套用类型闸门 `digest.exclude_types`（表情/系统/图片等噪音先挡掉）与 ★ 标记。

    返回 `([行, ...], {"count": n, "start": int_ts, "end": int_ts, "days": [...]})`；
    数据库不存在返回 None。
    """
    day0 = datetime.strptime(start, "%Y-%m-%d")
    day1 = datetime.strptime(end, "%Y-%m-%d")
    if day1 < day0:
        day0, day1 = day1, day0
    lo = int(day0.timestamp())
    hi = int((day1 + timedelta(days=1)).timestamp())      # 含 end 当天整天

    db_file = db_path or (PROJECT_DIR / "data" / "messages.db")
    if not db_file.is_file():
        return None
    labels = _load_labels(LABELS_PATH)
    exclude = _exclude_types()

    conn = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT msg_id, group_name, sender, content, msg_type, received_at, transcript
               FROM messages
               WHERE group_name = ? AND received_at >= ? AND received_at < ?
               ORDER BY received_at""",
            (gid, lo, hi),
        ).fetchall()
    finally:
        conn.close()

    lines: list[str] = []
    days: set[str] = set()
    # 图片理解（阶段总结也一样带图意；带缓存、仅开启时调模型）
    try:
        from consumer.digest import image_descriptions
        img_ids = [str(r["msg_id"]) for r in rows
                   if int(r["msg_type"] or 0) == 3 and r["msg_id"]]
        descs = image_descriptions(img_ids) if img_ids else {}
    except Exception:  # noqa: BLE001
        descs = {}
    for r in rows:
        if int(r["msg_type"] or 0) in exclude:
            continue
        ts = int(r["received_at"] or 0)
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts)
        # 跨天时把日期也带上，模型才能区分"周一的讨论"和"周三的结论"
        stamp = dt.strftime("%m-%d %H:%M") if start != end else dt.strftime("%H:%M")
        days.add(dt.strftime("%Y-%m-%d"))
        skey = r["sender"] or "?"
        sname = ((labels.get("senders") or {}).get(skey) or {}).get("name") or skey
        star = "★" if _is_star(r, labels) else ""
        body = _render_content(r, descs.get(str(r["msg_id"]), "")).replace("\n", " ").strip()
        lines.append(f"[{stamp}]{star}{sname}（{_type_label(int(r['msg_type'] or 0))}）：{body}")

    meta = {"count": len(lines), "start": start, "end": end, "days": sorted(days)}
    return lines, meta


def resolve_range(text: str) -> tuple[str, str, str]:
    """把用户填的时间段解析成 `(start, end, 说明)`。

    支持（大小写/空格随意）：
      * 具体日期：`2026-10-05`（单个日期 = 那一天）
      * 区间：`2026-10-05 ~ 2026-10-07`、`2026-10-05 到 2026-10-07`、
        `2026/10/5-2026/10/7`、`2026-10-05,2026-10-07`
      * 星期：`周一~周三`（= **本周**一 → 周三）、`星期一到星期五`、
        `周一`（只有起点的星期 = 那天单日；`周一~周三` 这种两个星期都认）
    解析失败抛 `ValueError`，由调用方显示成人话。

    返回 `(start, end, 人看的说明)`，说明里会写清"星期几实际落到了哪几个日期"，
    避免用户以为选了周一~周三、实际却是上周。
    """
    s = (text or "").strip().replace("～", "~").replace("—", "-").replace("－", "-")
    if not s:
        raise ValueError("请填写时间段")
    plain = s.replace(" ", "")
    today = datetime.now().date()

    def _wk(tok: str) -> int | None:
        """周一=0 … 周日=6；认不出返回 None。"""
        t = tok.strip()
        for i, names in enumerate((("周一", "星期一", "礼拜一", "周1", "星期1"),
                                   ("周二", "星期二", "礼拜二", "周2", "星期2"),
                                   ("周三", "星期三", "礼拜三", "周3", "星期3"),
                                   ("周四", "星期四", "礼拜四", "周4", "星期4"),
                                   ("周五", "星期五", "礼拜五", "周5", "星期5"),
                                   ("周六", "星期六", "礼拜六", "周6", "星期6"),
                                   ("周日", "周天", "星期日", "星期天", "礼拜日", "礼拜天", "周7", "星期7"))):
            if t in names:
                return i
        return None

    def _date(tok: str) -> str | None:
        t = (tok or "").strip().replace("/", "-").replace(".", "-")
        for fmt in ("%Y-%m-%d", "%y-%m-%d", "%m-%d"):
            try:
                d = datetime.strptime(t, fmt)
            except ValueError:
                continue
            if fmt == "%m-%d":                      # 没写年份 → 补当年
                d = d.replace(year=today.year)
            return d.strftime("%Y-%m-%d")
        return None

    # ① 具体日期（单个或区间）。⚠️ **不能把单个 `-` 当分隔符**，
    #    否则 `2026-10-05` 会被拆成 2026 / 10 / 05 三段，日期解析全废。
    # 具体日期（单个或区间）。⚠️ 关键难点：`-` 既是**日期内部**分隔符又是**区间**分隔符
    #（`2026-10-05-2026-10-07` 这种混用写法必须能切开），所以顺序是：
    #   ① 先在"日期与日期之间"的 `-` 上插入 `~`（用前瞻/后顾判断两侧都是日期）
    #   ② 再把 `/`、`.` 分隔的日期归一成 `-`
    #   ③ 最后按 `~`/逗号/顿号/到/至/空白 切分
    # 踩过：先归一（`2026/10/5` → `2026-10-5`）会得到无法切分的 `2026-10-5-2026-10-7`。
    spaced = re.sub(r"(?<=\d)\s*-\s*(?=\d{4}[-/.]\d)", "~", plain)
    spaced = re.sub(r"(?<=\d)\s*-\s*(?=\d{1,2}[-/.]\d)", "~", spaced)
    norm = re.sub(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", r"\1-\2-\3", spaced)
    norm = re.sub(r"(?<![\d-])(\d{1,2})[-/.](\d{1,2})(?![\d-])", r"\1-\2", norm)
    date_toks = [p for p in re.split(r"[~,，、]|到|至|\s+", norm) if p]
    dates = [d for d in (_date(p) for p in date_toks) if d]
    if dates:
        # `2026-10-05 ~ 10-07`：第二个只写了月-日 → 沿用起始年份
        year = dates[0][:4]
        dates = [(year + d[4:]) if len(d) == 5 else d for d in dates]
        start, end = min(dates), max(dates)
        note = f"{fmt_range(start, end)}（共 {_date_span(start, end)} 天）"
        return start, end, note

    # ② 星期：`周一~周三` / `星期一到星期五`（同样不按 `-` 拆，否则"星期一"会被拆碎）
    parts = [p for p in re.split(r"[~,，、]|到|至|\s+", plain) if p]
    wks = [_wk(p) for p in parts]
    wks = [w for w in wks if w is not None]
    if wks:
        monday = today - timedelta(days=today.weekday())     # 本周一
        start_d, end_d = monday + timedelta(days=wks[0]), monday + timedelta(days=wks[-1])
        if end_d < start_d:                                   # 例：周五~周一 → 跨周
            end_d += timedelta(days=7)
        start, end = start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d")
        note = (f"本周 {parts[0]} ~ {parts[-1]} → 实际 {fmt_range(start, end)}"
                f"（共 {_date_span(start, end)} 天）")
        return start, end, note

    raise ValueError(f"看不懂这个时间段：{text!r}。可填 `2026-10-05`、`2026-10-05 ~ 2026-10-07` "
                     f"或 `周一~周三`")


def _date_span(start: str, end: str) -> int:
    a = datetime.strptime(start, "%Y-%m-%d").date()
    b = datetime.strptime(end, "%Y-%m-%d").date()
    return (b - a).days + 1


def build_range_prompt(gid: str, lines: list[str], start: str, end: str,
                       extra_requirement: str = "", labels: dict | None = None) -> str:
    """阶段总结的 prompt：同一套规则模板，但标题是"<起> ~ <止>"，并可**追加用户的具体要求**。

    `extra_requirement` 会作为**第 10 条**插在通用规则之后、每群总结提示之前 ——
    它是"这次任务额外要什么"（例如"按事件线梳理""只统计金额与决定""输出表格"），
    优先级低于每群总结提示（后者是该群的长期口径）。
    """
    if labels is None:
        labels = _load_labels(LABELS_PATH)
    gname = name_for_gid(gid, labels)
    day_label = fmt_range(start, end)

    body = "\n".join([f"### 群：{gname}（{len(lines)} 条）", *lines])
    hint = _summary_hint(gid, labels)
    hints_block = _hints_block([(gname, hint)]) if hint else ""

    extra = ""
    req = (extra_requirement or "").strip()
    if req:
        extra = ("10. **本次任务的额外要求（用户本次指定，必须满足）**：\n"
                 + "\n".join("    " + ln for ln in req.splitlines() if ln.strip()) + "\n")

    out = (PROMPT_TEMPLATE
           .replace("下面是「{date}」的群聊发言记录", f"下面是「{day_label}」的群聊发言记录")
           .replace("{date}", day_label)
           .replace("{extra_rules}", hints_block + extra)
           .replace("{body}", body.strip()))
    return out


def summarize_range(gid: str, start: str, end: str, extra_requirement: str = "",
                    out_dir: Path | None = None, dry_run: bool = False,
                    db_path: Path | None = None) -> dict:
    """阶段总结主入口（Web 与 CLI 共用）。

    `dry_run=True` 只拼 prompt、不联网（返回 prompt 与统计），便于"先看再花钱"。
    返回 dict：`{"ok", "prompt", "content", "path", "total", "days", "chars", "error"}`。
    """
    labels = _load_labels(LABELS_PATH)
    got = collect_group_range(gid, start, end, db_path=db_path)
    if got is None:
        return {"ok": False, "error": f"找不到数据库 {db_path or (PROJECT_DIR / 'data' / 'messages.db')}"}
    lines, meta = got
    prompt = build_range_prompt(gid, lines, start, end, extra_requirement, labels)
    result = {"ok": True, "prompt": prompt, "chars": len(prompt),
              "total": meta["count"], "days": meta["days"],
              "group": name_for_gid(gid, labels), "start": start, "end": end,
              "content": "", "path": ""}
    if dry_run:
        return result
    if not lines:
        result["ok"] = False
        result["error"] = (f"{name_for_gid(gid, labels)} 在 {fmt_range(start, end)} "
                           f"没有可总结的消息（检查时间段，或该群这几天没消息）")
        return result

    llm_cfg = _llm_cfg()
    try:
        content = strip_think(call_llm(prompt, llm_cfg))
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    if not content:
        result["ok"] = False
        result["error"] = "LLM 返回了空内容（模型只输出了推理过程？）"
        return result

    out = out_dir or (PROJECT_DIR / "reports")
    out.mkdir(parents=True, exist_ok=True)
    tag = start if start == end else f"{start}_{end}"
    fname = f"summary-{tag}-{safe_name(name_for_gid(gid, labels), fallback='group')}.md"
    path = out / fname
    head = [
        f"# 阶段群聊总结 · {fmt_range(start, end)} · {name_for_gid(gid, labels)}",
        "",
        f"> 会话：**{name_for_gid(gid, labels)}**（{meta['count']} 条，覆盖 "
        f"{len(meta['days'])} 天：{'、'.join(meta['days'])}）",
        f"> 由大模型归纳生成；模型：{llm_cfg.get('model') or '?'} @ {llm_cfg.get('base_url') or '?'}",
    ]
    if (extra_requirement or "").strip():
        head.append(f"> 额外要求：{(extra_requirement or '').strip()}")
    hint = _summary_hint(gid, labels)
    if hint:
        head.append(f"> 本群总结提示：{hint}")
    head += ["", content, "", "---", "", "## 附：提交给模型的原始记录", ""]
    head += [f"### {name_for_gid(gid, labels)}"] + lines
    path.write_text("\n".join(head) + "\n", encoding="utf-8")

    result["content"] = content
    result["path"] = str(path)
    result["name"] = path.name
    return result



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
                _safe_print(f"\n########## {gname}（{len(lines)} 条）##########")
                _safe_print(build_prompt(date_str, {gid: lines}, labels))
        else:
            _safe_print(build_prompt(date_str, grouped))
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
    # 控制台可能是 GBK：必须带 errors="replace"，否则 prompt 里出现
    # `↔`/`⚠️` 这类字符时 --print-prompt 会 UnicodeEncodeError 直接崩（10-07 踩到）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
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
