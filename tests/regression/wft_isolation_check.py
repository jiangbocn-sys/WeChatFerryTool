"""隔离性测试：任一保存操作都不得影响其它部分（上次的教训）。

对三个保存入口，分别做"快照 → 只改一处 → 保存 → 逐字段比对"：
  A. /config/save          —— 改一项，其余配置段与 labels 必须逐字段一致
  B. /config/save（截断）  —— 少数字段的提交必须整份拒绝、不写盘
  C. /filter/update        —— 只改关键词，监控名单/标定不许动
  D. /config/filter/save   —— 加监控群/人、设重点人，只动该动的
另外校验：写配置与写标定**都会留备份**。
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-isolation")
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


# 一份"内容很丰富"的配置：任何一处被误清都能看出来
CFG = {
    "hook": {"api_base": "http://127.0.0.1:30001", "callback_host": "127.0.0.1",
             "callback_port": 8888, "api_timeout_s": 5.0, "self_wxid": "ruibo_jiang"},
    "filter": {"groups": ["195940014@chatroom"], "senders": ["wxid_aa"], "keywords": ["报价"],
               "case_insensitive": True},
    "llm": {"base_url": "https://api.minimax.cn/v1", "api_key": "sk-real-key-abcdef",
            "model": "MiniMax-M3", "push_threshold": 4},
    "storage": {"sqlite_path": "data/messages.db",
                "ingest_exclude_types": [42, 43, 47, 48, 49, 50, 51, 10000]},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "summarize": True,
               "exclude_types": [47, 51, 10000, 10002]},
    "voice": {"enabled": True, "dir": "data/voices",
              "conv_overrides": {"62c2198d307c2d5f9ab8963288fe0c97": "叶子"}},
    "asr": {"enabled": True, "url": "http://mac:8170/v1/audio/transcriptions", "timeout_s": 120},
    "bark": {"enabled": False, "server": "https://api.day.app", "key": "REPLACE_ME"},
    "replies": {"enabled": True, "history_count": 10,
                "templates": [{"name": "t1", "trigger": "@我", "match_type": "substring",
                               "response": "人设文本\n", "use_llm": True, "test_only": False,
                               "scope": {"groups": [], "senders": ["wxid_aa"]}}],
                "rate_limit": {"per_group_cooldown_s": 15, "global_daily_limit": 100,
                               "min_delay_s": 5, "max_delay_s": 30}},
    "app": {"web_host": "127.0.0.1", "web_port": 6060, "launch_wechat": True},
}
LABELS = {
    "groups": {"195940014@chatroom": {"name": "幸福小家", "important": True,
                                      "focus_members": ["wxid_aa"], "keywords": ["出去玩"]},
               "958062774@chatroom": {"name": "中澳一家亲"}},
    "senders": {"wxid_aa": {"name": "老张", "important": True},
                "wxid_bb": {"name": "小李", "auto": True, "source": "harvest"}},
}


def reset_files() -> None:
    (SC / "config.yaml").write_text(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False),
                                    encoding="utf-8")
    (SC / "data" / "labels.json").write_text(
        json.dumps(LABELS, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    for p in list(SC.glob("config.yaml.bak-*")) + list((SC / "data").glob("labels.json.bak-*")):
        p.unlink()


def load() -> tuple[dict, dict]:
    return (yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8")),
            json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8")))


def diff_sections(before: dict, after: dict, allow: set[str]) -> list[str]:
    """返回被改动的段（排除 allow 里允许改的）。"""
    changed = []
    for k in set(before) | set(after):
        if k in allow:
            continue
        if before.get(k) != after.get(k):
            changed.append(k)
    return changed


reset_files()
import web.app as webapp  # noqa: E402

app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()


def harvest_settings_form(html: str) -> dict:
    """把设置页表单原样收集（模拟用户点"保存全部设置"，什么都没改）。"""
    form: dict = {}
    for tag in re.findall(r"(<input[^>]*>)", html):
        nm = re.search(r'name="(f_[^"]+)"', tag)
        if not nm:
            continue
        n = nm.group(1)
        val = (re.search(r'value="([^"]*)"', tag) or [None, ""])[1]
        typ = (re.search(r'type="([^"]+)"', tag) or [None, "text"])[1]
        if typ == "checkbox":
            if "checked" in tag or "disabled" in tag:
                if val.isdigit():
                    form.setdefault(n, [])
                    form[n].append(val)
                else:
                    form[n] = "on"
        else:
            form[n] = val
    for m in re.finditer(r'<textarea[^>]*name="(f_[^"]+)"[^>]*>(.*?)</textarea>', html, re.S):
        form[m.group(1)] = m.group(2)
    for m in re.finditer(r'name="(tpl_\d+__[a-z_]+)"[^>]*value="([^"]*)"', html):
        form[m.group(1)] = m.group(2)
    for m in re.finditer(r'<textarea[^>]*name="(tpl_\d+__[a-z_]+)"[^>]*>(.*?)</textarea>', html, re.S):
        form[m.group(1)] = m.group(2)
    for m in re.finditer(r'name="(tpl_\d+__[a-z_]+)"[^>]*checked', html):
        form[m.group(1)] = "on"
    return form


print("[A] /config/save：只改『推送阈值』，其余（含 labels）不许动")
b_cfg, b_labels = load()
html = c.get("/config").get_data(as_text=True)
form = harvest_settings_form(html)
check("采集到足够多的设置字段", len(form) >= 15, f"{len(form)} 个")
form["f_llm__push_threshold"] = "5"
r = c.post("/config/save", data=form, follow_redirects=False)
a_cfg, a_labels = load()
check("保存 -> 302", r.status_code == 302, str(r.status_code))
check("目标字段确实改了（push_threshold=5）", a_cfg["llm"]["push_threshold"] == 5,
      str(a_cfg["llm"]["push_threshold"]))
check("其余配置段逐字段未变", not diff_sections(b_cfg, a_cfg, {"llm"}), str(diff_sections(b_cfg, a_cfg, {"llm"})))
check("llm 段内除该字段外都未变",
      {k: v for k, v in b_cfg["llm"].items() if k != "push_threshold"} ==
      {k: v for k, v in a_cfg["llm"].items() if k != "push_threshold"})
check("labels.json 完全未变", a_labels == b_labels)

print("\n[B] 截断/误提交：整份拒绝、一个字都不写")
reset_files()
b_cfg, b_labels = load()
snap_cfg = (SC / "config.yaml").read_text(encoding="utf-8")
snap_lab = (SC / "data" / "labels.json").read_text(encoding="utf-8")
for desc, payload in [("只带一个监控动作", {"action": "add_group", "group": "x@chatroom"}),
                      ("完全空提交", {}),
                      ("只带了 3 个字段",
                       {"f_llm__base_url": "https://x/v1", "f_llm__model": "m",
                        "f_llm__push_threshold": "4"})]:
    r = c.post("/config/save", data=payload, follow_redirects=False)
    ok = r.status_code == 302 and "error" in (r.headers.get("Location") or "")
    check(f"{desc} → 拒绝", ok, str(r.headers.get("Location"))[:70])
check("配置字节完全未变", (SC / "config.yaml").read_text(encoding="utf-8") == snap_cfg)
check("标定字节完全未变", (SC / "data" / "labels.json").read_text(encoding="utf-8") == snap_lab)
check("拒绝时也不该产生备份（没写就没备份）", not list(SC.glob("config.yaml.bak-*")),
      str([p.name for p in SC.glob("config.yaml.bak-*")]))

print("\n[C] /filter/update：只改关键词，监控名单与标定不许动")
reset_files()
b_cfg, b_labels = load()
r = c.post("/filter/update", data={"keywords": "报价\n合同", "case_insensitive": "on"},
           follow_redirects=False)
a_cfg, a_labels = load()
check("保存 -> 302", r.status_code == 302, str(r.status_code))
check("关键词已更新", a_cfg["filter"]["keywords"] == ["报价", "合同"], str(a_cfg["filter"]["keywords"]))
check("**监控名单未变**", a_cfg["filter"]["groups"] == b_cfg["filter"]["groups"]
      and a_cfg["filter"]["senders"] == b_cfg["filter"]["senders"],
      f'{a_cfg["filter"]["groups"]} / {a_cfg["filter"]["senders"]}')
check("其余配置段未变", not diff_sections(b_cfg, a_cfg, {"filter"}))
check("filter 段内除 keywords 外未变",
      {k: v for k, v in b_cfg["filter"].items() if k != "keywords"} ==
      {k: v for k, v in a_cfg["filter"].items() if k != "keywords"})
check("labels.json 完全未变", a_labels == b_labels)
check("写入留了备份", bool(list(SC.glob("config.yaml.bak-*"))),
      str([p.name for p in SC.glob("config.yaml.bak-*")]))

print("\n[D] /config/filter/save：加监控群/人、设重点人，只动该动的")
reset_files()
b_cfg, b_labels = load()
c.post("/config/filter/save", data={"action": "add_group", "group": "958062774@chatroom"})
a_cfg, a_labels = load()
check("filter.groups 增加一项",
      set(a_cfg["filter"]["groups"]) - set(b_cfg["filter"]["groups"]) == {"958062774@chatroom"},
      str(a_cfg["filter"]["groups"]))
check("filter 段其它字段未变",
      {k: v for k, v in b_cfg["filter"].items() if k != "groups"} ==
      {k: v for k, v in a_cfg["filter"].items() if k != "groups"})
check("其余配置段未变", not diff_sections(b_cfg, a_cfg, {"filter"}))
check("labels：只有该群被打了 monitored 标记",
      a_labels["groups"]["958062774@chatroom"].get("monitored") is True and
      a_labels["groups"]["195940014@chatroom"] == b_labels["groups"]["195940014@chatroom"],
      json.dumps(a_labels["groups"].get("958062774@chatroom"), ensure_ascii=False))
check("labels.senders 完全未变", a_labels["senders"] == b_labels["senders"])
check("标定写入也留了备份", bool(list((SC / "data").glob("labels.json.bak-*"))),
      str([p.name for p in (SC / "data").glob("labels.json.bak-*")]))

b_labels2 = copy.deepcopy(a_labels)
c.post("/config/filter/save", data={"action": "set_focus", "group": "958062774@chatroom",
                                    "focus": ["wxid_bb"]})
_, a_labels3 = load()
check("设重点人只改该群的 focus_members",
      a_labels3["groups"]["958062774@chatroom"]["focus_members"] == ["wxid_bb"] and
      a_labels3["groups"]["195940014@chatroom"] == b_labels2["groups"]["195940014@chatroom"] and
      a_labels3["senders"] == b_labels2["senders"],
      json.dumps(a_labels3["groups"]["958062774@chatroom"], ensure_ascii=False))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("保存隔离性（改一处不动其它）验证通过")
