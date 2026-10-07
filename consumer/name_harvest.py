"""从**我们自己抓到的消息**里挖 wxid → 昵称。不依赖 DLL、不依赖解密。

为什么需要它：DLL 的 `QueryDB/GetAllDBName` 与 `execute` 在登录标志 `g_IsLogin`
永远是 0 的情况下必然失败（源码里没有任何地方把它置 1），所以"让微信用自己的密钥查库"
这条路**不可用**。而微信消息本身带昵称：

* **引用消息**（local_type 49）里成对出现::

      <refermsg>...<chatusr>wxid_xxx</chatusr>...<displayname>张三</displayname>...

* 群里的 `@名字` 文本（只有名字，没有 wxid，所以只作旁证，不单独建映射）

`harvest()` 扫 messages 表，取"出现次数最多的名字"作为该 wxid 的昵称
（有人改过昵称时，出现最多的通常是最稳定的那个）。

人工补充走 `consumer/name_overrides.py` 的那个文件（`data/name_overrides.json`），
优先级高于挖掘结果 —— 你手工纠正过的名字不会被挖出来的覆盖。

命令行（源码侧）::

    python -m consumer.name_harvest --show      # 只统计，不改文件
    python -m consumer.name_harvest --apply     # 并进 labels.json（只填空白）
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paths import data_root  # noqa: E402

log = logging.getLogger("consumer.name_harvest")

PROJECT_DIR = data_root()
LABELS_PATH = PROJECT_DIR / "data" / "labels.json"
OVERRIDES_PATH = PROJECT_DIR / "data" / "name_overrides.json"

# 引用消息里的成对标签（顺序固定：先 chatusr 后 displayname，但为稳妥仍按 zip 对齐）
RE_CHATUSR = re.compile(r"<chatusr>([^<]*)</chatusr>")
RE_DISPLAY = re.compile(r"<displayname>([^<]*)</displayname>")
# 群内 @名字（\u2005 是微信 @ 后那个细空格）
RE_AT = re.compile(r"@([^\s@\u2005]{1,24})")


def _db_path() -> Path:
    """消息库路径：优先读 config.yaml 的 storage.sqlite_path（相对数据根）。"""
    try:
        import yaml
        cfg = yaml.safe_load((PROJECT_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        rel = str((cfg.get("storage") or {}).get("sqlite_path") or "data/messages.db")
    except Exception:  # noqa: BLE001
        rel = "data/messages.db"
    p = PROJECT_DIR / rel
    return p if p.is_file() else PROJECT_DIR / "data" / "messages.db"


def load_overrides() -> dict[str, str]:
    if not OVERRIDES_PATH.is_file():
        return {}
    try:
        d = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in d.items() if str(k).strip() and str(v).strip()}
    except Exception as e:  # noqa: BLE001
        log.warning("读 name_overrides.json 失败: %s", e)
        return {}


def save_overrides(d: dict[str, str]) -> None:
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True),
                              encoding="utf-8")


def harvest(db_path: Path | None = None) -> dict[str, str]:
    """返回 {wxid: 昵称}。引用消息挖掘 + 人工补充（人工优先）。"""
    db_path = db_path or _db_path()
    names: dict[str, str] = {}
    if db_path.is_file():
        pairs: Counter = Counter()          # {(wxid, 名字): 次数}
        ats: Counter = Counter()            # {名字: 次数}（旁证，不单独建映射）
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                    "SELECT content FROM messages "
                    "WHERE content LIKE '%<displayname>%' OR content LIKE '%@%'"):
                c = row["content"] or ""
                if "<displayname>" in c:
                    us = RE_CHATUSR.findall(c)
                    ds = RE_DISPLAY.findall(c)
                    for u, d in zip(us, ds):
                        u, d = u.strip(), d.strip()
                        if u and d and u != d and not d.startswith("<"):
                            pairs[(u, d)] += 1
                if "@" in c:
                    for m in RE_AT.findall(c):
                        m = m.strip()
                        if m:
                            ats[m] += 1
            conn.close()
        except sqlite3.Error as e:
            log.warning("读消息库失败（%s）: %s", db_path, e)
        # 出现次数最多的名字优先
        for (u, d), _n in pairs.most_common():
            names.setdefault(u, d)
    names.update(load_overrides())          # 人工补充/纠正优先
    log.info("从消息里挖到 %d 个 wxid→昵称（人工补充 %d 个）", len(names), len(load_overrides()))
    return names


def _load_labels(p: Path) -> dict:
    if not p.is_file():
        return {"groups": {}, "senders": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"groups": {}, "senders": {}}
    d.setdefault("groups", {})
    d.setdefault("senders", {})
    return d


def apply_to_labels(labels_path: Path | None = None, create_missing: bool = True,
                    names: dict[str, str] | None = None) -> dict:
    """把挖到的名字并进 labels.json。**只填空白**，绝不覆盖任何已有非空名字。"""
    labels_path = labels_path or LABELS_PATH
    labels = _load_labels(labels_path)
    names = names if names is not None else harvest()

    filled = created = 0
    for wx, nm in names.items():
        kind = "groups" if wx.endswith("@chatroom") else "senders"
        ent = labels[kind].get(wx)
        if ent is None:
            if not create_missing:
                continue
            labels[kind][wx] = {"name": nm, "important": False, "auto": True, "source": "harvest"}
            created += 1
        elif not (ent.get("name") or "").strip():
            ent["name"] = nm
            ent["auto"] = True
            ent["source"] = "harvest"
            filled += 1

    if names:
        labels_path.parent.mkdir(parents=True, exist_ok=True)
        labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=2, sort_keys=True),
                               encoding="utf-8")
    return {"ok": bool(names), "found": len(names), "filled": filled, "created": created,
            "labels_path": str(labels_path), "sample": dict(list(names.items())[:8])}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description="从已抓消息里挖 wxid→昵称")
    ap.add_argument("--show", action="store_true", help="只统计，不改文件")
    ap.add_argument("--apply", action="store_true", help="并进 labels.json（只填空白）")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("消息库:", _db_path())
    names = harvest()
    print(f"挖到 {len(names)} 个 wxid→昵称")
    for wx, nm in list(names.items())[:20]:
        print(f"  {wx:28s} -> {nm}")
    if args.apply:
        print(json.dumps({k: v for k, v in apply_to_labels(names=names).items() if k != "sample"},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
