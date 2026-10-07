"""验证新页面 /llm（大模型设置）：初始化之后也能改 LLM，且只动 llm 段。

用临时数据根（含一份"别的段有内容"的 config.yaml），不碰真实配置。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import yaml

PROJ = Path(r"D:\projects\WeChatFerryTool")
SCRATCH = Path(r"D:\projects\.wft-llm-page")
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


# 一份"其它段有内容"的配置：用来证明改 LLM 不会把它们弄丢
BASE_CFG = {
    "hook": {"api_base": "http://127.0.0.1:30001", "callback_host": "127.0.0.1",
             "callback_port": 8888, "self_wxid": "ruibo_jiang"},
    "filter": {"groups": [], "senders": [], "keywords": [], "case_insensitive": True},
    "llm": {"base_url": "https://old.example/v1", "api_key": "sk-old-key-123456",
            "model": "old-model", "push_threshold": 4},
    "storage": {"sqlite_path": "data/messages.db", "ingest_exclude_types": [47]},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "summarize": True,
               "exclude_types": [47, 51, 10000, 10002]},
    "replies": {"enabled": True, "history_count": 10,
                "templates": [{"name": "keep-me", "trigger": "@我", "response": "x"}],
                "rate_limit": {"per_group_cooldown_s": 15}},
    "asr": {"enabled": True, "url": "http://mac:8170/v1/audio/transcriptions"},
    "voice": {"enabled": True, "dir": "data/voices", "conv_overrides": {"abc": "叶子"}},
    "app": {"web_port": 6060, "run_mode": "capture_reply"},
}
(SCRATCH / "config.yaml").write_text(yaml.safe_dump(BASE_CFG, allow_unicode=True, sort_keys=False),
                                     encoding="utf-8")

import web.app as webapp  # noqa: E402

check("config_path 指向临时数据根", webapp.CONFIG_PATH == SCRATCH / "config.yaml", str(webapp.CONFIG_PATH))

app = webapp.create_app()
app.config.update(TESTING=True)
c = app.test_client()

print("\n[1] 页面与导航")
r = c.get("/llm")
check("GET /llm -> 200", r.status_code == 200, str(r.status_code))
body = r.get_data(as_text=True)
check("含 base_url 输入框", 'name="base_url"' in body)
check("含 model 输入框", 'name="model"' in body)
check("含 API Key 输入框", 'name="api_key"' in body)
check("含测试连接按钮", 'api/llm/test' in body)
nav = c.get("/").get_data(as_text=True)
check("导航里有「大模型设置」", "/llm" in nav and "大模型设置" in nav)

print("\n[2] 回显已保存值 + key 不回显")
check("回显 base_url", 'https://old.example/v1' in body)
check("回显 model", 'old-model' in body)
check("不回显 key 本体", "sk-old-key-123456" not in body)
check("提示 key 已保存并脱敏", "已保存" in body and "sk-old…3456" in body)

print("\n[3] 只动 llm 段（其它段必须原样）")
r = c.post("/llm/update", data={"base_url": "https://api.minimax.cn/v1",
                                "model": "MiniMax-M3", "api_key": "",
                                "push_threshold": "5"}, follow_redirects=True)
check("保存 -> 200/302", r.status_code in (200, 302), str(r.status_code))
saved = yaml.safe_load((SCRATCH / "config.yaml").read_text(encoding="utf-8"))
check("base_url 已更新", saved["llm"]["base_url"] == "https://api.minimax.cn/v1")
check("model 已更新", saved["llm"]["model"] == "MiniMax-M3")
check("push_threshold 已更新", saved["llm"]["push_threshold"] == 5)
check("**key 被保留**（留空 = 不修改）", saved["llm"]["api_key"] == "sk-old-key-123456",
      str(saved["llm"]["api_key"]))
# 其它段必须与写入前**完全一致**
for sect in ("hook", "filter", "storage", "digest", "replies", "asr", "voice", "app"):
    check(f"{sect} 段与保存前完全一致", saved.get(sect) == BASE_CFG.get(sect),
          f"before={str(BASE_CFG.get(sect))[:60]} after={str(saved.get(sect))[:60]}")
check("replies 模板没被重建（仍是 keep-me）",
      saved["replies"]["templates"][0]["name"] == "keep-me", str(saved["replies"]["templates"][0]))
check("voice.conv_overrides 没丢", saved["voice"]["conv_overrides"] == {"abc": "叶子"})

print("\n[4] 换 key / 清空 key")
c.post("/llm/update", data={"base_url": "https://api.minimax.cn/v1", "model": "MiniMax-M3",
                            "api_key": "sk-new-key-abcdef", "push_threshold": "4"})
s2 = yaml.safe_load((SCRATCH / "config.yaml").read_text(encoding="utf-8"))
check("填新 key 会覆盖", s2["llm"]["api_key"] == "sk-new-key-abcdef")
c.post("/llm/update", data={"base_url": "https://api.minimax.cn/v1", "model": "MiniMax-M3",
                            "api_key": "", "clear_key": "on", "push_threshold": "4"})
s3 = yaml.safe_load((SCRATCH / "config.yaml").read_text(encoding="utf-8"))
check("勾选清空后 key 为空", s3["llm"]["api_key"] == "")

print("\n[5] 校验：不合法组合被挡（不让写出坏配置）")
r = c.post("/llm/update", data={"base_url": "", "model": "", "api_key": "sk-only-key"})
check("有 key 没 base_url -> 400", r.status_code == 400, str(r.status_code))
check("错误信息可读", "base_url" in r.get_data(as_text=True))
before = (SCRATCH / "config.yaml").read_text(encoding="utf-8")
r = c.post("/llm/update", data={"base_url": "https://api.minimax.cn/v1", "model": ""})
check("有 base_url 没 model -> 400", r.status_code == 400, str(r.status_code))
check("失败时不写盘", (SCRATCH / "config.yaml").read_text(encoding="utf-8") == before)

print("\n[6] 测试连接接口（用打不通的地址，必须回可读 JSON）")
r = c.post("/api/llm/test", json={"base_url": "http://127.0.0.1:1/v1", "model": "x", "api_key": "sk-x"})
j = r.get_json()
check("返回 JSON", isinstance(j, dict), str(j)[:80])
check("失败时 ok=false 且带原因", j.get("ok") is False and bool(j.get("error")), str(j)[:120])
r = c.post("/api/llm/test", json={"base_url": "", "model": ""})
check("缺参数 -> 400", r.status_code == 400, str(r.status_code))

check("真实 config.yaml 未被改动", "https://api.minimax.cn/v1" in REAL_CFG.read_text(encoding="utf-8"))

shutil.rmtree(SCRATCH, ignore_errors=True)
print("=" * 58)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("大模型设置页 验证通过")
