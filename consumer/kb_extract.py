"""群知识库：资料文件 → **纯文字**抽取 + 指纹（增量用）。

设计要点（2026-10-15 与用户确认）
--------------------------------
* **只取文字，忽略图片**。两个超大文件（126 MB `.doc` / 192 MB `.docx`）的正文其实很小
  （52,800 / 95,893 字），体积几乎全是内嵌图 —— 忽略图片后完全可控。
* 支持格式：`.txt` `.md` `.csv` `.pdf` `.docx` `.doc` `.pptx` `.ppt` `.xlsx`
* **零新依赖（除 PDF）**：
  - `.docx`/`.pptx`/`.xlsx` → 纯标准库解 zip + `ElementTree`（不用正则，正则漏嵌套形状）
  - `.doc`/`.ppt` → 系统 **Word/PowerPoint COM**（本机已确认可用），
    经 **PowerShell 子进程**调用 —— 这样不必给冻结 exe 加 `pywin32`
  - `.pdf` → `pypdf`（唯一新依赖；实测本机 4 个 PDF 都有文字层）
* **指纹**：`(size, mtime, sha256, extractor_version)`。内容没变就**不重抽**；
  抽取器升级（加格式/改归一化）导致 `extractor_version` 变化时**自动重抽**。
* **文本归一化**：PDF/`.doc` 常按排版逐行甚至逐字换行
  （实测 `'六\\n爻·\\n讲\\n义'`），不处理会白烧 token 且破坏语义。
  规则：行内汉字被单换行分隔 → 合并；空行/标题行/列表项/行尾标点 → 保留分段。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("consumer.kb_extract")

#: 抽取器版本。**改动抽取/归一化逻辑时必须 +1** —— 指纹里存了它，
#: 版本变化会让所有文件自动重抽（否则老文字稿会一直是旧的抽取结果）。
#: v1 首版；v2 修"排版硬断未合并"（`…能/够代表…`）+ 列表项保护。
EXTRACTOR_VERSION = 2

#: 默认支持的扩展名（用户要求含 md/txt/ppt/pptx 等）。
DEFAULT_EXT = (".txt", ".md", ".csv", ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx")

#: 忽略的临时/系统文件前缀与后缀（Office 锁文件、网盘上传中文件等）
_SKIP_PREFIX = ("~$", ".~")
_SKIP_SUFFIX = (".tmp", ".part", ".crdownload", ".baiduyun.uploading.cfg")

#: COM 抽取单个文件的超时（秒）。大文件（126 MB）实测需要十几秒。
COM_TIMEOUT_S = 240


class ExtractError(RuntimeError):
    """抽取失败（会带着文件名抛出，由调用方逐文件记录，不影响其它文件）。"""


@dataclass
class Extraction:
    """一个文件的抽取结果。"""
    path: Path
    text: str
    chars: int
    method: str                  # txt | docx-xml | pptx-xml | xlsx-xml | pdf | word-com | ppt-com
    warning: str = ""            # 非致命问题（如"疑似无文字层"）
    fingerprint: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 文本归一化
# ---------------------------------------------------------------------------

#: 行尾是这些字符 → 认为句子没说完，与下一行合并
_NO_END = "，,、；;：:（(【[《<「『“\"'‘"
#: 行首是这些字符 → 与上一行合并（续行）
_NO_START = "，,、；;：:）)】]》>」』”\"'’"
#: 标题式整行（短、无句读、常以“一、/1./第..课/一.”开头）→ 独立成段
_HEADING_RE = re.compile(
    r"^\s*(?:[一二三四五六七八九十百]+[、.．]|[0-9]{1,2}[、.．)]|第[一二三四五六七八九十百0-9]+[课节讲章]"
    r"|[（(][一二三四五六七八九十0-9]+[）)])\s*\S")


def _is_cjk(ch: str) -> bool:
    """是否汉字（用于判断"下一行是不是中文正文的续行"）。"""
    return bool(ch) and "\u4e00" <= ch <= "\u9fff"


def normalize_text(text: str) -> str:
    r"""把排版换行还原成自然段落（PDF/`.doc` 必需，docx 一般不需要）。

    实测输入（PDF 排版）::

        六\n爻·\n讲\n义\n岳 章\n主\n讲\n人 : 可\n贞\n

    期望输出::

        六爻·讲义岳 章主讲人 : 可贞

    规则（保守，宁少合不多合）：
      1. 单换行 + 上一行短(<=1 字) 或 上一行行尾无结句标点 → 与下一行拼接；
      2. 行首是收尾标点（，。）→ 接到上一行；
      3. 空行 = 段落边界，保留；
      4. 标题式行、列表项（`-`/`*`/`1.`）、代码围栏 → 独立成段，不合并。
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    raw_lines = [ln.rstrip() for ln in text.split("\n")]

    out: list[str] = []
    buf = ""                       # 正在拼的段落

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            out.append(buf.strip())
        buf = ""

    in_fence = False
    for ln in raw_lines:
        stripped = ln.strip()
        if stripped.startswith("```"):
            flush()
            in_fence = not in_fence
            out.append(stripped)
            continue
        if in_fence:
            out.append(ln)
            continue
        if not stripped:                      # 空行 = 段落边界
            flush()
            if out and out[-1] != "":
                out.append("")
            continue
        # 标题 / 列表项 / 显式段落：独立成段
        # ⚠️ 这些必须优先判断，否则会被下面的"无句读就合并"规则吞掉
        #    （列表项 `1、采光要好` 也没句读）
        is_struct = (
            _HEADING_RE.match(stripped)
            or stripped.startswith(("- ", "* ", "·", "•", "#", ">"))
            or re.match(r"^\d{1,2}[.)、]\s*\S", stripped)
            or re.match(r"^[（(]\d{1,2}[）)]\s*\S", stripped)
            # 短标题式行（<=20 字且以冒号结尾）
            or (len(stripped) <= 20 and stripped.endswith(("：", ":")))
        )
        if is_struct:
            # ⚠️ 结构行必须**留在 buf 里**（而不是直接写进 out）：
            #    否则下一行的"续行合并"永远跑不到（`if not buf` 会把它当成新段落）。
            #    实测 `6、…容易造成客欺` + `主的现象。` 就是因为这个原因一直被切开
            #    （2026-10-15 第三次修，前两次改的判定条件其实都没被执行到）。
            flush()
            buf = stripped
            continue
        if not buf:
            buf = stripped
            continue
        # 与上一行合并的条件
        prev = buf
        join = False
        # ⚠️ 列表项/标题行是**新段落的起点**（不能被上一条吞掉），
        #    但**下一行仍可以接到它后面** —— 实测 `6、主人的座位…容易造成客欺` +
        #    `主的现象。` 必须合并（2026-10-15 两次踩到：先被"没说完就接上"吞进上一条，
        #    又被"列表项永不续行"挡住）。
        prev_starts_para = bool(
            re.match(r"^\d{1,2}[.)、]\s*\S", prev)
            or re.match(r"^[（(]\d{1,2}[）)]\s*\S", prev)
            or prev.startswith(("- ", "* ", "·", "•", "#", ">"))
        )
        if len(prev.strip()) <= 2:                            # 逐字/极短排版
            join = True
        elif prev[-1] in _NO_END:                             # 上句没说完（含逗号）
            join = True
        elif prev_starts_para and not re.search(r"[。！？!?；;：:]", prev):
            # 上行是**列表项/标题起点**且自身没有句末标点 → 本行是它的续行。
            # 实测 `6、主人的座位…容易造成客欺` + `主的现象。` 属此类。
            join = True
        elif stripped[0] in _NO_START:                        # 本行以标点开头
            join = True
        elif not re.search(r"[。！？!?；;]", prev):
            # 上一行**没有结句标点** → 排版硬断，接上。
            # ⚠️ 不能再用"短行"当额外条件：实测 `…能 / 够代表社交属性`、`…客欺 / 主的现象`
            #    这种断点两侧都较长，漏判会把句子切碎。
            join = True
        elif len(prev) <= 15 and prev[-1] in "，,、" and _is_cjk(stripped[:1]):
            # 短行以逗号类结尾 + 本行以汉字开头 → 排版硬断
            join = True
        # 若本行是**新段落的起点**（标题/列表项），即使 `join` 为真也不能续到上一条：
        #   它是独立语义单元。但"上行是列表项"不影响合并（见上）。
        if is_struct:
            join = False
        if join:
            # 汉字之间不留空格；英文/数字之间保留一个空格
            if re.search(r"[A-Za-z0-9]$", prev) and re.match(r"^[A-Za-z0-9]", stripped):
                buf = prev + " " + stripped
            else:
                buf = prev + stripped
        else:
            flush()
            buf = stripped
    flush()

    # 折叠 3+ 连续空行
    res: list[str] = []
    blank = 0
    for ln in out:
        if ln == "":
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        res.append(ln)
    return "\n".join(res).strip()


