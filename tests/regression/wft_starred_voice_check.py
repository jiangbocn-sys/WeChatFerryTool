"""回归：加星成员语音的**发送时间**必须出现在总结里（2026-10-13 用户报的问题）。

用户现象
--------
他在「易青岚乙巳年弟子群」的总结提示里写了：
  "如果加星号人员发送了语音信息，提示语音信息发送时间，连续的语音信息提示第一条的时间即可"
但最近一次总结**只顺带提了一处**。

排查结论（三环，都在本套件里锁住）
--------------------------------
1. **全局规则 2 写着「输出不需要逐条标注时间」** → 与本群提示**直接冲突**，
   模型倾向遵守更显眼的全局格式规则 → 现在规则 9（群提示优先级）里**明确写了
   "提示要求输出时间就照做，第 2 条是默认值、被本群提示覆盖"**。
2. **加星成员的语音常常没有转写文本**（PC 微信不落盘音频，实测 33 条里 0 条有转写），
   行里只是"内容不可得"占位符 → 模型按规则 6「忽略无实质内容」丢掉 →
   现在由 `digest.starred_voice_note()` **确定性提取**"谁在几点发了语音"并放进 prompt。
3. 清单曾被插到"请开始总结"**之后**（像附录，容易被忽略）→ 现在插在**之前**。

覆盖：
  A. `starred_voice_note`：只统计**加星**成员、按人合并、连续语音合并时段、跳过非语音
  B. 无加星 / 无语音 → 返回空（不产生噪音）
  C. `build_prompt(rows=...)`：清单进 prompt、位置在收尾指令**之前**、含"请回群自行查看"要求
  D. 不传 rows / 无语音时 prompt 里**没有**这段（不影响其它群）
  E. 规则 9 写明"提示要求时间就照做"（解决与规则 2 的冲突）
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-starvoice-suite")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from consumer.digest import starred_voice_note  # noqa: E402
from consumer.summarize import build_prompt  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    line = ("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if not cond:
        FAILS.append(name)


GID = "g1@chatroom"
STAR = "wxid_star"          # 加星成员
OTHER = "wxid_other"        # 非加星
T0 = int(datetime(2026, 10, 12, 12, 44).timestamp())


def row(mid, sender, mt, ts, transcript=""):
    return {"msg_id": mid, "group_name": GID, "sender": sender, "msg_type": mt,
            "received_at": ts, "transcript": transcript, "content": ""}


LABELS = {
    "groups": {GID: {"name": "测试群", "focus_members": [STAR]}},
    "senders": {STAR: {"name": "周宏伟"}, OTHER: {"name": "路人甲"}},
}

print("[A] starred_voice_note：只统计加星成员、按人合并、连续语音合成时段")
rows = [
    row("v1", STAR, 34, T0),                 # 12:44
    row("v2", STAR, 34, T0 + 10),            # 12:44:10 连续 → 同一串
    row("v3", STAR, 34, T0 + 20),
    row("v4", STAR, 34, T0 + 3600),          # 13:44 新一串
    row("t1", STAR, 1, T0 + 30),             # 文本，不该进
    row("v5", OTHER, 34, T0 + 40),           # 非加星，不该进
]
note = starred_voice_note(rows, LABELS)
check("有内容", bool(note.strip()))
check("只出现加星成员", "周宏伟" in note and "路人甲" not in note, note[:120])
check("总条数正确（4 条，不含文本与非加星）", "共 4 条语音" in note, note[:160])
check("连续语音合并成一段并给**首条时间**", "12:44 起（3 条）" in note, note[:200])
check("另一串单独列出", "13:44" in note, note[:200])
check("只列时间与条数（清单本身不含对语音内容的描述）",
      "说了" not in note and "讲解" not in note, note[:160])
check("标题说明内容未获取、请提醒读者",
      "内容未获取" in note and "回群自行查看" in note, note[:160])

print("\n[B] 空情形：不产生噪音")
check("没有加星成员 → 空", starred_voice_note(rows, {"groups": {GID: {"focus_members": []}}}) == "")
check("没有语音 → 空",
      starred_voice_note([row("t1", STAR, 1, T0)], LABELS) == "")
check("空 rows → 空", starred_voice_note([], LABELS) == "")
check("空 labels → 空", starred_voice_note(rows, {}) == "")

print("\n[C] build_prompt：清单进 prompt 且位置正确")
grouped = {GID: ["[12:44]★周宏伟（语音转文本）：[语音 16秒·未获取到语音文件，内容不可得]"]}
# 规则 9（群提示优先级）只有在**有群提示**时才渲染，所以这里给个真提示
# （就是用户实际写的那句：加星人员发语音要提示发送时间）
LABELS_HINT = {
    "groups": {GID: {"name": "测试群", "focus_members": [STAR],
                     "summary_hint": "如果加星号人员发送了语音信息，提示语音信息发送时间"}},
    "senders": LABELS["senders"],
}
prompt = build_prompt("2026-10-12", grouped, LABELS_HINT, rows=rows)
check("prompt 含语音清单", "加星（重点关注）成员的语音发送时间" in prompt)
check("prompt 含提取出的时间", "12:44" in prompt)
check("prompt 要求写「提醒」行", "提醒：本时段" in prompt)
check("prompt 要求读者回群自行查看", "请回群自行查看" in prompt)
i_note = prompt.find("加星（重点关注）成员的语音发送时间")
i_tail = prompt.find("请开始总结")
check("**清单位于收尾指令之前**（放后面会被当附录忽略）", 0 < i_note < i_tail,
      f"{i_note} vs {i_tail}")
check("清单要求在收尾指令之前", prompt.find("请回群自行查看") < i_tail)

print("\n[D] 不影响其它情形")
p_no_rows = build_prompt("2026-10-12", grouped, LABELS)
check("不传 rows → 没有清单段",
      "加星（重点关注）成员的语音发送时间" not in p_no_rows)
p_no_voice = build_prompt("2026-10-12", grouped, LABELS,
                          rows=[row("t1", STAR, 1, T0)])
check("传了 rows 但没有加星语音 → 没有清单段",
      "加星（重点关注）成员的语音发送时间" not in p_no_voice)

print("\n[E] 规则 9 解决与规则 2 的冲突（提示要求时间就照做）")
check("规则 9 提到「提示里如果要求输出时间」", "提示里如果要求输出时间" in prompt)
check("规则 9 说明第 2 条是默认值、被本群提示覆盖",
      "是**默认值**，被本群提示覆盖" in prompt, "")
check("prompt 里保留了全局规则 2（未删，只是标明可被覆盖）",
      "输出**不需要**逐条标注时间" in prompt)

print("\n[F] Consumer 接线：自动总结会把原始行传进去")
import inspect  # noqa: E402
import consumer.summarize as S  # noqa: E402
s_src = inspect.getsource(S.summarize)
check("summarize() 取窗口原始行", "win_rows = load_window_rows(" in s_src, "")
check("summarize() 把 rows 传给 build_prompt", "rows=win_rows" in s_src)
check("load_window_rows 与归档同窗口",
      "_digest_window_for" in inspect.getsource(S.load_window_rows))

print("\n[G] **总结层**兜底：模型不写提醒行也要保证它在（10-15 用户报）")
# 2026-10-15 实测：prompt 里给了清单（可贞 41 条 + 周少彬 4 条），但该群模型就是没写
# 提醒行 → 归档稿没有、提炼层无从保留。所以兜底必须**落在总结这一层**。
from consumer.digest import reminder_line, replace_reminder  # noqa: E402
from consumer.summarize import _ensure_reminder_in_summary  # noqa: E402
from consumer.group_summary import ensure_reminder, extract_reminder  # noqa: E402

ROWS2 = [
    {"msg_id": "a", "group_name": GID, "sender": STAR, "msg_type": 34,
     "received_at": T0, "transcript": "", "content": ""},
    {"msg_id": "b", "group_name": GID, "sender": STAR, "msg_type": 34,
     "received_at": T0 + 60, "transcript": "", "content": ""},
]
WANT = reminder_line(ROWS2, LABELS)
check("reminder_line 生成成品行", WANT.startswith("- 提醒：本时段") and "12:44" in WANT, WANT)
check("reminder_line 含人名与条数", "周宏伟" in WANT and "2 条语音" in WANT, WANT)
check("reminder_line 说明内容未获取 + 请回群",
      "内容未获取" in WANT and "请回群自行查看" in WANT)
check("无加星语音时 reminder_line 为空",
      reminder_line([{**ROWS2[0], "msg_type": 1}], LABELS) == "")

fake = "- 讲了取用神\n- 安排了明晚纳甲课"
got = _ensure_reminder_in_summary(fake, GID, "测试群", ROWS2, LABELS)
check("**模型没写 → 兜底补到正文里**", "提醒：本时段" in got and "12:44" in got, got[-70:])
check("兜底加在最后一行", got.rstrip().splitlines()[-1].startswith("- 提醒"), got[-60:])
bad = fake + "\n- 提醒：本时段 有语音（内容未获取），请回群自行查看"
got2 = _ensure_reminder_in_summary(bad, GID, "测试群", ROWS2, LABELS)
check("**模型写歪 → 规范成程序生成的确切文本**",
      got2.count("提醒：本时段") == 1 and "12:44" in got2, got2[-80:])
check("没有加星语音时不凭空加",
      _ensure_reminder_in_summary(fake, GID, "测试群",
                                  [{**ROWS2[0], "msg_type": 1}], LABELS) == fake)
check("replace_reminder 无提醒行时补在最后",
      replace_reminder(fake, WANT).rstrip().splitlines()[-1].startswith("- 提醒"))
refined = "- 归纳一\n- 提醒：本时段 有语音，请回群自行查看"
fixed = ensure_reminder(refined, "x", want=WANT)
check("提炼层规范成同一份文本（归档稿 = 发群稿）",
      extract_reminder(fixed) == WANT, extract_reminder(fixed)[:90])

print("\n[H] **绝不串群**：本群没有加星语音就不能出现提醒（10-15 用户报）")
# 用户报："发起语音的提示串群了，可贞并没有在乙巳年弟子群中发语音"。
# 根因：兜底里写了 `fixed or rows` —— 某群过滤后为空时退回**全部会话**的行，
# 于是岳章形家风水班的 ★可贞 串进了乙巳年弟子群的总结。
G2 = "g2@chatroom"
ROWS_MIX = ROWS2 + [
    {"msg_id": "c", "group_name": G2, "sender": "wxid_other_star", "msg_type": 34,
     "received_at": T0 + 300, "transcript": "", "content": ""},
]
LAB2 = {
    "groups": {
        GID: {"name": "甲群", "focus_members": [STAR]},
        G2: {"name": "乙群", "focus_members": ["wxid_other_star"]},
    },
    "senders": {STAR: {"name": "周宏伟"}, "wxid_other_star": {"name": "可贞"}},
}
only_g2 = reminder_line([r for r in ROWS_MIX if r["group_name"] == G2], LAB2)
check("乙群自己的提醒只含乙群的人", "可贞" in only_g2 and "周宏伟" not in only_g2, only_g2)
empty_rows = [r for r in ROWS_MIX if r["group_name"] == "g3@chatroom"]
check("没有该群的行时 reminder_line 为空（不能拿别群顶）",
      reminder_line(empty_rows, LAB2) == "")
# 关键：给甲群做兜底时，绝不能把乙群的可贞写进去
out_g1 = _ensure_reminder_in_summary(fake, GID, "甲群", ROWS_MIX, LAB2)
check("**甲群兜底不含乙群的可贞**", "可贞" not in out_g1, out_g1[-90:])
check("甲群兜底含自己的周宏伟", "周宏伟" in out_g1)
# 该群一条加星语音都没有 → 不加任何提醒行
no_voice_g1 = [r for r in ROWS_MIX if r["group_name"] == GID and r["msg_type"] != 34]
check("本群无加星语音 → 不补提醒行（即使别群有）",
      _ensure_reminder_in_summary(fake, GID, "甲群", no_voice_g1 + ROWS_MIX[2:], LAB2) == fake)
# 识别要宽容，否则会补出第二条（上午岳章那份就出现两条）
loose = fake + "\n- 提醒：本时段 有语音，回群自行查看"
check("模型漏写「请」字也能识别（不会补出第二条）",
      _ensure_reminder_in_summary(loose, GID, "甲群", ROWS_MIX, LAB2).count("提醒：本时段") == 1)
check("extract_reminder 认「提醒：本时段」开头即可",
      extract_reminder("- 提醒：本时段 随便写的什么").startswith("- 提醒"))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("加星语音发送时间进总结（确定性提取 + 提醒行 + 规则冲突修正）验证通过")
