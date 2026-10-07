r"""version.dll 部署（含自提权）。

为什么需要提权
--------------
微信装在 `C:\Program Files\...`，写入需要管理员权限。但**日常运行不需要**，
所以主程序不加 UAC 清单，只在真正要写文件时用 `runas` 拉起自己一次。

两段式结构
----------
    主进程（不提权） deploy_orchestrate()
        └─ 满足条件时 ShellExecuteExW("runas", <exe> --deploy-dll)   ← 一次 UAC
             └─ 提权后的同一份程序进入 worker_main()
                  校验版本 → 校验哈希 → 备份同名文件 → 复制 → 写记录

安全设计
--------
* **绝不擅自关闭微信**：文件被占用就报错让你自己退出（返回码 4）。
* **版本不符一律拒绝复制**（返回码 2）—— hook DLL 的偏移与微信版本强绑定，
  跨版本复制可能导致微信启动异常。
* 覆盖前**先改名备份**已有的同名文件，不直接删。
* 结果同时返回退出码和 `data/deploy_result.json`，便于向导展示原因。
"""
from __future__ import annotations

import ctypes
import json
import shutil
import sys
import time
from ctypes import wintypes
from pathlib import Path

import paths
import setup_env

HOOK_DLL_NAME = "version.dll"

# 退出码
RC_OK = 0
RC_BAD_ARGS = 1
RC_VERSION_MISMATCH = 2
RC_VENDOR_HASH_BAD = 3
RC_TARGET_LOCKED = 4
RC_COPY_FAILED = 5
RC_NO_VENDOR = 6

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.GetExitCodeProcess.restype = wintypes.BOOL
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


# ---------------------------------------------------------------------------
def _result_path() -> Path:
    return paths.data_root() / "data" / "deploy_result.json"


