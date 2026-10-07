"""消息内容解析与"卡片"渲染（web 浏览页 + 当日归档/总结共用）。

背景（2026-10-07）：以前浏览页对非文本消息直接吐 `content`（图片/链接就是一大坨 XML），
归档里也只有 `[图片]` / `[链接]` 一行。这里把 DLL 给的原始 payload 解析成结构化信息，
让两个出口都能显示出人看得懂的东西。

设计要点：
* **纯函数、零依赖**（只用标准库 `re`/`html`），web 与 consumer 都能 import；
* **只解析、不联网、不读盘** —— 图片/视频的 CDN 下载与本地解密是另一条路（见 media.py）；
* 解析失败**绝不抛异常**：消息内容千奇百怪，宁可退回原始文本；
* 富文本里的 HTML 标签要还原成**文本**（`<title>` 里常带 `<![CDATA[…]]>` 与 `<br/>`）。

消息类型（msg_type，DLL 给的）与 appmsg 子类型是两码事：
  * msg_type 3 图片 / 34 语音 / 42 名片 / 43 视频 / 48 位置 / 49 链接类 / 50 通话 / 10002 撤回
  * msg_type 49 内部再用 `<appmsg><type>` 细分（5=链接/图文, 57=引用回复, 6=文件, 21=小程序, …）
"""
from __future__ import annotations

import html as _html
import re
from typing import Any

# ---------------------------------------------------------------- 工具

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_NL_RE = re.compile(r"\n{3,}")

#: appmsg 子类型 → 中文标签（来自实际数据里的分布 + 微信公开语义）
APPMSG_TYPES: dict[int, str] = {
    1: "文字",
    3: "音乐",
    4: "视频",
    5: "链接/图文",
    6: "文件",
    8: "表情",
    17: "实时位置",
    19: "聊天记录",
    21: "小程序",
    24: "收藏笔记",
    33: "直播",
    36: "小程序",
    47: "表情包",
    49: "引用/合并转发",
    51: "视频号/不支持的内容",
    53: "接龙",
    57: "引用回复",
    62: "视频号",
    63: "直播回放",
    74: "文件",
    87: "群接龙",
    2000: "转账",
    2001: "红包",
}

#: msg_type → 中文标签
MSG_TYPES: dict[int, str] = {
    1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频",
    47: "表情", 48: "位置", 49: "链接/卡片", 50: "通话",
    51: "系统", 10000: "系统", 10002: "撤回",
}


def strip_tags(s: str) -> str:
    """把富文本还原成纯文本：去 CDATA、去标签、解码实体、压掉多余空白。"""
    if not s:
        return ""
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = _NL_RE.sub("\n\n", s)
    return s.strip()


def _tag(xml: str, name: str) -> str:
    """取第一个 <name>…</name> 的内容（已去标签/解码）。取不到返回空串。"""
    m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S)
    return strip_tags(m.group(1)) if m else ""


def _attr(xml: str, name: str, default: str = "") -> str:
    """取属性值。

    ⚠️ 用 `(?<![A-Za-z0-9_])` 而不是 `\\b`：`\\blength=` 会**先匹配到 `cdnthumblength=`**
    （下划线是 \\w 字符，`\\b` 在 `b`/`l` 之间不成立但 `_length` 处成立）——
    实测把缩略图长度当成了原图大小（10-07 踩到，被 wft_cards_check 抓住）。
    """
    m = re.search(rf'(?<![A-Za-z0-9_]){re.escape(name)}="([^"]*)"', xml)
    return _html.unescape(m.group(1)).strip() if m else default


def _int(s: str) -> int:
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def human_size(n: int) -> str:
    if n <= 0:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


# ---------------------------------------------------------------- 主入口

