"""验证 app 退出语义：从托盘点「退出」必须能让 run() 返回并执行 shutdown()。

用假的 tray / consumer / supervisor，不启动微信、不注入 DLL。
回归点：`request_quit()` 必须顺手 `tray.stop()`（否则主线程卡在托盘消息循环）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
sys.path.insert(0, str(PROJ))

import app.main as am  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


class FakeTray:
    def __init__(self):
        self.stop_calls = 0
        self.run_returned = False

    def run(self):
        # 模拟托盘：阻塞在消息循环里，只有 stop() 才会让它返回
        while self.stop_calls == 0:
            time.sleep(0.02)
        self.run_returned = True

    def stop(self):
        self.stop_calls += 1


class FakeConsumer:
    def __init__(self):
        self.stop_calls = 0
        self.thread = None

    def stop(self):
        self.stop_calls += 1


class FakeSupervisor:
    def restore_normal(self, restart_wechat=True):
        return True


FAKE = {"tray": None}


class _TrayFactory:
    """替换 tray 模块的 available()/Tray()，让 App.run() 用我们的假托盘。"""
    def __init__(self, tray):
        self._tray = tray

    def available(self):
        return True

    def Tray(self, app):
        return self._tray


def make_app(with_tray=True):
    global FAKE
    args = type("Args", (), {"no_tray": not with_tray, "restore": False, "no_browser": True,
                             "no_launch": True, "account": None, "replies": False})()
    cfg = {"hook": {"api_base": "http://127.0.0.1:39999", "callback_host": "127.0.0.1",
                    "callback_port": 38999, "self_wxid": "tester"},
           "storage": {"sqlite_path": str(Path(r"D:\projects\.wft-quit-test\data\messages.db"))}}
    a = am.App(cfg, args)
    # 屏蔽真实副作用
    a.uninitialized = True                      # run() 里就不启动 consumer / 微信
    a.consumer = FakeConsumer()
    a.supervisor = FakeSupervisor()
    a._consumer_thread = None
    a._tray = FakeTray()
    a.start_web = lambda: None                  # 不真的起 Flask
    return a


import shutil  # noqa: E402
root = Path(r"D:\projects\.wft-quit-test")
shutil.rmtree(root, ignore_errors=True)
(root / "data").mkdir(parents=True, exist_ok=True)

print("[1] 托盘菜单「退出」→ request_quit() 必须停掉托盘")
a = make_app(with_tray=True)
tray = a._tray
# 关键：让 App.run() 用我们这个假托盘（真实 tray_mod.Tray 在沙箱里跑不起来）
am.tray_mod = _TrayFactory(tray)
check("初始未请求退出", not a._stop.is_set())

done = threading.Event()
box: dict = {}


def runner():
    try:
        a.run()                                  # 会阻塞在 tray.run()
        box["run_returned"] = True
    except Exception as e:  # noqa: BLE001
        box["error"] = f"{type(e).__name__}: {e}"
    finally:
        done.set()


th = threading.Thread(target=runner, daemon=True)
th.start()
time.sleep(0.4)
check("run() 已进入托盘循环（阻塞中）", th.is_alive() and not box.get("run_returned"))

a.request_quit()                                 # 等价于托盘菜单点「退出」
check("request_quit() 设置了 _stop", a._stop.is_set())
check("request_quit() 调用了 tray.stop()", tray.stop_calls >= 1, str(tray.stop_calls))
ok = done.wait(5)
check("run() 已返回（不再卡在托盘）", ok and box.get("run_returned") is True, str(box))
check("托盘循环确实退出了", tray.run_returned)

print("\n[2] shutdown() 也要能停托盘，并且重复调用不炸")
a2 = make_app(with_tray=True)
a2.shutdown()
check("shutdown() 调用了 tray.stop()", a2._tray.stop_calls >= 1, str(a2._tray.stop_calls))
a2.shutdown()                                    # 幂等
check("shutdown() 可重复调用", True)
check("shutdown() 停掉了 consumer", a2.consumer.stop_calls >= 1, str(a2.consumer.stop_calls))

print("\n[3] 没托盘（--no-tray / 托盘不可用）时不能因为 _tray=None 而炸")
a3 = make_app(with_tray=False)
a3._tray = None
a3.start_web = lambda: None
a3.request_quit()
check("_tray=None 时 request_quit 不抛异常", a3._stop.is_set())
a3.shutdown()
check("_tray=None 时 shutdown 不抛异常", True)

print("\n[4] 托盘 stop() 幂等性（tray 模块本体）")
from app import tray as tray_mod  # noqa: E402
t = tray_mod.Tray(type("A", (), {"web_url": "x", "status_line": lambda self: "s",
                                 "snapshot": lambda self: {}})())
t.stop()
t.stop()
check("未启动的托盘 stop() 可重复调用", True)

shutil.rmtree(root, ignore_errors=True)
print("=" * 56)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("退出语义验证通过")