# ---------------------------------------------------------------------------
# 各格式抽取
# ---------------------------------------------------------------------------

def _decode_bytes(data: bytes) -> str:
    """按常见中文编码依次尝试解码（BOM 优先）。"""
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    for enc in ("utf-8", "gb18030", "gbk", "big5", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _extract_text_file(path: Path) -> str:
    return _decode_bytes(path.read_bytes())


_XLSX_SHARED = re.compile(r"<si>(.*?)</si>", re.S)
_XLSX_T = re.compile(r"<t[^>]*>(.*?)</t>", re.S)


def _extract_xlsx(path: Path) -> str:
    """xlsx：共享字符串表 + 各 sheet 的单元格（只取文字，数字也保留）。"""
    import html
    out: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
            for si in _XLSX_SHARED.findall(sx):
                shared.append(html.unescape("".join(_XLSX_T.findall(si))))
        sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        for sn in sheets:
            sx = z.read(sn).decode("utf-8", "replace")
            rows = re.findall(r"<row[^>]*>(.*?)</row>", sx, re.S)
            out.append(f"\n## {sn.split('/')[-1]}")
            for r in rows:
                cells = re.findall(r'<c[^>]*?(?: t="(\w+)")?[^>]*>(?:<v>(.*?)</v>|<is>(.*?)</is>)?</c>',
                                   r, re.S)
                vals: list[str] = []
                for t, v, istr in cells:
                    if t == "s" and v:
                        try:
                            vals.append(shared[int(v)])
                        except (ValueError, IndexError):
                            vals.append("")
                    elif t == "inlineStr" and istr:
                        vals.append(html.unescape("".join(_XLSX_T.findall(istr))))
                    elif v:
                        vals.append(html.unescape(v))
                line = "\t".join(x for x in vals if x).strip()
                if line:
                    out.append(line)
    return "\n".join(out)


_DOCX_PARA = re.compile(r"<w:p[ >].*?</w:p>", re.S)
_DOCX_T = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)
_DOCX_TAB = re.compile(r"<w:tab\s*/>")
_DOCX_BR = re.compile(r"<w:br\s*/>")


