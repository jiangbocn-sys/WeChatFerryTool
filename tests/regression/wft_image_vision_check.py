"""回归：图片理解（视觉模型描述写进归档/总结）+ 缓存 + 开关。

背景（2026-10-08 方案 B）：PC 微信的图默认加密，但我们能拿到**偶发明文**
（缓存 Thumb，由 ImageSweeper 抢存成 `data/images/<msg_id>.jpg`）。
把能拿到的这部分交给多模态模型描述，让图**真正参与总结**；拿不到的老实标注。

覆盖：
  A. `vision.enabled()` 开关语义（默认关）
  B. 缓存：描述按 (msg_id,size,mtime) 落盘；**重跑不重复调模型**
  C. 只有存在明文图才调；没有明文图返回空（不编造）
  D. `cards.summary_line` 的图片分支：有描述写"图意：…"，无描述标"图未获取到内容"
  E. `digest.image_descriptions()`：批量 + 内存缓存 + 关掉开关就不调模型
  F. `generate_digest()` 端到端：归档正文里出现"图意"
  G. 设置页有该开关（llm.image_understand）
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-vision-suite")
shutil.rmtree(SC, ignore_errors=True)
(SC / "data" / "images").mkdir(parents=True)
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


GID = "g1@chatroom"
LLM = {"base_url": "https://x/v1", "api_key": "k", "model": "m", "image_understand": True}
LLM_OFF = dict(LLM, image_understand=False)

(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001"},
    "filter": {"groups": [GID], "senders": []},
    "storage": {"sqlite_path": str(SC / "data" / "messages.db"),
                "ingest_exclude_types": [47]},
    "digest": {"enabled": True, "dir": str(SC / "reports"), "exclude_types": [47]},
    "llm": dict(LLM),
}, allow_unicode=True, sort_keys=False), encoding="utf-8")
(SC / "data" / "labels.json").write_text(json.dumps(
    {"groups": {GID: {"name": "测试群"}}, "senders": {}}, ensure_ascii=False), encoding="utf-8")

# 造：1 条有明文图的图片消息 + 1 条没有明文图的图片消息
db = sqlite3.connect(SC / "data" / "messages.db")
db.executescript("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
 msg_id TEXT UNIQUE, group_name TEXT, sender TEXT, sender_id TEXT, content TEXT,
 msg_type INTEGER DEFAULT 1, received_at INTEGER NOT NULL, score INTEGER,
 score_reason TEXT, pushed INTEGER DEFAULT 0, priority INTEGER DEFAULT 0,
 direction TEXT, transcript TEXT);""")
TS = int(datetime(2026, 10, 8, 10, 0).timestamp())
IMG_XML = ('<msg><img aeskey="x" cdnthumbwidth="214" cdnthumbheight="480" '
           'length="489072"/></msg>')
for i, mid in enumerate(("img-with", "img-without")):
    db.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at)"
               " VALUES (?,?,?,?,?,?)", (mid, GID, "wxid_a", IMG_XML, 3, TS + i))
db.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at)"
           " VALUES (?,?,?,?,?,?)", ("txt-1", GID, "wxid_a", "看图", 1, TS + 2))
db.commit()
db.close()
from PIL import Image  # noqa: E402
Image.new("RGB", (60, 60), (200, 30, 30)).save(SC / "data" / "images" / "img-with.jpg")

from consumer import vision  # noqa: E402

print("[A] vision.enabled() 开关语义")
check("默认关（没这个键就是关）", vision.enabled({}) is False)
check("显式 False 是关", vision.enabled(LLM_OFF) is False)
check("显式 True 才开", vision.enabled(LLM) is True)

print("\n[B] 缓存：落盘 + 重跑不重复调模型")
calls: list[str] = []
_real = vision.describe_image


def fake(path, llm, timeout_s=120.0):
    calls.append(str(path))
    return "【卦例截图】卦名：天地否；世爻申金，应爻寅木"


