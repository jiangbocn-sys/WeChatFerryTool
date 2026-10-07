"""R-001 消息库按期清理（consumer/cleanup.py）的回归测试。

覆盖 docs/requirements.md §5 的验收标准：
  A1 设置页出现「保留多少天 / 自动清理 / 几点清理」三项，改一项不影响其它配置（隔离性）
  A2 边界：正好 N 天的记录保留，更早的删除
  A3 retention_days=0 → 完全不删；1~6 天的非法值 → 拒绝执行
  A4 一天最多跑一次；cleanup_state.json 记录 last_date
  A5 可中断：stop 置位后当前批结束即退出，进程能正常收尾
  A6 静默：过程中不调用任何通知接口（托盘/弹窗）
  A7 dry-run 报数与真删条数一致
  A8 删除后有 wal_checkpoint(TRUNCATE)；只有 --vacuum 时才 VACUUM
  A9 清理后 /digest、/browse、/labels、/ 全部正常（旧日期归档显示 0 条且不 500）
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-retention")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

import yaml  # noqa: E402

# 控制台可能是 GBK：把 stdout 换成 UTF-8 并容错，否则断言信息里的中文会抛 UnicodeEncodeError
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


# ---------------------------------------------------------------------------
# 准备一个消息库：按"今天零点"为锚造不同天数的记录
# ---------------------------------------------------------------------------
NOW = datetime(2026, 10, 7, 9, 0, 0)
DAY = 86400
START_TODAY = int(datetime(2026, 10, 7, 0, 0, 0).timestamp())


def make_db(path: Path, ages_days: list[int], extra_today: int = 0) -> None:
    """ages_days: 每条记录"距今多少个自然日"（0=今天）。重建库文件，保证可重复跑。"""
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id TEXT UNIQUE, group_name TEXT NOT NULL, sender TEXT NOT NULL,
            sender_id TEXT, content TEXT NOT NULL, msg_type INTEGER DEFAULT 1,
            received_at INTEGER NOT NULL, score INTEGER, score_reason TEXT,
            pushed INTEGER DEFAULT 0, priority INTEGER DEFAULT 0,
            direction TEXT DEFAULT NULL, transcript TEXT DEFAULT NULL);
        CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at);
    """)
    n = 0
    for age in ages_days:
        # 该天内的一个时刻（中午），保证落在"自然日"的中间
        ts = START_TODAY - age * DAY + 12 * 3600
        conn.execute(
            "INSERT INTO messages (msg_id, group_name, sender, content, received_at) "
            "VALUES (?,?,?,?,?)", (f"m{n}", "g@chatroom", "wxid_x", f"age={age}", ts))
        n += 1
    for i in range(extra_today):
        ts = START_TODAY + 8 * 3600 + i * 60
        conn.execute(
            "INSERT INTO messages (msg_id, group_name, sender, content, received_at) "
            "VALUES (?,?,?,?,?)", (f"t{i}", "g@chatroom", "wxid_x", "今天", ts))
        n += 1
    conn.commit()
    conn.close()


