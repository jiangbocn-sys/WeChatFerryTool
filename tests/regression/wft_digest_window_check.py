"""回归：归档时间窗口按**归档时刻**划分（2026-10-12 用户提问引出的修复）。

用户问题："我把总结归档时间改到早上6点，这种情况下，他是总结0点到6点的对话，
还是前一天早上6点到今天早上6点？"

核查结论：**原来是"当天 0 点到 6 点"（只有约 6 小时）** —— 因为窗口写死按自然日
（`D 00:00 ~ D+1 00:00`），而触发时刻是"当天到点就跑"。用户要的是后者，故修复为
**刚刚结束的那个 24 小时窗口**：06:00 跑 → 覆盖「前一天 06:00 ~ 当天 06:00」。

覆盖：
  A. `digest_window()`：不同 `time` 的窗口换算；`00:00` 等价于旧的自然日行为
  B. 连续两天的窗口**首尾相接**（不重不漏）
  C. `Consumer._digest_window_now()`：不同触发时刻都拿到"上一个完整窗口"；
     到点前触发拿的是再前一个窗口（避免拿到只跑了一半的窗口）
  D. `_norm_hhmm()`：`6:00`→`06:00`（**不补零会导致字符串比较判断失效**，实际踩到）
  E. **端到端**：造带时间戳的消息库，验证归档只收录窗口内的消息
     （窗口外的绝不出现），且 06:00 口径下"前一天白天+晚上"确实进得来
  F. `summarize.collect` 与 `generate_digest` 用**同一个窗口**（错位会导致归档/总结不一致）
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-window-suite")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import yaml  # noqa: E402

# ⚠️ 必须把配置根指到临时目录：`paths.config_path()` 的顺序是
#   数据根（可能已被选中为真实账号目录）→ `_config_root` → 基目录。
#   不设的话会读到**项目根的 config.yaml**（`time: 6:00`），测试就测不到自己写的配置
#   （2026-10-12 踩到：断言里看到的是项目根配置的窗口）。
import paths  # noqa: E402
paths.set_config_root(SC)

from consumer.digest import digest_window  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


def hm(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


D = "2026-10-13"

print("[A] digest_window：按归档时刻划窗口（**标签 = 窗口结束日**）")
s, e = digest_window(D, "06:00")
check("06:00 → [D-1 06:00, D 06:00)（标签 D 装的是前一天 6 点到当天 6 点）",
      hm(s) == "10-12 06:00" and hm(e) == "10-13 06:00", f"{hm(s)} ~ {hm(e)}")
check("窗口长度 = 24 小时", e - s == 86400, str((e - s) / 3600))
s2, e2 = digest_window(D, "00:00")
check("00:00 → [D-1 00:00, D 00:00)（零点时标签 D 装的是 D-1 那一自然日）",
      hm(s2) == "10-12 00:00" and hm(e2) == "10-13 00:00", f"{hm(s2)} ~ {hm(e2)}")
s3, e3 = digest_window(D, "23:30")
check("23:30 → [D-1 23:30, D 23:30)",
      hm(s3) == "10-12 23:30" and hm(e3) == "10-13 23:30", f"{hm(s3)} ~ {hm(e3)}")
check("坏输入退回自然日", digest_window(D, "abc") == digest_window(D, "00:00"))
check("**窗口结束时刻不晚于触发时刻**（06:00 触发时结束 = 当天 06:00）",
      digest_window(D, "06:00")[1] == int(datetime(2026, 10, 13, 6, 0).timestamp()))

print("\n[B] 连续窗口首尾相接（不重不漏）")
for hh in ("06:00", "00:00", "23:30"):
    days = ["2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14"]
    wins = [digest_window(d, hh) for d in days]
    ok = all(wins[i][1] == wins[i + 1][0] for i in range(len(wins) - 1))
    total = sum(b - a for a, b in wins)
    check(f"{hh}：相邻窗口 end==next start 且总时长 = 天数×24h",
          ok and total == 86400 * len(days), f"ok={ok} total={total / 3600:.0f}h")

print("\n[C] Consumer._digest_window_now：无论几点触发都拿「上一个完整窗口」")
from consumer.main import Consumer, _norm_hhmm  # noqa: E402


def window_at(now_str: str, digest_time: str):
    fake = datetime.strptime(now_str, "%Y-%m-%d %H:%M")
    obj = object.__new__(Consumer)
    obj.digest_time = _norm_hhmm(digest_time)
    with mock.patch("consumer.main.datetime") as m:
        m.now.return_value = fake
        m.fromtimestamp = datetime.fromtimestamp
        m.strptime = datetime.strptime
        return Consumer._digest_window_now(obj)


cases = [
    ("2026-10-13 06:00", "6:00", "10-12 06:00", "10-13 06:00"),   # 正点
    ("2026-10-13 06:05", "6:00", "10-12 06:00", "10-13 06:00"),   # 晚 5 分钟才跑
    ("2026-10-13 09:30", "6:00", "10-12 06:00", "10-13 06:00"),   # 晚几小时补跑
    ("2026-10-13 23:00", "6:00", "10-12 06:00", "10-13 06:00"),   # 当天很晚
    ("2026-10-13 05:59", "6:00", "10-11 06:00", "10-12 06:00"),   # 到点前 → 再前一个窗口
    ("2026-10-13 23:30", "23:30", "10-12 23:30", "10-13 23:30"),  # 原行为也要对
]
for now_s, dt, want_s, want_e in cases:
    ws, we, label = window_at(now_s, dt)
    got = (hm(ws), hm(we))
    check(f"触发 {now_s[5:]} time={dt} → {want_s} ~ {want_e}",
          got == (want_s, want_e) and label == want_e[:7].replace("-0", "-0")[:0] + label
          and label == datetime.fromtimestamp(we).strftime("%Y-%m-%d"),
          f"得到 {got} 标签={label}")
check("**关键**：06:00 口径下，触发时拿到的不是「当天 0 点到现在」",
      window_at("2026-10-13 06:00", "6:00")[0] ==
      int(datetime(2026, 10, 12, 6, 0).timestamp()))
check("**关键**：`_digest_window_now` 与 `digest_window(标签)` 必须一致",
      window_at("2026-10-13 06:00", "6:00")[:2] == digest_window("2026-10-13", "06:00"),
      f"{window_at('2026-10-13 06:00', '6:00')[:2]} vs {digest_window('2026-10-13', '06:00')}")

print("\n[D] _norm_hhmm：必须补零（否则字符串比较会导致不触发）")
for raw, want in (("6:00", "06:00"), ("06:00", "06:00"), ("9:5", "09:05"),
                  ("23:30", "23:30"), (" 7:00 ", "07:00"), ("abc", "23:30"),
                  ("", "23:30"), ("25:99", "23:59")):
    check(f"{raw!r} → {want}", _norm_hhmm(raw) == want, _norm_hhmm(raw))
check("**修复前的 bug**：'09:00' >= '6:00' 为假（不触发）", not ("09:00" >= "6:00"))
check("修复后：'09:00' >= '06:00' 为真（会触发）", "09:00" >= _norm_hhmm("6:00"))

print("\n[E] 端到端：归档只收录窗口内的消息")
# 造消息：10-12 白天/晚上（06:00 口径应进来）、10-13 凌晨（06:00 口径**不该**进来）、
#          10-12 凌晨 05:00（06:00 口径不该进来）
GID = "g1@chatroom"
(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "filter": {"groups": [GID], "senders": []},
    "storage": {"sqlite_path": str(SC / "data" / "messages.db")},
    "digest": {"enabled": True, "time": "06:00", "dir": str(SC / "reports"),
               "exclude_types": [47], "window_end_at_time": True},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")
(SC / "data" / "labels.json").write_text(json.dumps(
    {"groups": {GID: {"name": "测试群", "monitored": True}}, "senders": {}},
    ensure_ascii=False), encoding="utf-8")

DB = SC / "data" / "messages.db"
db = sqlite3.connect(DB)
db.executescript("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
 msg_id TEXT UNIQUE, group_name TEXT, sender TEXT, sender_id TEXT, content TEXT,
 msg_type INTEGER DEFAULT 1, received_at INTEGER NOT NULL, score INTEGER,
 score_reason TEXT, pushed INTEGER DEFAULT 0, priority INTEGER DEFAULT 0,
 direction TEXT, transcript TEXT);""")