def parse(msg_type: int | str, content: str) -> dict[str, Any]:
    """把一条消息的 content 解析成结构化数据。

    返回的 dict 里 **kind** 是渲染分支用的标签，其余字段按 kind 而定：
      text      : text
      quote     : quote_from, quote_text, text(附加说明), appmsg_type
      link      : title, desc, url, app, appmsg_type
      image     : width, height, size, size_h, aeskey, has_hd
      video     : width, height, size, duration
      voice     : duration, transcript(由调用方补)
      location  : label, poiname, lat, lng
      card      : nickname, username, avatar
      revoke    : who, text
      call      : text, duration
      file      : title, size, ext
      unsupported: title(通常是"当前版本不支持…"), appmsg_type
      other     : label
    另附 `label`（中文类型名）与 `raw_head`（原始 XML 前 200 字，兜底展示用）。
    """
    mt = _int(str(msg_type)) if not isinstance(msg_type, int) else msg_type
    raw = content or ""
    out: dict[str, Any] = {"msg_type": mt, "label": MSG_TYPES.get(mt, f"type={mt}"),
                           "raw_head": raw[:200], "kind": "other"}

    try:
        if mt == 1:
            out.update(kind="text", text=raw.strip())
            return out

        if mt == 3:
            out.update(
                kind="image",
                width=_int(_attr(raw, "cdnthumbwidth")),
                height=_int(_attr(raw, "cdnthumbheight")),
                size=_int(_attr(raw, "length")),
                size_hd=_int(_attr(raw, "hevc_mid_size")),
                aeskey=_attr(raw, "aeskey"),
                md5s=re.findall(r'md5="([0-9a-fA-F]{32})"', raw),
            )
            out["size_h"] = human_size(out["size"])
            return out

        if mt == 34:
            mm = re.search(r'voicelength="(\d+)"', raw)
            out.update(kind="voice", duration=round(_int(mm.group(1)) / 1000) if mm else 0)
            return out

        if mt == 43:
            out.update(
                kind="video",
                width=_int(_attr(raw, "cdnthumbwidth")),
                height=_int(_attr(raw, "cdnthumbheight")),
                size=_int(_attr(raw, "length") or _attr(raw, "cdnvideolength")),
                duration=_int(_attr(raw, "playlength")),
            )
            out["size_h"] = human_size(out["size"])
            return out
        if mt == 42:
            out.update(kind="card", nickname=_attr(raw, "nickname"),
                       username=_attr(raw, "username"), avatar=_attr(raw, "smallheadimgurl"))
            return out

        if mt == 48:
            out.update(kind="location", label=_attr(raw, "label"), poiname=_attr(raw, "poiname"),
                       lat=_attr(raw, "x"), lng=_attr(raw, "y"))
            return out

        if mt == 50:
            dur = _int(_attr(raw, "duration"))
            out.update(kind="call", text=_tag(raw, "msg") or strip_tags(raw)[:60], duration=dur)
            return out

        if mt == 10002:
            who = strip_tags(re.search(r"<replacemsg>(.*?)</replacemsg>", raw, re.S).group(1)) \
                if re.search(r"<replacemsg>(.*?)</replacemsg>", raw, re.S) else ""
            out.update(kind="revoke", who=who, text=who or "撤回了一条消息")
            return out

        if mt == 49:
            return _parse_appmsg(raw, out)

    except Exception:  # noqa: BLE001
        # 解析失败绝不抛：退回"原始文本"展示
        out.update(kind="other", parse_error=True)
        return out

    return out


def _parse_appmsg(raw: str, out: dict[str, Any]) -> dict[str, Any]:
    """msg_type=49 的细分（链接 / 引用回复 / 文件 / 小程序 / 视频号 …）。"""
    m = re.search(r"<type>(\d+)</type>", raw)
    atype = _int(m.group(1)) if m else 0
    title = _tag(raw, "title")
    desc = _tag(raw, "des")
    url = _tag(raw, "url")
    app = _tag(raw, "appname") or _tag(raw, "sourcename") or _tag(raw, "username")
    appmsg_label = APPMSG_TYPES.get(atype, f"卡片 type={atype}")
    out["appmsg_type"] = atype
    out["appmsg_label"] = appmsg_label

    # 引用回复：<refermsg> 里带被引用的人与原话（这是群里上下文的关键）
    if "<refermsg>" in raw:
        ref = raw[raw.find("<refermsg>"):]
        q_from = _tag(ref, "displayname") or _tag(ref, "chatusr")
        q_text = _tag(ref, "content")
        q_label = ""
        is_xml = bool(q_text) and ("<?xml" in q_text or q_text.lstrip().startswith("<"))
        if not q_text or is_xml:
            # 被引用的内容本身是"非文本"（图片/链接/文件…）：直接嵌的是 XML。
            # 这里**再解析一层**，把它变成 [图片 214×480] / [链接] 标题 这类人话，
            # 而不是把 XML 原样塞进摘要（10-07 实测有 76 条这种嵌套引用）。
            ref_type = _int(_tag(ref, "type")) or 49
            inner = parse(ref_type, q_text or "")
            q_text = summary_line(ref_type, q_text or "")
            q_label = inner.get("label") or ""
        out.update(kind="quote", quote_from=q_from, quote_text=q_text, quote_label=q_label,
                   text=title or desc, label="引用回复")
        return out

    # 文件
    if atype in (6, 74):
        att = raw[raw.find("<appattach>"):] if "<appattach>" in raw else ""
        out.update(kind="file", title=title or _tag(raw, "filename"),
                   size=_int(_tag(att, "totallen")), ext=_tag(att, "fileext"),
                   label="文件")
        out["size_h"] = human_size(out["size"])
        return out

    # 真链接（有 url）
    if url and not url.startswith("http://support.weixin.qq.com"):
        out.update(kind="link", title=title or url, desc=desc, url=url,
                   app=app, label="链接")
        return out

    # 视频号 / 直播 / 不支持的内容
    if atype in (51, 33, 62, 63) or "finderFeed" in raw:
        nick = _tag(raw, "nickname")
        out.update(kind="unsupported", title=title or appmsg_label,
                   text=(f"来自 {nick}" if nick else desc), label=appmsg_label)
        return out

    # 小程序
    if atype in (21, 36) or "<miniprogramappid>" in raw:
        out.update(kind="unsupported", title=title or desc or "小程序",
                   text=desc, label="小程序")
        return out

    # 其他卡片：能给标题就给
    out.update(kind="unsupported" if title or desc else "other",
               title=title, text=desc, label=appmsg_label)
    return out


