"""回归：消息卡片解析与渲染（consumer/cards.py + web 浏览页 + 归档摘要）。

背景（2026-10-07）：以前浏览页对图片/链接直接吐原始 XML，归档里只有 `[图片]`。
现在两个出口共用 consumer/cards.py：
  * msg_type 49 细分：引用回复(57) / 链接(5) / 文件(6,74) / 小程序(21,36) / 视频号(51,33,62,63)
  * msg_type 3 图片：尺寸/大小；43 视频；48 位置；42 名片；50 通话；10002 撤回
断言要点：**不再出现原始 XML 特征**（`<?xml`、`aeskey=`、`<appmsg`），且关键信息被提取出来。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-cards")
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


from consumer import cards  # noqa: E402

print("[1] 解析器：各类型")
IMG = ('<?xml version="1.0"?><msg><img aeskey="c8bd25997eb553482d4fc7816f9846bd" encryver="1" '
       'cdnthumbaeskey="c8bd25997eb553482d4fc7816f9846bd" cdnthumburl="305f02010004" '
       'cdnthumblength="4700" cdnthumbheight="480" cdnthumbwidth="214" length="489072" '
       'hevc_mid_size="60593" md5="c98dc3582663eb2caa102af42300c8f6" '
       'md5="78262560687d8c6d25b3fd4f0925cd46" /></msg>')
d = cards.parse(3, IMG)
check("图片：kind/尺寸/大小", d["kind"] == "image" and d["width"] == 214 and d["height"] == 480
      and d["size"] == 489072 and d["size_h"] == "478 KB", str(d)[:120])
check("图片：aeskey 与两个 md5 都提取到",
      d["aeskey"].startswith("c8bd") and len(d["md5s"]) == 2, str(d.get("md5s")))

QUOTE = ('<?xml version="1.0"?><msg><appmsg appid="" sdkver="0"><title>标记一看确实有点像</title>'
         '<type>57</type><appattach><cdnthumbaeskey /><aeskey /></appattach><refermsg><type>1</type>'
         '<svrid>6795345497211948863</svrid><fromusr>50279811726@chatroom</fromusr>'
         '<chatusr>wxid_w4wyjd5heuv221</chatusr><displayname>春之语</displayname>'
         '<content>@壹品名仕物業-謝經理&nbsp;明堂象个三角形？</content></refermsg></appmsg></msg>')
d = cards.parse(49, QUOTE)
check("引用回复：kind=quote + 被引用人/原话",
      d["kind"] == "quote" and d["quote_from"] == "春之语" and "明堂象个三角形" in d["quote_text"],
      f'{d.get("quote_from")} / {d.get("quote_text")}')
check("引用回复：附言也取到了", d["text"] == "标记一看确实有点像", str(d.get("text")))
check("引用回复：label 是「引用回复」", d["label"] == "引用回复", str(d.get("label")))

LINK = ('<?xml version="1.0"?><msg><appmsg><title>邀请你观看直播“第九课”</title>'
        '<des>直播人：可贞</des><type>33</type>'
        '<url>https://mp.weixin.qq.com/mp/waerrpage?appid=wx7424&amp;type=upgrade</url>'
        '<appname>微信公众平台</appname></appmsg></msg>')
d = cards.parse(49, LINK)
check("链接：title/desc/url 提取 + 实体解码",
      d["kind"] == "link" and d["title"].startswith("邀请你观看直播")
      and "可贞" in d["desc"] and "&amp;" not in d["url"] and "appid=wx7424" in d["url"],
      f'{d.get("title")} | {d.get("url")[:60]}')

FILE = ('<?xml version="1.0"?><msg><appmsg><title>20260930_090832.kml</title><type>6</type>'
        '<appattach><totallen>241737</totallen><fileext>kml</fileext></appattach></appmsg></msg>')
d = cards.parse(49, FILE)
check("文件：大小格式化", d["kind"] == "file" and d["size_h"] == "236 KB", str(d.get("size_h")))

LOC = ('<msg><location x="34.005459" y="113.840416" label="河南省许昌市建安区" '
       'poiname="胖东来超市(许昌金三角店)" /></msg>')
d = cards.parse(48, LOC)
check("位置：poiname/label", d["kind"] == "location" and "胖东来" in d["poiname"], str(d.get("poiname")))

REVOKE = ('<sysmsg type="revokemsg"><revokemsg><session>50279811726@chatroom</session>'
          '<replacemsg><![CDATA["青禾" 撤回了一条消息]]></replacemsg></revokemsg></sysmsg>')
d = cards.parse(10002, REVOKE)
check("撤回：还原成人话", d["kind"] == "revoke" and "青禾" in d["text"], str(d.get("text")))

VIDEO = ('<msg><videomsg aeskey="e33c" cdnvideourl="305f" cdnthumburl="305f" '
         'cdnthumbwidth="1280" cdnthumbheight="720" length="3783904" playlength="15" /></msg>')
d = cards.parse(43, VIDEO)
check("视频：尺寸/大小/时长", d["kind"] == "video" and d["width"] == 1280
      and d["duration"] == 15 and d["size_h"] == "3.6 MB", str(d)[:110])

check("异常输入不抛异常", cards.parse(3, "<msg><img")["kind"] == "image")
check("乱码输入不抛异常", isinstance(cards.parse(49, "\x00\xff not xml"), dict))

print("\n[2] summary_line：归档用单行摘要（不能出现原始 XML 特征）")
lines = {
    "图片": cards.summary_line(3, IMG),
    "引用": cards.summary_line(49, QUOTE),
    "链接": cards.summary_line(49, LINK),
    "文件": cards.summary_line(49, FILE),
    "位置": cards.summary_line(48, LOC),
    "撤回": cards.summary_line(10002, REVOKE),
    "语音": cards.summary_line(34, '<msg voicelength="5200" />'),
    "语音(有转写)": cards.summary_line(34, '<msg voicelength="5200" />', transcript="今天下午开会"),
}
for k, v in lines.items():
    print(f"    {k:<12} {v}")
check("图片摘要带尺寸与大小", "214×480" in lines["图片"] and "478 KB" in lines["图片"], lines["图片"])
check("引用摘要带被引用人与原话",
      "春之语" in lines["引用"] and "明堂象个三角形" in lines["引用"], lines["引用"])
check("链接摘要带标题", "邀请你观看直播" in lines["链接"], lines["链接"])
check("语音摘要优先用转写", lines["语音(有转写)"] == "[语音] 今天下午开会", lines["语音(有转写)"])
check("**所有摘要都不含 XML 特征**",
      not any(("<?xml" in v or "aeskey" in v or "<appmsg" in v or "cdnthumb" in v)
              for v in lines.values()),
      "; ".join(v for v in lines.values() if "<?xml" in v or "aeskey" in v))

print("\n[3] 归档渲染（digest._render_content）走同一个解析器")
(SC / "config.yaml").write_text(yaml.safe_dump({
    "hook": {"api_base": "http://127.0.0.1:30001", "callback_host": "127.0.0.1",
             "callback_port": 8888, "api_timeout_s": 5.0, "self_wxid": "wxid_me"},
    "filter": {"groups": [], "senders": [], "keywords": [], "case_insensitive": True},
    "llm": {"base_url": "https://x/v1", "api_key": "k", "model": "m", "push_threshold": 4},
    "storage": {"sqlite_path": "data/messages.db", "ingest_exclude_types": [47]},
    "digest": {"enabled": False},
}, allow_unicode=True, sort_keys=False), encoding="utf-8")
db = SC / "data" / "messages.db"
conn = sqlite3.connect(str(db))
conn.executescript("""
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id TEXT UNIQUE, group_name TEXT NOT NULL,
  sender TEXT NOT NULL, sender_id TEXT, content TEXT NOT NULL, msg_type INTEGER DEFAULT 1,
  received_at INTEGER NOT NULL, score INTEGER, score_reason TEXT, pushed INTEGER DEFAULT 0,
  priority INTEGER DEFAULT 0, direction TEXT DEFAULT NULL, transcript TEXT DEFAULT NULL);
