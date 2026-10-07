"""E6 验证：vendor/manifest.json 的微信安装程序信息是否能被 setup_env 正确识别。"""
import json
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
sys.path.insert(0, str(PROJ))

import paths  # noqa: E402
import setup_env  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


man = setup_env.load_manifest()
print("vendor_root:", paths.vendor_root())
check("manifest 能读到", bool(man), f"schema={man.get('schema')}")

info = setup_env.installer_info(man)
print("installer_info:", json.dumps(info, ensure_ascii=False, indent=2))

check("随包安装程序被识别为存在", info["present"] is True, str(info["present"]))
check("文件名与磁盘一致", info["file"] == "wechat4.1.10.27.exe", str(info["file"]))
check("manifest 里的 sha256 已填", bool(info["expected_sha256"]), str(info["expected_sha256"]))
check("sha256 与磁盘文件一致", info["hash_ok"] is True,
      f'sha={info["sha256"]} want={info["expected_sha256"]}')

# 采集报告里不应该再报"随包没有微信安装程序"
rep = setup_env.collect_report()
warn_txt = " / ".join(rep.get("warnings") or [])
check("报告的 warnings 里不再有'没有微信安装程序'", "没有微信安装程序" not in warn_txt, warn_txt[:120])
print("warnings:", json.dumps(rep.get("warnings"), ensure_ascii=False))
print("blockers:", json.dumps(rep.get("blockers"), ensure_ascii=False))
print("ready_to_configure:", rep.get("ready_to_configure"))

print("=" * 56)
if FAILS:
    print("FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("E6 manifest 校验通过")
