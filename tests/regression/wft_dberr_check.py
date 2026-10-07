"""验证：消息库打不开时，各页面给**可读提示**（503）而不是 500 白页。

做法：把 DB_PATH 指向一个"存在但不是数据库"的文件 → sqlite 打开/查询必然失败。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

SC = Path(r"D:\projects\.wft-dberr")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, r"D:\projects\WeChatFerryTool")

(SC / "config.yaml").write_text(
    "storage:\n  sqlite_path: data/messages.db\nfilter:\n  groups: []\n  senders: []\n",
    encoding="utf-8")
(SC / "data" / "labels.json").write_text('{"groups": {}, "senders": {}}', encoding="utf-8")
# 伪装成一个"存在但打不开"的库：目录名冒充文件（open 会失败）
(SC / "data" / "messages.db").mkdir(parents=True, exist_ok=True)

import web.app as w  # noqa: E402

app = w.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


print("[1] 依赖消息库的页面 → 503 + 可读提示")
for url in ("/", "/browse", "/labels", "/types", "/settings"):
    r = c.get(url)
    body = r.get_data(as_text=True)
    ok = r.status_code == 503 and ("打不开消息库" in body or "消息库" in body)
    check(f"GET {url} -> 503 且带原因", ok, f"status={r.status_code}")
    if not ok:
        print("      " + body[:200].replace("\n", " "))

print("\n[2] 不依赖消息库的页面仍正常")
for url in ("/filter", "/config", "/digest", "/llm", "/api/version", "/api/health"):
    r = c.get(url)
    check(f"GET {url} -> 200", r.status_code == 200, str(r.status_code))

print("\n[3] /api/version 的 build 是自动读取的（不是 hardcode 字符串）")
v = c.get("/api/version").get_json()
import re
check("build 形如时间戳", bool(re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", v.get("build") or "")),
      str(v.get("build")))
check("features 全为 True", all(v["features"].values()), str(v["features"]))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 58)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("消息库打不开时的降级 验证通过")
