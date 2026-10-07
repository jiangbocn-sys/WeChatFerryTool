"""待初始化（空配置）下所有页面必须能打开：/ 曾经因为 dashboard.html 里 cfg.hook 而 500。"""
import os
import sys
from pathlib import Path

SCRATCH = Path(r"D:\projects\.wft-emptycfg-scratch")
if SCRATCH.exists():
    import shutil
    shutil.rmtree(SCRATCH)
(SCRATCH / "data").mkdir(parents=True, exist_ok=True)
(SCRATCH / "reports").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SCRATCH)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, r"D:\projects\WeChatFerryTool")

import web.app as webapp  # noqa: E402

# 待初始化 = 有 config.yaml 但内容是空的（向导还没生成配置）。
# 这里显式把 CONFIG_PATH 指到临时数据根，避免读到项目里的真实配置。
(SCRATCH / "config.yaml").write_text("", encoding="utf-8")
webapp.CONFIG_PATH = SCRATCH / "config.yaml"
print("config 路径 :", webapp.CONFIG_PATH)
print("load_config :", webapp.load_config())

app = webapp.create_app()
app.config.update(TESTING=True)
c = app.test_client()

FAILS = []
PAGES = ["/", "/filter", "/labels", "/settings", "/digest", "/browse", "/types", "/replies", "/setup", "/api/health"]
for p in PAGES:
    try:
        r = c.get(p)
        ok = r.status_code == 200
    except Exception as e:  # noqa: BLE001
        ok, r = False, None
        print(f"  EXC   GET {p} -> {type(e).__name__}: {e}")
    print(("  PASS  " if ok else "  FAIL  ") + f"GET {p} -> {r.status_code if r else 'exception'}")
    if not ok:
        FAILS.append(p)

body = c.get("/").get_data(as_text=True)
for needle in ("待初始化", "前往初始化向导", "入库排除类型", "当前生效配置"):
    hit = needle in body
    print(("  PASS  " if hit else "  FAIL  ") + f"仪表盘含「{needle}」")
    if not hit:
        FAILS.append(needle)

import shutil  # noqa: E402
shutil.rmtree(SCRATCH, ignore_errors=True)
print("=" * 50)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("待初始化下所有页面正常")