def at(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi).timestamp())


rows = [
    ("m-12-0500", at(2026, 10, 12, 5, 0), "十凌晨五点（窗口外）"),      # 窗口外（< 10-12 06:00）
    ("m-12-0700", at(2026, 10, 12, 7, 0), "十二早七点（窗口内）"),      # 窗口内
    ("m-12-1400", at(2026, 10, 12, 14, 0), "十二下午两点（窗口内）"),   # 窗口内
    ("m-12-2359", at(2026, 10, 12, 23, 59), "十二深夜（窗口内）"),      # 窗口内
    ("m-13-0300", at(2026, 10, 13, 3, 0), "十三凌晨三点（窗口外）"),    # 窗口外（>= 10-13 06:00）
    ("m-13-0700", at(2026, 10, 13, 7, 0), "十三早七点（窗口外）"),      # 窗口外
]
for mid, ts, txt in rows:
    db.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type,"
               " received_at, priority) VALUES (?,?,?,?,?,?,?)",
               (mid, GID, "wxid_a", txt, 1, ts, 1))
db.commit()
db.close()

import consumer.digest as digest_mod  # noqa: E402
p = digest_mod.generate_digest(D, db_path=DB, labels_path=SC / "data" / "labels.json",
                               out_dir=SC / "reports")
