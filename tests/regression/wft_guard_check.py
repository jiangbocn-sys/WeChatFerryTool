"""验证：修复后的配置页不会再因"误提交"清空配置，且真实配置已恢复完整。

checks:
 1. /config/save 收到缺少总设置字段的提交（模拟 form 嵌套/其它按钮误提交）→ 拒绝且**不写盘**
 2. 正常表单提交仍然工作
 3. 真实 config.yaml 的 llm 段完整（base_url/model/api_key）
 4. /config 页面渲染出的 form 结构：主表单只有一个，监控区 form 在它之外
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
SC = Path(r"D:\projects\.wft-guard-test")
REAL_CFG = PROJ / "config.yaml"

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


CFG = {
    "hook": {"api_base": "http://127.0.0.1:30001", "callback_host": "127.0.0.1",
             "callback_port": 8888, "api_timeout_s": 5.0, "self_wxid": "me"},
    "filter": {"groups": [], "senders": [], "keywords": [], "case_insensitive": True},
    "llm": {"base_url": "https://api.minimax.cn/v1", "api_key": "sk-real-key-123456",
            "model": "MiniMax-M3", "push_threshold": 4},
    "storage": {"sqlite_path": "data/messages.db", "ingest_exclude_types": [42, 47, 51]},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "summarize": True,
               "exclude_types": [47, 51, 10000, 10002]},
    "voice": {"enabled": True, "dir": "data/voices", "conv_overrides": {"abc": "叶子"}},
    "asr": {"enabled": True, "url": "http://mac:8170/v1/audio/transcriptions", "timeout_s": 120},
    "replies": {"enabled": True, "history_count": 10, "templates": [], "rate_limit": {}},
    "bark": {"enabled": False, "server": "https://api.day.app", "key": "REPLACE_ME"},
    "app": {"web_host": "127.0.0.1", "web_port": 6060, "launch_wechat": True},
}
(SC / "config.yaml").write_text(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False), encoding="utf-8")
(SC / "data" / "labels.json").write_text('{"groups": {}, "senders": {}}', encoding="utf-8")

import web.app as webapp  # noqa: E402

app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()
before = (SC / "config.yaml").read_text(encoding="utf-8")

print("[1] 误提交（缺总设置字段）必须被拒绝且不写盘")
r = c.post("/config/save", data={"action": "add_group", "group": "195940014@chatroom"},
           follow_redirects=False)
check("误提交 -> 302 带 error", r.status_code == 302 and "error" in (r.headers.get("Location") or ""),
      str(r.headers.get("Location"))[:100])
check("误提交后配置**未被清空**（字节完全一致）",
      (SC / "config.yaml").read_text(encoding="utf-8") == before)
after = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
check("llm 段仍在", after["llm"]["base_url"] == "https://api.minimax.cn/v1"
      and after["llm"]["model"] == "MiniMax-M3", str(after["llm"].get("model")))
check("digest 段仍在", after["digest"]["time"] == "23:30" and after["digest"]["summarize"] is True)

print("\n[2] 空提交（完全没字段）也被拒绝")
r = c.post("/config/save", data={})
check("空提交 -> 302 带 error", "error" in (r.headers.get("Location") or ""))
check("空提交后配置未被清空", (SC / "config.yaml").read_text(encoding="utf-8") == before)

print("\n[3] 正常表单提交仍然工作")
html = c.get("/config").get_data(as_text=True)
form = {}
for m in re.finditer(r'name="(f_[^"]+)"[^>]*value="([^"]*)"', html):
    form[m.group(1)] = m.group(2)
for m in re.finditer(r'<textarea name="(f_[^"]+)"[^>]*>(.*?)</textarea>', html, re.S):
    form[m.group(1)] = m.group(2)
for m in re.finditer(r'name="(f_[^"]+)"\s+value="(\d+)"[^>]*checked', html):
    form.setdefault(m.group(1), [])
    if isinstance(form[m.group(1)], list):
        form[m.group(1)].append(m.group(2))
for m in re.finditer(r'name="(f_[^"]+)"[^>]*checked(?!\s+disabled)', html):
    form[m.group(1)] = "on"
form["f_llm__base_url"] = "https://api.minimax.cn/v1"
form["f_llm__model"] = "MiniMax-M3"
form["f_digest__time"] = "22:15"
r = c.post("/config/save", data=form, follow_redirects=False)
saved = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
check("正常提交 -> 302 且带 Location", r.status_code == 302 and bool(r.headers.get("Location")),
      str(r.headers.get("Location"))[:80])
check("正常提交真的生效（归档时间改到 22:15）", saved["digest"]["time"] == "22:15",
      str(saved["digest"]["time"]))
check("正常提交保留 api_key", saved["llm"]["api_key"] == "sk-real-key-123456")
check("正常提交保留 voice.conv_overrides", saved["voice"]["conv_overrides"] == {"abc": "叶子"})

print("\n[4] 真实 config.yaml 已恢复完整")
real = yaml.safe_load(REAL_CFG.read_text(encoding="utf-8"))
check("llm.base_url 已恢复", bool(real["llm"]["base_url"]), str(real["llm"].get("base_url")))
check("llm.model 已恢复", bool(real["llm"]["model"]), str(real["llm"].get("model")))
check("llm.api_key 在", bool(real["llm"]["api_key"]))
check("hook.api_base 已恢复", real["hook"]["api_base"] == "http://127.0.0.1:30001")
check("storage.sqlite_path 已恢复", real["storage"]["sqlite_path"] == "data/messages.db")
check("digest 已恢复（23:30 + 自动总结）",
      real["digest"]["time"] == "23:30" and real["digest"]["summarize"] is True, str(real["digest"]))
check("voice/asr 已恢复", real["voice"]["enabled"] is True and real["asr"]["enabled"] is True)
check("replies 段结构完好（模板由用户在页面上增删，数量不做断言）",
      isinstance(real["replies"].get("templates"), list) and bool(real["replies"]["enabled"]) is not None,
      str([t.get("name") for t in real["replies"]["templates"]]))

print("\n[5] 页面表单结构（主表单唯一，监控区在其之外）")
forms = re.findall(r"<form[^>]*>", html)
i_main = next(i for i, f in enumerate(forms) if 'action="/config/save"' in f)
check("主表单是最后一个 form", i_main == len(forms) - 1, f"index={i_main} of {len(forms)}")
check("监控区 form 都指向 /config/filter/save",
      all("/config/filter/save" in f for f in forms[:i_main]))
tail = html[html.find('action="/config/save"'):]
check("主表单内部无嵌套 form", tail[:tail.find("</form>")].count("<form") == 0)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 58)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("配置保护 + 表单结构 + 真实配置恢复 验证通过")
