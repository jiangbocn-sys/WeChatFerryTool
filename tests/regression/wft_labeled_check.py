"""验证：下拉菜单只列**已标记**的群/人；未标记的不出现。

已标记的定义：
  群 → 有名字 / ★重点 / 已设重点关注人 / 已配敏感关键词
  人 → 有名字 / ★重点
另外：已在监控名单里的项即使没标记也要能显示（否则移除入口会消失）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-labeled-test")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "filter": {"groups": [], "senders": []},
    "storage": {"sqlite_path": "data/messages.db"},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "exclude_types": [47]},
    "llm": {"base_url": "https://x/v1", "api_key": "k", "model": "m"},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")

# labels：混合"已标记"和"未标记"
(SC / "data" / "labels.json").write_text(json.dumps({
    "groups": {
        "g_named@chatroom": {"name": "有名字的群"},                       # 有名字 → 候选
        "g_important@chatroom": {"name": "", "important": True},           # ★重点 → 候选
        "g_focus@chatroom": {"name": "", "focus_members": ["wxid_x"]},     # 有重点人 → 候选
        "g_kw@chatroom": {"name": "", "keywords": ["报价"]},               # 有关键词 → 候选
        "g_bare@chatroom": {"name": "", "important": False},               # 什么都没 → 不候选
        "g_empty@chatroom": {},                                            # 空条目 → 不候选
    },
    "senders": {
        "wxid_named": {"name": "老张"},                                    # 有名字 → 候选
        "wxid_imp": {"name": "", "important": True},                       # ★重点 → 候选
        "wxid_bare": {"name": "", "important": False},                     # 什么都没 → 不候选
        "wxid_empty": {},                                                  # 空条目 → 不候选
        "wxid_auto": {"name": "挖出来的名字", "auto": True},                # 挖掘来的（有名字）→ 候选
    },
}, ensure_ascii=False, indent=2), encoding="utf-8")

import web.app as webapp  # noqa: E402

app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()
html = c.get("/filter").get_data(as_text=True)


def dropdown(kind: str) -> str:
    """取出指定下拉框（add_group / add_sender）的 HTML 片段。"""
    m = re.search(r'value="' + kind + r'".*?</form>', html, re.S)
    return m.group(0) if m else ""


gsel, ssel = dropdown("add_group"), dropdown("add_sender")

print("[1] 群下拉：只列已标记的群")
for gid, why, want in [
    ("g_named@chatroom", "有名字", True),
    ("g_important@chatroom", "★重点", True),
    ("g_focus@chatroom", "有重点关注人", True),
    ("g_kw@chatroom", "有敏感关键词", True),
    ("g_bare@chatroom", "什么都没标记", False),
    ("g_empty@chatroom", "空条目", False),
]:
    check(f"{why} 的群 {'出现' if want else '不出现'}", (gid in gsel) is want, gid)

print("\n[2] 人下拉：只列已标记的人")
for sid, why, want in [
    ("wxid_named", "有名字", True),
    ("wxid_imp", "★重点", True),
    ("wxid_auto", "挖掘来的名字", True),
    ("wxid_bare", "什么都没标记", False),
    ("wxid_empty", "空条目", False),
]:
    check(f"{why} 的人 {'出现' if want else '不出现'}", (sid in ssel) is want, sid)

print("\n[3] 已在监控名单里的项必须仍可见（否则移除入口消失）")
c.post("/config/filter/save", data={"action": "add_group", "group": "g_bare@chatroom"})
c.post("/config/filter/save", data={"action": "add_sender", "sender": "wxid_bare"})
html2 = c.get("/filter").get_data(as_text=True)
check("未标记但已监控的群出现在监控列表里", "g_bare@chatroom" in html2)
check("未标记但已监控的人出现在监控列表里", "wxid_bare" in html2)
gsel2, ssel2 = dropdown("add_group"), dropdown("add_sender")
check("它不会重复出现在下拉里（避免重复添加）", "g_bare@chatroom" not in gsel2)
check("人同理", "wxid_bare" not in ssel2)

print("\n[4] 重点关注人页也只列已标记的人")
f = c.get("/config/focus/g_named@chatroom").get_data(as_text=True)
check("已标记的人出现（老张）", "wxid_named" in f)
check("已标记的人出现（★重点 wxid_imp）", "wxid_imp" in f)
check("未标记的人不出现（wxid_bare）", "wxid_bare" not in f)
check("空条目不出现（wxid_empty）", "wxid_empty" not in f)

print("\n[5] 页面还提示了怎么补标记")
check("提示去标定页/挖昵称", ("标定页" in f) or ("挖昵称" in f), "")

shutil.rmtree(SC, ignore_errors=True)
print("=" * 58)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("下拉只列已标记项 验证通过")