txt = Path(p).read_text(encoding="utf-8")
check("窗口内的三条都在（早七点/下午/深夜）",
      all(k in txt for k in ("十二早七点", "十二下午两点", "十二深夜")), "")
check("**前一天凌晨 05:00 不在**（窗口是 [10-12 06:00, 10-13 06:00)，05:00 更早）",
      "十凌晨五点" not in txt)
check("**当天凌晨 03:00 在**（这正是修复的意义：凌晨的记录进前一天的稿子）",
      "十三凌晨三点" in txt)
check("当天早七点不在（已过窗口结束时刻 06:00）", "十三早七点" not in txt)
check("归档文件按窗口结束日命名", p.name == f"digest-{D}.md", p.name)

print("\n[F] summarize.collect 与 generate_digest 窗口一致")
import consumer.summarize as summarize_mod  # noqa: E402
collected = summarize_mod.collect(D, db_path=DB)
check("collect 有结果", collected is not None)
if collected:
    grouped, total = collected
    body = "\n".join(v for vs in grouped.values() for v in vs)
    check("collect 也只看得到窗口内四条（含当天凌晨 03:00）", total == 4, str(total))
    check("collect 含窗口内的内容", "十二深夜" in body and "十三凌晨三点" in body)
    check("collect **不含**窗口外的内容（前一天 05:00 / 当天 07:00）",
          "十凌晨五点" not in body and "十三早七点" not in body)
w1 = digest_mod._digest_window_for(D)
check("两处窗口完全相同（错位会导致归档/总结不一致）",
      w1 == digest_window(D, "06:00"), str((hm(w1[0]), hm(w1[1]))))

print("\n[G] 兼容开关：window_end_at_time=false 退回自然日")
cfg = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
cfg["digest"]["window_end_at_time"] = False
(SC / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")
w_off = digest_mod._digest_window_for(D)
check("关掉后回到自然日口径 [D-1 00:00, D 00:00)",
      w_off == digest_window(D, "00:00"), str((hm(w_off[0]), hm(w_off[1]))))

print("\n[H] 状态记账按**窗口**（改了归档时刻也要能生成）")
import inspect  # noqa: E402
from consumer.main import Consumer as _C  # noqa: E402
loop = inspect.getsource(_C._digest_loop)
check("状态里记 window_key", "window_key" in loop)
check("判断比的是窗口键而不是只比日期", "last_key != window_key" in loop)
check("兼容旧状态文件（只有 last_date 时也能读）",
      'st.get("window_key") or st.get("last_date")' in loop)
check("总结用**窗口标签**（与归档同口径），不是 today",
      "summarize_mod.summarize(w_label" in loop and "summarize_mod.summarize(today" not in loop)

print("\n[I] 每天只生成一次（改到 06:00 后晚上不该再跑一遍）")
# 用户 10-12 确认："改早上6点总结前一日消息后，晚上就不需要再跑一遍总结了"。
# 机制上本来就只有**一个** digest.time（每天到点一次）+ 窗口键防重复；
# 这里锁成断言，防止日后有人加"第二个时间点"时悄悄变成一天两份。
src_main = (PROJ / "consumer" / "main.py").read_text(encoding="utf-8")
check("只有一个 digest.time（没有 times 列表 / 第二个触发时刻）",
      "digest.times" not in src_main and "digest_times" not in src_main)
check("同一窗口生成过就不再生成（window_key 记账）", "last_key != window_key" in loop)
# 模拟：06:00 口径下，从早上到深夜算出来的窗口键都应相同 → 只会跑一次
keys = set()
for hh in ("06:00", "07:30", "12:00", "18:00", "22:00", "23:59"):
    ws, we, _lbl = window_at(f"2026-10-13 {hh}", "6:00")
    keys.add(f"{ws}-{we}")
check("**06:00 到 23:59 之间窗口键保持不变** → 一天只跑一次", len(keys) == 1, str(keys))
ws2, we2, _ = window_at("2026-10-14 06:00", "6:00")
check("跨到次日 06:00 才换新窗口（第二天再跑一次）", f"{ws2}-{we2}" not in keys)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("归档时间窗口（按归档时刻划分 + 补零 + 与总结一致）验证通过")