def _write_result(ok: bool, action: str, rc: int, **extra) -> None:
    rec = {"ok": ok, "action": action, "rc": rc, "at": int(time.time())}
    rec.update(extra)
    try:
        p = _result_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_result() -> dict | None:
    try:
        return json.loads(_result_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# 计划（只读，供向导展示"将会做什么"）
# ---------------------------------------------------------------------------
def plan() -> dict:
    manifest = setup_env.load_manifest()
    wechat_dir = Path((manifest.get("hook_dll") or {}).get("target_dir")
                      or r"C:\Program Files\Tencent\Weixin")
    wx = setup_env.wechat_info(wechat_dir, manifest)
    hook = setup_env.hook_dll_state(wechat_dir, manifest)

    needs = hook["state"] in ("missing", "mismatch")
    steps: list[str] = []
    blockers: list[str] = []
    if not wx["installed"]:
        blockers.append(f"微信未安装：{wechat_dir}")
    elif not wx["version_ok"]:
        blockers.append(f"微信版本 {wx['version']} ≠ 要求 {wx['expected']} —— 拒绝部署")
    if hook["state"] == "no_vendor":
        blockers.append("随包缺少 vendor/version.dll")
    if hook["state"] == "vendor_hash_bad":
        blockers.append("vendor/version.dll 哈希与清单不符")

    if needs and not blockers:
        if hook["target_present"]:
            steps.append(f"备份现有 {HOOK_DLL_NAME} → {HOOK_DLL_NAME}.bak-<时间戳>")
        steps.append(f"复制 vendor\\{HOOK_DLL_NAME} → {hook['target']}")
        steps.append("写 data\\deploy_result.json 记录版本与哈希")
        steps.append("需要一次管理员授权（UAC）")

    return {
        "state": hook["state"],
        "needs_deploy": needs and not blockers,
        "steps": steps,
        "blockers": blockers,
        "wechat": wx,
        "hook": hook,
        "requires_elevation": needs and not blockers,
    }


# ---------------------------------------------------------------------------
# worker：提权后的实际动作
# ---------------------------------------------------------------------------
def worker_main() -> int:
    """在提权进程里执行部署。返回退出码，同时写 deploy_result.json。"""
    manifest = setup_env.load_manifest()
    hook_cfg = manifest.get("hook_dll") or {}
    target_dir = Path(hook_cfg.get("target_dir") or r"C:\Program Files\Tencent\Weixin")
    vendor = paths.vendor_root() / HOOK_DLL_NAME
    target = target_dir / HOOK_DLL_NAME
    want_hash = str(hook_cfg.get("sha256") or "").upper()

    if not vendor.is_file():
        _write_result(False, "no_vendor", RC_NO_VENDOR, error=f"缺少 {vendor}")
        return RC_NO_VENDOR

    # 1) 版本闸门
    wx = setup_env.wechat_info(target_dir, manifest)
    if not wx["installed"]:
        _write_result(False, "wechat_missing", RC_VERSION_MISMATCH,
                      error=f"微信未安装：{target_dir}")
        return RC_VERSION_MISMATCH
    if not wx["version_ok"]:
        _write_result(False, "version_mismatch", RC_VERSION_MISMATCH,
                      error=f"微信版本 {wx['version']} ≠ 要求 {wx['expected']}，拒绝部署",
                      actual=wx["version"], expected=wx["expected"])
        return RC_VERSION_MISMATCH

    # 2) 随包 DLL 自检
    vendor_hash = setup_env.sha256_file(vendor)
    if want_hash and vendor_hash != want_hash:
        _write_result(False, "vendor_hash_bad", RC_VENDOR_HASH_BAD,
                      error="vendor/version.dll 与 manifest 的 sha256 不符",
                      actual=vendor_hash, expected=want_hash)
        return RC_VENDOR_HASH_BAD

    # 3) 已就位就什么都不做
    if target.is_file() and setup_env.sha256_file(target) == vendor_hash:
        _write_result(True, "none", RC_OK, note="目标已是同一份文件，无需部署")
        return RC_OK

    # 4) 备份已有同名文件（不删）
    backup = None
    if target.is_file():
        backup = target.with_name(f"{HOOK_DLL_NAME}.bak-{int(time.time())}")
        try:
            shutil.move(str(target), str(backup))
        except OSError as e:
            _write_result(False, "backup_failed", RC_COPY_FAILED, error=str(e))
            return RC_COPY_FAILED

    # 5) 复制
    try:
        shutil.copy2(vendor, target)
    except PermissionError as e:
        # 最常见：微信正在运行，文件被占用。这里不擅自关微信。
        if backup is not None:
            try:
                shutil.move(str(backup), str(target))
            except OSError:
                pass
        _write_result(False, "target_locked", RC_TARGET_LOCKED,
                      error=f"目标文件被占用（{e}）。请完全退出微信后重试。")
        return RC_TARGET_LOCKED
    except OSError as e:
        _write_result(False, "copy_failed", RC_COPY_FAILED, error=str(e))
        return RC_COPY_FAILED

    # 6) 复检
    got = setup_env.sha256_file(target)
    if got != vendor_hash:
        _write_result(False, "verify_failed", RC_COPY_FAILED,
                      error="复制后哈希不一致", actual=got, expected=vendor_hash)
        return RC_COPY_FAILED

    _write_result(True, "copied", RC_OK, target=str(target), backup=str(backup) if backup else None,
                  sha256=got, wechat_version=wx["version"])
    return RC_OK


# ---------------------------------------------------------------------------
# 编排：必要时自提权
# ---------------------------------------------------------------------------
def _worker_cmdline() -> tuple[str, str]:
    if paths.is_frozen():
        return sys.executable, "--deploy-dll"
    entry = paths.base_root() / "app_exe.py"
    return sys.executable, f'"{entry}" --deploy-dll'


def run_elevated(timeout_s: float = 300) -> tuple[int | None, str]:
    """用 runas 拉起自己执行 --deploy-dll。返回 (退出码, 说明)。"""
    exe, params = _worker_cmdline()
    sei = _SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.lpDirectory = str(paths.base_root())
    sei.nShow = SW_SHOWNORMAL

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err == 1223:      # ERROR_CANCELLED
            return None, "已取消管理员授权"
        return None, f"无法启动提权进程，Win32={err}"

    try:
        _k32.WaitForSingleObject(sei.hProcess, int(timeout_s * 1000))
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code)):
            return None, "拿不到提权进程退出码"
        return int(code.value), "ok"
    finally:
        if sei.hProcess:
            _k32.CloseHandle(sei.hProcess)


def deploy_orchestrate(elevate: bool = True) -> dict:
    """主进程侧入口：先看计划，需要且允许时才提权执行。"""
    p = plan()
    if not p["needs_deploy"]:
        if p["blockers"]:
            return {"ok": False, "action": "blocked", "plan": p, "error": "；".join(p["blockers"])}
        return {"ok": True, "action": "none", "plan": p}
    if not elevate:
        return {"ok": False, "action": "needs_elevation", "plan": p,
                "error": "需要管理员权限（未授权提权）"}

    rc, note = run_elevated()
    result = read_result() or {}
    if rc is None:
        return {"ok": False, "action": "elevation_failed", "plan": p,
                "error": note, "result": result}
    return {"ok": rc == RC_OK, "action": result.get("action", "unknown"),
            "rc": rc, "plan": p, "result": result}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if "--deploy-dll" in sys.argv:
        sys.exit(worker_main())
    # 默认只打印计划，不动任何文件
    print(json.dumps(plan(), ensure_ascii=False, indent=2))