vision.describe_image = fake         # 打桩：不联网、不花钱
d1 = vision.get_description("img-with", SC / "data" / "images", SC, LLM)
check("首次返回描述", d1.startswith("【卦例截图】"), d1[:30])
check("调用了 1 次模型", len(calls) == 1, str(len(calls)))
cache_file = SC / "data" / "image_desc.json"
check("描述写入缓存文件", cache_file.is_file())
check("缓存键含 msg_id 与文件大小",
      any(k.startswith("img-with|") for k in json.loads(cache_file.read_text(encoding="utf-8"))),
      str(list(json.loads(cache_file.read_text(encoding="utf-8")).keys())[:2]))
calls.clear()
d2 = vision.get_description("img-with", SC / "data" / "images", SC, LLM)
check("第二次命中缓存、**零调用**", len(calls) == 0 and d2 == d1, str(len(calls)))

print("\n[C] 没有明文图时不调模型、返回空")
calls.clear()
d3 = vision.get_description("img-without", SC / "data" / "images", SC, LLM)
check("没有 <msg_id>.jpg → 返回空", d3 == "")
check("也没调模型", len(calls) == 0, str(len(calls)))

print("\n[D] cards.summary_line 的图片分支")
from consumer import cards  # noqa: E402
s_with = cards.summary_line(3, IMG_XML, image_desc="【卦例截图】世爻申金")
s_without = cards.summary_line(3, IMG_XML)
check("有描述 → 写「图意：」", "图意：" in s_with and "世爻申金" in s_with, s_with)
check("无描述 → 标注「图未获取到内容」", "图未获取到内容" in s_without, s_without)
check("两种都不含 XML", "aeskey" not in s_with and "aeskey" not in s_without)

print("\n[E] digest.image_descriptions()：批量 + 内存缓存 + 开关")
import consumer.digest as D  # noqa: E402
calls.clear()
D._IMG_DESC_LOADED = False
D._IMG_DESC_MEM.clear()
got = D.image_descriptions(["img-with", "img-without"])
check("有明文的拿到描述", got.get("img-with", "").startswith("【卦例截图】"), str(got)[:60])
check("无明文的返回空", got.get("img-without", "") == "")
# 描述已在 B 段落盘缓存 → 这里**不应该**再调模型
check("走磁盘缓存、不重复调模型", len(calls) == 0, str(len(calls)))
# 反向验证：清掉缓存文件后，应该真的去调一次（证明上面的 0 不是打桩失效）
(SC / "data" / "image_desc.json").unlink(missing_ok=True)
calls.clear()
D._IMG_DESC_LOADED = False
D._IMG_DESC_MEM.clear()
got2 = D.image_descriptions(["img-with"])
check("清掉缓存后会真的调一次模型", len(calls) == 1, str(len(calls)))
check("清缓存后仍能拿到描述", got2.get("img-with", "").startswith("【卦例截图】"), str(got2)[:40])
calls.clear()
off = D.image_descriptions(["img-with"])          # 先把 llm 段改成关
cfg = yaml.safe_load((SC / "config.yaml").read_text(encoding="utf-8"))
cfg["llm"]["image_understand"] = False
(SC / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")
D._IMG_DESC_LOADED = False
D._IMG_DESC_MEM.clear()
got_off = D.image_descriptions(["img-with"])
check("关掉开关后不返回描述（也不调模型）", got_off.get("img-with", "") == "")
check("关掉时零调用", len(calls) == 0, str(len(calls)))

print("\n[F] generate_digest() 端到端：归档正文出现「图意」")
cfg["llm"]["image_understand"] = True
(SC / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")
D._IMG_DESC_LOADED = False
D._IMG_DESC_MEM.clear()
p = D.generate_digest("2026-10-08", db_path=SC / "data" / "messages.db",
                      labels_path=SC / "data" / "labels.json", out_dir=SC / "reports")
txt = Path(p).read_text(encoding="utf-8")
check("归档含图意", "图意：" in txt)
check("归档里那条图带描述", "世爻申金" in txt)
check("没明文的图标注未获取", "图未获取到内容" in txt, "")
check("文本消息正常在归档里", "看图" in txt)

print("\n[G] 设置页有该开关")
appsrc = (PROJ / "web" / "app.py").read_text(encoding="utf-8")
check("设置页有 llm.image_understand", "llm.image_understand" in appsrc)
check("标注了默认关闭", "用视觉模型描述归档图片（默认关闭）" in appsrc)

vision.describe_image = _real
shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("图片理解（描述进归档 + 缓存 + 开关）验证通过")
