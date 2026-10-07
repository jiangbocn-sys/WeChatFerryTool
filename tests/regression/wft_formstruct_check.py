"""验证：/config 渲染出的表单结构合法 —— 监控名单的 form 在外层表单之外，且各自独立。"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import yaml

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-formstruct")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "filter": {"groups": ["958062774@chatroom"], "senders": ["wxid_bbb222"]},
    "storage": {"sqlite_path": "data/messages.db"},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "exclude_types": [47]},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")
(SC / "data" / "labels.json").write_text(
    '{"groups": {"958062774@chatroom": {"name": "中澳一家亲"},'
    ' "195940014@chatroom": {"name": "幸福小家"}},'
    ' "senders": {"wxid_bbb222": {"name": "小李"}, "wxid_aaa111": {"name": "老张"}}}',
    encoding="utf-8")

import web.app as webapp  # noqa: E402

app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()
html = c.get("/filter").get_data(as_text=True)

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


print("[1] 渲染出的表单结构")
forms = re.findall(r"<form[^>]*>", html)
print("   form 数:", len(forms))
for f in forms:
    print("     ", f[:110])
check("渲染出的 form 数量 = 监控区 4 + 主表单 1 = 5", len(forms) == 5, str(len(forms)))
check("没有 name=text（说明没渲染出旧 JSON 编辑器）", 'name="text"' not in html)

# 关键：每个 form 的 action，以及"主表单"之后不应再出现其它 form 的开始标签
i_main = next(i for i, f in enumerate(forms) if 'action="/filter/update"' in f)
check("表单页的 form 都在监控区之后（监控区 form 在前）", i_main == len(forms) - 1, f"index={i_main}")
check("监控区的 form 指向 config_filter_save",
      all("/config/filter/save" in f for f in forms[:i_main]), str([f[:60] for f in forms[:i_main]]))

# DOM 顺序：include 的监控区块出现在主表单之前
i_mon = html.find("监控名单")
i_form = html.find('action="/filter/update"')
check("监控名单区块在主表单之前（DOM 顺序）", 0 <= i_mon < i_form, f"{i_mon} < {i_form}")

# 不能出现嵌套：主表单之后的 HTML 里不应再有 <form ...> 直到 </form>
tail = html[i_form:]
close = tail.find("</form>")
inner = tail[:close].count("<form")
check("主表单内部没有嵌套 form", inner == 0, f"内部 form 数={inner}")

print("\n[2] 监控区功能仍然可用")
# 只取"添加群"那个 form 里的下拉框内容来检查
m = re.search(r'value="add_group".*?</form>', html, re.S)
grp_form = m.group(0) if m else ""
check("监控的群下拉框里没有已监控的群（避免重复添加）",
      "958062774@chatroom" not in grp_form, grp_form[:120])
m2 = re.search(r'value="add_sender".*?</form>', html, re.S)
snd_form = m2.group(0) if m2 else ""
check("监控的人下拉框里没有已监控的人", "wxid_bbb222" not in snd_form, snd_form[:120])
check("已监控的群显示为整群归档", "整群归档" in html)
check("已监控的人以标签展示", "小李" in html)
check("有选择重点人入口", "/config/focus/958062774@chatroom" in html)
check("添加按钮带防呆 JS", "monitor-select" in html)
r = c.post("/config/filter/save", data={"action": "add_group", "group": "195940014@chatroom"},
           follow_redirects=False)
check("添加群 -> 302 且写入 filter.groups",
      r.status_code == 302 and "195940014@chatroom" in
      (yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))["filter"]["groups"]),
      str(yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))["filter"]["groups"]))
html2 = c.get("/filter").get_data(as_text=True)
check("添加后页面上能看到该群",
      "幸福小家" in html2.split("监控的群")[1].split("监控的人")[0])

shutil.rmtree(SC, ignore_errors=True)
print("=" * 55)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("总设置页表单结构 + 监控名单 验证通过")
