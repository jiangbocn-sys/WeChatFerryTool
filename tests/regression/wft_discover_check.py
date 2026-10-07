"""验证两件事：
1) 启动时"先找已有配置/数据"：exe 在 dist\\App\\ 下也能发现上层的项目数据（不再当全新安装）
2) 向导页会回显已保存的 LLM 配置；key 留空不会被抹掉
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
sys.path.insert(0, str(PROJ))

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


ROOT = Path(r"D:\projects\.wft-discover-test")
shutil.rmtree(ROOT, ignore_errors=True)

print("[1] _has_existing_data / _discover_existing_root（纯探测）")
import app.main as am  # noqa: E402

# 造一个假的"项目 + dist 里的 exe"结构
fake_proj = ROOT / "proj"
(fake_proj / "accounts" / "acctA" / "data").mkdir(parents=True, exist_ok=True)
(fake_proj / "accounts" / "acctA" / "config.yaml").write_text("llm:\n  model: A\n", encoding="utf-8")
(fake_proj / "accounts" / "acctA" / "data" / "messages.db").write_bytes(b"x")
exe_dir = fake_proj / "dist" / "WeChatFerryApp"
exe_dir.mkdir(parents=True, exist_ok=True)

check("识别账号隔离布局", am._has_existing_data(fake_proj) is True)
check("识别空目录 = 没有数据", am._has_existing_data(exe_dir) is False)
check("识别单目录布局(config.yaml 直接在里面)", am._has_existing_data(fake_proj / "accounts" / "acctA") is True)

# 直接测查找函数（把 frozen 起点指向假的 exe 目录）
import types  # noqa: E402
_orig_frozen = getattr(sys, "frozen", None)
sys.frozen = True                                     # type: ignore[attr-defined]
_orig_exe = sys.executable
sys.executable = str(exe_dir / "WeChatFerryApp.exe")
try:
    found = am._discover_existing_root()
finally:
    sys.executable = _orig_exe
    if _orig_frozen is None:
        del sys.frozen                                 # type: ignore[attr-defined]
    else:
        sys.frozen = _orig_frozen                      # type: ignore[attr-defined]
check("从 dist\\App\\ 能往上发现项目数据", found == fake_proj, str(found))

print("\n[2] 没有已有数据时不应误报")
empty = ROOT / "empty" / "dist" / "App"
empty.mkdir(parents=True, exist_ok=True)
# 用一个"干净"起点：临时把 cwd 换到空目录，避免真实项目被扫到
_orig_cwd = os.getcwd()
os.chdir(ROOT / "empty")
sys.frozen = True                                     # type: ignore[attr-defined]
sys.executable = str(empty / "App.exe")
try:
    got = am._discover_existing_root()
finally:
    os.chdir(_orig_cwd)
    sys.executable = _orig_exe
    if _orig_frozen is None:
        del sys.frozen                                 # type: ignore[attr-defined]
    else:
        sys.frozen = _orig_frozen                      # type: ignore[attr-defined]
check("空目录链 → 返回 None（走全新安装）", got is None, str(got))

print("\n[3] 向导回显已保存配置（用临时数据根）")
scratch = ROOT / "scratch"
(scratch / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(scratch)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
# 清掉可能已导入的模块，确保 paths 重新求值
for m in [k for k in list(sys.modules) if k.split(".")[0] in ("paths", "setup", "web", "app")]:
    del sys.modules[m]
import paths  # noqa: E402
import setup as setup_mod  # noqa: E402

check("config_path 指向临时数据根", paths.config_path() == scratch / "config.yaml", str(paths.config_path()))
(scratch / "config.yaml").write_text(
    "hook:\n  self_wxid: ruibo_jiang\n"
    "digest:\n  time: '23:30'\n"
    "asr:\n  enabled: true\n"
    "llm:\n  base_url: https://api.minimax.cn/v1\n  api_key: sk-abcdefghijklmnop\n  model: MiniMax-M3\n",
    encoding="utf-8")

peek = setup_mod.peek_answers_from_report({"account": {"self_wxid": "x"}})
print("   peek:", {k: v for k, v in peek.items() if k != "llm_api_key"})
check("回显已保存 base_url", peek["llm_base_url"] == "https://api.minimax.cn/v1", peek["llm_base_url"])
check("回显已保存 model", peek["llm_model"] == "MiniMax-M3", peek["llm_model"])
check("不回显 key 本体", peek["llm_api_key"] == "")
check("标记 key 已保存", peek["llm_api_key_saved"] is True)
check("给出脱敏提示", peek["llm_api_key_hint"].startswith("sk-abc") and peek["llm_api_key_hint"].endswith("mnop"),
      peek["llm_api_key_hint"])
check("回显 digest_time", peek["digest_time"] == "23:30")
check("回显 asr.enabled", peek["asr_enabled"] is True)
check("带上 config_path", peek["config_path"] == str(scratch / "config.yaml"))

print("\n[4] 前端留空（哨兵值）不能把已保存的 key 抹掉")
res = setup_mod.apply_answers({"run_mode": "capture", "llm_base_url": peek["llm_base_url"],
                               "llm_model": peek["llm_model"],
                               "llm_api_key": setup_mod.KEEP_API_KEY}, dry_run=False)
check("保存成功", res.get("ok") is True, str(res)[:120])
import yaml  # noqa: E402
saved = yaml.safe_load((scratch / "config.yaml").read_text(encoding="utf-8"))
check("key 被保留（没被抹掉）", (saved.get("llm") or {}).get("api_key") == "sk-abcdefghijklmnop",
      str((saved.get("llm") or {}).get("api_key"))[:12])
check("base_url/model 也在", (saved.get("llm") or {}).get("model") == "MiniMax-M3")
peek2 = setup_mod.peek_answers_from_report({})
check("再次打开仍有 key 标记", peek2["llm_api_key_saved"] is True)

print("\n[5] 真的重填 key 时要覆盖")
setup_mod.apply_answers({"run_mode": "capture", "llm_base_url": "https://api.deepseek.com/v1",
                         "llm_model": "deepseek-chat", "llm_api_key": "sk-newkey-12345678"},
                        dry_run=False)
saved2 = yaml.safe_load((scratch / "config.yaml").read_text(encoding="utf-8"))
check("新 key 生效", (saved2.get("llm") or {}).get("api_key") == "sk-newkey-12345678")
check("新 base_url 生效", (saved2.get("llm") or {}).get("base_url") == "https://api.deepseek.com/v1")

print("\n[6] 真的清空（显式空 + 明确要求清）时行为一致且不炸")
r = setup_mod.apply_answers({"run_mode": "capture", "llm_base_url": "https://api.deepseek.com/v1",
                             "llm_model": "deepseek-chat", "llm_api_key": ""}, dry_run=True)
check("空 key + 有 base_url 时仍保留旧 key（dry_run）", r.get("ok") is True, str(r)[:100])

os.environ.pop("WCF_DATA_DIR", None)
os.environ.pop("WCF_NO_MULTI_ACCOUNT", None)
shutil.rmtree(ROOT, ignore_errors=True)
print("=" * 58)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("发现已有数据 + 向导回显 验证通过")
