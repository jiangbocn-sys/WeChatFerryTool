"""验证总设置页：/config 总览 + /config/<段> 编辑，所有段都能改、写前校验、只改目标段。"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import yaml

PROJ = Path(r"D:\projects\WeChatFerryTool")
SCRATCH = Path(r"D:\projects\.wft-config-page")
REAL_CFG = PROJ / "config.yaml"

shutil.rmtree(SCRATCH, ignore_errors=True)
(SCRATCH / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SCRATCH)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


BASE = {
    "hook": {"api_base": "http://127.0.0.1:30001", "callback_host": "127.0.0.1",
             "callback_port": 8888, "self_wxid": "ruibo_jiang"},
    "filter": {"groups": [], "senders": [], "keywords": ["报价"], "case_insensitive": True},
    "llm": {"base_url": "https://api.minimax.cn/v1", "api_key": "sk-secret-key-987654",
            "model": "MiniMax-M3", "push_threshold": 4},
    "bark": {"enabled": False, "server": "https://api.day.app", "key": "REPLACE_ME"},
    "storage": {"sqlite_path": "data/messages.db", "ingest_exclude_types": [47]},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "summarize": True,
               "exclude_types": [47, 51, 10000, 10002]},
    "replies": {"enabled": True, "history_count": 10,
                "templates": [{"name": "keep", "trigger": "@我", "response": "x"}],
                "rate_limit": {"per_group_cooldown_s": 15}},
    "voice": {"enabled": True, "dir": "data/voices", "conv_overrides": {"abc": "叶子"}},
    "asr": {"enabled": False, "url": ""},
    "app": {"web_port": 6060, "run_mode": "capture"},
}
(SCRATCH / "config.yaml").write_text(yaml.safe_dump(BASE, allow_unicode=True, sort_keys=False),
                                     encoding="utf-8")
cfg_before = (SCRATCH / "config.yaml").read_text(encoding="utf-8")

import web.app as webapp  # noqa: E402

app = webapp.create_app()
app.config.update(TESTING=True)
c = app.test_client()


def load():
    return yaml.safe_load((SCRATCH / "config.yaml").read_text(encoding="utf-8"))


print("[1] 总览页")
r = c.get("/config/raw")
check("GET /config/raw -> 200", r.status_code == 200, str(r.status_code))
body = r.get_data(as_text=True)
check("导航里有「总设置」", "/config" in c.get("/").get_data(as_text=True))
for sect in BASE:
    check(f"列出了 {sect} 段", f'"{sect}"' in body or f">{sect}<" in body or sect in body)
check("**不回显 api_key 明文**", "sk-secret-key-987654" not in body)
check("key 显示为掩码", "sk-sec…7654" in body)
check("bark.key 也掩码", "REPLACE_ME" not in body or "••••" in body)
check("给出改大模型的表单页入口", "/llm" in body)
check("给出消息分类页入口", "/settings" in body)

print("\n[2] 每段都有编辑器，且能打开")
for sect in BASE:
    r = c.get(f"/config/raw/{sect}")
    ok = r.status_code == 200 and sect in r.get_data(as_text=True)
    check(f"GET /config/{sect} -> 200 且是编辑器", ok, str(r.status_code))
r = c.get("/config/raw/不存在的段")
check("未知段 -> 404", r.status_code == 404, str(r.status_code))

print("\n[3] 改一段：只动这一段，其它段原样")
new_digest = dict(BASE["digest"], time="23:45", summarize=False)
r = c.post("/config/raw/digest/save", data={"text": json.dumps(new_digest, ensure_ascii=False)},
           follow_redirects=True)
check("保存成功 -> 200/302", r.status_code in (200, 302), str(r.status_code))
after = load()
check("digest.time 已改", after["digest"]["time"] == "23:45", str(after["digest"]["time"]))
check("digest.summarize 已改", after["digest"]["summarize"] is False)
for sect in BASE:
    if sect == "digest":
        continue
    check(f"{sect} 段未受影响", after.get(sect) == BASE[sect], str(after.get(sect))[:60])
check("自动备份已生成", bool(list(SCRATCH.glob("config.yaml.bak-*"))),
      str([p.name for p in SCRATCH.glob("config.yaml.bak-*")]))

print("\n[4] 校验：坏输入被挡且不写盘")
snapshot = (SCRATCH / "config.yaml").read_text(encoding="utf-8")
r = c.post("/config/raw/digest/save", data={"text": "{ not json"})
check("JSON 语法错 -> 400", r.status_code == 400, str(r.status_code))
check("语法错不写盘", (SCRATCH / "config.yaml").read_text(encoding="utf-8") == snapshot)
r = c.post("/config/raw/digest/save", data={"text": json.dumps({"time": "25:99"})})
check("非法时间 -> 400", r.status_code == 400, str(r.status_code))
r = c.post("/config/raw/storage/save", data={"text": json.dumps({"sqlite_path": "x", "ingest_exclude_types": [51]})})
s = load()
check("storage 保存时把 47 加回", 47 in s["storage"]["ingest_exclude_types"], str(s["storage"]["ingest_exclude_types"]))
r = c.post("/config/raw/llm/save", data={"text": json.dumps({"base_url": "", "api_key": "sk-x", "model": "m"})})
check("llm 有 key 没 base_url -> 400", r.status_code == 400, str(r.status_code))
r = c.post("/config/raw/llm/save", data={"text": json.dumps({"base_url": "https://x/v1", "model": ""})})
check("llm 有 base_url 没 model -> 400", r.status_code == 400, str(r.status_code))
r = c.post("/config/raw/hook/save", data={"text": json.dumps({"api_base": "", "callback_port": 8888})})
check("hook 空 api_base -> 400", r.status_code == 400, str(r.status_code))
r = c.post("/config/raw/filter/save", data={"text": json.dumps({"groups": "不是数组"})})
check("filter.groups 非数组 -> 400", r.status_code == 400, str(r.status_code))
r = c.post("/config/raw/digest/save", data={"text": "[1,2,3]"})
check("整段不是对象 -> 400", r.status_code == 400, str(r.status_code))

print("\n[5] 能改到以前没有入口的段（bark / voice / asr / app）")
for sect, patch in (("bark", {"enabled": True, "server": "https://api.day.app", "key": "bk-123"}),
                    ("voice", {"enabled": False, "dir": "data/voices", "conv_overrides": {"z": "某人"}}),
                    ("asr", {"enabled": True, "url": "http://mac:8170/v1/audio/transcriptions"}),
                    ("app", {"web_port": 6070, "run_mode": "capture"})):
    r = c.post(f"/config/raw/{sect}/save", data={"text": json.dumps(patch, ensure_ascii=False)})
    ok = r.status_code in (200, 302) and load().get(sect) == patch
    check(f"{sect} 段可改生效", ok, f"status={r.status_code} now={load().get(sect)}")

print("\n[6] 微调数字字段会被规范化（避免写出字符串端口）")
c.post("/config/raw/hook/save", data={"text": json.dumps(
    {"api_base": "http://127.0.0.1:30001", "callback_host": "127.0.0.1",
     "callback_port": "8899", "self_wxid": "x"})})
check("callback_port 存成整数", isinstance(load()["hook"]["callback_port"], int),
      repr(load()["hook"]["callback_port"]))
c.post("/config/raw/llm/save", data={"text": json.dumps(
    {"base_url": "https://api.minimax.cn/v1", "api_key": "sk-a", "model": "M", "push_threshold": "9"})})
check("push_threshold 被夹到 1~5", load()["llm"]["push_threshold"] == 5, repr(load()["llm"]["push_threshold"]))

print("\n[7] 真实 config.yaml 未被这些测试改动")
real_now = REAL_CFG.read_text(encoding="utf-8")
check("真实配置未被改动", "23:45" not in real_now and "127.0.0.1:6070" not in real_now)

shutil.rmtree(SCRATCH, ignore_errors=True)
print("=" * 58)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("总设置页 验证通过")
