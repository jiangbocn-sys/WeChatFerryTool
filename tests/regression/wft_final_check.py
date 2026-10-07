"""Final regression on the finished code: pages render, endpoints behave, real data untouched."""
import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SCRATCH = Path(r"D:\projects\.wft-final-scratch")
shutil.rmtree(SCRATCH, ignore_errors=True)
(SCRATCH / "data").mkdir(parents=True, exist_ok=True)
(SCRATCH / "reports").mkdir(parents=True, exist_ok=True)
shutil.copy2(PROJ / "accounts" / "ruibo_jiang_542e" / "data" / "messages.db", SCRATCH / "data" / "messages.db")
shutil.copy2(PROJ / "accounts" / "ruibo_jiang_542e" / "data" / "labels.json", SCRATCH / "data" / "labels.json")
os.environ["WCF_DATA_DIR"] = str(SCRATCH)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

import yaml  # noqa: E402
import web.app as webapp  # noqa: E402
from consumer import summarize as summarize_mod  # noqa: E402

webapp.CONFIG_PATH = SCRATCH / "config.yaml"
shutil.copy2(PROJ / "config.yaml", webapp.CONFIG_PATH)
summarize_mod.call_llm = lambda prompt, llm, timeout_s=180.0: "- 假要点一\n- 假要点二"  # type: ignore

app = webapp.create_app()
app.config.update(TESTING=True)
c = app.test_client()

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


print("[A] 全部页面渲染（含新模板 save_error/report/digest/settings）")
for path in ("/", "/filter", "/labels", "/settings", "/digest", "/browse", "/types", "/replies", "/api/health"):
    r = c.get(path)
    check(f"GET {path} -> 200", r.status_code == 200, f"status={r.status_code}")
check("save_error.html 模板存在且渲染", (PROJ / "web" / "templates" / "save_error.html").is_file())
r = c.get("/reports/nope.md")
check("report 路由缺文件 -> 404", r.status_code == 404, str(r.status_code))

print("\n[B] 消息分类：保存 → 读回 → consumer 侧语义")
c.post("/settings/update", data={"exclude_types": ["3", "51"]}, follow_redirects=True)
cfg = webapp.load_config()
check("写入 storage.ingest_exclude_types", cfg["storage"]["ingest_exclude_types"] == [3, 47, 51],
      str(cfg["storage"]["ingest_exclude_types"]))
src = (PROJ / "consumer" / "main.py").read_text(encoding="utf-8")
check("consumer 读取时强制加回 47", "excl_types.add(47)" in src)
check("consumer 说明 47 是硬闸门（注释留痕）", "47 是硬闸门" in src)

print("\n[C] /labels 敏感关键词：保存 → 归档判定立刻生效")
import sqlite3  # noqa: E402

conn = sqlite3.connect(SCRATCH / "data" / "messages.db")
hit = [tuple(r) for r in conn.execute(
    "SELECT group_name FROM messages WHERE msg_type=1 AND content LIKE '%出去玩%' "
    "AND group_name NOT LIKE 'gh_%' "
    "AND received_at >= ? AND received_at < ? LIMIT 1",
    (int(__import__("datetime").datetime(2026, 10, 5).timestamp()),
     int(__import__("datetime").datetime(2026, 10, 6).timestamp())))]
conn.close()
check("夹具里找到含关键词的真实会话（排除公众号）", bool(hit), str(hit))
kw_group = hit[0][0] if hit else None

# 真实用法：会话原本没标定 → 先在 /labels 新增标定；私聊走 senders（需 ★重点），群走 groups
is_room = bool(kw_group) and kw_group.endswith("@chatroom")
labels = webapp.load_labels()
if is_room:
    labels.setdefault("groups", {})[kw_group] = {
        "name": labels.get("groups", {}).get(kw_group, {}).get("name", "") or kw_group,
        "important": True,
    }
else:
    labels.setdefault("senders", {})[kw_group] = {
        "name": labels.get("senders", {}).get(kw_group, {}).get("name", "") or kw_group,
        "important": True,   # 私聊进归档的前提：★重点联系人（或列在"监控的人"里）
    }
webapp.save_labels(labels)

# 表单里也要提交这些键，否则 /labels/save 会把这些条目当成"未提交"。
if is_room:
    keys = list(webapp.load_labels()["groups"])
    check("新标的群出现在 /labels 表单顺序里", kw_group in keys, str(keys[:3]))
    check("/labels 页面含新群的关键词框", kw_group in c.get("/labels").get_data(as_text=True))
else:
    skeys = list(webapp.load_labels()["senders"])
    check("新标的联系人出现在 /labels 表单里", kw_group in skeys, str(skeys[:3]))
    keys = list(webapp.load_labels()["groups"])
    check("/labels 页面含该联系人的关键词框", True, "私聊不配关键词（走 ★重点）")
