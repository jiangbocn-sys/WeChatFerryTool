"""冒烟：/filter 现在是「监控规则（归档范围）」页，/config 只留链接。

R-002（2026-10-07）后：本页**只维护归档范围**，不再有"入库关键词"表单，
也不再接受 keywords（关键词已不参与入库判定）。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import yaml

SC = Path(r"D:\projects\.wft-smoke2")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, r"D:\projects\WeChatFerryTool")

(SC / "config.yaml").write_text(yaml.safe_dump({
    "filter": {"groups": ["a@chatroom"], "senders": [], "keywords": ["报价"], "case_insensitive": True},
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "llm": {"base_url": "https://x/v1", "api_key": "k", "model": "m"},
    "storage": {"sqlite_path": "data/messages.db"},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "exclude_types": [47]},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")
(SC / "data" / "labels.json").write_text(
    '{"groups": {"a@chatroom": {"name": "甲群"}}, "senders": {"wxid_x": {"name": "老张"}}}',
    encoding="utf-8")

import web.app as w  # noqa: E402

app = w.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


f = c.get("/filter")
fs = f.get_data(as_text=True)
print("[1] /filter = 监控规则页")
check("GET /filter -> 200", f.status_code == 200, str(f.status_code))
check("含监控名单区块", "监控名单" in fs)
check("含群下拉/人下拉", 'name="group"' in fs and 'name="sender"' in fs)
check("含重点人入口", "/config/focus/a@chatroom" in fs)
check("**_不再有入库关键词表单**（关键词已不参与入库）", 'name="keywords"' not in fs)
check("明确写出「本页不影响入库」", "本页不影响入库" in fs)
check("不再有 groups/senders 手填框", 'name="groups"' not in fs and 'name="senders"' not in fs)
check("显示已监控的群（甲群）", "甲群" in fs)
check("显示当前归档范围", "当前归档范围" in fs)

print("\n[2] /config 不再内联监控名单，只给链接")
cf = c.get("/config")
cs = cf.get_data(as_text=True)
check("GET /config -> 200", cf.status_code == 200, str(cf.status_code))
check("有跳转链接", "去监控规则页设置" in cs)
check("没有内联的监控表单", "config_filter_save" not in cs, "内联表单应已移除")
check("导航里监控规则仍在", "监控规则" in c.get("/").get_data(as_text=True))

print("\n[3] 提交 keywords 被忽略、监控名单不被清空（回归防护）")
r = c.post("/filter/update", data={"keywords": "报价\n合同", "case_insensitive": "on"},
           follow_redirects=False)
cfg = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
check("保存 -> 302", r.status_code == 302, str(r.status_code))
check("**keywords 未被改写**（服务端不接收）",
      cfg["filter"]["keywords"] == ["报价"], str(cfg["filter"]["keywords"]))
check("**监控群未被清空**", cfg["filter"]["groups"] == ["a@chatroom"], str(cfg["filter"]["groups"]))

print("\n[4] 监控名单增删仍可用（在 /filter 页上）")
r = c.post("/config/filter/save", data={"action": "add_group", "group": "b@chatroom"},
           follow_redirects=False)
cfg = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
check("添加群 -> 写入 filter.groups", "b@chatroom" in cfg["filter"]["groups"], str(cfg["filter"]["groups"]))
check("重定向到监控规则页", "/filter" in (r.headers.get("Location") or ""), str(r.headers.get("Location")))
r = c.post("/config/filter/save", data={"action": "add_sender", "sender": "wxid_x"})
cfg = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
check("添加人 -> 写入 filter.senders", cfg["filter"]["senders"] == ["wxid_x"], str(cfg["filter"]["senders"]))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 55)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("监控规则页整合 验证通过")
