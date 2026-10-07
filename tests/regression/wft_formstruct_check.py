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
check("渲染出的 form 数量 = 监控区 3 + 归档范围表单 1 = 4", len(forms) == 4, str(len(forms)))
# 说明：/filter 页原来的「入库关键词」表单在 R-002（2026-10-07）被移除 ——
# 关键词不再参与入库判定，页面改成"只维护归档范围 + 指向标定页的链接"，
# 免得出现"看着能改、其实对入库没用"的误导。
check("没有 name=text（说明没渲染出旧 JSON 编辑器）", 'name="text"' not in html)

# 关键：本页现在**只有监控区的 form**（入库关键词表单已在 R-002 移除），
# 所以这里改成断言"每个 form 都指向 config_filter_save"，以及"页面不含 /filter/update 表单"。
check("每个 form 都指向 config/filter/save（监控区）",
      all("/config/filter/save" in f for f in forms), str([f[:60] for f in forms]))
check("**页面上不再有 /filter/update 表单**（关键词表单已移除）",
      'action="/filter/update"' not in html)

# DOM 顺序：监控区块之后还有指向标定页的"每群敏感关键词"入口
# 注意不能直接找 "/labels"：左侧导航里就有这个链接（位置更靠前），会误判。
i_mon = html.find("监控名单")
i_labels_card = html.find("归档关键词")
check("监控名单区块之后有「归档关键词 → 标定页」的说明卡",
      0 <= i_mon < i_labels_card, f"监控名单@{i_mon} < 归档关键词卡@{i_labels_card}")
check("该卡给出标定页入口", 'href="/labels"' in html)

# 不能出现嵌套：任一 form 内部不得再出现 <form
nested = 0
for m in re.finditer(r"<form[^>]*>", html):
    tail = html[m.end():]
    close = tail.find("</form>")
    nested += tail[:close].count("<form") if close >= 0 else 0
check("没有任何 form 嵌套", nested == 0, f"嵌套 form 数={nested}")

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