# ---------------------------------------------------------------- 单行摘要

def summary_line(msg_type: int | str, content: str, *, transcript: str = "",
                 max_len: int = 160) -> str:
    """给归档/总结用的**单行**摘要（图片/视频/链接都不再只显示 [类型]）。"""
    d = parse(msg_type, content)
    kind = d.get("kind")
    if kind == "text":
        s = d.get("text", "")
    elif kind == "voice":
        t = (transcript or "").strip()
        if t and not t.startswith(("[待转写]", "[解码失败]", "[未捕获")):
            s = f"[语音] {t}"
        else:
            s = f"[语音 ~{d.get('duration') or '?'}s]"
    elif kind == "image":
        wh = f" {d['width']}×{d['height']}" if d.get("width") and d.get("height") else ""
        sz = f" · {d['size_h']}" if d.get("size_h") else ""
        s = f"[图片{wh}{sz}]"
    elif kind == "video":
        wh = f" {d['width']}×{d['height']}" if d.get("width") and d.get("height") else ""
        sz = f" · {d['size_h']}" if d.get("size_h") else ""
        dur = f" · {d['duration']}s" if d.get("duration") else ""
        s = f"[视频{wh}{sz}{dur}]"
    elif kind == "link":
        s = f"[链接] {d.get('title') or d.get('url')}"
        if d.get("desc"):
            s += f" —— {d['desc']}"
    elif kind == "quote":
        s = f"[引用 {d.get('quote_from') or '?'}] {d.get('quote_text') or ''}"
        if d.get("text"):
            s += f" ｜ 附言：{d['text']}"
    elif kind == "file":
        s = f"[文件] {d.get('title') or ''}" + (f" ({d['size_h']})" if d.get("size_h") else "")
    elif kind == "location":
        s = f"[位置] {d.get('poiname') or ''} {d.get('label') or ''}".strip()
    elif kind == "card":
        s = f"[名片] {d.get('nickname') or d.get('username') or ''}"
    elif kind == "call":
        s = f"[通话] {d.get('text') or ''}" + (f" {d['duration']}s" if d.get("duration") else "")
    elif kind == "revoke":
        s = d.get("text") or "撤回了一条消息"
    elif kind == "unsupported":
        s = f"[{d.get('label')}] {d.get('title') or d.get('text') or ''}".strip()
    else:
        # other（含历史遗留的系统/操作类消息）：**绝不把 XML 或内部 JSON 塞进摘要**。
        # 实测 type=51 是 `<op id=5><name>lastMessage</name><arg>{"messageSvrId":…}</arg>`
        # 这类内部操作消息，本来就没有人看的正文（「消息分类」默认已把它挡在库外），
        # 所以这里只留一行中文类型标签。
        plain = strip_tags(d.get("raw_head") or "")
        noisy = (not plain) or plain.lstrip().startswith(("{", "<?xml")) or "lastMessage" in plain
        s = f"[{d.get('label')}]" if noisy else f"[{d.get('label')}] {plain[:60]}"
    s = _WS_RE.sub(" ", (s or "").strip())
    return s[:max_len] + ("…" if len(s) > max_len else "")