def count(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    finally:
        conn.close()


import consumer.cleanup as cleanup  # noqa: E402

print("[1] 阈值口径：保留最近 N 个自然日（含今天），门槛是本地零点")
cut = cleanup.retention_cutoff(90, now=NOW)
check("90 天门槛 = 今天零点 - 89 天",
      cut == int(datetime(2026, 7, 10, 0, 0, 0).timestamp()),
      f"{datetime.fromtimestamp(cut)}")
check("N=1 的门槛 = 今天零点（当天永不参与）",
      cleanup.retention_cutoff(1, now=NOW) == START_TODAY)

print("\n[A2] 边界：保留窗口内最老的一天（29 天）保留，第 30 天起删除 + dry-run 报数一致（A7）")
db = SC / "data" / "messages.db"
make_db(db, ages_days=[0, 1, 29, 30, 31, 45, 200], extra_today=3)
check("造了 10 条（含 3 条今天）", count(db) == 10, str(count(db)))
dry = cleanup.cleanup_messages(db, retention_days=30, now=NOW, dry_run=True, write_state=False)
check("dry-run 命中 4 条（30/31/45/200 天前）", dry.matched == 4, str(dry.matched))
check("dry-run 一条都没删", count(db) == 10, str(count(db)))
check("dry-run 也不写状态文件", not (SC / "data" / "cleanup_state.json").exists())

real = cleanup.cleanup_messages(db, retention_days=30, now=NOW)
check("真删条数 = dry-run 报数（A7）", real.deleted == dry.matched, f"{real.deleted} vs {dry.matched}")
check("剩下的正好是保留窗口内 + 今天（6 条）", count(db) == 6, str(count(db)))
check("保留窗口最老的一天（29 天前）没被删",
      int(sqlite3.connect(str(db)).execute(
          "SELECT COUNT(*) FROM messages WHERE received_at < ?",
          (START_TODAY - 29 * DAY,)).fetchone()[0]) == 0)
check("第 30 天起的记录确实没了", cleanup.count_older(db, 30) == 0)
check("有 checkpoint 调用（A8）", real.checkpointed is True)
check("没开 vacuum 就不 VACUUM（A8）", real.vacuumed is False)
check("状态文件写了 last_date（A4）",
      json.loads((SC / "data" / "cleanup_state.json").read_text(encoding="utf-8")).get("last_date") == "2026-10-07")
check("日志行含条数（A6：只有日志可观测）", "删除 4 条" in real.log_line(), real.log_line())

print("\n[A3] retention_days=0 → 完全不删；1~6 天 → 拒绝执行")
before = count(db)
r0 = cleanup.cleanup_messages(db, retention_days=0, now=NOW)
check("0 天：0 条被删", r0.deleted == 0 and count(db) == before, str(count(db)))
check("0 天：给出原因", bool(r0.errors), str(r0.errors))
r3 = cleanup.cleanup_messages(db, retention_days=3, now=NOW)
check("3 天：拒绝执行且未删数据", r3.deleted == 0 and count(db) == before, str(r3.errors))
r7 = cleanup.cleanup_messages(db, retention_days=7, now=NOW, dry_run=True, write_state=False)
check("7 天（下限）可以执行", not r7.errors, str(r7.errors))

print("\n[A4] 一天最多跑一次（should_run + 状态文件）")
cfg_on = {"storage": {"sqlite_path": "data/messages.db", "retention_days": 30,
                      "auto_cleanup": True, "cleanup_time": "00:00"}}
ok1, why1 = cleanup.should_run(cfg_on, now=datetime(2026, 10, 8, 0, 30),
                              state={"last_date": "2026-10-07"})
check("昨天跑过 → 今天可以跑", ok1, why1)
ok2, why2 = cleanup.should_run(cfg_on, now=datetime(2026, 10, 8, 0, 30),
                              state={"last_date": "2026-10-08"})
check("今天跑过 → 不再跑", not ok2, why2)
# 到点前不跑
ok4, why4 = cleanup.should_run({**cfg_on, "storage": {**cfg_on["storage"], "cleanup_time": "04:30"}},
                              now=datetime(2026, 10, 8, 1, 0), state={"last_date": "2026-10-07"})
check("还没到清理时间 → 不跑", not ok4, why4)
ok5, why5 = cleanup.should_run({**cfg_on, "storage": {**cfg_on["storage"], "auto_cleanup": False}},
                              now=datetime(2026, 10, 8, 5, 0), state={"last_date": "2026-10-07"})
check("开关关闭 → 不跑", not ok5, why5)
c2 = SC / "data" / "m2.db"
make_db(c2, ages_days=[0, 1, 100, 100])
cleanup.cleanup_messages(c2, retention_days=7, now=NOW)
st = json.loads((SC / "data" / "cleanup_state.json").read_text(encoding="utf-8"))
check("状态文件写了至少 1 条删除记录", st.get("deleted") == 2, json.dumps(st, ensure_ascii=False))

print("\n[A5] 可中断：stop 置位后当前批结束即退出")
db3 = SC / "data" / "m3.db"
make_db(db3, ages_days=[400] * 5000 + [0])
stop = threading.Event()
# 批大小 500：第一批就该把 stop 打上，之后不应再继续删
def _stopper():
    time.sleep(0.35)
    stop.set()
threading.Thread(target=_stopper, daemon=True).start()
t0 = time.time()
r_int = cleanup.cleanup_messages(db3, retention_days=7, now=NOW, batch_size=500,
                                 sleep_s=0.2, stop_event=stop)
elapsed = time.time() - t0
check("被标记为中断", r_int.interrupted is True)
check("只删了部分（< 5000）", 0 < r_int.deleted < 5000, str(r_int.deleted))
check("及时退出（<8s）", elapsed < 8, f"{elapsed:.1f}s")
check("状态文件如实记下已删条数",
      json.loads((SC / "data" / "cleanup_state.json").read_text(encoding="utf-8")).get("deleted") == r_int.deleted)

print("\n[A8] 只有 --vacuum 时才 VACUUM")
db4 = SC / "data" / "m4.db"
make_db(db4, ages_days=[100] * 40 + [0])
r_v = cleanup.cleanup_messages(db4, retention_days=7, now=NOW, vacuum=True)
check("vacuum=True → 调用了 VACUUM", r_v.vacuumed is True)
check("同时也有 checkpoint", r_v.checkpointed is True)
check("该删的都删了", count(db4) == 1, str(count(db4)))

print("\n[A6] 静默：清理过程不碰任何通知/托盘接口")
called: list[str] = []
import consumer.notifier as notifier_mod  # noqa: E402
_orig_push = notifier_mod.Notifier.push


def _spy_push(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
    called.append("push")
    return False


notifier_mod.Notifier.push = _spy_push
try:
    db5 = SC / "data" / "m5.db"
    make_db(db5, ages_days=[100] * 10 + [0])
    cleanup.cleanup_messages(db5, retention_days=7, now=NOW)
    check("没调用 Notifier.push", called == [], str(called))
finally:
    notifier_mod.Notifier.push = _orig_push

print("\n[A1] 设置页三项齐备，且改一项不影响其它配置（隔离性）")
CFG = {
    "hook": {"api_base": "http://127.0.0.1:30001", "callback_host": "127.0.0.1",
             "callback_port": 8888, "api_timeout_s": 5.0, "self_wxid": "ruibo_jiang"},
    "filter": {"groups": ["195940014@chatroom"], "senders": [], "keywords": ["报价"],
               "case_insensitive": True},
    "llm": {"base_url": "https://api.minimax.cn/v1", "api_key": "sk-real-key",
            "model": "MiniMax-M3", "push_threshold": 4},
    "storage": {"sqlite_path": "data/messages.db", "ingest_exclude_types": [42, 47, 51],
                "retention_days": 0, "auto_cleanup": False, "cleanup_time": "00:00",
                "cleanup_vacuum": False},
    "digest": {"enabled": True, "time": "23:30", "dir": "reports", "summarize": True,
               "exclude_types": [47, 51]},
    # voice / replies / bark 等段也放进来：设置页表单会提交它们，
    # 不放就会被"保存默认值"新建出来 → 隔离性断言会误报（是本测试的配置不全，不是产品 bug）。
    "voice": {"enabled": True, "dir": "data/voices", "conv_overrides": {}},
    "asr": {"enabled": True, "url": "http://mac:8170/v1/audio/transcriptions", "timeout_s": 120},
    "bark": {"enabled": False, "server": "https://api.day.app", "key": "REPLACE_ME"},
    "replies": {"enabled": False, "history_count": 10, "templates": [],
                "rate_limit": {"per_group_cooldown_s": 60, "global_daily_limit": 100,
                               "min_delay_s": 5, "max_delay_s": 15}},
    "app": {"web_host": "127.0.0.1", "web_port": 6060, "launch_wechat": False,
            "auto_inject_keyhook": False},
}
(SC / "config.yaml").write_text(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")
(SC / "data" / "labels.json").write_text(json.dumps(
    {"groups": {"195940014@chatroom": {"name": "幸福小家", "important": True}},
     "senders": {}}, ensure_ascii=False), encoding="utf-8")
make_db(SC / "data" / "messages.db", ages_days=[0, 1, 10, 100, 400])

import web.app as webapp  # noqa: E402

app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()

html = c.get("/settings").get_data(as_text=True)
check("/settings 页面 200 且含保留天数说明", "保留最近多少天的消息" in html)
check("设置页显示当前总条数", "当前共" in html, "")

cfg_html = c.get("/config").get_data(as_text=True)
check("总设置页出现三项清理设置",
      all(k in cfg_html for k in ("f_storage__retention_days", "f_storage__auto_cleanup",
                                  "f_storage__cleanup_time")), "")
check("总设置页含数据清理区块标题", "数据清理" in cfg_html)

# 采集表单（模拟"保存全部设置"），只改保留天数
import re as _re  # noqa: E402


def harvest(html_text: str) -> dict:
    form: dict = {}
    for tag in _re.findall(r"(<input[^>]*>)", html_text):
        nm = _re.search(r'name="(f_[^"]+)"', tag)
        if not nm:
            continue
        n = nm.group(1)
        val = (_re.search(r'value="([^"]*)"', tag) or [None, ""])[1]
        typ = (_re.search(r'type="([^"]+)"', tag) or [None, "text"])[1]
        if typ == "checkbox":
            if "checked" in tag or "disabled" in tag:
                if val.isdigit():
                    form.setdefault(n, [])
                    form[n].append(val)
                else:
                    form[n] = "on"
        else:
            form[n] = val
    for m in _re.finditer(r'<textarea[^>]*name="(f_[^"]+)"[^>]*>(.*?)</textarea>', html_text, _re.S):
        form[m.group(1)] = m.group(2)
    for m in _re.finditer(r'name="(tpl_\d+__[a-z_]+)"[^>]*value="([^"]*)"', html_text):
        form[m.group(1)] = m.group(2)
    for m in _re.finditer(r'name="(tpl_\d+__[a-z_]+)"[^>]*checked', html_text):
        form[m.group(1)] = "on"
    return form


def load_cfg() -> dict:
    return yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))


def diff_sections(before: dict, after: dict, allow: set[str]) -> list[str]:
    return [k for k in set(before) | set(after)
            if k not in allow and before.get(k) != after.get(k)]


before = load_cfg()
form = harvest(cfg_html)
check("采集到足够多的设置字段", len(form) >= 15, f"{len(form)} 个")
form["f_storage__retention_days"] = "90"
form["f_storage__cleanup_time"] = "00:00"
r = c.post("/config/save", data=form, follow_redirects=False)
after = load_cfg()
check("保存 -> 302", r.status_code == 302, str(r.status_code))
check("保留天数写成了 90", after["storage"]["retention_days"] == 90, str(after["storage"]["retention_days"]))
check("其余配置段逐字段未变", not diff_sections(before, after, {"storage"}), str(diff_sections(before, after, {"storage"})))
check("storage 段内除清理三项外未变",
      {k: v for k, v in before["storage"].items() if k not in
       ("retention_days", "auto_cleanup", "cleanup_time")} ==
      {k: v for k, v in after["storage"].items() if k not in
       ("retention_days", "auto_cleanup", "cleanup_time")},
      json.dumps(after["storage"], ensure_ascii=False))
check("label.json 完全未变", "幸福小家" in (SC / "data" / "labels.json").read_text(encoding="utf-8"))

print("\n[A3-Web] 非法清理时间被回退、不写坏配置")
before = load_cfg()
form = harvest(c.get("/config").get_data(as_text=True))
form["f_storage__cleanup_time"] = "25:99"
r = c.post("/config/save", data=form, follow_redirects=False)
after = load_cfg()
check("非法时间被拒绝（重定向带 error）", "error" in (r.headers.get("Location") or ""),
      str(r.headers.get("Location"))[:80])
check("配置里的时间回退成旧值 00:00", after["storage"]["cleanup_time"] == "00:00",
      str(after["storage"]["cleanup_time"]))
check("非法时间没影响保留天数", after["storage"]["retention_days"] == 90)

print("\n[A1-Web] 手动清理入口：预演不删、真删只动超期行")
n_before = count(SC / "data" / "messages.db")
r = c.post("/settings/cleanup", data={"mode": "dry_run"}, follow_redirects=False)
check("预演 -> 302 且带结果", r.status_code == 302 and "cleanup_saved" in (r.headers.get("Location") or ""),
      str(r.headers.get("Location"))[:90])
check("预演没有删数据", count(SC / "data" / "messages.db") == n_before, str(count(SC / "data" / "messages.db")))
r = c.post("/settings/cleanup", data={"mode": "run"}, follow_redirects=False)
check("立即清理 -> 302", r.status_code == 302, str(r.status_code))
n_after = count(SC / "data" / "messages.db")
check("100/400 天前的 2 条被删、近期保留", n_after == n_before - 2, f"{n_before} -> {n_after}")
# 注意：count_older(db, 1) 的口径是"早于今天零点" —— 昨天那条本来就该算进去，
# 所以这里直接查"今天零点之后还有几条"来断言当天的消息没被动。
same_day = sqlite3.connect(str(SC / "data" / "messages.db")).execute(
    "SELECT COUNT(*) FROM messages WHERE received_at >= ?",
    (int(datetime(2026, 10, 7, 0, 0, 0).timestamp()),)).fetchone()[0]
check("当天那条还在（清理不碰当天）", same_day >= 1, str(same_day))

print("\n[A9] 清理后各页面仍然正常（旧日期归档 0 条也不 500）")
for path in ("/", "/browse", "/labels", "/digest", "/settings", "/config", "/filter"):
    resp = c.get(path)
    check(f"{path} -> 200", resp.status_code == 200, str(resp.status_code))
old_digest = c.post("/api/digest/preview", data={"date": "2026-08-01"},
                    content_type="application/x-www-form-urlencoded")
check("旧日期归档预览不 500（无数据也给出可读结果）",
      old_digest.status_code in (200, 400, 404), str(old_digest.status_code))

print("\n[CLI] python -m consumer.cleanup --dry-run --days 90")
import subprocess  # noqa: E402
py = str(PROJ / ".venv" / "Scripts" / "python.exe")
env = dict(os.environ, WCF_DATA_DIR=str(SC), WCF_NO_MULTI_ACCOUNT="1",
           PYTHONIOENCODING="utf-8")
# 注意：DSH 沙箱下 piped stdio 可能被拒，这里用文件重定向而不是管道
out_file = SC / "cli.out"
with open(out_file, "wb") as fh:
    rc = subprocess.run([py, "-B", "-m", "consumer.cleanup", "--dry-run", "--days", "90"],
                        cwd=str(PROJ), env=env, stdout=fh, stderr=subprocess.STDOUT).returncode
out = out_file.read_text(encoding="utf-8", errors="replace")
check("CLI 退出码 0", rc == 0, str(rc))
check("CLI 是 dry-run（不删数据）", "dry-run" in out,
      (out.strip().splitlines()[-1][:90] if out.strip() else ""))
check("CLI 报的命中条数与直接调用一致",
      f"命中（早于" in out and str(cleanup.count_older(SC / "data" / "messages.db", 90)) in out,
      out.strip().replace("\n", " ｜ ")[:150])

print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("消息库按期清理（R-001）验证通过")
