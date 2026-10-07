"""Web 端当日总结链路回归：/digest 预览 + 生成（假 LLM，返回真实 MiniMax 风格的 <think> 块）。

覆盖 10-06 的两处修复：config_path 口径（配置读写落到数据根）、summarize 剥离 <think>。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SCRATCH = Path(r"D:\projects\.wft-web3-scratch")
REAL_CFG = PROJ / "config.yaml"
REAL_LABELS = PROJ / "accounts" / "ruibo_jiang_542e" / "data" / "labels.json"

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


cfg_before, labels_before = sha(REAL_CFG), sha(REAL_LABELS)

shutil.rmtree(SCRATCH, ignore_errors=True)
(SCRATCH / "data").mkdir(parents=True, exist_ok=True)
(SCRATCH / "reports").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SCRATCH)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))
shutil.copy2(REAL_LABELS, SCRATCH / "data" / "labels.json")
shutil.copy2(PROJ / "accounts" / "ruibo_jiang_542e" / "data" / "messages.db",
             SCRATCH / "data" / "messages.db")
shutil.copy2(REAL_CFG, SCRATCH / "config.yaml")            # 配置也放数据根 → 走新口径

# 真实 MiniMax-M3 那次调用返回的形状（推理块 + 正文）
LEGACY_REPLY = (
    "<think>The user wants me to summarize the group chat.\n"
    "Let me analyze the messages chronologically.</think>\n"
    '- 学员确认今晚无课，改到明晚并会另行通知\n'
    '- 讨论「龙水拖出/歪哥路」与「白虎过堂」对判断的影响，结论是青龙空虚时连钓鱼的念头都不敢有'
)

import web.app as webapp  # noqa: E402
from consumer import summarize as summarize_mod  # noqa: E402

check("config_path 指向数据根（新口径）", webapp.CONFIG_PATH == SCRATCH / "config.yaml",
      str(webapp.CONFIG_PATH))

calls = {"n": 0}


def fake_llm(prompt, llm, timeout_s=180.0):
    calls["n"] += 1
    # 顺带验证：Web 生成走的是数据根配置里配的模型
    check("生成时读到了数据根 config.yaml 的 llm 段",
          str(llm.get("model")) == "MiniMax-M3" and "minimax" in str(llm.get("base_url")),
          f'{llm.get("model")} @ {llm.get("base_url")}')
    return LEGACY_REPLY


summarize_mod.call_llm = fake_llm  # type: ignore[assignment]

app = webapp.create_app()
app.config.update(TESTING=True)
c = app.test_client()

print("[1] 页面与配置页在数据根口径下仍正常")
for path in ("/", "/settings", "/labels", "/digest", "/browse", "/types", "/filter", "/replies", "/api/health"):
    check(f"GET {path} -> 200", c.get(path).status_code == 200)

r = c.post("/settings/update", data={"exclude_types": ["51", "10002"]}, follow_redirects=True)
check("消息分类保存 -> 200/302", r.status_code in (200, 302), str(r.status_code))
saved_cfg = webapp.CONFIG_PATH.read_text(encoding="utf-8")
check("保存写进了数据根的 config.yaml（没碰真实配置）",
      "ingest_exclude_types" in saved_cfg and str(SCRATCH) not in saved_cfg)
check("真实 config.yaml 未被写", sha(REAL_CFG) == cfg_before)

print("\n[2] /digest 预览（不联网）")
r = c.post("/api/digest/preview", json={"date": "2026-10-05"})
j = r.get_json()
check("预览 200 且返回 prompt", r.status_code == 200 and j.get("ok") and j.get("chars", 0) > 0,
      f"chars={j.get('chars')} total={j.get('total')}")
check("预览没有调用 LLM", calls["n"] == 0)

print("\n[3] /digest 真实生成（假 LLM）→ 验证 <think> 已被剥掉")
r = c.post("/api/digest/summarize", json={"date": "2026-10-05"})
check("提交 202", r.status_code == 202, str(r.status_code))
st = {}
for _ in range(200):
    st = c.get("/api/digest/summarize/status").get_json()
    if not st["running"]:
        break
    time.sleep(0.2)
res = st.get("result") or {}
check("生成完成无错误", not st.get("error"), str(st.get("error"))[:80])
check("LLM 被调用一次", calls["n"] == 1, str(calls["n"]))
check("返回内容里没有 <think>", "<think>" not in (res.get("content") or ""),
      repr((res.get("content") or "")[:60]))
check("返回内容含要点正文", "- 学员确认今晚无课" in (res.get("content") or ""),
      repr((res.get("content") or "")[:40]))

_sums = sorted((SCRATCH / "reports").glob("summary-2026-10-05*.md"))
out = _sums[0] if _sums else (SCRATCH / "reports" / "summary-2026-10-05.md")
check("总结文件落在数据根 reports/（每群一份）", bool(_sums), str([x.name for x in _sums]))
text = out.read_text(encoding="utf-8")
check("文件里没有推理块", "<think>" not in text)
check("文件里正文要点在", "- 学员确认今晚无课" in text and "## 附：提交给模型的原始记录" in text)
check("文件头部标了模型与接口",
      "MiniMax-M3" in text.split("---")[0] and "api.minimax.cn" in text.split("---")[0])

print("\n[4] 归档生成 + 查看/下载")
r = c.post("/api/digest/generate", json={"date": "2026-10-05"})
check("只生成归档 200", r.status_code == 200 and (SCRATCH / "reports" / "digest-2026-10-05.md").is_file())
check("查看总结 200", c.get(f"/reports/{out.name}").status_code == 200)
check("下载总结 200", c.get(f"/reports/{out.name}/download").status_code == 200)
check("目录穿越 404", c.get("/reports/../../config.yaml").status_code == 404)
body = c.get("/digest?date=2026-10-05").get_data(as_text=True)
check("/digest 页列出总结文件", out.name in body, out.name)

print("\n[5] 真实文件完好")
check("真实 config.yaml sha256 不变", sha(REAL_CFG) == cfg_before)
check("真实 labels.json sha256 不变", sha(REAL_LABELS) == labels_before)

print("\n" + "=" * 60)
shutil.rmtree(SCRATCH, ignore_errors=True)
if FAILS:
    print("FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("Web 当日总结链路回归全部通过")
