"""验证初始化向导保存：正常路径 + 异常路径都必须给前端可读 JSON。"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SCRATCH = Path(r"D:\projects\.wft-wizard2")
if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
(SCRATCH / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SCRATCH)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

import paths  # noqa: E402
import setup as setup_mod  # noqa: E402
import web.app as webapp  # noqa: E402

# 把"读配置"和"写配置"都指到临时数据根，免得碰真实 config.yaml
(SCRATCH / "config.yaml").write_text("", encoding="utf-8")
webapp.CONFIG_PATH = SCRATCH / "config.yaml"
setup_mod.paths.base_root = lambda: SCRATCH      # type: ignore[assignment]
setup_mod.paths.data_root = lambda: SCRATCH      # type: ignore[assignment]

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


print("data_root:", paths.data_root())
print("config_path:", paths.config_path())

app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)   # 模拟生产（真错误走 errorhandler）
c = app.test_client()

PAYLOAD = {
    "run_mode": "capture_reply", "self_wxid": "ruibo_jiang", "digest_time": "23:30",
    "llm_base_url": "https://api.minimax.cn/v1", "llm_api_key": "sk-fake", "llm_model": "MiniMax-M3",
    "capture_keys": False,
    "template": {"trigger": "@我", "match_type": "substring", "response": "稍后回复",
                 "use_llm": False, "fallback": "", "test_only": True},
}

print("\n[1] 正常保存（dry_run=False 正式路径）")
r = c.post("/api/setup/apply", json=PAYLOAD)
j = r.get_json(silent=True)
print("   status:", r.status_code, "| json:", json.dumps(j, ensure_ascii=False)[:200])
check("返回 200", r.status_code == 200, str(r.status_code))
check("返回 JSON 且 ok=True", isinstance(j, dict) and j.get("ok") is True, str(j))
check("写到了数据根的 config.yaml", (SCRATCH / "config.yaml").is_file(), str(paths.config_path()))
check("模板数 = 1", j.get("templates") == 1, str(j.get("templates")))
saved = (SCRATCH / "config.yaml").read_text(encoding="utf-8")
check("写出的配置含 llm.model", "MiniMax-M3" in saved)
check("写出的配置含 storage.ingest_exclude_types", "ingest_exclude_types" in saved)

print("\n[2] 异常路径：apply_answers 抛错时必须回可读 JSON（不是 HTML 500）")
orig = setup_mod.apply_answers


def boom(_ans):
    raise RuntimeError("模拟写入失败")


setup_mod.apply_answers = boom  # type: ignore[assignment]
r = c.post("/api/setup/apply", json=PAYLOAD)
body = r.get_data(as_text=True)
setup_mod.apply_answers = orig  # type: ignore[assignment]
print("   status:", r.status_code, "| content-type:", r.headers.get("Content-Type"))
print("   body:", body[:200])
check("异常时仍是 JSON", "application/json" in (r.headers.get("Content-Type") or ""), r.headers.get("Content-Type"))
check("异常时正文含错误原因", "模拟写入失败" in body or "\\u6a21\\u62df" in body, body[:120])
check("异常时不含 HTML", "<html" not in body.lower())

print("\n[3] 完全没被接住的路由异常也要变 JSON（errorhandler 生效）")
r = c.post("/api/setup/deploy-dll")
body = r.get_data(as_text=True)
print("   status:", r.status_code, "| content-type:", r.headers.get("Content-Type"))
check("deploy-dll 返回 JSON（不炸 HTML）",
      "application/json" in (r.headers.get("Content-Type") or ""), body[:120])

print("\n[4] 4xx 仍然保持原来的语义")
r = c.get("/reports/../../config.yaml")
check("目录穿越 404（不被 errorhandler 吞掉）", r.status_code == 404, str(r.status_code))
r = c.get("/api/health")
check("/api/health 仍 200 JSON", r.status_code == 200 and r.get_json().get("status") == "ok")

shutil.rmtree(SCRATCH, ignore_errors=True)
print("=" * 56)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("向导保存路径验证通过")
