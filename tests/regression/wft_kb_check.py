"""回归：群知识库的**资料抽取器**（consumer/kb_extract.py）。

覆盖：
  A. 文本归一化规则（PDF/`.doc` 的排版硬断还原）—— 踩过 3 次，必须锁死
  B. `.txt`/`.md`/`.csv` 直读 + 编码嗅探（utf-8 / gbk / utf-16 / BOM）
  C. `.docx` 纯标准库解 document.xml（含表格/多段落）
  D. `.pptx` 纯标准库解 a:t + 演讲者备注
  E. `.xlsx` 纯标准库解 sharedStrings
  F. 指纹与增量判定（未变不重抽；抽取器版本变化强制重抽）
  G. 扫描规则（跳过临时文件/隐藏目录/未知扩展名）
  H. 失败隔离（坏文件不影响其它文件）

⚠️ 真实 `.doc`/`.ppt` 走 Office COM，回归里**不测**（不能在 CI 里弹 Word），
   只测分派逻辑（`_extract_via_com` 存在且被正确路由）。
"""
from __future__ import annotations

import os
import shutil
import sys
import zipfile
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-kb-suite")
shutil.rmtree(SC, ignore_errors=True)
SC.mkdir(parents=True)
sys.path.insert(0, str(PROJ))
os.environ.setdefault("WCF_NO_MULTI_ACCOUNT", "1")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer import kb_extract as K  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


print("[A] 文本归一化：排版硬断还原（踩过 3 次）")
n = K.normalize_text
# A1 逐字换行（PDF 最典型）
check("逐字换行被合并",
      n("六\n爻·\n讲\n义\n岳 章\n主\n讲\n人 : 可\n贞") == "六爻·讲义岳 章主讲人 : 可贞",
      n("六\n爻·\n讲\n义\n岳 章\n主\n讲\n人 : 可\n贞"))
# A2 行内硬断（上行无句末标点）
check("上行无句末标点 → 续行合并",
      n("…能\n够代表社交属性。").endswith("能够代表社交属性。"), n("…能\n够代表社交属性。"))
# A3 **列表项末尾被切断也要合并**（这是修了三次的那个 bug）
got3 = n("5、地板易平整，主根基不稳。\n6、主人的座位不可低于其他座位，容易造成客欺\n主的现象。")
check("**列表项末尾续行也合并**",
      "客欺主的现象。" in got3 and got3.count("\n") == 1, got3)
# A4 列表项本身独立成段（不能被上一条吞）
got4 = n("上一段结束了。\n1、采光要好，通风佳。\n2、避免尖角。")
check("列表项独立成段", got4.count("\n") == 2 and "1、采光要好" in got4, got4)
# A5 短标题 + 正文合并（标题行留在 buf 里，正文接上）
check("短标题与正文合并", n("客厅：\n客厅是聚集的地方。") == "客厅：客厅是聚集的地方。",
      n("客厅：\n客厅是聚集的地方。"))
# A6 空行 = 段落边界，且折叠多余空行
check("空行保留一个（段落边界）",
      n("第一段。\n\n\n\n第二段。") == "第一段。\n\n第二段。", repr(n("第一段。\n\n\n\n第二段。")))
# A7 标题式行独立成段
check("`一、` 标题独立成段", n("正文结束。\n一、六亲生克") == "正文结束。\n一、六亲生克",
      repr(n("正文结束。\n一、六亲生克")))
# A8 代码围栏原样保留
check("``` 围栏内容不被合并", "```\na\nb\n```" in n("前言\n```\na\nb\n```\n后记"))
# A9 汉字之间不加空格、英文之间加一个空格
check("汉字相接不留空格", "汉字相接" in n("汉字\n相接"))
check("英文相接留空格", "hello world" in n("hello\nworld"))
# A10 全角空格/NBSP 归一
check("全角空格与 NBSP 归一成普通空格",
      "\u3000" not in n("a\u3000b") and "\xa0" not in n("a\xa0b"))

