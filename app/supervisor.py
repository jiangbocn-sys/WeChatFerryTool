"""Supervisor —— 负责微信进程的生命周期、keyhook 注入、回调注册与"恢复正常"。

职责边界（重要）
----------------
* 消息抓取、评分、自动回复、归档都**不在**这里，而在 consumer 里。
* 本模块只做"让微信以正确方式跑起来/停下来"这件事：
    - 启动微信 → 抢在开库前注入 keyhook（否则采不到密钥）
    - 等 DLL 的 HTTP API 就绪 → 注册回调
    - 退出时按需把 version.dll 移走，让微信回到**原始未 hook**状态

关于 version.dll
----------------
微信原版安装目录**并不包含** version.dll（另一版本的完整目录备份可证）。
该文件是 hook 本体：它利用 Windows "应用目录优先" 的 DLL 搜索顺序被加载，
再把系统 version.dll 的导出转发出去。所以：

    抓取模式 =  version.dll 存在
    正常模式 =  version.dll 不存在（改名成 version.dll.disabled 即可）

两个方向的切换都要求**微信完全退出**（文件被占用时改不了名）。
"""
from __future__ import annotations

import ctypes
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

from paths import vendor_root

log = logging.getLogger("app.supervisor")

# ---------------------------------------------------------------------------
# 默认路径（都可在 config.yaml 的 app: 段覆盖）
# ---------------------------------------------------------------------------
DEFAULT_WECHAT_DIR = Path(r"C:\Program Files\Tencent\Weixin")
# 随包组件：不能再硬编码开发机上的 D:\projects\... 路径，否则换机器/分发即失效
DEFAULT_KEYHOOK_DLL = vendor_root() / "keyhook3.dll"
HOOK_API = "http://127.0.0.1:30001"

VERSION_DLL_NAME = "version.dll"
DISABLED_SUFFIX = ".disabled"
WECHAT_EXE_NAME = "Weixin.exe"


# ---------------------------------------------------------------------------
# 纯 ctypes 注入：CreateRemoteThread(LoadLibraryW)
# ---------------------------------------------------------------------------
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_READWRITE = 0x04

_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.VirtualAllocEx.restype = wintypes.LPVOID
_k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                                wintypes.DWORD, wintypes.DWORD]
_k32.WriteProcessMemory.restype = wintypes.BOOL
_k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.GetModuleHandleW.restype = wintypes.HMODULE
_k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_k32.GetProcAddress.restype = wintypes.LPVOID
_k32.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]
_k32.CreateRemoteThread.restype = wintypes.HANDLE
_k32.CreateRemoteThread.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                                    wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.VirtualFreeEx.restype = wintypes.BOOL
_k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                               wintypes.DWORD]


