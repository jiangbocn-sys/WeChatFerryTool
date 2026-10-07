"""初始化环境采集 —— 纯只读，无任何副作用。

阶段 0 的职责：在用户做任何设置之前，先把"当前环境的真实情况"摸清楚，
供初始化向导展示。**这里不写配置、不复制文件、不注入、不抓消息。**

`version.dll` 的操作（复制/备份/提权）在 deploy.py，不在这里。

关键事实（本次实测得到，代码据此设计）
--------------------------------------
* 微信账号目录名形如 `<别名>_<短哈希>`，而 `GetSelfProfile` 的 wxid 字段**每次会话都变**
  （实测 wxid_9099770999011 → wxid_m6l5o2jqseud12 → wxid_0372723722212），
  所以账号身份以**目录名**为准，wxid 只作参考展示。
* hook DLL 的函数偏移与微信版本强绑定 → 必须先比对版本，再谈复制。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

import paths

HOOK_DLL_NAME = "version.dll"
REQUIRED_HOOK_KEYS = ("api_base", "callback_host", "callback_port")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def sha256_file(p: Path, chunk: int = 1 << 20) -> str | None:
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest().upper()
    except OSError:
        return None


def file_version(path: Path) -> str | None:
    """读 PE 文件版本号（用系统 version.dll，不引入新依赖）。"""
    try:
        if not path.is_file():
            return None
        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return None
        ptr = ctypes.c_void_p()
        ln = wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(ln)):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD), ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD), ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD), ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD), ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD), ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD), ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        info = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001
        return None


def load_manifest() -> dict:
    p = paths.vendor_root() / "manifest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# 配置是否就绪
# ---------------------------------------------------------------------------
def config_ready() -> bool:
    """config.yaml 存在且关键字段齐全。不齐就当"未初始化"。"""
    p = paths.config_path()
    if not p.is_file():
        return False
    try:
        import yaml
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return False
    hook = cfg.get("hook") or {}
    return all(hook.get(k) not in (None, "") for k in REQUIRED_HOOK_KEYS)


# ---------------------------------------------------------------------------
# 数据库（只读打开，避免任何写权限要求）
# ---------------------------------------------------------------------------
def _count(db: Path, sql: str) -> int:
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            return int(conn.execute(sql).fetchone()[0])
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return -1


def _labels_count(p: Path) -> int:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return len(data.get("groups") or {}) + len(data.get("senders") or {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return -1


# ---------------------------------------------------------------------------
# 微信 / hook DLL
# ---------------------------------------------------------------------------
def wechat_info(wechat_dir: Path, manifest: dict) -> dict:
    exe = wechat_dir / "Weixin.exe"
    installed = exe.is_file()
    actual = file_version(exe) if installed else None
    expected = str((manifest.get("wechat") or {}).get("version") or "")
    return {
        "installed": installed,
        "dir": str(wechat_dir),
        "version": actual,
        "expected": expected,
        "version_ok": bool(actual and expected and actual == expected),
    }


def hook_dll_state(wechat_dir: Path, manifest: dict) -> dict:
    """目标位置的 version.dll 现状：missing / mismatch / ok / no_vendor / no_manifest。"""
    vendor = paths.vendor_root() / HOOK_DLL_NAME
    target = wechat_dir / HOOK_DLL_NAME
    want = str((manifest.get("hook_dll") or {}).get("sha256") or "").upper()

    out = {
        "target": str(target),
        "vendor": str(vendor),
        "vendor_present": vendor.is_file(),
        "target_present": target.is_file(),
        "vendor_sha256": None,
        "target_sha256": None,
        "expected_sha256": want or None,
        "state": "unknown",
    }
    if not vendor.is_file():
        out["state"] = "no_vendor"          # 随包 DLL 缺失（分发不完整）
        return out
    out["vendor_sha256"] = sha256_file(vendor)
    if want and out["vendor_sha256"] != want:
        out["state"] = "vendor_hash_bad"     # 随包 DLL 与清单不符
        return out
    if not target.is_file():
        out["state"] = "missing"             # 需要部署
        return out
    out["target_sha256"] = sha256_file(target)
    out["state"] = "ok" if out["target_sha256"] == out["vendor_sha256"] else "mismatch"
    return out


def installer_info(manifest: dict) -> dict:
    name = str((manifest.get("wechat") or {}).get("installer") or "")
    p = paths.vendor_root() / name if name else None
    present = bool(p and p.is_file())
    info = {
        "file": name or None,
        "present": present,
        "path": str(p) if present else None,
        "sha256": sha256_file(p) if present else None,
        "download_hint": (manifest.get("wechat") or {}).get("download_hint"),
    }
    want = (manifest.get("wechat") or {}).get("installer_sha256")
    info["expected_sha256"] = want
    info["hash_ok"] = (sha256_file(p) == want) if (present and want) else None
    return info


# ---------------------------------------------------------------------------
# 账号 / 运行时
# ---------------------------------------------------------------------------
def account_info() -> dict:
    accounts = paths.list_accounts()
    acc = accounts[0] if accounts else None
    return {
        "active": acc["slug"] if acc else None,
        "alias": acc["alias"] if acc else None,
        "self_wxid": paths.slug_alias(acc["slug"]) if acc else None,
        "wechat_files_root": str(paths.wechat_files_root() or ""),
        "all": [{"slug": a["slug"], "alias": a["alias"],
                 "last_active": int(a["last_active"])} for a in accounts],
        "note": "账号身份取自 xwechat_files 目录名；GetSelfProfile 的 wxid 每次会话都会变，仅作参考",
    }


def dll_api_info(api_base: str = "http://127.0.0.1:30001") -> dict:
    try:
        with urllib.request.urlopen(f"{api_base}/QueryDB/status", timeout=1.5) as r:
            return {"ready": True, "status": json.loads(r.read().decode("utf-8", "replace"))}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return {"ready": False, "status": None,
                "note": "微信未运行时离线属正常"}


def data_info() -> dict:
    root = paths.data_root()
    data = root / "data"
    keys = paths.base_root() / "Documents" / "wechat-keys.jsonl"   # 占位，实际在文档目录
    return {
        "data_root": str(root),
        "base_root": str(paths.base_root()),
        "account_isolated": paths.current_account() is not None,
        "messages": _count(data / "messages.db", "SELECT count(*) FROM messages"),
        "labels": _labels_count(data / "labels.json"),
        "has_key_map": (data / "db_key_map.json").is_file(),
        "has_keys_file": False,
    }


def llm_ready() -> bool:
    """LLM 是否可用：**只要 base_url 有值就算就绪**。

    不能要求 api_key 非空 —— 本地模型（Ollama / vLLM / LM Studio）通常不需要 key，
    按"必须有 key"判断会把它们误判成"未配置 LLM"从而关掉评分。
    """
    try:
        import yaml
        cfg = yaml.safe_load(paths.config_path().read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return False
    llm = cfg.get("llm") or {}
    base = str(llm.get("base_url") or "").strip()
    if not base:
        return False
    key = str(llm.get("api_key") or "").strip()
    return key not in ("REPLACE_ME",)   # 占位符视为未配置；空 key 允许（本地模型）


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def collect_report() -> dict:
    """采集当前环境。**只读**：不改配置、不复制文件、不注入、不抓消息。"""
    manifest = load_manifest()
    wechat_dir = Path((manifest.get("hook_dll") or {}).get("target_dir")
                      or r"C:\Program Files\Tencent\Weixin")

    rep = {
        "generated": int(time.time()),
        "frozen": paths.is_frozen(),
        "config": {"ready": config_ready(), "path": str(paths.config_path())},
        "wechat": wechat_info(wechat_dir, manifest),
        "hook": hook_dll_state(wechat_dir, manifest),
        "installer": installer_info(manifest),
        "account": account_info(),
        "data": data_info(),
        "dll_api": dll_api_info(),
        "llm": {"ready": llm_ready()},
        "manifest": {"present": bool(manifest),
                     "schema": manifest.get("schema")},
    }

    # 阻塞项：初始化必须解决的
    blockers: list[str] = []
    if not rep["wechat"]["installed"]:
        blockers.append("微信未安装在预期位置：" + rep["wechat"]["dir"])
    elif not rep["wechat"]["version_ok"]:
        blockers.append(
            f"微信版本 {rep['wechat']['version']} 与 hook 要求的 "
            f"{rep['wechat']['expected'] or '(未知)'} 不一致 —— 严禁复制 version.dll，"
            "请先安装随包的同版本微信")
    if rep["hook"]["state"] == "no_vendor":
        blockers.append("随包缺少 vendor/version.dll（分发不完整）")
    if rep["hook"]["state"] == "vendor_hash_bad":
        blockers.append("vendor/version.dll 与 manifest.json 的 sha256 不符")
    if not rep["account"]["active"]:
        blockers.append("未找到任何微信账号目录（xwechat_files 下为空或路径异常）")

    # 待办项：不阻塞初始化，但功能会受限
    warnings: list[str] = []
    if rep["hook"]["state"] in ("missing", "mismatch"):
        warnings.append("version.dll 尚未就位（初始化时可一键部署）")
    if not rep["installer"]["present"]:
        warnings.append("随包没有微信安装程序（版本不符时无法自动重装）")
    if not rep["llm"]["ready"]:
        warnings.append("LLM 未配置 —— 消息仍会抓取入库，但不会评分")
    if rep["data"]["messages"] > 0:
        warnings.append(f"检测到已有 {rep['data']['messages']} 条历史消息，"
                        "初始化时会迁移到账号目录（复制，原件保留）")

    rep["blockers"] = blockers
    rep["warnings"] = warnings
    rep["ready_to_configure"] = not blockers
    return rep


def save_report(rep: dict | None = None) -> Path:
    rep = rep or collect_report()
    p = paths.data_root() / "data" / "setup_report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    r = collect_report()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print()
    if r["blockers"]:
        print("阻塞项：")
        for b in r["blockers"]:
            print("  ✗", b)
    else:
        print("✓ 环境可用于初始化")
    for w in r["warnings"]:
        print("  ! ", w)
