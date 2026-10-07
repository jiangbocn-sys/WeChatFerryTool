"""验证 keyhook 注入的安全闸门（2026-10-06 事故后的防护）。

事故：app 启动微信 → 抢在开库前注入 keyhook3.dll → 微信读不出消息库（对话历史空白）。
本测试锁死三条：
  1. 默认**不注入**（app.auto_inject_keyhook 缺省 = False）
  2. 未显式开启时，inject_safety_error() 给出可读原因
  3. 微信版本与 manifest 要求不符时**拒绝注入**（版本不符的钩子最容易读坏库）
  4. 显式开启且版本匹配时，才返回 None（允许注入）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-inject-guard")
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


from app.supervisor import Supervisor  # noqa: E402
import setup_env  # noqa: E402

REAL_WX = Path(r"C:\Program Files\Tencent\Weixin")
manifest = setup_env.load_manifest()
want = str((manifest.get("wechat") or {}).get("version") or "?")
print(f"manifest 要求的微信版本: {want}")

print("\n[1] 默认不注入")
s = Supervisor({})
check("缺省 auto_inject 为 False", s.auto_inject is False, str(s.auto_inject))
why = s.inject_safety_error()
check("给出可读原因", bool(why) and "默认关闭" in why, str(why)[:90])

print("\n[2] 显式开启 + 版本匹配 → 允许")
s2 = Supervisor({"auto_inject_keyhook": True})
why2 = s2.inject_safety_error()
real_ver = setup_env.file_version(REAL_WX / "Weixin.exe")
check(f"真实微信版本 = {real_ver}", real_ver == want, f"{real_ver} vs {want}")
check("版本匹配时允许注入（返回 None）", why2 is None, str(why2))

print("\n[3] 版本不符 → 拒绝注入")
fake = SC / "fakewechat"
fake.mkdir(parents=True, exist_ok=True)
(fake / "Weixin.exe").write_bytes(b"MZ" + b"\x00" * 100)      # 不是真 PE，读不到版本
s3 = Supervisor({"auto_inject_keyhook": True, "wechat_dir": str(fake)})
why3 = s3.inject_safety_error()
check("版本读不到/不符时拒绝", bool(why3), str(why3)[:120])
check("原因里说明是版本不符", "不符" in (why3 or ""), str(why3)[:120])

print("\n[4] keyhook DLL 不存在 → 拒绝")
s4 = Supervisor({"auto_inject_keyhook": True, "keyhook_dll": str(SC / "nope.dll")})
why4 = s4.inject_safety_error()
check("DLL 缺失时拒绝", bool(why4) and "不存在" in why4, str(why4)[:90])

print("\n[5] 真实配置里两个开关都应为安全值")
import yaml  # noqa: E402
cfg = yaml.safe_load((PROJ / "config.yaml").read_text(encoding="utf-8"))
app_cfg = cfg.get("app") or {}
check("launch_wechat = false（不让 app 启动微信）", app_cfg.get("launch_wechat") is False, str(app_cfg.get("launch_wechat")))
check("auto_inject_keyhook = false（不注入 keyhook）", app_cfg.get("auto_inject_keyhook") is False, str(app_cfg.get("auto_inject_keyhook")))
c2 = Supervisor(app_cfg)
check("用真实配置构造时也不注入", c2.auto_inject is False)
check("且给出可读原因", bool(c2.inject_safety_error()))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 58)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("keyhook 注入安全闸门 验证通过")