def inject_dll(pid: int, dll_path: str, timeout_s: float = 15.0) -> None:
    """把 dll_path 注入 pid。失败抛 RuntimeError（带 Win32 错误码）。"""
    dll = str(Path(dll_path).resolve())
    if not Path(dll).is_file():
        raise RuntimeError(f"DLL 不存在: {dll}")

    h = _k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        err = ctypes.get_last_error()
        hint = ""
        if err == 5:
            hint = ("（拒绝访问：本进程的权限或完整性级别不足以操作目标进程。"
                    "常见原因：app 以受限权限运行，或需要以管理员身份运行）")
        raise RuntimeError(f"OpenProcess(PID={pid}) 失败，Win32={err}{hint}")

    try:
        buf = (dll + "\x00").encode("utf-16-le")
        size = len(buf)
        remote = _k32.VirtualAllocEx(h, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not remote:
            raise RuntimeError(f"VirtualAllocEx 失败，Win32={ctypes.get_last_error()}")

        written = ctypes.c_size_t(0)
        if not _k32.WriteProcessMemory(h, remote, buf, size, ctypes.byref(written)):
            raise RuntimeError(f"WriteProcessMemory 失败，Win32={ctypes.get_last_error()}")

        hk = _k32.GetModuleHandleW("kernel32.dll")
        load_library = _k32.GetProcAddress(hk, b"LoadLibraryW")
        if not load_library:
            raise RuntimeError("拿不到 LoadLibraryW 地址")

        th = _k32.CreateRemoteThread(h, None, 0, load_library, remote, 0, None)
        if not th:
            raise RuntimeError(
                f"CreateRemoteThread 失败，Win32={ctypes.get_last_error()}"
                "（常见原因：权限不足，请以管理员身份运行）"
            )
        try:
            _k32.WaitForSingleObject(th, int(timeout_s * 1000))
        finally:
            _k32.CloseHandle(th)
        _k32.VirtualFreeEx(h, remote, 0, 0x8000)  # MEM_RELEASE
        log.info("已注入 %s → PID %d", Path(dll).name, pid)
    finally:
        _k32.CloseHandle(h)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------
class Supervisor:
    def __init__(self, app_cfg: dict | None = None):
        cfg = app_cfg or {}
        self.wechat_dir = Path(cfg.get("wechat_dir") or DEFAULT_WECHAT_DIR)
        self.keyhook_dll = Path(cfg.get("keyhook_dll") or DEFAULT_KEYHOOK_DLL)
        self.api_base = str(cfg.get("api_base") or HOOK_API).rstrip("/")
        self.launch_wechat = bool(cfg.get("launch_wechat", True))
        # ⚠️ keyhook 注入**默认关闭**（2026-10-06 事故后改的）：
        # 实测「app 启动微信 → 抢在开库前注入 keyhook3.dll」会让微信**读不出消息库
        # （对话历史空白）**；而在微信已开好库之后再注入则正常。抓消息并不需要 keyhook
        # （消息是靠 vendor/version.dll 的回调推给我们的），keyhook 只用于离线解密采密钥。
        # 要采密钥必须显式开启（--inject-keys 或 app.auto_inject_keyhook: true），
        # 而且会先过版本闸门。
        self.auto_inject = bool(cfg.get("auto_inject_keyhook", False))
        self.inject_timeout_s = float(cfg.get("inject_timeout_s", 600))
        self._wechat_proc: subprocess.Popen | None = None
        self._injected_pid: int | None = None
        self._lock = threading.Lock()
        self.last_callback_url: str | None = None

    # ---- 路径 ----
    @property
    def wechat_exe(self) -> Path:
        return self.wechat_dir / WECHAT_EXE_NAME

    @property
    def version_dll(self) -> Path:
        return self.wechat_dir / VERSION_DLL_NAME

    @property
    def version_dll_disabled(self) -> Path:
        return self.wechat_dir / (VERSION_DLL_NAME + DISABLED_SUFFIX)

    # ---- 抓取/正常 模式 ----
    def capture_mode(self) -> str:
        """'capture' / 'normal' / 'unknown'"""
        if self.version_dll.exists():
            return "capture"
        if self.version_dll_disabled.exists():
            return "normal"
        return "unknown"

    def wechat_running(self) -> bool:
        return self._find_main_pid() is not None

    # ---- 进程发现 ----
    def _find_main_pid(self) -> int | None:
        """找 Weixin.exe 主进程（排除 --type= 渲染子进程与 crashpad）。"""
        if self._wechat_proc and self._wechat_proc.poll() is None:
            return self._wechat_proc.pid
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='Weixin.exe'\" | "
                 "Where-Object { $_.CommandLine -and $_.CommandLine -notmatch '--type=' "
                 "-and $_.CommandLine -notmatch '--crashpad-handler' } | "
                 "Select-Object -First 1 -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            pid = out.stdout.strip()
            return int(pid) if pid.isdigit() else None
        except Exception:  # noqa: BLE001
            return None

    # ---- 启动 ----
    def start_wechat(self, inject_keyhook: bool | None = None) -> int | None:
        """启动微信并在进程出现后立刻注入 keyhook（抢在开库之前）。

        返回主进程 PID；已在运行则返回现有 PID。
        """
        with self._lock:
            pid = self._find_main_pid()
            if pid:
                log.info("微信已在运行，PID=%d", pid)
            else:
                if not self.launch_wechat:
                    log.info("配置为不主动启动微信，等待你自己打开")
                    return None
                if not self.wechat_exe.is_file():
                    log.error("找不到 %s", self.wechat_exe)
                    return None
                log.info("启动微信: %s", self.wechat_exe)
                try:
                    self._wechat_proc = subprocess.Popen([str(self.wechat_exe)],
                                                         cwd=str(self.wechat_dir))
                    pid = self._wechat_proc.pid
                except OSError as e:
                    log.error("启动微信失败: %s", e)
                    return None

            want_inject = self.auto_inject if inject_keyhook is None else inject_keyhook
            if want_inject:
                why = self.inject_safety_error()
                if why:
                    log.warning("跳过 keyhook 注入：%s", why)
                else:
                    # 立刻注入：此时 WeChat 多半还没打开数据库
                    threading.Thread(target=self._inject_when_ready, args=(pid,),
                                     name="keyhook-inject", daemon=True).start()
            return pid

    def inject_safety_error(self) -> str | None:
        """注入前的安全检查。返回不可注入的原因；None = 可以注入。

        **为什么需要**：2026-10-06 实测到"app 启动微信 → 开库前注入 keyhook3.dll"
        会让微信对话历史变成空白（解密被干扰）。所以注入必须显式开启，且
        微信版本要与 manifest 要求一致 —— 版本不符的钩子最容易把库读坏。
        """
        if not self.auto_inject:
            return ("配置未开启 keyhook 自动注入（默认关闭）。"
                    "要采集密钥请用 --inject-keys，或把 app.auto_inject_keyhook 设为 true")
        if not self.keyhook_dll.is_file():
            return f"keyhook DLL 不存在: {self.keyhook_dll}"
        try:
            import setup_env
            manifest = setup_env.load_manifest()
            wx = setup_env.wechat_info(self.wechat_dir, manifest)
            if not wx.get("installed"):
                return f"没找到微信主程序：{self.wechat_dir}"
            if not wx.get("version_ok"):
                return (f"微信版本 {wx.get('version')!r} 与要求 {wx.get('expected')!r} 不符 —— "
                        f"版本不符的钩子可能让微信读不出消息库，已拒绝注入")
        except Exception as e:  # noqa: BLE001
            return f"版本检查失败（保守拒绝注入）：{type(e).__name__}: {e}"
        return None

    def _inject_when_ready(self, pid: int) -> None:
        """尽早注入。Weixin.dll 还没加载也没关系——DLL 自己会等。"""
        if not self.keyhook_dll.is_file():
            log.warning("keyhook DLL 不存在，跳过注入: %s", self.keyhook_dll)
            return
        deadline = time.time() + self.inject_timeout_s
        delay = 0.2
        while time.time() < deadline:
            try:
                inject_dll(pid, str(self.keyhook_dll))
                self._injected_pid = pid
                return
            except RuntimeError as e:
                msg = str(e)
                if "OpenProcess" in msg and "拒绝访问" in msg:
                    log.error("%s", msg)
                    return
                # 进程还没建好/还没起来，稍后重试
                time.sleep(delay)
                delay = min(delay * 1.5, 2.0)
        log.error("注入 keyhook 超时（%.0fs）", self.inject_timeout_s)

    # ---- DLL HTTP API ----
    def api_ready(self, timeout_s: float = 1.5) -> dict | None:
        try:
            with urllib.request.urlopen(f"{self.api_base}/QueryDB/status",
                                        timeout=timeout_s) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return None

    def wait_api(self, timeout_s: float = 600) -> bool:
        deadline = time.time() + timeout_s
        last = 0.0
        while time.time() < deadline:
            if self.api_ready():
                return True
            now = time.time()
            if now - last >= 10:
                last = now
                log.info("等待 DLL HTTP API (%s) 就绪…剩余 %ds",
                         self.api_base, int(deadline - now))
            time.sleep(0.5)
        return False

    def register_callback(self, url: str, timeout_s: float = 10) -> bool:
        """注册回调。注意：**绝不能用空 body** —— 空 body 的语义是"清空回调"。"""
        body = json.dumps({"url": url}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.api_base}/set_callback", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
            ok = resp.get("ret") == 0 and resp.get("callback") == url
            if ok:
                self.last_callback_url = url
                log.info("回调已注册: %s", url)
            else:
                log.warning("回调注册返回异常: %s", resp)
            return bool(ok)
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
            log.warning("注册回调失败: %s", e)
            return False

    # ---- 正常模式 / 抓取模式 切换 ----
    def _close_wechat(self, wait_s: float = 30) -> bool:
        """优雅关闭微信；必要时强杀。返回是否已退出。"""
        pid = self._find_main_pid()
        if not pid:
            return True
        log.info("关闭微信（PID=%d）", pid)
        subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if not self._find_main_pid():
                return True
            time.sleep(0.5)
        log.warning("微信未在 %.0fs 内退出，强制结束", wait_s)
        subprocess.run(["taskkill", "/IM", WECHAT_EXE_NAME, "/F", "/T"],
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(2)
        return not self._find_main_pid()

    def _swap_version_dll(self, to_normal: bool) -> bool:
        """移动 version.dll。需要微信已退出，否则文件被占用。"""
        for _ in range(20):
            try:
                if to_normal:
                    if self.version_dll.exists():
                        shutil.move(str(self.version_dll), str(self.version_dll_disabled))
                    return True
                if self.version_dll_disabled.exists():
                    shutil.move(str(self.version_dll_disabled), str(self.version_dll))
                return True
            except OSError as e:
                log.debug("文件仍被占用，重试: %s", e)
                time.sleep(0.5)
        return False

    def restore_normal(self, restart_wechat: bool = True) -> bool:
        """完全恢复：关微信 → 移走 hook 的 version.dll →（可选）重启微信到原始状态。"""
        log.info("=== 完全恢复：让微信回到未 hook 状态 ===")
        if not self._close_wechat():
            log.error("微信没能关闭，无法移动 version.dll")
            return False
        if not self._swap_version_dll(to_normal=True):
            log.error("移动 %s 失败（可能仍被占用）", self.version_dll)
            return False
        log.info("已移除 hook：%s → %s", self.version_dll.name,
                 self.version_dll_disabled.name)
        self._injected_pid = None
        if restart_wechat:
            time.sleep(1)
            self._wechat_proc = None
            self.start_wechat(inject_keyhook=False)
            log.info("微信已以**原始状态**重启")
        return True

    def enter_capture_mode(self, restart_wechat: bool = True) -> bool:
        """回到抓取模式：关微信 → 放回 version.dll → 重启微信。"""
        log.info("=== 进入抓取模式 ===")
        if not self._close_wechat():
            log.error("微信没能关闭，无法恢复 version.dll")
            return False
        if not self._swap_version_dll(to_normal=False):
            log.error("恢复 %s 失败", self.version_dll)
            return False
        log.info("已装回 hook：%s", self.version_dll.name)
        if restart_wechat:
            time.sleep(1)
            self._wechat_proc = None
            self.start_wechat()
        return True

    # ---- 状态 ----
    def snapshot(self) -> dict:
        api = self.api_ready()
        return {
            "capture_mode": self.capture_mode(),
            "wechat_running": self.wechat_running(),
            "wechat_pid": self._find_main_pid(),
            "keyhook_injected_pid": self._injected_pid,
            "dll_api_ready": api is not None,
            "dll_status": api,
            "callback_url": self.last_callback_url,
            "keyhook_dll": str(self.keyhook_dll),
        }
