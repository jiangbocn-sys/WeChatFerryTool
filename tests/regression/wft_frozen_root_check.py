"""回归：模拟"冻结版 exe 放在 <proj>\\dist\\App\\"，启动后必须
   1) 发现已有数据 → 数据根 = <proj>\\accounts\\<账号>（不是 exe 旁边）
   2) config_path = <proj>\\config.yaml
   3) 不再在 exe 旁边新建 accounts 目录（老 bug：拿对账号名、建错地方）

用临时假项目 + 子进程（frozen 只能在进程启动时模拟），不碰真实数据。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
PY = str(PROJ / ".venv" / "Scripts" / "python.exe")
ROOT = Path(r"D:\projects\.wft-frozen-test")
FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


shutil.rmtree(ROOT, ignore_errors=True)
proj = ROOT / "proj"
acct = proj / "accounts" / "ruibo_jiang_542e"
(acct / "data").mkdir(parents=True, exist_ok=True)
(proj / "config.yaml").write_text(
    "hook:\n  self_wxid: ruibo_jiang\nllm:\n  base_url: https://x/v1\n  model: M\n"
    "storage:\n  sqlite_path: data/messages.db\n", encoding="utf-8")
(acct / "data" / "messages.db").write_bytes(b"X" * 128)
(acct / "data" / "labels.json").write_text('{"groups":{},"senders":{}}', encoding="utf-8")
exe_dir = proj / "dist" / "WeChatFerryApp"
exe_dir.mkdir(parents=True, exist_ok=True)

case = ROOT / "case.py"
case.write_text(
    "import sys\n"
    "from pathlib import Path\n"
    f"sys.path.insert(0, r'{PROJ}')\n"
    "sys.frozen = True\n"
    f"sys.executable = r'{exe_dir / 'WeChatFerryApp.exe'}'\n"
    "import paths\n"
    "import app.main as am\n"
    "print('FOUND', am._discover_existing_root())\n"
    "print('SLUG', am._ACCOUNT_SLUG)\n"
    "print('DATA_ROOT', paths.data_root())\n"
    "print('CONFIG', paths.config_path())\n"
    "print('PROJECT_DIR', am.PROJECT_DIR)\n",
    encoding="utf-8")

env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
r = subprocess.run([PY, "-B", str(case)], capture_output=True, text=True, encoding="utf-8",
                   cwd=str(proj), env=env)
out = (r.stdout or "") + "\n" + (r.stderr or "")


def val(tag):
    for line in out.splitlines():
        if line.startswith(tag + " "):
            return line[len(tag) + 1:].strip()
    return ""


print("---- 子进程输出 ----")
for line in out.strip().splitlines():
    if line[:6] in ("FOUND ", "SLUG ", "DATA_R", "CONFIG", "PROJEC") or "发现已有" in line:
        print("   " + line)

check("发现到假项目目录", val("FOUND") == str(proj), val("FOUND"))
check("账号 slug 正确", val("SLUG") == "ruibo_jiang_542e", val("SLUG"))
check("数据根 = 项目里的账号目录（不再落到 exe 旁边）",
      val("DATA_ROOT") == str(acct), val("DATA_ROOT"))
check("config_path = 项目根的 config.yaml",
      val("CONFIG") == str(proj / "config.yaml"), val("CONFIG"))
check("PROJECT_DIR 与数据根一致", val("PROJECT_DIR") == str(acct), val("PROJECT_DIR"))
check("没有在 exe 旁边新建 accounts 目录", not (exe_dir / "accounts").exists(),
      str(exe_dir / "accounts"))
check("原数据仍在（未被搬动/破坏）", (acct / "data" / "messages.db").read_bytes() == b"X" * 128)

shutil.rmtree(ROOT, ignore_errors=True)
print("=" * 58)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("冻结版数据根发现 验证通过")
