"""回归：图片巡检器（ImageSweeper）+ 明文缩略图按时间戳配对。

关键发现（2026-10-08）：微信在缓存里放**明文 JPEG 缩略图**：
    cache\\<月>\\Message\\<会话md5>\\Thumb\\<序号>_<秒级时间戳>_thumb.jpg   （头 ff d8 ff e0）
    cache\\<月>\\Message\\<会话md5>\\ImageTemp\\<序号>_<秒级时间戳>_mid_temp_convert
实测含 540x720 的真聊天图；文件名带秒级时间戳 → 与消息 received_at 配对（差值 9 秒可对上）。
⚠️ 微信会清理这些文件 → 必须常驻巡检"出现即抢"。

覆盖：
  A. scan_once 抢存 Thumb 与 ImageTemp、写入带会话 md5 与时间戳的文件名、幂等
  B. 时间戳解析（两种文件名格式）
  C. _prune 按抢存时刻计时
  D. store.message_near()：按会话 + 类型 + 时间接近找消息
  E. pending/take 的取出与容差语义
  F. Consumer 接线存在（巡检器 + 配对循环 + image_dir）
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-imgsweep-suite")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer.image_sweeper import ImageSweeper  # noqa: E402

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

# 造假的微信缓存：Thumb（真图）与 ImageTemp（中间产物）
acc = SC / "fake_account"
thumb = acc / "cache" / "2026-10" / "Message" / GHASH / "Thumb"
temp_dir = acc / "cache" / "2026-10" / "Message" / GHASH / "ImageTemp"
thumb.mkdir(parents=True)
temp_dir.mkdir(parents=True)
(thumb / f"752_{NOW - 20}_thumb.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"a" * 5000)
(thumb / f"757_{NOW - 10}_thumb.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"b" * 3000)
(temp_dir / f"192_{NOW - 30}_mid_temp_convert").write_bytes(b"wxgf" + b"c" * 4000)
# 非图片噪音（不应被抢）
(thumb / "readme.txt").write_bytes(b"x" * 100)
(thumb / "123_short.jpg").write_bytes(b"\xff\xd8\xff" + b"d" * 100)   # ts 段不是时间戳

out = SC / "data" / "images" / "swept"

print("[A] scan_once 抢存 Thumb + ImageTemp")
sw = ImageSweeper(acc, out, interval_s=3.0)
n = sw.scan_once()
check("抢到 3 个（2 Thumb + 1 ImageTemp）", n == 3, f"n={n} stats={sw.stats}")
check("噪音文件没被抢", not (out / "readme.txt").exists() and
      not any("short" in p.name for p in out.glob("*")), str([p.name for p in out.glob("*")]))
check("文件名带会话 md5 与秒级时间戳",
      {(h, ts) for h, ts, _p in sw.pending()} ==
      {(GHASH, NOW - 20), (GHASH, NOW - 10), (GHASH, NOW - 30)},
      str(sorted((h[:8], ts - NOW) for h, ts, _p in sw.pending())))
check("重复扫描幂等", sw.scan_once() == 0, str(sw.stats))

print("\n[B] 时间戳解析（两种文件名格式）")
check("解析 _thumb.jpg", ImageSweeper._ts_of(f"752_{NOW - 20}_thumb.jpg") == NOW - 20)
check("解析 _mid_temp_convert",
      ImageSweeper._ts_of(f"192_{NOW - 30}_mid_temp_convert") == NOW - 30)
check("非时间戳返回 None", ImageSweeper._ts_of("123_short.jpg") is None)
check("纯名字返回 None", ImageSweeper._ts_of("readme.txt") is None)

print("\n[C] _prune 按抢存时刻计时")
check("刚抢到的都还在", len(sw.pending()) == 3, str(len(sw.pending())))
sw.keep_s = 0.0001
sw._prune(time.time())
check("keep_s 极小 → 清理干净", sw.pending() == [] and len(list(out.glob("*"))) == 0)

print("\n[D] store.message_near()：按会话 + 类型 + 时间接近找消息")
from consumer.store import Store  # noqa: E402
st = Store(str(SC / "data" / "messages.db"))
rows = [("img1", 3, NOW - 20), ("img2", 3, NOW - 10), ("img3", 3, NOW - 3600),
        ("voice1", 34, NOW - 20), ("text1", 1, NOW - 20)]
for mid, mt, ts in rows:
    st.insert_message(msg_id=mid, group_name=GID, sender="wxid_a", sender_id="wxid_a",
                      content="x", msg_type=mt, received_at=ts, direction="in")
# 注意：NOW-15 到 img1(NOW-20) 与 img2(NOW-10) 距离都是 5 → 并列，
# 实现按 (距离, msg_id) 稳定排序会返回 img1。测试要用**无歧义**的时刻。
check("按时间找到最近的图片消息（无并列）",
      st.message_near(GID, NOW - 8, msg_type=3, tol_s=90) == "img2",
      str(st.message_near(GID, NOW - 8, msg_type=3, tol_s=90)))
check("并列时结果稳定（同一输入两次一致）",
      st.message_near(GID, NOW - 15, msg_type=3, tol_s=90) ==
      st.message_near(GID, NOW - 15, msg_type=3, tol_s=90))
check("只匹配指定类型（不返回语音/文本）",
      st.message_near(GID, NOW - 20, msg_type=3, tol_s=5) == "img1")
check("容差外返回 None", st.message_near(GID, NOW + 9999, msg_type=3, tol_s=30) is None)
st.insert_message(msg_id="other_img", group_name="other@chatroom", sender="wxid_a",
                  sender_id="wxid_a", content="x", msg_type=3, received_at=NOW - 20,
                  direction="in")
check("不跨会话匹配", st.message_near(GID, NOW - 20, msg_type=3, tol_s=5) in ("img1", "img2"),
      str(st.message_near(GID, NOW - 20, msg_type=3, tol_s=5)))

print("\n[E] pending/take 的取出与容差")
shutil.rmtree(out, ignore_errors=True)
sw2 = ImageSweeper(acc, out, interval_s=3.0)
sw2.scan_once()
check("pending 有 3 条", len(sw2.pending()) == 3, str(len(sw2.pending())))
got = sw2.take(GHASH, NOW - 10, tol_s=0)
check("精确 ts 取到对应图", got is not None and str(NOW - 10) in got.name, str(got))
check("取走后 pending 减 1", len(sw2.pending()) == 2, str(len(sw2.pending())))
check("容差外返回 None", sw2.take(GHASH, NOW - 99999, tol_s=5) is None)
check("别的会话返回 None", sw2.take("0" * 32, NOW - 20, tol_s=60) is None)

print("\n[F] Consumer 接线")
import inspect  # noqa: E402
from consumer.main import Consumer  # noqa: E402
src = inspect.getsource(Consumer.__init__)
check("__init__ 创建并启动 ImageSweeper",
      "ImageSweeper(" in src and "image_sweeper.start()" in src)
check("有 image_dir（图片归档目录）", "self.image_dir" in src)
check("装配了图片配对线程", "image-pair" in src)
check("配对循环存在", hasattr(Consumer, "_image_pair_loop"))
loop_src = inspect.getsource(Consumer._image_pair_loop)
check("配对循环用 store.message_near 找图片消息", "message_near" in loop_src)
check("配对循环把图存成 <msg_id>.jpg", "{mid}" in loop_src)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("图片巡检器（抢存明文缩略图 + 时间戳配对归档）验证通过")
