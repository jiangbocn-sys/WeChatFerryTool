"""WeChatFerry 消息助手 —— 应用入口。

启动顺序
--------
1. 单实例检查（命名互斥体）
2. 起 consumer 的 HTTP server（:8888，线程）—— 先起，微信一登录就能注册回调
3. 起 Web 管理后台（:6060，线程）
4. 启动微信 → 进程一出现就抢注 keyhook（赶在它打开数据库之前）
5. consumer 等到 DLL 就绪后自动注册回调
6. 托盘常驻

退出顺序（安全优先）
--------------------
1. **先关自动回复** —— 保证此后不会再发出任何消息
2. 停 consumer → 停 Web → 停托盘
3. 按配置决定是否"完全恢复"（关微信 + 移走 version.dll）

默认退出**不动微信**：只停抓取，微信继续正常运行（下次启动 app 直接复用）。
需要彻底恢复时用 `--restore` 或托盘菜单里的「完全恢复」。
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

from paths import (  # noqa: E402
    active_account,
    account_self_wxid,
    base_root,
    data_root,
    ensure_data_dirs,
    migrate_legacy_data,
    resource_root,
    use_account,
)
import paths as pathmod  # noqa: E402
import paths  # noqa: E402  （发现已有数据时要用 use_account）
import setup_env  # noqa: E402


def _argv_value(flag: str) -> str | None:
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


_migrated: list[str] = []


def _warn(msg: str) -> None:
    """启动早期（日志还没配好、甚至没有控制台）的安全告警。

    ⚠️ `--windowed` 打包时 `sys.stdout` / `sys.stderr` 都是 **None**，
    直接 `print(..., file=sys.stderr)` 会抛 AttributeError —— 表现就是"双击 exe 没反应"。
    """
    logging.getLogger("app.main").warning(msg)
    try:
        if sys.stderr is not None:
            print(f"[warn] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _fallback_data_root() -> str | None:
    """数据根（exe 所在目录 / 账号目录）写不了时的退路：用户目录下的 APPDATA。

    打包成 exe 后如果被装在 Program Files 这类**只读目录**，账号目录建不出来。
    原先这会直接抛 PermissionError，表现是"双击 exe 闪一下就没反应"。
    这里退回一个可写位置并明确告知用户。
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        return None
    p = Path(base) / "WeChatFerryTool"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return str(p)


def _is_plausible_root(d: Path) -> bool:
    """排除明显不该当"项目根"的目录：盘根（D:\\）、用户主目录（C:\\Users\\xxx）、Windows 目录。

    否则 exe 放在桌面时，往上找可能把整个 D:\\ 或 C:\\Users\\bojia 当成数据根。
    """
    try:
        if d.parent == d:                                   # 盘根
            return False
        if d == Path.home():
            return False
        if d.name.lower() in ("windows", "system32", "program files", "program files (x86)",
                              "programdata", "users", "desktop", "documents", "downloads"):
            return False
        if d.parent.name.lower() == "users":                # C:\Users\<用户名>
            return False
    except OSError:
        return False
    return True


def _has_existing_data(d: Path) -> bool:
    """这个目录看起来"已经有一套数据"吗？（纯只读探测，不创建任何东西）

    认三种布局：
    * `<d>/config.yaml`                  —— 单目录布局
    * `<d>/accounts/<某账号>/config.yaml` —— 账号隔离布局
    * `<d>/data/messages.db`             —— 至少有消息库
    """
    try:
        if (d / "config.yaml").is_file() or (d / "data" / "messages.db").is_file():
            return True
        accounts = d / "accounts"
        if accounts.is_dir():
            for sub in accounts.iterdir():
                if sub.is_dir() and ((sub / "config.yaml").is_file()
                                     or (sub / "data" / "messages.db").is_file()):
                    return True
    except OSError:
        return False
    return False