""")
for i, (t, c) in enumerate([(3, IMG), (49, QUOTE), (49, LINK)]):
    conn.execute("INSERT INTO messages (msg_id, group_name, sender, content, msg_type, received_at)"
                 " VALUES (?,?,?,?,?,?)", (f"m{i}", "g@chatroom", "wxid_x", c, t, 1791343501 + i))
conn.commit()
conn.row_factory = sqlite3.Row
from consumer import digest as digest_mod  # noqa: E402
rows = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
rendered = [digest_mod._render_content(r) for r in rows]
for r in rendered:
    print("    ", r)
check("归档里图片不再是 [图片] 一行", "[图片 214×480" in rendered[0], rendered[0])
check("归档里引用带上了被引用内容", "春之语" in rendered[1], rendered[1])
check("归档里链接带上了标题", "邀请你观看直播" in rendered[2], rendered[2])
conn.close()

print("\n[4] web 浏览页：真实渲染不吐 XML")
sys.path.insert(0, str(PROJ))
import web.app as webapp  # noqa: E402
webapp.DB_PATH = db
webapp.CONFIG_PATH = SC / "config.yaml"
app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()
r = c.get("/browse")
html = r.get_data(as_text=True)
check("GET /browse -> 200", r.status_code == 200, str(r.status_code))
check("页面含图片元数据（214×480）", "214×480" in html)
check("页面含引用块样式 cardquote", "cardquote" in html and "春之语" in html)
check("页面含链接卡片样式 linkcard", "linkcard" in html and "邀请你观看直播" in html)
check("**页面里没有裸露的 XML/aeskey**",
      "aeskey=" not in html and "<appmsg" not in html and "cdnthumburl" not in html)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("消息卡片解析与渲染 验证通过")