def _extract_docx(path: Path) -> str:
    """docx：直接解 `word/document.xml`（纯标准库，实测可取全部正文）。"""
    import html
    with zipfile.ZipFile(path) as z:
        try:
            x = z.read("word/document.xml").decode("utf-8", "replace")
        except KeyError as e:
            raise ExtractError(f"不是有效的 docx（缺 word/document.xml）: {e}") from e
    out: list[str] = []
    for para in _DOCX_PARA.findall(x):
        para = _DOCX_TAB.sub("\t", para)
        para = _DOCX_BR.sub("\n", para)
        texts = _DOCX_T.findall(para)
        line = html.unescape(re.sub(r"<[^>]+>", "", "".join(texts))).strip()
        if line:
            out.append(line)
    return "\n".join(out)


_A_T = re.compile(r"<a:t>(.*?)</a:t>", re.S)


def _extract_pptx(path: Path) -> str:
    """pptx：各页形状文字 + 演讲者备注。

    ⚠️ 用**正则扫 `<a:t>`** 在嵌套组合形状下也够用（`a:t` 是叶子节点），
    但**表格、图表、SmartArt** 的文字可能取不全 —— 取不到时明确标注，不假装成功。
    """
    import html
    out: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)", n.split("/")[-1]).group(1)))
        notes = {int(re.search(r"(\d+)", n.split("/")[-1]).group(1)): n
                 for n in names if re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", n)}
        for sn in slides:
            no = int(re.search(r"(\d+)", sn.split("/")[-1]).group(1))
            x = z.read(sn).decode("utf-8", "replace")
            body = html.unescape("".join(_A_T.findall(x))).strip()
            out.append(f"\n## 第 {no} 页")
            if body:
                out.append(body)
            nn = notes.get(no)
            if nn:
                nx = z.read(nn).decode("utf-8", "replace")
                note = html.unescape("".join(_A_T.findall(nx))).strip()
                # 备注页会把页码当文字带出来，剥掉纯数字行
                note = "\n".join(l for l in note.splitlines() if not re.fullmatch(r"\s*\d+\s*", l))
                if note.strip():
                    out.append(f"【备注】{note.strip()}")
    return "\n".join(out)


def _extract_pdf(path: Path) -> Extraction:
    """pdf：pypdf 取文字层。无文字层时给出 warning（不产出空笔记）。"""
    try:
        from pypdf import PdfReader  # 延迟导入：没装也能跑别的格式
    except ImportError as e:  # noqa: BLE001
        raise ExtractError("未安装 pypdf，无法抽取 PDF（pip install pypdf）") from e
    reader = PdfReader(str(path))
    pages = len(reader.pages)
    chunks: list[str] = []
    empty = 0
    for i, p in enumerate(reader.pages, 1):
        try:
            t = p.extract_text() or ""
        except Exception as e:  # noqa: BLE001
            log.debug("PDF 第 %d 页抽取失败: %s", i, e)
            t = ""
        if not t.strip():
            empty += 1
        chunks.append(t)
    text = "\n".join(chunks)
    warn = ""
    if pages and not text.strip():
        warn = f"PDF 共 {pages} 页但**取不到任何文字**（疑似扫描件/纯图片）—— 未产出内容"
    elif pages and empty >= max(1, pages // 2):
        warn = f"PDF {pages} 页中有 {empty} 页取不到文字（可能是图片页）"
    return Extraction(path=path, text=normalize_text(text), chars=len(text.strip()),
                      method="pdf", warning=warn)


# --- Office COM（PowerShell 子进程，避免给冻结 exe 加 pywin32）----------------

_PS_DOC = r"""
$ErrorActionPreference = 'Stop'
$src = $env:WFT_KB_SRC; $dst = $env:WFT_KB_DST
$app = New-Object -ComObject Word.Application
$app.Visible = $false; $app.DisplayAlerts = 0
$doc = $null
try {
  $doc = $app.Documents.Open($src, $false, $true)
  $doc.SaveAs2($dst, 2)          # wdFormatText=2 -> 纯文本
} finally {
  if ($doc) { $doc.Close(0) }
  $app.Quit()
  [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
"""

_PS_PPT = r"""
$ErrorActionPreference = 'Stop'
$src = $env:WFT_KB_SRC; $dst = $env:WFT_KB_DST
$app = New-Object -ComObject Powerpoint.Application
$pres = $null
try {
  $pres = $app.Presentations.Open($src, $true, $false, $false)  # ReadOnly, Untitled, WithWindow=false
  $pres.SaveAs($dst, 17)         # ppSaveAsText=17 -> 纯文本大纲
  $pres.Close()
} finally {
  if ($pres) { try { $pres.Close() } catch {} }
  $app.Quit()
  [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
"""


def _powershell_exe() -> str:
    """找 PowerShell：优先 pwsh（更稳），退回 Windows PowerShell。"""
    for exe in ("pwsh.exe", "powershell.exe"):
        found = shutil.which(exe)
        if found:
            return found
    return "powershell.exe"


def _extract_via_com(path: Path, script: str, method: str) -> Extraction:
    """用 Office COM 把文件另存为纯文本，再读回来（图片被自然忽略）。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="wft-kb-"))
    dst = tmpdir / "out.txt"
    try:
        env = dict(os.environ)
        env["WFT_KB_SRC"] = str(path)
        env["WFT_KB_DST"] = str(dst)
        exe = _powershell_exe()
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, env=env, timeout=COM_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not dst.is_file():
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            raise ExtractError(
                f"{method} 失败（Office 未安装/文件损坏/被占用？）rc={proc.returncode} {err}")
        raw = dst.read_bytes()
        text = _decode_bytes(raw)
        return Extraction(path=path, text=normalize_text(text), chars=len(text.strip()),
                          method=method)
    except subprocess.TimeoutExpired as e:
        raise ExtractError(f"{method} 超时（>{COM_TIMEOUT_S}s，文件过大或被弹窗阻塞）") from e
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 指纹 / 调度
# ---------------------------------------------------------------------------

#: 超过这个体积就不算 sha256（退化为 size+mtime），避免给 100+ MB 文件反复哈希。
#: 做成模块常量是为了**测试可以调小**（否则回归里要造 64 MB 文件）。
SHA256_MAX_BYTES = 64 * 1024 * 1024


def file_fingerprint(path: Path) -> dict:
    """文件指纹：size + mtime + sha256 + 抽取器版本。

    只有体积不大才做 sha256（超大文件退化为 size+mtime，`sha256=None`）。
    """
    st = path.stat()
    digest = None
    if st.st_size <= SHA256_MAX_BYTES:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
    return {
        "size": int(st.st_size),
        "mtime": int(st.st_mtime),
        "sha256": digest,
        "extractor_version": EXTRACTOR_VERSION,
    }


def fingerprint_changed(old: dict | None, new: dict) -> bool:
    """新指纹与旧指纹是否**需要重抽**。

    ⚠️ 判定顺序很重要（2026-10-15 修）：
      1. 抽取器版本变了 → 重抽；
      2. **size 或 mtime 变了 → 直接重抽**（快速路径）。
         不能"有 sha256 就只比 sha256" —— 那样在 **mtime 不更新**的场景
         （网盘按内容同步、只读挂载、容器里拷贝文件）会**漏掉内容更新**；
      3. 两者都没变时，才用 sha256 兜底（防止 mtime 被伪造/粒度粗导致的漏判）。
    """
    if not old:
        return True
    if int(old.get("extractor_version") or 0) != int(new.get("extractor_version") or 0):
        return True
    if (int(old.get("size") or -1) != int(new.get("size") or -2)
            or int(old.get("mtime") or -1) != int(new.get("mtime") or -2)):
        return True
    if new.get("sha256") and old.get("sha256"):
        return old["sha256"] != new["sha256"]
    return False


def scan_files(root: Path, allow_ext: tuple[str, ...] | list[str] = DEFAULT_EXT) -> list[Path]:
    """列出资料目录下要处理的文件（跳过临时/隐藏/未知扩展名）。"""
    exts = tuple(e.lower() if e.startswith(".") else "." + e.lower() for e in allow_ext)
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        name = p.name
        if name.startswith(_SKIP_PREFIX) or name.lower().endswith(_SKIP_SUFFIX):
            continue
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() not in exts:
            continue
        out.append(p)
    return out


def extract_file(path: Path) -> Extraction:
    """按扩展名分派抽取。失败抛 `ExtractError`（带文件名）。"""
    ext = path.suffix.lower()
    try:
        if ext in (".txt", ".md", ".csv"):
            text = _extract_text_file(path)
            # txt/md 通常已是自然段落，做一次轻量归一（去掉多余空行）
            return Extraction(path=path, text=text.strip(), chars=len(text.strip()),
                              method="txt")
        if ext == ".docx":
            text = _extract_docx(path)
            return Extraction(path=path, text=text, chars=len(text), method="docx-xml")
        if ext == ".pptx":
            text = _extract_pptx(path)
            warn = "" if text.strip() else "pptx 取不到任何文字（可能是纯图片幻灯片）"
            return Extraction(path=path, text=text, chars=len(text), method="pptx-xml",
                              warning=warn)
        if ext == ".xlsx":
            text = _extract_xlsx(path)
            return Extraction(path=path, text=text, chars=len(text), method="xlsx-xml")
        if ext == ".pdf":
            return _extract_pdf(path)
        if ext == ".doc":
            return _extract_via_com(path, _PS_DOC, "word-com")
        if ext == ".ppt":
            return _extract_via_com(path, _PS_PPT, "ppt-com")
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ExtractError(f"{type(e).__name__}: {e}") from e
    raise ExtractError(f"不支持的扩展名 {ext}（可在 kb.allow_ext 里增减）")


@dataclass
class ExtractReport:
    """一次"抽取目录"的结果汇总。"""
    ok: list[Extraction] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)   # (文件, 原因)
    failed: list[tuple[Path, str]] = field(default_factory=list)
    warnings: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(e.chars for e in self.ok)


def extract_dir(root: Path, allow_ext: tuple[str, ...] | list[str] = DEFAULT_EXT) -> ExtractReport:
    """抽取整个资料目录（不做增量判断，调用方负责指纹比对）。"""
    rep = ExtractReport()
    for p in scan_files(root, allow_ext):
        try:
            e = extract_file(p)
            e.fingerprint = file_fingerprint(p)
            if e.warning:
                rep.warnings.append((p, e.warning))
            if not e.text.strip():
                rep.skipped.append((p, e.warning or "抽取结果为空"))
                continue
            rep.ok.append(e)
        except ExtractError as ex:
            rep.failed.append((p, str(ex)))
        except Exception as ex:  # noqa: BLE001
            rep.failed.append((p, f"{type(ex).__name__}: {ex}"))
    return rep


def main() -> None:
    """命令行：抽取某个资料目录，打印统计（不写任何东西）。"""
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="知识库资料文字抽取（只读，不写文件）")
    ap.add_argument("dir", help="资料目录")
    ap.add_argument("--ext", default=",".join(DEFAULT_EXT), help="允许的扩展名，逗号分隔")
    ap.add_argument("--show", type=int, default=0, help="每个文件额外打印前 N 字")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    root = Path(a.dir)
    rep = extract_dir(root, tuple(x.strip() for x in a.ext.split(",") if x.strip()))
    print(f"\n资料目录: {root}")
    print(f"  成功 {len(rep.ok)} 个文件，合计 {rep.total_chars:,} 字")
    for e in rep.ok:
        warn = f"  ⚠️ {e.warning}" if e.warning else ""
        print(f"    ✅ [{e.method:<9}] {e.chars:>8,} 字  {e.path.name}{warn}")
        if a.show:
            print(f"        {e.text[:a.show]!r}")
    for p, r in rep.skipped:
        print(f"    ⏭️  跳过 {p.name}: {r}")
    for p, r in rep.failed:
        print(f"    ❌ 失败 {p.name}: {r}")
    for p, w in rep.warnings:
        print(f"    ⚠️  {p.name}: {w}")


if __name__ == "__main__":
    main()