def _discover_existing_root() -> Path | None:
    """启动时**先找已有的配置/数据**，别一上来就把自己当全新安装。

    为什么需要：冻结后数据根 = exe 所在目录，而用户常把 exe 放在
    `项目\\dist\\WeChatFerryApp\\` 里，真实 config.yaml / messages.db 却在项目目录（往上两三层）。
    以前会直接在 dist 下新建一套空数据 → 平台显示"未配置/没有消息"，看着像数据丢了。

    查找顺序（就近优先）：
        1. exe（或源码项目）所在目录
        2. 往上最多 4 层（覆盖 `项目\\dist\\WeChatFerryApp\\` 这种层级）
        3. 当前工作目录 —— **只在它是"起点本身或起点之下"时才用**，
           否则会把一堆无关的上层目录拉进来（踩过：cwd 在 D:\\projects 时，
           连它上面都被当成候选）。
    """
    seen: set[Path] = set()
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        start = Path(sys.executable).resolve().parent
    else:
        start = Path(__file__).resolve().parent.parent      # 源码：项目根
    p: Path | None = start
    for _ in range(5):                                       # 自己 + 往上 4 层
        if p is None:
            break
        candidates.append(p)
        p = p.parent
    try:
        cwd = Path.cwd().resolve()
        if cwd == start or start in cwd.parents:             # cwd 在起点之下才纳入
            candidates.append(cwd)
    except OSError:
        pass

    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if not _is_plausible_root(c):
            continue
        if _has_existing_data(c):
            return c
    return None


def _bootstrap_account() -> str | None:
    """决定数据根。

    **必须在导入 consumer / web 之前调用** —— 它们的 PROJECT_DIR 在导入时就求值了。

    优先级：
        1. `WCF_DATA_DIR`（显式指定，最高；`--data-dir` 会先转成它）
        2. `--no-multi-account` 逃生开关（→ 基目录）
        3. **发现已有数据**：找旁边/上层的已有 config.yaml 或 messages.db，找到就用它
           （有账号目录就用那个账号，否则整个目录当数据根）
        4. 按微信账号目录隔离（`accounts/<slug>/`）
        5. 都没有 → 基目录（全新安装，走初始化向导）
    """
    if os.environ.get("WCF_DATA_DIR"):
        return None                                   # 显式指定数据根，不做隔离
    if os.environ.get("WCF_NO_MULTI_ACCOUNT") or "--no-multi-account" in sys.argv:
        return None                                   # 逃生开关

    # ---- 3) 先找已有数据，别当全新安装 ----
    found = _discover_existing_root()
    if found is not None:
        accounts_dir = found / "accounts"
        subdirs: list[Path] = []
        if accounts_dir.is_dir():
            try:
                subdirs = [d for d in accounts_dir.iterdir()
                           if d.is_dir() and ((d / "config.yaml").is_file()
                                              or (d / "data" / "messages.db").is_file())]
            except OSError:
                subdirs = []
        if subdirs:
            want = _argv_value("--account")
            chosen = next((d for d in subdirs if d.name == want), None) if want else None
            if chosen is None:
                chosen = max(subdirs, key=lambda d: d.stat().st_mtime)
            _warn(f"发现已有数据：{chosen}（--account 可指定其它账号，--data-dir 可指定别处）")
            try:
                # ⚠️ 必须把发现到的**绝对路径**传进去：冻结后 base_root() 是 exe 所在目录，
                # 只给 slug 会让 use_account() 去 exe 旁边新建一套空数据（踩过）。
                paths.use_account(chosen.name, root=chosen)
                # 同样地，config.yaml 也要从发现的目录找（否则会找 dist\App\config.yaml）
                paths.set_config_root(found)
                ensure_data_dirs()
                return chosen.name
            except OSError as e:
                _warn(f"该目录不可写（{e}），改走其它方式")
        else:
            os.environ["WCF_DATA_DIR"] = str(found)
            paths.set_config_root(found)
            _warn(f"发现已有配置/数据：{found}（本次以它为数据根）")
            return None

    # ---- 4) 按微信账号目录隔离 ----
    slug = _argv_value("--account")
    if not slug:
        acc = active_account()
        if not acc:
            return None                               # 找不到微信账号目录 → 用基目录
        slug = acc["slug"]
    try:
        fresh = not (pathmod.account_root(slug) / "data" / "messages.db").exists()
        use_account(slug)
        ensure_data_dirs()
        if fresh:
            _migrated.extend(migrate_legacy_data(slug))
        return slug
    except OSError as e:
        # 目录不可写（只读安装位置 / 权限不足）：退回用户目录，别让 app 起不来
        alt = _fallback_data_root()
        msg = (f"数据目录不可写（{e}）")
        if alt:
            os.environ["WCF_DATA_DIR"] = alt
            msg += f" → 已改用 {alt}（可用 --data-dir 指定其它目录）"
        else:
            msg += " → 也找不到可写的备用目录，请用 --data-dir 指定一个"
        _warn(msg)
        return None


