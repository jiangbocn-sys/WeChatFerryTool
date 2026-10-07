"""单独验证托盘：pystray 可用性、图标能否真正 run()、菜单项是否齐全。

不启动 consumer/Web，只测托盘这一层（3 秒后自动停）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
sys.path.insert(0, str(PROJ))

from app import tray as tray_mod  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


print("[1] pystray / PIL 可用性")
check("tray_mod.available() = True", tray_mod.available())

import pystray  # noqa: E402
from PIL import Image  # noqa: E402

print("   pystray:", getattr(pystray, "__version__", "?"), "| 后端:", type(pystray.Icon).__module__)

print("\n[2] 图标生成（代码画图，不依赖资源文件）")
for name, color in (("绿/正常", tray_mod.COLOR_OK), ("橙/暂停", tray_mod.COLOR_PAUSED),
                    ("灰/待初始化", tray_mod.COLOR_UNINIT), ("红/异常", tray_mod.COLOR_ERROR)):
    img = tray_mod._make_image(color)
    ok = isinstance(img, Image.Image) and img.size == (64, 64) and img.mode == "RGBA"
    check(f"图标 {name} 生成正常", ok, f"{img.size} {img.mode}")

print("\n[3] 真正跑起来（3 秒）")


class FakeApp:
    web_url = "http://127.0.0.1:6060"
    replies_enabled = False
    paused = False
    uninitialized = True

    def status_line(self):
        return "待初始化 / 测试用"

    def snapshot(self):
        return {"dll_api_ready": False}

    def set_replies(self, v):
        self.replies_enabled = v

    def set_paused(self, v):
        self.paused = v

    def request_quit(self):
        pass


t = tray_mod.Tray(FakeApp())
err: list[str] = []


def runner():
    try:
        t.run()          # 阻塞在托盘消息循环
    except Exception as e:  # noqa: BLE001
        err.append(f"{type(e).__name__}: {e}")


th = threading.Thread(target=runner, daemon=True)
th.start()
time.sleep(3.0)
check("托盘 run() 没有抛异常", not err, err[0] if err else "")
check("托盘 icon 对象已创建", t._icon is not None)
check("托盘循环线程还活着（图标常驻）", th.is_alive())
menu_items = []
try:
    menu_items = [str(i.text) for i in t._icon.menu]
except Exception as e:  # noqa: BLE001
    print("   读菜单失败:", e)
print("   菜单项:", menu_items)
for want in ("打开管理页", "自动回复", "暂停抓取", "退出"):
    check(f"菜单含「{want}」", any(want in m for m in menu_items))

t.stop()
time.sleep(0.5)
print("   已 stop()")

print("=" * 56)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("托盘验证通过")