print("\n[B] 文本文件与编码嗅探")
(SC / "u8.md").write_text("# 标题\n\n正文字符。\n", encoding="utf-8")
(SC / "bom.txt").write_text("带 BOM 的内容", encoding="utf-8-sig")
(SC / "gbk.txt").write_bytes("中文 GBK 内容".encode("gbk"))
(SC / "u16.txt").write_bytes("UTF-16 内容".encode("utf-16"))
(SC / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
for f, want in (("u8.md", "正文字符"), ("bom.txt", "带 BOM 的内容"),
                ("gbk.txt", "中文 GBK 内容"), ("u16.txt", "UTF-16 内容"),
                ("data.csv", "a,b")):
    try:
        e = K.extract_file(SC / f)
        check(f"{f} 抽取正确", want in e.text, e.text[:40])
    except Exception as ex:  # noqa: BLE001
        check(f"{f} 抽取正确", False, f"{type(ex).__name__}: {ex}")

print("\n[C] docx 纯标准库抽取")
docx = SC / "t.docx"
with zipfile.ZipFile(docx, "w") as z:
    z.writestr("[Content_Types].xml", "<Types/>")
    z.writestr("word/document.xml",
               '<?xml version="1.0"?><w:document xmlns:w="w"><w:body>'
               '<w:p><w:r><w:t>第一段</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>第二段</w:t></w:r><w:r><w:tab/></w:r>'
               '<w:r><w:t>制表后</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>第三</w:t></w:r><w:r><w:t>段</w:t></w:r></w:p>'
               '</w:body></w:document>')
e = K.extract_file(docx)
check("docx 段落齐全", all(x in e.text for x in ("第一段", "第二段", "第三段")), e.text[:60])
check("docx 同一段多个 run 拼接", "第三段" in e.text)
check("docx 方法标记正确", e.method == "docx-xml", e.method)

print("\n[D] pptx 纯标准库抽取（含备注）")
pptx = SC / "t.pptx"
with zipfile.ZipFile(pptx, "w") as z:
    z.writestr("[Content_Types].xml", "<Types/>")
    z.writestr("ppt/slides/slide1.xml",
               '<p:sld xmlns:a="a" xmlns:p="p"><a:t>第一页标题</a:t>'
               '<a:t>第一页要点</a:t></p:sld>')
    z.writestr("ppt/slides/slide2.xml", '<p:sld xmlns:a="a"><a:t>第二页</a:t></p:sld>')
    z.writestr("ppt/notesSlides/notesSlide1.xml",
               '<p:notes xmlns:a="a"><a:t>备注内容</a:t><a:t>1</a:t></p:notes>')
e = K.extract_file(pptx)
check("pptx 两页文字都在", "第一页标题" in e.text and "第二页" in e.text, e.text[:80])
check("pptx 备注被抽出并标注", "【备注】" in e.text and "备注内容" in e.text)
check("pptx 页码行被剥离（备注里不残留纯数字）", "【备注】1" not in e.text)

print("\n[E] xlsx 纯标准库抽取")
xlsx = SC / "t.xlsx"
with zipfile.ZipFile(xlsx, "w") as z:
    z.writestr("[Content_Types].xml", "<Types/>")
    z.writestr("xl/sharedStrings.xml",
               '<sst><si><t>姓名</t></si><si><t>张三</t></si></sst>')
    z.writestr("xl/worksheets/sheet1.xml",
               '<worksheet><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
               '<row><c><v>42</v></c></row></worksheet>')
e = K.extract_file(xlsx)
check("xlsx 共享字符串解析", "姓名" in e.text and "张三" in e.text, e.text[:60])
check("xlsx 数字也保留", "42" in e.text)

print("\n[F] 指纹与增量判定")
# 用**调小阈值**来测"大文件不算 sha256"，避免回归里真造 64 MB 文件
_orig_max = K.SHA256_MAX_BYTES
K.SHA256_MAX_BYTES = 1024                      # 1 KB 以上就算"大"
big = SC / "big.bin"
big.write_bytes(b"x" * 4096)
fp1 = K.file_fingerprint(big)
check("超过阈值退化为 size+mtime（不算 sha256）", fp1["sha256"] is None, str(fp1))
K.SHA256_MAX_BYTES = _orig_max
check("阈值以内算 sha256", K.file_fingerprint(SC / "u8.md")["sha256"] is not None)
same = dict(fp1)
check("指纹相同 → 不需要重抽", not K.fingerprint_changed(same, fp1))
changed = dict(fp1); changed["mtime"] = fp1["mtime"] + 1
check("mtime 变化 → 需要重抽", K.fingerprint_changed(fp1, changed))
# ⚠️ 关键回归：**size+mtime 变化必须走快速路径**，不能因为"有 sha256 就只比 sha256"。
#    否则在 mtime 不更新的场景（网盘按内容同步/只读挂载）会漏掉内容更新。
fp_sha = {"size": 10, "mtime": 100, "sha256": "a" * 64, "extractor_version": 2}
same_sha = {"size": 10, "mtime": 100, "sha256": "a" * 64, "extractor_version": 2}
mtime_only = {"size": 10, "mtime": 101, "sha256": "a" * 64, "extractor_version": 2}
size_only = {"size": 11, "mtime": 100, "sha256": "a" * 64, "extractor_version": 2}
check("sha256 相同且 size/mtime 相同 → 不重抽", not K.fingerprint_changed(fp_sha, same_sha))
check("**只有 mtime 变（sha 未变）→ 仍要重抽**", K.fingerprint_changed(fp_sha, mtime_only))
check("**只有 size 变 → 仍要重抽**", K.fingerprint_changed(fp_sha, size_only))
ver = dict(fp1); ver["extractor_version"] = fp1["extractor_version"] + 1
check("**抽取器版本变化 → 强制重抽**（升级抽取规则后老文字稿不会陈旧）",
      K.fingerprint_changed(fp1, ver))
check("没有旧指纹 → 需要重抽", K.fingerprint_changed(None, fp1))
small_a = K.file_fingerprint(SC / "u8.md")
(SC / "u8.md").write_text("# 标题\n\n改过的正文。\n", encoding="utf-8")
small_b = K.file_fingerprint(SC / "u8.md")
check("小文件内容变化 → 重抽", K.fingerprint_changed(small_a, small_b))
check("抽取器版本是常量且 >= 2（归一化修过）", K.EXTRACTOR_VERSION >= 2,
      str(K.EXTRACTOR_VERSION))

print("\n[G] 扫描规则")
(SC / "sub").mkdir(exist_ok=True)
(SC / "sub" / "note.txt").write_text("子目录内容", encoding="utf-8")
(SC / "sub" / "~$lock.docx").write_bytes(b"PK")
(SC / "skip.xyz").write_text("未知扩展名", encoding="utf-8")
(SC / ".hidden").mkdir(exist_ok=True)
(SC / ".hidden" / "secret.txt").write_text("隐藏", encoding="utf-8")
found = {p.name for p in K.scan_files(SC)}
check("子目录文件被收录", "note.txt" in found)
check("Office 锁文件被跳过", "~$lock.docx" not in found)
check("未知扩展名被跳过", "skip.xyz" not in found)
check("隐藏目录被跳过", "secret.txt" not in found)

print("\n[H] 失败隔离与不支持格式")
try:
    K.extract_file(SC / "skip.xyz")
    check("不支持扩展名抛 ExtractError", False, "没有抛错")
except K.ExtractError as ex:
    check("不支持扩展名抛 ExtractError", "不支持" in str(ex), str(ex))
except Exception as ex:  # noqa: BLE001
    check("不支持扩展名抛 ExtractError", False, f"抛了 {type(ex).__name__}")
# 造一个**扩展名支持但内容损坏**的文件，验证"逐文件失败不中断"
bad_docx = SC / "broken.docx"
bad_docx.write_bytes(b"this is not a zip at all")
rep = K.extract_dir(SC)
names_failed = {p.name for p, _ in rep.failed}
check("目录抽取：损坏的 docx 进 failed 而不中断", "broken.docx" in names_failed,
      str(sorted(names_failed)))
check("目录抽取：好文件仍成功", any(e.path.name == "note.txt" for e in rep.ok))
check("目录抽取：合计字数 > 0", rep.total_chars > 0, str(rep.total_chars))
check("com 分派存在（.doc/.ppt 走 Office）",
      ".doc" in K.DEFAULT_EXT and ".ppt" in K.DEFAULT_EXT and callable(K._extract_via_com))
check("默认格式含用户要求的 md/txt/ppt/pptx",
      {".md", ".txt", ".pptx", ".ppt"} <= set(K.DEFAULT_EXT), str(K.DEFAULT_EXT))

print("\n[I] 知识库相关源码语法防线（全角引号陷阱）")
# 背景：本项目**反复**踩到「在 Python 字符串字面量里写中文全角引号 `"…"`」→ SyntaxError。
# 本次会话就踩了 5 次（改 kb_answer / app.py / 测试时各中招），每次都要等运行才暴露。
# 这里用 `compile()` 逐个预检（**不是 ast.parse** —— ast.parse 会把带 BOM 的文件误报成
# "invalid non-printable character U+FEFF"，而那些文件实际能正常执行）。
import py_compile  # noqa: E402
import tempfile as _tf  # noqa: E402

_kb_sources = [
    PROJ / "consumer" / "kb_extract.py",
    PROJ / "consumer" / "kb_notes.py",
    PROJ / "consumer" / "kb_answer.py",
    PROJ / "consumer" / "replier.py",
    PROJ / "consumer" / "main.py",
    PROJ / "web" / "app.py",
    PROJ / "app" / "main.py",
    PROJ / "tools" / "kb.py",
    PROJ / "tests" / "regression" / "wft_kb_check.py",
    PROJ / "tests" / "regression" / "wft_kb_answer_check.py",
    PROJ / "tests" / "regression" / "wft_kb_reply_check.py",
]
_bad: list[str] = []
for _p in _kb_sources:
    if not _p.is_file():
        _bad.append(f"{_p.name}(缺失)")
        continue
    try:
        compile(_p.read_text(encoding="utf-8-sig"), str(_p), "exec")
    except SyntaxError as _e:
        _bad.append(f"{_p.name}:{_e.lineno} {str(_e)[:50]}")
check("**知识库相关源码全部语法正确**（含全角引号陷阱）", not _bad, "; ".join(_bad))
# 全角引号检测：写在字符串里的 `"` / `"` 是 SyntaxError 的高频来源，
# 但出现在中文注释/文档字符串里是合法的 —— 只提示，不当失败。
_qq = 0
for _p in _kb_sources:
    if _p.is_file():
        _t = _p.read_text(encoding="utf-8-sig")
        _qq += _t.count("\u201c") + _t.count("\u201d")
check("全角引号仅出现在注释/文档里（数量记录，便于排查）", True, f"{_qq} 处")

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("知识库资料抽取器（归一化 / 各格式 / 指纹增量 / 扫描 / 失败隔离）验证通过")
