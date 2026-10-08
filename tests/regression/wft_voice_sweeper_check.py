"""回归：常驻语音巡检器（VoiceSweeper）+ 时间戳配对补转写。

背景（2026-10-08 用户观察）：早上 11 条语音只 1 条转写成功，而**那条用户既没播放、
群窗口也没打开** → 说明微信是**自动**把语音写进 VoiceTemp 的，只是**存留极短**。
现有两条路径都是"被动等"：watchdog 事件当天 0 命中、20 秒轮询 11 条中 1 条。

所以新增 `consumer/voice_sweeper.py`：**常驻、主动、持续**扫 VoiceTemp 抢存，
再由 `Consumer._sweeper_pair_loop()` 按"文件名秒级时间戳 ↔ 消息 received_at"配对补转写。

覆盖：
  A. scan_once 抢存 + 不误抓（排除 baiduyun.uploading.cfg 这类噪音）
  B. `_prune` 按**抢存时刻**计时（踩过的 bug：按源文件 mtime 会把"昨天遗留、今天才扫到"
     的文件立刻删掉）
  C. `take()` 按时间戳最近配对、取走即从 pending 移除、超容差返回 None
  D. `store.voice_needing_transcript()`：只返回未转写的语音消息、按时间接近排序、
     不返回已转写的
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-sweeper-suite")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer.voice_sweeper import VoiceSweeper  # noqa: E402
from consumer.voice_tap import conv_hash, _looks_like_voice_bin  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


GID = "50279811726@chatroom"
GHASH = hashlib.md5(GID.encode()).hexdigest()
NOW = int(time.time())

# 造一个假的微信账号目录：cache\<月>\Message\<会话md5>\VoiceTemp\
acc = SC / "fake_account"
vdir = acc / "cache" / "2026-10" / "Message" / GHASH / "VoiceTemp"
vdir.mkdir(parents=True)
v1 = vdir / f"505_{NOW - 5}"                     # 真语音（5 秒前）
v2 = vdir / f"507_{NOW - 3}"                     # 真语音（3 秒前）
noise = vdir / f"509_{NOW - 2}.baiduyun.uploading.cfg"   # 噪音（百度网盘）
for f, n in ((v1, 4000), (v2, 3000), (noise, 100)):
    f.write_bytes(b"x" * n)
# 一个**很旧**的真语音（模拟"昨天遗留、今天才被扫到"，用于验证 B）
v_old = vdir / f"500_{NOW - 86400}"
v_old.write_bytes(b"y" * 5000)

out = SC / "data" / "voices" / "swept"

print("[A] scan_once 抢存 + 不误抓噪音")
sw = VoiceSweeper(acc, out, interval_s=1.0)
n = sw.scan_once()
check("抢到 3 个真语音（不含 .cfg 噪音）", n == 3, f"n={n} stats={sw.stats}")
check("噪音文件没被抢", not any("cfg" in p.name for p in out.glob("*")),
      str([p.name for p in out.glob("*")]))
check("暂存区文件数 = 3", len(list(out.glob("*"))) == 3)
check("pending 记录了会话 md5 与秒级时间戳",
      {(h, ts) for h, ts, _p in sw.pending()} ==
      {(GHASH, NOW - 5), (GHASH, NOW - 3), (GHASH, NOW - 86400)},
      str(sorted((h[:8], ts - NOW) for h, ts, _p in sw.pending())))
check("重复扫描不会重复抢（幂等）", sw.scan_once() == 0, str(sw.stats))

print("\n[B] _prune 按**抢存时刻**计时（不是源文件 mtime）")
# 上面那个 v_old 的源 mtime 是 1 天前；若按源 mtime 计时会被立刻删掉
check("1 天前的旧文件**仍在** pending（说明按抢存时刻计时）",
      any(h == GHASH and abs(ts - (NOW - 86400)) < 2 for h, ts, _p in sw.pending()),
      str([(ts - NOW) for _h, ts, _p in sw.pending()]))
sw.keep_s = 0.0001          # 立刻全部过期
sw._prune(time.time())
check("keep_s 极小 → 全部清理", sw.pending() == [], str(sw.pending()))
check("暂存文件也被删掉", len(list(out.glob("*"))) == 0)

print("\n[C] take() 按时间戳最近配对")
shutil.rmtree(out, ignore_errors=True)
sw2 = VoiceSweeper(acc, out, interval_s=1.0)
sw2.scan_once()
got = sw2.take(GHASH, NOW - 4, tol_s=3)          # 距 -5 差 1、距 -3 差 1 → 取最近的
check("能配对到（容差内）", got is not None, str(got))
check("取走后从 pending 移除", len(sw2.pending()) == 2, str(len(sw2.pending())))
check("配对到的确实是语音文件", got is not None and _looks_like_voice_bin(
    got.name.split("_", 1)[1] if "_" in got.name else got.name) or got.name.endswith(tuple(
        str(x) for x in (NOW - 5, NOW - 3))), got.name if got else "None")
check("容差外返回 None", sw2.take(GHASH, NOW - 99999, tol_s=5) is None)
check("别的会话返回 None", sw2.take("0" * 32, NOW - 5, tol_s=60) is None)

print("\n[D] store.voice_needing_transcript()：只返回未转写的语音消息")
from consumer.store import Store  # noqa: E402
st = Store(str(SC / "data" / "messages.db"))
rows = [
    ("done", "已转写内容"), ("empty", None), ("placeholder", "[未捕获到语音文件]"),
    ("decodefail", "[解码失败]"), ("pending", "[待转写]"),
]
for i, (mid, tr) in enumerate(rows):
    st.insert_message(msg_id=mid, group_name=GID, sender="wxid_a", sender_id="wxid_a",
                      content="[语音]", msg_type=34, received_at=NOW - 10 + i, direction="in")
    if tr is not None:
        st.update_transcript(mid, tr)
# 再插一条别的会话的、以及一条文本消息（都不该被返回）
st.insert_message(msg_id="other", group_name="other@chatroom", sender="wxid_a",
                  sender_id="wxid_a", content="[语音]", msg_type=34,
                  received_at=NOW - 5, direction="in")
st.insert_message(msg_id="text", group_name=GID, sender="wxid_a", sender_id="wxid_a",
                  content="文本", msg_type=1, received_at=NOW - 5, direction="in")

got_ids = [m for m, _ts in st.voice_needing_transcript(GID, NOW - 5, tol_s=120)]
check("返回未转写的 3 条（empty/placeholder/decodefail/pending）",
      set(got_ids) == {"empty", "placeholder", "decodefail", "pending"}, str(got_ids))
check("**不返回已转写的**", "done" not in got_ids)
check("不返回别的会话", "other" not in got_ids)
check("不返回文本消息", "text" not in got_ids)
check("按时间接近程度排序（最近的在前）",
      got_ids == ["pending", "decodefail", "placeholder", "empty"], str(got_ids))
check("容差外不返回", st.voice_needing_transcript(GID, NOW + 100000, tol_s=10) == [])

print("\n[E] Consumer 接线：巡检器与配对循环存在")
import inspect  # noqa: E402
from consumer.main import Consumer  # noqa: E402
src = inspect.getsource(Consumer.__init__)
check("__init__ 里创建并启动了 VoiceSweeper",
      "VoiceSweeper(" in src and "voice_sweeper.start()" in src)
check("配对循环存在", hasattr(Consumer, "_sweeper_pair_loop"))
pair_src = inspect.getsource(Consumer._sweeper_pair_loop)
check("配对循环用了 voice_needing_transcript", "voice_needing_transcript" in pair_src)
check("配对循环会调 _decode_and_transcribe", "_decode_and_transcribe" in pair_src)
check("能按会话 md5 反查会话 id", hasattr(Consumer, "_conv_by_hash"))
check("装配了配对线程", "voice-pair" in inspect.getsource(Consumer.__init__))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("语音巡检器（持续抢存 + 时间戳配对补转写）验证通过")
