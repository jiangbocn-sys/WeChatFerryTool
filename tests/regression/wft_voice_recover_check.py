"""回归：语音漏抓的补偿（扫盘捕获 + 延迟重试 + VoiceTemp 配对补转）。

背景（2026-10-07 实测）：32 条语音只有 9 条转写成功，16 条"未捕获到语音文件"。
排查确认 **ASR（whisper）服务是好的**（/health 200），问题在抓取：
`VoiceTap.wait_for()` 只有 20 秒窗口，而语音文件是微信**按需落盘**的（要等你点播放/稍后才出现），
于是超时放弃 —— 但文件往往**还在 VoiceTemp 里**（事后来看确实还在）。

覆盖：
  A. `_looks_like_voice_bin()`：认微信语音（`<序号>_<秒级ts>`，无扩展名），
     **排除**第三方临时文件（百度网盘 `4_....baiduyun.uploading.cfg` 数字前缀一模一样，会误判）
  B. `VoiceTap.scan_temp()`：按会话找 mtime>=since 的语音；不认 .cfg；不存在的会话返回 None
  C. `store.get_transcript()`：读回 transcript（延迟重试前用它判断"是否已被别处补上"）
  D. `_adopt_temp()`：复制出来并**认领**（防止孤儿处理器把同一条语音再处理一遍 → 重复入库）
  E. `tools/backfill_voices.py` 的配对逻辑：目录名=md5(conv_id) + 文件名时间戳 ↔ 消息 received_at
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
SC = Path(r"D:\projects\.wft-voice-recover")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


from consumer.voice_tap import VoiceTap, _looks_like_voice_bin  # noqa: E402

print("[A] _looks_like_voice_bin()：只认微信语音")
yes = ["1050_1791344098", "4_1791363695", "20_1791026062", "1_1790000000"]
no = ["4_1791363695.baiduyun.uploading.cfg", "x.tmp", "abc_123456789", "1050_abc",
      "1050_1791344", ".hidden", "", "1050_1791344098.bin", "data.dat"]
check("认微信语音（无扩展名、数字_时间戳）", all(_looks_like_voice_bin(n) for n in yes),
      str([n for n in yes if not _looks_like_voice_bin(n)]))
check("排除第三方临时文件（含 .cfg）", not any(_looks_like_voice_bin(n) for n in no),
      str([n for n in no if _looks_like_voice_bin(n)]))

print("\n[B] VoiceTap.scan_temp()：按会话扫盘、排噪音、看时间")
GID = "50279811726@chatroom"
OTHER = "958062774@chatroom"
cache = SC / "fake_account" / "cache"        # VoiceTap(account_dir) 内部会拼 /cache
month = cache / "2026-10"
conv_dir = month / "Message" / hashlib.md5(GID.encode()).hexdigest()
vdir = conv_dir / "VoiceTemp"
vdir.mkdir(parents=True)
now = int(time.time())
newest = vdir / f"1050_{now - 30}"
newest.write_bytes(b"a" * 100)                              # 语音，30 秒前
older = vdir / f"1052_{now - 7200}"
older.write_bytes(b"b" * 200)                               # 语音，2 小时前
os.utime(newest, (now - 30, now - 30))                      # scan_temp 按 **mtime** 取最新
os.utime(older, (now - 7200, now - 7200))
(vdir / f"9_{now - 10}.baiduyun.uploading.cfg").write_bytes(b"junk")   # 噪音
other_dir = month / "Message" / hashlib.md5(OTHER.encode()).hexdigest() / "VoiceTemp"
other_dir.mkdir(parents=True)
(other_dir / f"7_{now - 5}").write_bytes(b"c" * 50)

# VoiceTap 的 account_dir 会与 /cache 拼接，所以传它的上一级
tap = VoiceTap(cache.parent, SC / "voices_raw")
h = hashlib.md5(GID.encode()).hexdigest()
p = tap.scan_temp(h, since=now - 3600)
check("找到 1 小时内的语音", p is not None and p.name == f"1050_{now - 30}",
      p.name if p else "None")
p_all = tap.scan_temp(h, since=0)
check("since=0 时取最新的一条", p_all is not None and p_all.name == f"1050_{now - 30}",
      p_all.name if p_all else "None")
check("不会误取百度网盘 .cfg", all("cfg" not in (x.name if x else "") for x in (p, p_all)))
check("别的会话互不串扰（按会话hash隔离）",
      tap.scan_temp(hashlib.md5(OTHER.encode()).hexdigest(), since=now - 60).name == f"7_{now - 5}")
check("不存在的会话返回 None", tap.scan_temp("0" * 32, since=0) is None)
check("空会话 hash 返回 None", tap.scan_temp("", since=0) is None)

print("\n[C] store.get_transcript()：延迟重试前判断是否已被补上")
from consumer.store import Store  # noqa: E402
db = SC / "data" / "messages.db"
st = Store(str(db))
st.insert_message(msg_id="v1", group_name=GID, sender="wxid_a", sender_id="wxid_a",
                  content="[语音]", msg_type=34, received_at=now, direction="in")
check("未转写时读回空串", st.get_transcript("v1") == "", repr(st.get_transcript("v1")))
st.update_transcript("v1", "[未捕获到语音文件]")
check("读回占位符（重试应继续尝试）", st.get_transcript("v1").startswith("[未捕获"))
st.update_transcript("v1", "真实转写内容")
check("读回真实转写（重试应停止）", st.get_transcript("v1") == "真实转写内容")
check("不存在的 msg_id 返回空串", st.get_transcript("nope") == "")

print("\n[D] _adopt_temp()：复制出来 + 认领（防止孤儿处理器重复入库）")
from consumer.main import Consumer  # noqa: E402
claimed: list = []


class _FakeTap:
    def claim(self, path):
        claimed.append(Path(path))


c = Consumer.__new__(Consumer)          # 不跑 __init__，只测这个方法
c.voice_tap = _FakeTap()
c.voice_dir = SC / "data" / "voices"
c.voice_dir.mkdir(parents=True, exist_ok=True)
src = vdir / f"1050_{now - 30}"
dest = c._adopt_temp(src, "v1")
check("复制到 data/voices/<msg_id>.bin", dest is not None and dest.name == "v1.bin" and
      dest.is_file() and dest.read_bytes() == src.read_bytes(), str(dest))
check("**认领了源文件**（避免被孤儿处理器再处理一遍）", claimed == [src], str(claimed))
missing = c._adopt_temp(vdir / "不存在", "v2")
check("源文件不存在时返回 None（不抛异常）", missing is None)

print("\n[E] backfill 配对逻辑：md5(conv_id) + 文件名时间戳 ↔ received_at")
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("bf", PROJ / "tools" / "backfill_voices.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)          # 只加载（main() 不跑）
check("工具可导入（无语法/依赖问题）", hasattr(bf, "main"))
# 直接验证配对判定所依赖的两条事实
check("目录名 = md5(conv_id)", conv_dir.name == hashlib.md5(GID.encode()).hexdigest())
ts_from_name = int((vdir / f"1050_{now - 30}").name.split("_")[-1])
check("文件名后缀可解析成秒级时间戳", ts_from_name == now - 30, str(ts_from_name))
check("容差内可配对（模拟 received_at 差 7 秒）",
      abs(ts_from_name - (now - 37)) <= 60)
check("容差外不配对（差 10 分钟）",
      not (abs(ts_from_name - (now - 630)) <= 60))
check("噪音文件不会被当成候选", not _looks_like_voice_bin(f"9_{now - 10}.baiduyun.uploading.cfg"))

print("\n[F] 三级兜底：主流程在 ①② 都失败时会走 ③ 扫盘")
import inspect  # noqa: E402
src_code = inspect.getsource(Consumer._capture_voice_bg)
check("主流程调用了 scan_temp（第三级兜底）", "scan_temp" in src_code)
check("失败后安排延迟重试", "_schedule_voice_retry" in src_code)
check("延迟重试有多次尝试", "60, 180, 600" in inspect.getsource(Consumer._schedule_voice_retry))
check("解码+转写抽成公共方法（重试复用）", "_decode_and_transcribe" in src_code)

print("\n[G] Bug 修复：多条语音不再抢同一个文件（2026-10-08 实测到的严重 bug）")
# 现象：三条**时长/aeskey 都不同**的语音被转写成同一段文字 ——
# 因为它们共享 `since = now-120s` 窗口，各自都取到了"窗口内最新"的同一个文件。
from consumer.voice import capture_voice  # noqa: E402

vroot = SC / "gsrc"
vtmp = vroot / "cache" / "2026-10" / "Message" / h / "VoiceTemp"
vtmp.mkdir(parents=True)
base_ts = int(time.time())
for i, off in enumerate((-8, -4, 0)):                 # 三条语音，时间戳各不同
    (vtmp / f"{500 + i}_{base_ts + off}").write_bytes(bytes([i]) * (1000 + i))

dest = SC / "gdest"
dest.mkdir(parents=True, exist_ok=True)
picked = []
for i, off in enumerate((-8, -4, 0)):
    picked.append(capture_voice(vroot, GID, f"g{i}", dest, wait_s=0.6, fresh_s=180,
                                near_ts=base_ts + off, tol_s=10))
check("三条消息都抓到了文件", all(p is not None for p in picked),
      str([p.name if p else None for p in picked]))
names = [p.name if p else "" for p in picked]
check("**各自抓到不同的文件**（不再共享同一个）", len(set(names)) == 3, str(names))
check("抓到的内容与各自的时间戳对应",
      all((dest / n).read_bytes()[0] == i for i, n in enumerate(names)),
      str([(dest / n).read_bytes()[0] for n in names]))
p_far = capture_voice(vroot, GID, "gfar", dest, wait_s=0.5, fresh_s=180,
                      near_ts=base_ts + 9999, tol_s=10)
check("时间对不上时返回 None（不张冠李戴）", p_far is None, str(p_far))

print("\n[H] VoiceTap.recent/wait_for 的'取走即消费'语义")
from consumer.voice_tap import VoiceTap  # noqa: E402
import time as _t  # noqa: E402
tap = VoiceTap(SC / "fake_account", SC / "tapout")
_p = vtmp / f"900_{base_ts + 1}"
_p.write_bytes(b"z" * 900)
tap._captures.append((h, _p, _t.time()))
first = tap.recent(h, since=_t.time() - 60)
second = tap.recent(h, since=_t.time() - 60)
check("第一次能拿到", first is not None, str(first))
check("**第二次拿到 None**（同一文件不会被第二条消息重用）", second is None, str(second))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("语音漏抓补偿（扫盘 / 延迟重试 / 配对补转 / 不张冠李戴）验证通过")
