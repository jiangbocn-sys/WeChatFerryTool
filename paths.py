"""路径与账号解析 —— 可写数据根、只读资源根、以及**按账号隔离**。

两个要解决的问题
----------------
1. **打包后路径错位**：PyInstaller 打包后 `__file__` 指向 `_internal`（onedir）或
   临时解包目录（onefile），原先到处写的 `Path(__file__).parent.parent` 会把
   config.yaml / data / logs 带进安装目录 —— 用户改不到配置，升级还会丢数据。

2. **多账号混装**：换一个微信号登录后，两个账号的消息会进同一个 messages.db、
   标定全部失效、而且 `self_wxid` 对不上会导致"自己发的消息被当成别人发的"
   （自动回复可能回你自己）。所以每个账号一套独立数据根。

数据根优先级
------------
    use_account(slug) 显式指定   >  WCF_DATA_DIR 环境变量  >  accounts/<活跃账号>  >  基目录

目录布局
--------
    <基目录>/
    ├── config.yaml                全局配置（LLM、模板等账号无关项）
    └── accounts/
        └── <账号目录名>/           与微信 xwechat_files 下的目录同名
            ├── account.json        该账号的 self_wxid 等元信息
            ├── data/  logs/  reports/

    WCF_NO_MULTI_ACCOUNT=1 可退回旧的单目录布局。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parent

ENV_DATA_DIR = "WCF_DATA_DIR"          # 显式指定数据根（最高优先级）
ENV_NO_MULTI = "WCF_NO_MULTI_ACCOUNT"  # 置 1 则禁用账号隔离

_account_root: Path | None = None
_account_slug: str | None = None
_config_root: Path | None = None      # 显式指定"配置目录"（发现已有数据时用）


def set_config_root(root: str | Path | None) -> None:
    """显式指定 config.yaml 的所在目录（须在导入 consumer/web 之前调用）。

    冻结后 `base_root()` 是 exe 所在目录，但老配置可能在项目目录（被"发现已有数据"找出来）。
    发现到已有数据时把它设成那个目录，config_path() 就优先从那里找。
    """
    global _config_root
    _config_root = Path(root) if root is not None else None


# ---------------------------------------------------------------------------
# 基目录 / 资源目录
# ---------------------------------------------------------------------------
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def base_root() -> Path:
    """基目录：冻结运行时 = exe 所在目录；源码运行时 = 项目根目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _SOURCE_ROOT


def resource_root() -> Path:
    """只读资源根（模板、静态文件）。冻结运行时 = sys._MEIPASS。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else _SOURCE_ROOT


# ---------------------------------------------------------------------------
# 微信账号目录
# ---------------------------------------------------------------------------
def _documents_dir() -> Path:
    """取真实「文档」目录（可能被重定向到别的盘）。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        val, _ = winreg.QueryValueEx(key, "Personal")
        return Path(str(val).replace("%USERPROFILE%", str(Path.home())))
    except Exception:  # noqa: BLE001
        return Path.home() / "Documents"


def wechat_files_root() -> Path | None:
    """定位 <文档>\\xwechat_files。"""
    root = _documents_dir() / "xwechat_files"
    if root.is_dir():
        return root
    # 兜底：Documents 被重定向到其他盘
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        base = Path(f"{letter}:/")
        if not base.is_dir():
            continue
        for cand in (base / "xwechat_files",):
            if cand.is_dir():
                return cand
        try:
            subs = [d for d in base.iterdir() if d.is_dir()]
        except OSError:
            continue
        for sub in subs:
            cand = sub / "Documents" / "xwechat_files"
            if cand.is_dir():
                return cand
    return None


def slug_alias(slug: str) -> str:
    """账号目录名 → 微信号（别名）。

    微信账号目录形如 `<别名>_<短哈希>`，例如 `ruibo_jiang_542e` → `ruibo_jiang`。
    这个别名正是 DLL 回调里 sender 对"自己"的取值，所以可以直接当 self_wxid 用。
    """
    if "_" not in slug:
        return slug
    head, _, _tail = slug.rpartition("_")
    return head or slug


def _account_last_active(d: Path) -> float:
    """用一个目录下最近被写入的 db 文件时间代表活跃度。"""
    best = 0.0
    for pat in ("db_storage/message/message_0.db", "db_storage/session/session.db"):
        f = d / pat
        try:
            best = max(best, f.stat().st_mtime)
        except OSError:
            pass
    if best == 0.0:
        try:
            best = d.stat().st_mtime
        except OSError:
            pass
    return best


def list_accounts() -> list[dict]:
    """列出微信账号目录（排除 all_users / Backup），按最近活跃排序。"""
    root = wechat_files_root()
    if not root:
        return []
    out = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for d in entries:
        if not d.is_dir() or d.name in ("all_users", "Backup"):
            continue
        out.append({
            "slug": d.name,
            "dir": d,
            "alias": slug_alias(d.name),
            "last_active": _account_last_active(d),
        })
    out.sort(key=lambda a: a["last_active"], reverse=True)
    return out