_ACCOUNT_SLUG = _bootstrap_account()
PROJECT_DIR = data_root()

# sys.path 要用「基目录/资源目录」，不是数据根 —— 后者现在是账号目录
sys.path.insert(0, str(base_root()))
sys.path.insert(0, str(resource_root()))

from app import tray as tray_mod  # noqa: E402
from app.supervisor import Supervisor  # noqa: E402
from consumer.main import ConfigError, Consumer, load_config  # noqa: E402

LOG_DIR = PROJECT_DIR / "logs"
MUTEX_NAME = "Local\\WeChatFerryToolApp"
ERROR_ALREADY_EXISTS = 183

log = logging.getLogger("app.main")


# ---------------------------------------------------------------------------
def _force_utf8_stdio() -> None:
    """把控制台 stdout/stderr 切到 UTF-8 容错。

    为什么必须在 **`argparse` 之前**调用：
      * Windows 控制台默认 GBK，而 `--help` 的帮助文本里有 `⚠` 这类字符，
        argparse 打印帮助时直接 `UnicodeEncodeError` 崩掉（实测：冻结版
        `WeChatFerryApp.exe --help` 退出码非 0、连用法都看不到）；
      * 同理，日志里的警告符号也会让 StreamHandler 报 "--- Logging error ---"。
    `errors="replace"` 是兜底：真遇到编不出来的字符也只显示成 `?`，绝不抛异常。
    --windowed 打包时这两个流是 None，直接跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def _setup_logging() -> None:
    """配好根 logger：控制台 + 文件。**文件日志写不了也不能让 app 起不来**。

    打包成 exe 后它可能在只读目录里跑（或 logs/ 被占用），原先这里直接
    `mkdir` + `FileHandler`，一失败就抛异常 —— 表现是"双击 exe 闪一下就没反应"。
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 控制台编码兜底（日志里的 ⚠️ 之类会让 StreamHandler 抛 UnicodeEncodeError）
    _force_utf8_stdio()
    if not root.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        if sys.stdout is not None:       # --windowed 打包没有控制台
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            root.addHandler(sh)
    already = any(isinstance(h, logging.FileHandler) and "app.log" in str(getattr(h, "baseFilename", ""))
                  for h in root.handlers)
    if not already:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            root.addHandler(fh)
        except OSError as e:
            # 退化运行：没有文件日志，但 app 照常起
            _warn(f"无法写日志文件 {LOG_DIR / 'app.log'}（{e}），本次只输出到控制台")


