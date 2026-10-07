"""回归：语音「会话名手工映射」(`voice.conv_overrides`) 的键同时接受
**完整 32 位 md5** 和 **8 位前缀**，并且 8 位前缀在**实时孤儿语音归档**这条路上真的生效。

背景（2026-10-07）：
  * 语音文件名是 `<会话md5>_<毫秒>.bin`，用户抄文件名时可能只抄前 8 位；
    改动前实时路径只做精确匹配（`rev.get(32位)`），填 8 位前缀**静默不生效**，
    只有事后脚本 `remap_convs.py` 认前 8 位 —— 两边口径不一致。
  * 另一个隐患：映射值填"显示名"时，孤儿语音会落成**独立会话**，不跟该会话的
    其它消息合并；填 wxid/roomid 才会合并。页面文案已经写清楚，这里顺便断言行为。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-convmap")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
(SC / "data" / "voices" / "raw").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

_orig_cwd = os.getcwd()
# ⚠️ 必须切到沙箱目录：config 里的 sqlite_path 是相对路径，而 Store 按 **CWD** 解析
#    （见 docs/checkpoint.md 的"相对路径"条目）。不切会打开项目下的老库。
os.chdir(SC)

import yaml  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


from consumer.voice_tap import conv_hash  # noqa: E402

CONV_FULL = "wxid_convmap_probe"                 # 一个"已知会话"
CONV_FULL_HASH = conv_hash(CONV_FULL)
PREFIX = CONV_FULL_HASH[:8]
OTHER_FULL = "wxid_other_conv"
OTHER_HASH = conv_hash(OTHER_FULL)
NAME_ONLY_HASH = conv_hash("wxid_displayname_probe")

CFG = {
    "hook": {"api_base": "http://127.0.0.1:30001", "callback_host": "127.0.0.1",
             "callback_port": 8888, "api_timeout_s": 5.0, "self_wxid": "wxid_me"},
    "filter": {"groups": [], "senders": [], "keywords": [], "case_insensitive": True},
    "llm": {"base_url": "https://x/v1", "api_key": "k", "model": "m", "push_threshold": 4},
    "storage": {"sqlite_path": "data/messages.db", "ingest_exclude_types": [47]},
    "digest": {"enabled": False},
    # 关键：8 位前缀键 + 32 位键 + 「显示名」键各一条
    "voice": {"enabled": False, "dir": "data/voices", "conv_overrides": {
        PREFIX: CONV_FULL,                    # 8 位前缀 → 真实 wxid
        OTHER_HASH: "room123@chatroom",       # 完整 32 位 → roomid
        NAME_ONLY_HASH: "叶子(YEZi7825)",     # 只有显示名（用于对照）
    }},
    "asr": {"enabled": False},
    "app": {"launch_wechat": False, "auto_inject_keyhook": False},
}
(SC / "config.yaml").write_text(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")
(SC / "data" / "labels.json").write_text(
    '{"groups": {}, "senders": {"wxid_convmap_probe": {"name": "映射探针"}}}', encoding="utf-8")

from consumer import main as cmain  # noqa: E402

cfg = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
cons = cmain.Consumer(cfg)
print(f"[准备] 会话 {CONV_FULL}")
print(f"        完整 hash = {CONV_FULL_HASH}")
print(f"        前缀(8)   = {PREFIX}\n")

print("[1] _reverse_conv_map 的键：完整 32 位与 8 位前缀都能查到")
rev = cons._reverse_conv_map()
check("8 位前缀能查到真实 wxid", rev.get(PREFIX) == CONV_FULL, str(rev.get(PREFIX)))
check("完整 32 位也能查到（兼容老配置）",
      rev.get(CONV_FULL_HASH) == CONV_FULL, str(rev.get(CONV_FULL_HASH)))
check("32 位手工键也留下了它的 8 位前缀",
      rev.get(OTHER_HASH[:8]) == "room123@chatroom", str(rev.get(OTHER_HASH[:8])))
check("显示名映射原样可查", rev.get(NAME_ONLY_HASH[:8]) == "叶子(YEZi7825)",
      str(rev.get(NAME_ONLY_HASH[:8])))
check("_conv_key 统一取前 8 位",
      cmain.Consumer._conv_key(CONV_FULL_HASH) == PREFIX
      and cmain.Consumer._conv_key(PREFIX) == PREFIX)

print("\n[2] 端到端：孤儿语音归档（_sweep_orphans）用 8 位前缀映射落到真实会话")
raw = SC / "data" / "voices" / "raw"
old = time.time() - 600          # 必须 >90s 才会被 unclaimed_files 认领
# (a) 文件名带完整 32 位 hash
f_full = raw / f"{CONV_FULL_HASH}_{int(old * 1000)}.bin"
f_full.write_bytes(b"\x02silk-probe-full")
# (b) 文件名只带 8 位前缀（老文件的情形）
f_pref = raw / f"{PREFIX}_{int((old - 10) * 1000)}.bin"
f_pref.write_bytes(b"\x02silk-probe-prefix")
# (c) 显示名映射那条
f_name = raw / f"{NAME_ONLY_HASH}_{int((old - 20) * 1000)}.bin"
f_name.write_bytes(b"\x02silk-probe-name")
for f in (f_full, f_pref, f_name):
    os.utime(f, (old, old))


class _Tap:
    """最小 VoiceTap 替身：只提供 _sweep_orphans 用到的两个方法。"""
    def __init__(self, files):
        self._files = list(files)
        self.claimed = []

    def unclaimed_files(self, older_than_s=90.0):
        return list(self._files)

    def claim(self, p):
        self.claimed.append(Path(p).name)


cons.voice_tap = _Tap([f_full, f_pref, f_name])
cons.asr_transcribe = None  # 占位，说明下面不依赖 ASR
cons._sweep_orphans()

rows = cons.store._conn.execute(
    "SELECT msg_id, group_name FROM messages WHERE msg_type = 34 ORDER BY received_at").fetchall()
by_id = {r[0]: r[1] for r in rows}
print("   归档结果:", by_id)
check("三条孤儿语音都落库了", len(rows) == 3, str(len(rows)))
check("**32 位文件名 → 归到真实 wxid**",
      by_id.get(f"vt-{f_full.stem}") == CONV_FULL, str(by_id.get(f"vt-{f_full.stem}")))
check("**8 位前缀文件名 → 也归到真实 wxid（本次修复点）**",
      by_id.get(f"vt-{f_pref.stem}") == CONV_FULL, str(by_id.get(f"vt-{f_pref.stem}")))
check("映射值填显示名 → 落成独立会话（符合页面文案：只作标记）",
      by_id.get(f"vt-{f_name.stem}") == "叶子(YEZi7825)",
      str(by_id.get(f"vt-{f_name.stem}")))
check("未映射的 hash 会落成 (未知会话:<前8位>)",
      all(not (str(g).startswith("(未知会话:") and len(str(g)) != len("(未知会话:") + 8)
              for g in by_id.values()), str(by_id))
check("处理完的文件都被 claim 了", len(cons.voice_tap.claimed) == 3, str(cons.voice_tap.claimed))

print("\n[3] 页面文案：说清「填 wxid / roomid 才归到真实会话」")
# 直接检查源码里的字段文案（避免起 web 栈）
appsrc = (PROJ / "web" / "app.py").read_text(encoding="utf-8")
field_ok = ("md5=微信id" in appsrc and "前 8 位或完整 32 位都认" in appsrc
            and "填显示名只是做个标记" in appsrc)
check("FIELDS 里的建议文案已更新", field_ok)

cons.stop()
os.chdir(_orig_cwd)
shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("会话名手工映射（32 位/8 位前缀）验证通过")
