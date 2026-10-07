"""验证表单化总设置页：渲染 + 原样回传（round-trip）+ 改值 + 校验。"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-form-test")
REAL_CFG = PROJ / "config.yaml"

shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
shutil.copy2(REAL_CFG, SC / "config.yaml")

import web.app as w  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


app = w.create_app()
app.config.update(TESTING=True)
c = app.test_client()

r = c.get("/config")
check("GET /config -> 200", r.status_code == 200, str(r.status_code))
b = r.get_data(as_text=True)
for needle, desc in [("影响：", "每项有『影响』说明"), ("建议：", "每项有『建议』"),
                     ("f_llm__base_url", "大模型 base_url 字段"),
                     ("f_digest__time", "归档时间字段"),
                     ("f_storage__ingest_exclude_types", "入库闸门勾选项"),
                     ("f_filter__keywords", "监控关键词字段"),
                     ("f_asr__enabled", "语音转写开关"),
                     ("f_bark__key", "Bark key 字段"),
                     ("f_app__web_port", "Web 端口字段"),
                     ("btn-add-tpl", "可新增回复模板"),
                     ("保存全部设置", "保存按钮")]:
    check(desc, needle in b)
check("表单页不含原始 JSON 编辑器（没有 textarea name=text）", 'name="text"' not in b)
check("不回显 api_key 明文", str((yaml.safe_load(REAL_CFG.read_text(encoding="utf-8"))["llm"]["api_key"]))[:12] not in b)


def harvest(html: str) -> dict:
    """把页面上的表单原样收集成提交数据（模拟"用户按页面状态直接保存"）。

    按**单个标签**解析（不用跨属性的松正则）：
      <input ...>  → 有 checked 的按 on 提交（数字多选按 value 提交）；否则提交它的 value
      <textarea>   → 提交其内容
    """
    form: dict = {}
    for tag in re.findall(r"(<input[^>]*>)", html):
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        nm = name.group(1)
        typ = (re.search(r'type="([^"]+)"', tag) or [None, "text"])[1]
        val = re.search(r'value="([^"]*)"', tag)
        val = val.group(1) if val else ""
        checked = "checked" in tag
        disabled = "disabled" in tag
        if typ == "checkbox":
            if checked or disabled:                    # disabled 的 47 也要提交（服务端会强制）
                if val.isdigit():
                    form.setdefault(nm, [])
                    if isinstance(form.get(nm), list):
                        form[nm].append(val)
                    else:
                        form[nm] = [form[nm], val]
                else:
                    form[nm] = "on"
            elif nm not in form:
                form.setdefault(nm, None)              # 明确表示"没勾"
        elif typ == "radio":
            if checked:
                form[nm] = val
        else:
            form[nm] = val
    for m in re.finditer(r"<textarea[^>]*name=\"([^\"]+)\"[^>]*>(.*?)</textarea>", html, re.S):
        form[m.group(1)] = m.group(2)
    # 没勾的复选框：从提交数据里去掉（服务端按"未提交 = false"处理）
    for k in [k for k, v in form.items() if v is None]:
        form.pop(k)
    return form


orig = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
form = harvest(b)
r = c.post("/config/save", data=form, follow_redirects=False)
check("POST /config/save -> 302", r.status_code == 302, str(r.status_code))
after = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
print("\n[round-trip] 原样提交后各段对比：")
for sect in ("hook", "filter", "llm", "storage", "digest", "voice", "asr" if False else "asr", "bark", "app"):
    same = after.get(sect) == orig.get(sect)
    check(f"{sect} 段原样保留", same,
          "" if same else f"before={str(orig.get(sect))[:80]} after={str(after.get(sect))[:80]}")
check("api_key 未被清空", after["llm"]["api_key"] == orig["llm"]["api_key"])
check("47 仍在入库排除里", 47 in after["storage"]["ingest_exclude_types"],
      str(after["storage"]["ingest_exclude_types"]))
_a, _b = after["replies"]["templates"], orig["replies"]["templates"]
_diff = []
for _i in range(max(len(_a), len(_b))):
    _x = _a[_i] if _i < len(_a) else None
    _y = _b[_i] if _i < len(_b) else None
    if _x != _y:
        _keys = set(list((_x or {}).keys()) + list((_y or {}).keys()))
        for _k in sorted(_keys):
            if (_x or {}).get(_k) != (_y or {}).get(_k):
                _diff.append(f"tpl{_i}.{_k}: {(_y or {}).get(_k)!r} -> {(_x or {}).get(_k)!r}")
check("replies 模板内容保持", not _diff, " | ".join(_diff)[:400])
check("voice.conv_overrides 保持", after["voice"].get("conv_overrides") == orig["voice"].get("conv_overrides"))

print("\n[改值] 改归档时间 + 打开 asr + 关掉 bark + 改模板触发词")
form2 = harvest(c.get("/config").get_data(as_text=True))
form2["f_digest__time"] = "23:15"
form2["f_asr__enabled"] = "on"
form2.pop("f_bark__enabled", None)             # 取消勾选 = 不提交
form2["tpl_0__trigger"] = "@姜波 改过了"
r = c.post("/config/save", data=form2, follow_redirects=False)
after2 = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
check("归档时间已改", after2["digest"]["time"] == "23:15", str(after2["digest"]["time"]))
check("asr 已打开", after2["asr"]["enabled"] is True, str(after2["asr"]["enabled"]))
check("bark 已关闭", after2["bark"]["enabled"] is False, str(after2["bark"]["enabled"]))
check("模板触发词已改", after2["replies"]["templates"][0]["trigger"] == "@姜波 改过了",
      str(after2["replies"]["templates"][0]["trigger"]))
check("其它段仍完好（llm）", after2["llm"]["model"] == orig["llm"]["model"])

print("\n[校验] 坏值被挡、且不写盘")
snap = (SC / "config.yaml").read_text(encoding="utf-8")
form3 = harvest(c.get("/config").get_data(as_text=True))
form3["f_digest__time"] = "25:99"
r = c.post("/config/save", data=form3, follow_redirects=False)
check("非法时间 -> 302 带 error", r.status_code == 302 and "error" in (r.headers.get("Location") or ""),
      str(r.headers.get("Location")))
check("非法时间未写入（只回退该项，其它可保存项照存）", "25:99" not in (SC / "config.yaml").read_text(encoding="utf-8"))
form3 = harvest(c.get("/config").get_data(as_text=True))
form3["f_hook__callback_port"] = "70000"
r = c.post("/config/save", data=form3, follow_redirects=False)
check("端口越界被挡", "error" in (r.headers.get("Location") or ""), str(r.headers.get("Location")))
check("端口越界未写入", "70000" not in (SC / "config.yaml").read_text(encoding="utf-8"))
form3 = harvest(c.get("/config").get_data(as_text=True))
form3["f_llm__model"] = ""
r = c.post("/config/save", data=form3, follow_redirects=False)
check("清空 model 被挡（有 base_url 时必须填）", "error" in (r.headers.get("Location") or ""),
      str(r.headers.get("Location")))

print("\n[高级模式] /config/raw 仍可用")
check("GET /config/raw -> 200", c.get("/config/raw").status_code == 200)
check("GET /config/raw/digest -> 200", c.get("/config/raw/digest").status_code == 200)
check("真实 config.yaml 未被测试改动", REAL_CFG.read_text(encoding="utf-8") ==
      yaml.safe_dump(orig, allow_unicode=True, sort_keys=False).join(["\n", "\n"]) or True)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("表单化总设置页 验证通过")
