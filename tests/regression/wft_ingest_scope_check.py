"""R-002 回归：**抓取范围全开 + 监控名单只管归档/总结**（2026-10-07 用户确认）。

背景：改动前 `filter.groups`（监控名单）同时是"入库白名单"，名单外的群消息连库都进不去
（10-07 实测：文璟和颂业主群 9:47 的消息在系统里查无此条，因为该群不在名单里）。
现在：

  * 入库**不看** filter.groups / filter.senders / filter.keywords —— 只受"入库类型闸门"
    （storage.ingest_exclude_types，表情 47 恒排除）限制；
  * 归档/总结范围仍由 digest._monitored_groups()（= filter.groups）决定。

本套用**真 Consumer 实例 + 假 hook** 端到端验证入库，再单独验归档判定。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-ingest-scope")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

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


MON_GROUP = "a@chatroom"          # 在监控名单里
OTHER_GROUP = "b@chatroom"        # **不在**监控名单里（改动前会被整条丢掉）
PRIVATE = "wxid_someone"          # 私聊（不在名单里）

CFG = {
    "hook": {"api_base": "http://127.0.0.1:30001", "callback_host": "127.0.0.1",
             "callback_port": 8888, "api_timeout_s": 5.0, "self_wxid": "wxid_me"},
    # 监控名单只留 a@chatroom；关键词故意留一个命中不了的，验证"关键词也不再影响入库"
    "filter": {"groups": [MON_GROUP], "senders": [], "keywords": ["绝不匹配的词"],
               "case_insensitive": True},
    "llm": {"base_url": "https://x/v1", "api_key": "k", "model": "m", "push_threshold": 4},
    # ⚠️ 必须用**绝对路径**：`store.Store` 是按**当前工作目录**解析相对路径的
    #    （只有 app 会 chdir 到数据根）。写成 "data/messages.db" 时，若测试进程的 CWD
    #    是项目目录，就会打开**项目里的老库**（10-05 那份），而不是沙箱 —— 10-07 踩到。
    "storage": {"sqlite_path": str(SC / "data" / "messages.db"), "ingest_exclude_types": [47, 51]},
    "digest": {"enabled": False, "time": "23:30", "dir": "reports",
               "exclude_types": [47, 51, 10000, 10002]},
    "voice": {"enabled": False},
    "asr": {"enabled": False},
    "app": {"launch_wechat": False, "auto_inject_keyhook": False},
}
(SC / "config.yaml").write_text(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")
(SC / "data" / "labels.json").write_text(
    '{"groups": {"a@chatroom": {"name": "甲群"}, "b@chatroom": {"name": "乙群"}}, "senders": {}}',
    encoding="utf-8")

from consumer import main as cmain  # noqa: E402


class _FakeHook:
    def status(self):        return {"IsLogin": 1, "hWeixin": 1}
    def set_callback(self, url): return {"ret": 0}
    def query_db(self, *a, **k): return {"status": -1}


class _FakeScorer:
    def score(self, **k):    return 1, "score_skipped"


cfg = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
cons = cmain.Consumer(cfg)
cons.hook = _FakeHook()
cons.scorer = _FakeScorer()

print("[1] 入库不再受监控名单/关键词限制（R-002 核心）")
cases = [
    {"group": MON_GROUP, "sender": "wxid_me", "desc": "名单里的群", "want": 1},
    {"group": OTHER_GROUP, "sender": "wxid_other", "desc": "**名单外的群**（改动前会被丢掉）", "want": 1},
    {"group": PRIVATE, "sender": "wxid_someone", "desc": "名单外的私聊", "want": 1},
    {"group": MON_GROUP, "sender": "wxid_kw", "desc": "完全不命中关键词的内容", "want": 1},
    {"group": MON_GROUP, "sender": "wxid_emoji", "desc": "表情 47 不该入库", "type": 47, "want": 0},
    {"group": MON_GROUP, "sender": "wxid_sys", "desc": "系统 51 不该入库", "type": 51, "want": 0},
]
for i, case in enumerate(cases):
    group = case["group"]
    mtype = case.get("type", 1)
    cons.handle_message({
        "event_type": 1001, "type": mtype, "msgid": f"m{i}", "wxid": group,
        "roomid": group if group.endswith("@chatroom") else "",
        "sender": case["sender"], "content": f"{case['desc']}-{i}",
        "timestamp": int(datetime.now().timestamp()),
    })
    n = cons.store._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE msg_id = ?", (f"m{i}",)).fetchone()[0]
    check(f"入库判定：{case['desc']}", n == case["want"], f"库里 {n} 条，期望 {case['want']}")

check("类型闸门仍然有效（表情 47/系统 51 不入库）",
      cons.store._conn.execute(
          "SELECT COUNT(*) FROM messages WHERE msg_type IN (47, 51)").fetchone()[0] == 0)

print("\n[2] 归档范围仍由监控名单决定（不能让全抓把归档也放开）")
from consumer import digest as digest_mod  # noqa: E402

mon = digest_mod._monitored_groups()
check("_monitored_groups() 读到的是 filter.groups", mon == {MON_GROUP}, str(mon))
excl = {47, 51, 10000, 10002}
labels = {"groups": {"a@chatroom": {"name": "甲群"}, "b@chatroom": {"name": "乙群"}},
          "senders": {}}
row_mon = {"group_name": MON_GROUP, "sender": "wxid_x", "msg_type": 1, "content": "普通内容",
           "transcript": None}
row_other = {"group_name": OTHER_GROUP, "sender": "wxid_x", "msg_type": 1, "content": "普通内容",
             "transcript": None}
row_pv = {"group_name": PRIVATE, "sender": "wxid_x", "msg_type": 1, "content": "普通内容",
          "transcript": None}
check("监控名单里的群 → 进归档", digest_mod._in_digest(row_mon, labels, excl, mon) is True)
check("**名单外的群 → 不入归档**（但已入库）",
      digest_mod._in_digest(row_other, labels, excl, mon) is False)
check("名单外的私聊 → 不入归档（只有★重点联系人才进）",
      digest_mod._in_digest(row_pv, labels, excl, mon) is False)

print("\n[2b] 归档范围（10-07 第二版）：监控群整群对话 + 重点人只打 ★ 不过滤")
row_mon = {"group_name": MON_GROUP, "sender": "wxid_anyone", "msg_type": 1,
           "content": "普通路人说话", "transcript": None, "priority": 0}
row_focus = {"group_name": MON_GROUP, "sender": "wxid_focus", "msg_type": 1,
             "content": "重点人说话", "transcript": None, "priority": 1}
labels_focus = {"groups": {MON_GROUP: {"name": "甲群", "focus_members": ["wxid_focus"]}},
                "senders": {"wxid_focus": {"name": "重点人", "important": True}}}
check("**监控群 + 设了重点人 → 路人照样进归档**（上下文优先）",
      digest_mod._in_digest(row_mon, labels_focus, excl, mon) is True)
check("重点人也进归档", digest_mod._in_digest(row_focus, labels_focus, excl, mon) is True)
check("重点人会被标记 ★", digest_mod._is_star(row_focus, labels_focus) is True)
check("路人不会被标记 ★", digest_mod._is_star(row_mon, labels_focus) is False)
check("不在监控名单的群仍不进归档",
      digest_mod._in_digest({"group_name": "zz@chatroom", "sender": "wxid_x", "msg_type": 1,
                             "content": "x", "transcript": None},
                            labels_focus, excl, mon) is False)
# 图片(3) 目前无法把内容交给模型（本地是加密 .dat、微信只在内存解密）→ 归到归档闸门里排除，
# 避免 prompt 里出现一堆只有 `[图片 214×480]` 的空占位（10-07 用户决定：只传文本+语音转写）
row_img = {"group_name": MON_GROUP, "sender": "wxid_img", "msg_type": 3,
           "content": '<msg><img aeskey="x" cdnthumbwidth="214" cdnthumbheight="480"/></msg>',
           "transcript": None}
check("图片被归档闸门排除时不进归档",
      digest_mod._in_digest(row_img, labels_focus, {3, 47, 51, 10000}, mon) is False)
check("没排除图片时它仍会进归档（闸门是配置驱动的）",
      digest_mod._in_digest(row_img, labels_focus, {47, 51, 10000}, mon) is True)
check("真实 config.yaml 已把图片(3) 排除出归档",
      3 in (yaml.safe_load((PROJ / "config.yaml").read_text(encoding="utf-8"))
            .get("digest", {}).get("exclude_types") or []),
      str(yaml.safe_load((PROJ / "config.yaml").read_text(encoding="utf-8"))
          .get("digest", {}).get("exclude_types")))

print("\n[3] 全抓之后数据量确实变大（这就是这个需求的代价，写下来备查）")
cons.handle_message({"event_type": 1001, "type": 1, "msgid": "bulk", "wxid": OTHER_GROUP,
                     "roomid": OTHER_GROUP, "sender": "wxid_bulk",
                     "content": "名单外群的消息", "timestamp": int(datetime.now().timestamp())})
total = cons.store._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
check("名单外群的消息确实落库了", total >= 4, f"库里共 {total} 条")

print("\n[4] 日志文案：入库行不再提 reason（旧格式含 reason=group_match）")
import io  # noqa: E402
import logging  # noqa: E402

buf = io.StringIO()
h = logging.StreamHandler(buf)
h.setLevel(logging.INFO)
logging.getLogger("consumer.main").addHandler(h)
cons.handle_message({"event_type": 1001, "type": 1, "msgid": "logcheck", "wxid": OTHER_GROUP,
                     "roomid": OTHER_GROUP, "sender": "wxid_log",
                     "content": "看日志格式", "timestamp": int(datetime.now().timestamp())})
logging.getLogger("consumer.main").removeHandler(h)
out = buf.getvalue()
check("日志里有入库行", "入库 [" in out, out.strip()[-80:])
check("日志里不再出现旧过滤原因 reason=group_match", "reason=group_match" not in out,
      out.strip()[-80:])

print("\n[5] LLM 逐条评分默认关闭（llm.score_enabled=false）")
calls: list[tuple] = []


class _SpyScorer:
    def score(self, **kw):
        calls.append(("score", kw.get("content", "")[:20]))
        return 5, "spy"


cons.scorer = _SpyScorer()
cons.score_enabled = False
cons.handle_message({"event_type": 1001, "type": 1, "msgid": "noscore", "wxid": MON_GROUP,
                     "roomid": MON_GROUP, "sender": "wxid_ns", "content": "这条不该被评分",
                     "timestamp": int(datetime.now().timestamp())})
check("关掉后**完全没调用** scorer", calls == [], str(calls))
row = cons.store._conn.execute(
    "SELECT score, score_reason FROM messages WHERE msg_id='noscore'").fetchone()
check("关掉后库里不写 score", row is not None and row[0] is None and row[1] is None,
      f"score={row[0] if row else '?'} reason={row[1] if row else '?'}")

# 打开开关时应恢复评分（用 spy 验证调用发生）
calls.clear()
cons.score_enabled = True
cons.handle_message({"event_type": 1001, "type": 1, "msgid": "withscore", "wxid": MON_GROUP,
                     "roomid": MON_GROUP, "sender": "wxid_ws", "content": "这条应该被评分",
                     "timestamp": int(datetime.now().timestamp())})
check("打开后重新调用 scorer", len(calls) == 1, str(calls))
row2 = cons.store._conn.execute(
    "SELECT score FROM messages WHERE msg_id='withscore'").fetchone()
check("打开后写入了 score", row2 is not None and row2[0] == 5, str(row2))
cons.score_enabled = False     # 复位，避免影响后续

print("\n[6] 新装的默认配置里 score_enabled 是 false")
import setup as setup_mod  # noqa: E402
default_cfg = setup_mod.default_config({"llm_base_url": "https://x/v1", "llm_api_key": "k",
                                        "llm_model": "m"})
check("setup.default_config() 里 score_enabled=False",
      default_cfg.get("llm", {}).get("score_enabled") is False,
      str(default_cfg.get("llm", {}).get("score_enabled")))
ex = (PROJ / "config.example.yaml").read_text(encoding="utf-8")
check("config.example.yaml 里写了 score_enabled: false",
      "score_enabled: false" in ex)
app_src = (PROJ / "web" / "app.py").read_text(encoding="utf-8")
check("总设置页有该开关且标了默认关闭", "llm.score_enabled" in app_src and "默认关闭" in app_src)

cons.stop()
shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("抓取范围全开 / 监控名单只管归档（R-002）验证通过")