def _single_instance() -> bool:
    """命名互斥体防重复启动。返回 True 表示本次是唯一实例。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ctypes.set_last_error(0)
    # 句柄故意不释放：进程活着期间一直持有
    k32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
class App:
    def __init__(self, cfg: dict, args: argparse.Namespace):
        self.cfg = cfg
        self.args = args
        self.app_cfg = cfg.get("app") or {}
        hook_cfg = cfg.get("hook") or {}

        self.supervisor = Supervisor(self.app_cfg)
        self._account_warned = False

        # 没有可用配置 = **待初始化**状态（不是错误）：
        # 只跑 Web 向导，不抓取、不采密钥、不回复、不启动微信。
        self.uninitialized = not setup_env.config_ready()
        self.consumer = None
        if self.uninitialized:
            log.warning("未检测到可用配置 —— 进入「待初始化」模式："
                        "不抓取、不采密钥、不回复任何消息")
        else:
            # 账号隔离模式下：用账号目录名推导出的 self_wxid 覆盖配置值。
            # 这一步很关键 —— self_wxid 对不上时，你自己发的消息会被判成"别人发的"，
            # 自动回复就会去回你自己的发言（见 consumer/main.py L537/L564）。
            acc_self = account_self_wxid()
            if acc_self and hook_cfg.get("self_wxid") != acc_self:
                log.warning("self_wxid 按账号目录校正为 %r（config.yaml 里是 %r）",
                            acc_self, hook_cfg.get("self_wxid"))
                cfg.setdefault("hook", {})["self_wxid"] = acc_self
                hook_cfg = cfg["hook"]
            try:
                self.consumer = Consumer(cfg)
            except Exception:  # noqa: BLE001
                # 配置写坏了也不能让 app 起不来 —— 退回待初始化，让向导修
                log.exception("consumer 构造失败 —— 退回待初始化模式，请到 Web 向导修正配置")
                self.uninitialized = True
                self.consumer = None

        self.callback_host = str(hook_cfg.get("callback_host") or "127.0.0.1")
        self.callback_port = int(hook_cfg.get("callback_port") or 8888)
        self.web_host = str(self.app_cfg.get("web_host") or "127.0.0.1")
        self.web_port = int(self.app_cfg.get("web_port") or 6060)
        self.web_url = f"http://{self.web_host}:{self.web_port}"

        self.paused = False
        self._stop = threading.Event()
        self._tray: tray_mod.Tray | None = None
        self._web_server = None
        self._consumer_thread: threading.Thread | None = None
        self._started_at = time.time()

        # ---- 自动回复：默认强制关闭 ----
        want_replies = bool(args.replies)
        if self.consumer is not None and not want_replies:
            if self.consumer.replier.cfg.enabled:
                log.warning("配置里自动回复是开启的，但按 app 默认策略**强制关闭**"
                            "（需要时用托盘菜单或 --replies 开启）")
            self.consumer.replier.cfg.enabled = False

    # ---- 自动回复开关 ----
    @property
    def replies_enabled(self) -> bool:
        return bool(self.consumer.replier.cfg.enabled) if self.consumer else False

    def set_replies(self, on: bool) -> None:
        if not self.consumer:
            log.warning("待初始化状态，忽略自动回复开关")
            return
        self.consumer.replier.cfg.enabled = bool(on)
        log.warning("自动回复 → %s%s", "开启" if on else "关闭",
                    "（注意封号风险）" if on else "")

    # ---- 暂停抓取 ----
    # 实现方式：清空 DLL 的回调地址（实测 POST /set_callback 空 body 的语义就是"清空"）
    def set_paused(self, on: bool) -> None:
        if self.uninitialized:
            log.warning("待初始化状态，忽略暂停/恢复抓取")
            return
        if on:
            self.supervisor.register_callback("")
            self.paused = True
            log.info("已暂停抓取（回调已清空）")
        else:
            url = f"http://{self.callback_host}:{self.callback_port}/hook/callback"
            if self.supervisor.register_callback(url):
                self.paused = False
                log.info("已恢复抓取")

    # ---- 状态 ----
    def status_line(self) -> str:
        s = self.supervisor.snapshot()
        mode = {"capture": "抓取模式", "normal": "原始模式", "unknown": "未知"}[s["capture_mode"]]
        if self.paused:
            cap = "已暂停"
        elif s["dll_api_ready"]:
            cap = "抓取中"
        else:
            cap = "等待微信"
        return f"微信:{mode} / {cap} / 自动回复:{'开' if self.replies_enabled else '关'}"

    def snapshot(self) -> dict:
        s = self.supervisor.snapshot()
        s.update({
            "paused": self.paused,
            "replies_enabled": self.replies_enabled,
            "uninitialized": self.uninitialized,
            "web_url": self.web_url,
            "uptime_s": int(time.time() - self._started_at),
        })
        return s

    # ---- 组件启动 ----
    def _pick_web_port(self, tries: int = 10) -> int | None:
        """端口回退：被占用时依次往后试。绝不静默跳过（待初始化时 Web 是唯一入口）。"""
        for i in range(tries):
            p = self.web_port + i
            if _port_free(self.web_host, p):
                if i:
                    log.warning("Web 端口 %d 被占用，自动改用 %d", self.web_port, p)
                return p
        return None

    def _web_failed(self, reason: str) -> None:
        """Web 起不来时必须留下可行动的痕迹，而不是只写一行日志。"""
        log.error("Web 管理后台启动失败：%s", reason)
        try:
            (PROJECT_DIR / "WEB_FAILED.txt").write_text(
                "Web 管理后台未能启动\n"
                f"原因: {reason}\n\n"
                "可尝试：\n"
                f"  1) 关闭占用 {self.web_port} 的程序\n"
                "  2) 用 --web-port 指定其它端口\n"
                "  3) 独立启动: .venv\\Scripts\\python.exe -m web.app --port 6061\n",
                encoding="utf-8")
        except OSError:
            pass

    def start_web(self) -> None:
        try:
            import web.app as webapp
            flask_app = webapp.create_app() if hasattr(webapp, "create_app") else webapp.app
        except Exception as e:  # noqa: BLE001
            self._web_failed(f"加载失败（flask 未安装？模板资源缺失？）: {e}")
            return

        # 把控制器交给 Web：状态卡片因此不仅能显示，还能直接开关
        if hasattr(webapp, "set_app_controller"):
            try:
                webapp.set_app_controller(self)
                log.info("已把 app 控制器注册给 Web（仪表盘状态卡片可操作）")
            except Exception:  # noqa: BLE001
                log.debug("注册控制器失败", exc_info=True)

        port = self._pick_web_port()
        if port is None:
            self._web_failed(
                f"{self.web_host} 上 {self.web_port}~{self.web_port + 9} 全部被占用")
            return
        self.web_port = port
        self.web_url = f"http://{self.web_host}:{port}"

        def _run():
            try:
                flask_app.run(host=self.web_host, port=port,
                              use_reloader=False, threaded=True)
            except Exception as e:  # noqa: BLE001
                self._web_failed(f"服务异常退出: {e}")

        threading.Thread(target=_run, name="web", daemon=True).start()
        log.info("Web 管理后台: %s", self.web_url)
        try:
            (PROJECT_DIR / "WEB_FAILED.txt").unlink()
        except OSError:
            pass

        # 自动打开浏览器；失败只退化提示，不影响服务
        if not getattr(self.args, "no_browser", False):
            threading.Thread(target=self._open_browser, name="open-browser",
                             daemon=True).start()

    def _open_browser(self) -> None:
        time.sleep(1.5)   # 等服务真正监听
        url = self.web_url + ("/setup" if self.uninitialized else "/")
        try:
            import webbrowser
            if webbrowser.open(url):
                log.info("已打开浏览器: %s", url)
                return
        except Exception:  # noqa: BLE001
            log.debug("webbrowser 打开失败", exc_info=True)
        log.warning("无法自动打开浏览器，请手动访问: %s", url)

    def start_consumer(self) -> None:
        if not _port_free(self.callback_host, self.callback_port):
            log.error("回调端口 %d 被占用，consumer 可能已在运行", self.callback_port)
        self._consumer_thread = threading.Thread(
            target=self.consumer.run,
            kwargs={"wait_for_dll": True,
                    "dll_timeout_s": float(self.app_cfg.get("dll_timeout_s", 600))},
            name="consumer",
            daemon=True,
        )
        self._consumer_thread.start()
        log.info("consumer 已启动（等待微信/DLL 就绪）")

    def start_wechat(self) -> None:
        log.info("当前状态: %s", self.status_line())
        if self.supervisor.capture_mode() == "normal":
            log.error("检测到微信处于**原始模式**（version.dll 已被移走）——"
                      "这种状态下抓不到任何消息。"
                      "请用托盘菜单「进入抓取模式」，或手动把 version.dll.disabled 改回 version.dll")
        elif self.supervisor.capture_mode() == "unknown":
            log.error("微信目录里既没有 version.dll 也没有 version.dll.disabled ——"
                      "请确认 hook DLL 是否安装正确: %s", self.supervisor.version_dll)
        self.supervisor.start_wechat()

    # ---- 运行 ----
    def run(self) -> int:
        self.start_web()
        if self.uninitialized:
            log.warning("=== 待初始化模式 ===")
            log.warning("请打开 %s/setup 完成初始化", self.web_url)
            log.warning("初始化完成前：不抓取消息、不采密钥、不自动回复、不启动微信")
        else:
            self.start_consumer()
            self.start_wechat()

        # 监控线程：定期把状态写进日志，便于事后排查
        threading.Thread(target=self._monitor, name="monitor", daemon=True).start()

        if tray_mod.available() and not self.args.no_tray:
            self._tray = tray_mod.Tray(self)
            try:
                self._tray.run()          # 阻塞在托盘消息循环
            except Exception:  # noqa: BLE001
                # 托盘起不来（缺 explorer / 权限被限制等）→ 退回无界面后台运行。
                # ⚠️ windowed 打包下这样用户就**完全看不到**这个程序了，
                #    所以必须自己把浏览器打开并把找回来的办法写清楚。
                log.exception("托盘启动失败，改为后台运行（无界面）")
                log.warning("== 现在没有托盘图标：app 仍在后台运行，Web 仍在 %s ==", self.web_url)
                log.warning("== 想找回它：浏览器打开 %s；要彻底退出请用任务管理器结束 %s ==",
                            self.web_url, Path(sys.executable).name)
                self._console_loop()
        else:
            if not tray_mod.available():
                log.warning("未安装 pystray/Pillow，运行在控制台模式。"
                            "想要托盘：pip install pystray Pillow")
            self._console_loop()
        return 0

    def _console_loop(self) -> None:
        log.info("按 Ctrl+C 退出。状态: %s", self.web_url)
        try:
            while not self._stop.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass

    def _monitor(self) -> None:
        last = ""
        while not self._stop.is_set():
            try:
                line = self.status_line()
                if line != last:
                    log.info("状态: %s", line)
                    last = line
                self._write_state_file()
                self._check_account()
            except Exception:  # noqa: BLE001
                log.debug("状态采集失败", exc_info=True)
            time.sleep(5.0)

    def _check_account(self) -> None:
        """运行中检出微信账号被切换 → 告警并暂停抓取，避免两个账号数据混装。"""
        cur = pathmod.current_account()
        if not cur:
            return
        acc = pathmod.active_account()
        if not acc or acc["slug"] == cur:
            return
        if self._account_warned:
            return
        self._account_warned = True
        log.error("检测到微信账号已切换：当前数据根属于 %r，但活跃账号是 %r。"
                  "为避免数据混装，已暂停抓取。请重启 app 以切到该账号的独立数据目录。",
                  cur, acc["slug"])
        try:
            self.set_paused(True)
        except Exception:  # noqa: BLE001
            log.debug("暂停抓取失败", exc_info=True)

    def _write_state_file(self) -> None:
        """把状态写到 data/app_state.json。

        这样即使 Web 是独立进程（web.sh）跑起来的，仪表盘也能显示 app 状态；
        控制仍然需要同进程（独立 Web 会在卡片上提示）。
        """
        import json
        try:
            st = self.snapshot()
            st["_ts"] = time.time()
            path = PROJECT_DIR / "data" / "app_state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.debug("写状态文件失败", exc_info=True)

    # ---- 完全恢复 ----
    def restore_normal(self) -> None:
        """关微信 → 移走 version.dll → 以原始状态重启微信。

        这是耗时操作（要关掉再拉起微信），调用方负责放到后台线程。
        """
        log.warning("收到「完全恢复」请求：关闭微信 → 移除 hook → 原始状态重启")
        try:
            if self.replies_enabled:
                self.set_replies(False)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.consumer.stop()
        except Exception:  # noqa: BLE001
            log.debug("停止 consumer 异常", exc_info=True)
        ok = self.supervisor.restore_normal(restart_wechat=True)
        log.info("完全恢复%s", "完成" if ok else "失败（详见日志）")

    # ---- 退出 ----
    def request_quit(self) -> None:
        """请求退出。

        ⚠️ **必须顺手停掉托盘**：`_tray.run()` 阻塞在 Win32 消息循环里，只有
        `_console_loop()` 会看 `_stop`。以前这里只 `self._stop.set()`，
        结果从托盘菜单点「退出」时，主线程永远卡在托盘消息循环 →
        `app.run()` 不返回 → `shutdown()` 永不执行 → **app 退不出来**（图标还在）。
        """
        self._stop.set()
        try:
            self._tray.stop()
        except Exception:  # noqa: BLE001
            log.debug("停托盘失败", exc_info=True)

    def shutdown(self, restore: bool | None = None) -> None:
        log.info("=== 退出中 ===")
        # 0) 先让托盘消息循环退出来（否则 run() 不返回，后面都执行不到）
        self._stop.set()
        try:
            self._tray.stop()
        except Exception:  # noqa: BLE001
            log.debug("停托盘异常", exc_info=True)
        # 1) 最先关自动回复 —— 保证退出过程中不会再发任何消息
        try:
            if self.replies_enabled:
                self.set_replies(False)
        except Exception:  # noqa: BLE001
            pass

        # 2) 停托盘
        if self._tray is not None:
            self._tray.stop()

        # 3) 停 consumer（内部会关 store）
        self._stop.set()
        try:
            self.consumer.stop()
        except Exception:  # noqa: BLE001
            log.debug("停止 consumer 异常", exc_info=True)
        if self._consumer_thread is not None:
            self._consumer_thread.join(timeout=8)

        log.info("抓取已停止。微信保持运行（未改动 version.dll）")

        # 4) 可选：完全恢复
        do_restore = self.args.restore if restore is None else restore
        if do_restore:
            self.supervisor.restore_normal(restart_wechat=True)
        else:
            log.info("如需让微信彻底回到原始未 hook 状态，用 --restore 或托盘菜单「完全恢复」")
        log.info("=== 已退出 ===")


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    # 必须在 argparse 之前：否则 `--help` 里的 ⚠ 会在 GBK 控制台上
    # UnicodeEncodeError 崩掉（冻结版实测踩到）。
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(description="WeChatFerry 消息助手（先开 app，再管微信）")
    ap.add_argument("--replies", action="store_true",
                    help="启动时就启用自动回复（默认强制关闭）")
    ap.add_argument("--restore", action="store_true",
                    help="退出时完全恢复：关微信 + 移走 version.dll + 重启微信")
    ap.add_argument("--no-tray", action="store_true", help="不启用托盘（控制台模式）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--no-launch", action="store_true", help="不自动启动微信，只等待")
    ap.add_argument("--inject-keys", action="store_true",
                    help="显式开启 keyhook 注入以采集数据库密钥。"
                         "警告：若在微信开库前注入，可能让微信读不出消息库"
                         "（对话历史变空白）。默认关闭；抓消息不需要它。")
    ap.add_argument("--account", default=None,
                    help="指定微信账号目录名（多账号隔离用；默认自动取最近活跃账号）")
    ap.add_argument("--no-multi-account", action="store_true",
                    help="禁用按账号隔离，回到单目录布局")
    args = ap.parse_args(argv)

    _setup_logging()

    if _ACCOUNT_SLUG:
        log.info("账号隔离已启用：账号=%s  数据根=%s", _ACCOUNT_SLUG, PROJECT_DIR)
        if _migrated:
            log.info("已把基目录下旧的 data/logs/reports 复制进账号目录（原件保留，可自行删除）：%s",
                     ", ".join(_migrated[:8]) + ("…" if len(_migrated) > 8 else ""))
    elif not os.environ.get("WCF_DATA_DIR"):
        log.warning("未找到微信账号目录，数据根退回基目录: %s（此模式下换账号会混装）",
                    PROJECT_DIR)

    # 把工作目录切到数据根：config.yaml / data / logs / reports 都存在这里。
    # 冻结运行时数据根 = exe 所在目录，源码运行时 = 项目根目录。
    try:
        os.chdir(PROJECT_DIR)
        log.info("工作目录: %s", PROJECT_DIR)
    except OSError as e:
        log.warning("切换工作目录失败: %s", e)

    if not _single_instance():
        _warn("已经有一个实例在运行（或遗留进程）。请先退出它。")
        return 2

    try:
        cfg = load_config()
    except ConfigError as e:
        # 缺配置**不是错误** —— 这是「待初始化」状态，交给 Web 向导处理，不要退出
        log.warning("未找到可用配置（%s）—— 进入待初始化模式", e)
        cfg = {}

    if args.no_launch:
        cfg.setdefault("app", {})["launch_wechat"] = False
    # keyhook 注入默认关闭；只有显式 --inject-keys 才开（并有版本闸门 + 风险提示）
    if args.inject_keys:
        cfg.setdefault("app", {})["auto_inject_keyhook"] = True
        log.warning("已显式开启 keyhook 注入（--inject-keys）："
                    "⚠️ 若在微信打开数据库前注入，可能导致微信读不出消息库、对话历史空白。"
                    "采集完密钥建议用「完全恢复」把 hook 移走。")

    app = App(cfg, args)
    log.info("WeChatFerry 消息助手启动；配置: %s", PROJECT_DIR / "config.yaml")
    log.info("回调: %s:%d  Web: %s", app.callback_host, app.callback_port, app.web_url)

    import signal

    def _sig(_sig, _frm):
        log.info("收到退出信号")
        app.request_quit()

    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except ValueError:
        pass

    try:
        app.run()
    finally:
        try:
            app.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("退出清理异常（继续退出）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