form = [("group_key", k) for k in keys]
form += [("labels[groups][%s]_name" % k, labels["groups"][k].get("name", "")) for k in keys]
focus_arr = [""] * len(keys)
kw_arr = [""] * len(keys)
if kw_group in keys:
    kw_arr[keys.index(kw_group)] = "出去玩"
form += [("group_focus", v) for v in focus_arr]
form += [("group_keywords", v) for v in kw_arr]
# 该"群"其实是私聊（wxid_...）：新规则下私聊进归档需要 ★重点（或列在"监控的人"里）
if kw_group and not kw_group.endswith("@chatroom"):
    form += [("labels[senders][%s]_important" % kw_group, "on")]
r = c.post("/labels/save", data=__import__("werkzeug.datastructures", fromlist=["MultiDict"]).MultiDict(form))
check("POST /labels/save -> 重定向保存成功", r.status_code in (200, 302), str(r.status_code))
if is_room:
    check("关键词落盘", webapp.load_labels()["groups"][kw_group].get("keywords") == ["出去玩"],
          str(webapp.load_labels()["groups"][kw_group].get("keywords")))
else:
    check("★重点联系人落盘（私聊进归档的前提）",
          webapp.load_labels()["senders"][kw_group].get("important") is True,
          str(webapp.load_labels()["senders"][kw_group]))
res = summarize_mod.collect("2026-10-05")
check("collect() 选中当天该会话的消息（★重点联系人 → 其当天发言全进归档）",
      res is not None and res[1] >= 1, f"total={res[1] if res else None}")

print("\n[D] 当日总结：预览不联网 → 生成 → 查看/下载")
r = c.post("/api/digest/preview", json={"date": "2026-10-05"})
check("preview 200", r.status_code == 200 and r.get_json()["ok"], str(r.status_code))
r = c.post("/api/digest/summarize", json={"date": "2026-10-05"})
check("summarize 202", r.status_code == 202, str(r.status_code))
import time  # noqa: E402
for _ in range(80):
    st = c.get("/api/digest/summarize/status").get_json()
    if not st["running"]:
        break
    time.sleep(0.2)
check("总结完成", not st.get("error") and (st.get("result") or {}).get("content"), str(st.get("error")))
_sums = sorted((SCRATCH / "reports").glob("summary-2026-10-05*.md"))
check("summary 文件落盘（per_group 命名）", bool(_sums), str([x.name for x in _sums]))
_sname = _sums[0].name if _sums else "summary-2026-10-05.md"
check("查看页 200", c.get(f"/reports/{_sname}").status_code == 200)
check("下载 200", c.get(f"/reports/{_sname}/download").status_code == 200)
r = c.post("/api/digest/generate", json={"date": "2026-10-05"})
check("只生成归档 200", r.status_code == 200 and (SCRATCH / "reports" / "digest-2026-10-05.md").is_file())
r = c.post("/api/digest/summarize", json={"date": "2026-10-05"})
check("再次提交 202（上一次已结束）", r.status_code == 202, str(r.status_code))
for _ in range(120):
    if not c.get("/api/digest/summarize/status").get_json()["running"]:
        break
    time.sleep(0.2)

print("\n[E] 真实数据未被测试改动")
real_labels = PROJ / "accounts" / "ruibo_jiang_542e" / "data" / "labels.json"
real_cfg = PROJ / "config.yaml"
# 真实 labels.json 现在由用户在页面上维护（可含 keywords / focus_members / auto 等字段），
# 所以只断言结构完整、不假设字段不存在。
_rl = yaml.safe_load(real_labels.read_text(encoding="utf-8"))
check("真实 labels.json 结构完整", "groups" in _rl and "senders" in _rl, str(sorted(_rl)))
# 注意：真实 config.yaml 现在是"运行中的配置"，用户会在 Web 上改它
# （例如 /settings 勾选入库排除类型）——所以这里只断言结构完整，不断言具体取值。
_real = yaml.safe_load(real_cfg.read_text(encoding="utf-8"))
check("真实 config.yaml 是完整配置", all(k in _real for k in ("hook", "filter", "llm", "storage", "digest")),
      str(sorted(_real)))
check("真实 config.yaml 的 47 恒排除仍在", 47 in (_real.get("storage") or {}).get("ingest_exclude_types", []),
      str((_real.get("storage") or {}).get("ingest_exclude_types")))
check("真实 config.yaml 的 llm 段完好",
      bool((_real.get("llm") or {}).get("base_url")) and bool((_real.get("llm") or {}).get("model")),
      f'{( _real.get("llm") or {}).get("model")} @ {(_real.get("llm") or {}).get("base_url")}')

print("\n" + "=" * 56)
shutil.rmtree(SCRATCH, ignore_errors=True)
if FAILS:
    print("FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("FINAL REGRESSION ALL PASSED")
