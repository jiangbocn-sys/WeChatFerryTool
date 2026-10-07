"""系统托盘。缺 pystray / Pillow 时优雅降级（返回 False，由 main 走无托盘模式）。"""
from __future__ import annotations

import logging
import webbrowser

log = logging.getLogger("app.tray")


def available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


def _make_image(color=(7, 193, 96, 255)):
    """画一个简单的圆形图标（默认微信绿），避免额外资源文件。

    颜色可按运行状态变化，见 _state_color()。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.rectangle((20, 29, 44, 35), fill=(255, 255, 255, 255))
    d.rectangle((29, 20, 35, 44), fill=(255, 255, 255, 255))
    return img


# 状态 → 图标颜色
COLOR_OK = (7, 193, 96, 255)        # 绿：正常抓取
COLOR_PAUSED = (243, 156, 18, 255)  # 橙：已暂停
COLOR_UNINIT = (149, 165, 166, 255) # 灰：待初始化
COLOR_ERROR = (231, 76, 60, 255)    # 红：异常


def _state_color(app):
    """按 app 状态选图标颜色，让人一眼看出系统在干什么。"""
    try:
        if getattr(app, "uninitialized", False):
            return COLOR_UNINIT
        if getattr(app, "paused", False):
            return COLOR_PAUSED
        snap = app.snapshot() or {}
        if not snap.get("dll_api_ready") and not getattr(app, "uninitialized", False):
            return COLOR_PAUSED          # DLL 不在线：橙，表示没在抓
        return COLOR_OK
    except Exception:  # noqa: BLE001
        return COLOR_OK


class Tray:
    def __init__(self, app):
        self.app = app
        self._icon = None

    # ---- 菜单动作 ----
    def _open_web(self, icon=None, item=None) -> None:
        webbrowser.open(self.app.web_url)

    def _toggle_replies(self, icon=None, item=None) -> None:
        self.app.set_replies(not self.app.replies_enabled)

    def _pause(self, icon=None, item=None) -> None:
        self.app.set_paused(not self.app.paused)

    def _restore(self, icon=None, item=None) -> None:
        import threading
        threading.Thread(target=self.app.restore_normal, name="restore", daemon=True).start()

    def _quit(self, icon=None, item=None) -> None:
        self.app.request_quit()

    # ---- 运行 ----
    def run(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem(lambda i: self.app.status_line(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开管理页", self._open_web, default=True),
            pystray.MenuItem("自动回复", self._toggle_replies,
                             checked=lambda i: self.app.replies_enabled),
            pystray.MenuItem("暂停抓取", self._pause,
                             checked=lambda i: self.app.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("完全恢复（关微信 + 移除 hook）", self._restore),
            pystray.MenuItem("退出", self._quit),
        )
        self._icon = pystray.Icon("WeChatFerryTool", _make_image(_state_color(self.app)),
                                  "WeChatFerry 消息助手", menu)
        log.info("托盘已启动（右键图标打开菜单）")
        import threading
        threading.Thread(target=self._refresh_icon, name="tray-icon", daemon=True).start()
        self._icon.run()

    def _refresh_icon(self) -> None:
        """每 5 秒按状态刷新图标颜色与悬停标题（绿/橙/灰/红）。"""
        import time
        last = None
        while self._icon is not None:
            try:
                color = _state_color(self.app)
                if color != last:
                    last = color
                    self._icon.icon = _make_image(color)
                    self._icon.title = "WeChatFerry 消息助手\n" + self.app.status_line()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