def active_account() -> dict | None:
    """当前（最近使用的）账号。app 启动时常在微信启动之前，所以看的是"上次活跃"。"""
    accounts = list_accounts()
    return accounts[0] if accounts else None


# ---------------------------------------------------------------------------
# 数据根
# ---------------------------------------------------------------------------
def accounts_root() -> Path:
    return base_root() / "accounts"


def current_account() -> str | None:
    return _account_slug


def account_root(slug: str) -> Path:
    """账号数据根（纯路径，不创建任何东西）。"""
    return accounts_root() / slug


def _select_account(slug: str, create: bool, root: Path | None = None) -> Path:
    """`root` 为 None 时按老规矩用 `base_root()/accounts/<slug>`；

    显式给 root 时**直接用它**（发现"已有数据"时必须这样 —— 冻结后 base_root()
    是 exe 所在目录，而老数据可能在项目目录里，否则会拿对账号名、建错地方）。
    """
    global _account_root, _account_slug
    root = Path(root) if root is not None else account_root(slug)
    if create:
        root.mkdir(parents=True, exist_ok=True)
        meta = root / "account.json"
        if not meta.exists():
            meta.write_text(json.dumps({
                "slug": slug,
                "self_wxid": slug_alias(slug),
                "created": int(time.time()),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    _account_root = root
    _account_slug = slug
    return root


def use_account(slug: str, root: Path | None = None) -> Path:
    """显式选定账号并建好目录（写 account.json）。须在导入 consumer/web 之前调用。

    ``root``：该账号的数据根。给了就用它，不给则用 ``base_root()/accounts/<slug>``。
    """
    if not slug:
        return data_root()
    return _select_account(slug, create=True, root=root)


def account_self_wxid() -> str | None:
    """读当前账号的 self_wxid（由账号目录名推导，必要时以 account.json 为准）。"""
    if _account_root is None:
        return None
    meta = _account_root / "account.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            if data.get("self_wxid"):
                return str(data["self_wxid"])
        except (OSError, json.JSONDecodeError):
            pass
    return slug_alias(_account_slug) if _account_slug else None


def data_root() -> Path:
    """可写数据根。

    注意：本函数**不创建任何目录**（纯计算），只记住了选中的账号。
    需要目录就用 ensure_data_dirs() / use_account()。
    """
    if _account_root is not None:
        return _account_root
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        p = Path(env).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if p.is_dir():
            return p
    if not os.environ.get(ENV_NO_MULTI):
        acc = active_account()
        if acc:
            return _select_account(acc["slug"], create=False)
    return base_root()


def vendor_root() -> Path:
    """随包第三方组件目录（version.dll / keyhook3.dll / 微信安装程序 / manifest.json）。"""
    return base_root() / "vendor"


def config_path() -> Path:
    """config.yaml 的位置：**数据根优先 → 显式配置根 → 基目录**。

    config.yaml 是全局配置（LLM key、模板等），默认放在基目录；
    若某个数据根（账号目录，或 `WCF_DATA_DIR` 指定的目录）想覆盖它，
    把自己的 config.yaml 放进去即可 —— 数据根优先。

    冻结运行时 `base_root()` 是 **exe 所在目录**，所以"发现已有数据"时要用
    `set_config_root()` 指到真正的项目目录，否则会去找 `dist\\App\\config.yaml`（踩过）。

    注意：这里用 `data_root()` 而不是只看 `_account_root`，否则
    `WCF_DATA_DIR=<某目录>` 时文档承诺的"数据根优先"会失效（沙箱/测试里踩过）。
    """
    candidate = data_root() / "config.yaml"
    if candidate.exists():
        return candidate
    if _config_root is not None:
        p = _config_root / "config.yaml"
        if p.exists():
            return p
    return base_root() / "config.yaml"


def ensure_data_dirs() -> Path:
    root = data_root()
    for sub in ("data", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# 老布局迁移（只复制，不删原件，便于回退）
# ---------------------------------------------------------------------------
def migrate_legacy_data(slug: str) -> list[str]:
    """把基目录下旧的 data/ logs/ reports/ 复制进账号目录（仅当目标为空时）。

    只复制、不移动：原件保留，用户确认无误后可自行删除。
    """
    dst_root = accounts_root() / slug
    moved: list[str] = []
    for sub in ("data", "logs", "reports"):
        src = base_root() / sub
        dst = dst_root / sub
        if not src.is_dir():
            continue
        # 目标已有内容就跳过，避免覆盖
        has_content = False
        if dst.is_dir():
            try:
                has_content = any(dst.iterdir())
            except OSError:
                has_content = True
        if has_content:
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            target = dst / item.name
            try:
                if item.is_dir():
                    import shutil
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    import shutil
                    shutil.copy2(item, target)
                moved.append(f"{sub}/{item.name}")
            except OSError:
                continue
    return moved
