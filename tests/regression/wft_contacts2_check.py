"""验证昵称自动关联（新版：动态发现库名 + 按实际列名拼 SQL）。

用假 DLL 模拟真实契约：
    GetAllDBName -> [{"dbName": "...", "dbHandle": 1}]
    QueryDB/execute(optDbName, SQL) -> {"status":0,"desc":"","data":[...]}
以及"库名不对就回 status=-1"的真实行为。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-contacts2")
REAL_LABELS = PROJ / "accounts" / "ruibo_jiang_542e" / "data" / "labels.json"

shutil.rmtree(SC, ignore_errors=True)
(SC / "data").mkdir(parents=True, exist_ok=True)
os.environ["WCF_DATA_DIR"] = str(SC)
os.environ["WCF_NO_MULTI_ACCOUNT"] = "1"
sys.path.insert(0, str(PROJ))

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


(SC / "data" / "labels.json").write_text(json.dumps({
    "groups": {"195940014@chatroom": {"name": "幸福小家", "important": True},
               "958062774@chatroom": {"name": "", "important": False}},
    "senders": {"wxid_8cjwgonnvyq822": {"name": "Mr.Gao", "important": False},
                "wxid_qukw64gnzycr12": {"name": "", "important": False}},
}, ensure_ascii=False, indent=2), encoding="utf-8")

import web.app as webapp  # noqa: E402
from consumer import contact_sync  # noqa: E402


class FakeDll:
    """模拟真实 DLL：库名必须对上，否则回 status=-1（实测行为）。

    库里放 contact 表，列名故意用非主流组合（user_name / conremark / nick_name），
    验证"按实际列名拼 SQL"而不是硬编码。
    """

    def __init__(self, dbs=None, rows=None, contact_db="contact.db", bad_sql=False):
        self.dbs = dbs if dbs is not None else [{"dbName": "MicroMsg.db", "dbHandle": 1},
                                                {"dbName": contact_db, "dbHandle": 2}]
        self.contact_db = contact_db
        self.rows = rows if rows is not None else [
            {"user_name": "195940014@chatroom", "conremark": "幸福小家（备注）", "nick_name": "幸福小家"},
            {"user_name": "958062774@chatroom", "conremark": "", "nick_name": "中澳一家亲"},
            {"user_name": "wxid_8cjwgonnvyq822", "conremark": "Mr.Gao 改过", "nick_name": "高"},
            {"user_name": "wxid_qukw64gnzycr12", "conremark": "", "nick_name": "甜菜"},
            {"user_name": "wxid_newbie0001", "conremark": "新朋友", "nick_name": "新朋友"},
            {"user_name": "wxid_samenam3", "conremark": "", "nick_name": ""},
        ]
        self.bad_sql = bad_sql
        self.calls: list[tuple[str, str]] = []

    def list_dbs(self):
        return self.dbs

    def query_db(self, sql, db_name=""):
        self.calls.append((db_name, sql))
        no_handle = {"status": -1, "desc": "get database handle which named failed"}
        if not db_name or db_name not in [d["dbName"] for d in self.dbs]:
            return no_handle
        if "sqlite_master" in sql:
            if db_name == self.contact_db:
                return {"status": 0, "desc": "", "data": [{"name": "contact"}, {"name": "session"}]}
            if "MicroMsg" in db_name:
                return {"status": 0, "desc": "", "data": [{"name": "ChatRoom"}, {"name": "ChatInfo"}]}
            return {"status": 0, "desc": "", "data": []}
        if sql.startswith("PRAGMA table_info"):
            if "contact" in sql:
                return {"status": 0, "desc": "",
                        "data": [{"name": "user_name"}, {"name": "conremark"},
                                 {"name": "nick_name"}, {"name": "alias"}]}
            return {"status": 0, "desc": "", "data": [{"name": "id"}]}
        if "FROM contact" in sql:
            if self.bad_sql:
                return {"status": -1, "desc": "no such column"}
            return {"status": 0, "desc": "", "data": self.rows}
        return {"status": -1, "desc": "unexpected sql"}


print("[1] 契约：必须带 optDbName（不带就 status=-1）")
from consumer.hook_client import HookClient, HookConfig  # noqa: E402
h = HookClient(HookConfig())
seen = {}


class CapHook(HookClient):
    def _post(self, path, json_body=None):
        seen[path] = json_body
        return {"status": 0, "desc": "", "data": []}


ch = CapHook(HookConfig())
ch.query_db("SELECT 1", "contact.db")
check("execute 带 optDbName + SQL", seen.get("/QueryDB/execute", {}).get("optDbName") == "contact.db"
      and "SQL" in seen.get("/QueryDB/execute", {}), str(seen.get("/QueryDB/execute"))[:90])
ch.list_dbs()
check("GetAllDBName 打空对象", seen.get("/QueryDB/GetAllDBName") == {},
      str(seen.get("/QueryDB/GetAllDBName")))

print("\n[2] 动态发现：库名 + 表名 + 列名都靠发现")
h = FakeDll()
plan = contact_sync.discover(h, verbose=False)
check("发现库里挂的库名", plan["dbs"] == ["MicroMsg.db", "contact.db"], str(plan["dbs"]))
check("命中 contact.db / contact", (plan["source"] or {}).get("db") == "contact.db"
      and (plan["source"] or {}).get("table") == "contact", str(plan["source"]))
sql = (plan["source"] or {}).get("sql") or ""
check("SQL 用的是**实际列名** user_name/conremark/nick_name",
      "user_name" in sql and "conremark" in sql and "nick_name" in sql, sql[:120])
check("跳过了没有联系人的 MicroMsg.db（只查 sqlite_master）",
      all(not (db == "MicroMsg.db" and "FROM contact" in s) for db, s in h.calls))
check("来源已缓存到 contact_source.json", (SC / "data" / "contact_source.json").is_file())

print("\n[3] 只有 chatroom_member 时能退化到群成员表")
h2 = FakeDll(dbs=[{"dbName": "message.db", "dbHandle": 3}])


def q_msg(sql, db_name=""):
    if db_name != "message.db":
        return {"status": -1, "desc": "get database handle which named failed"}
    if "sqlite_master" in sql:
        return {"status": 0, "desc": "", "data": [{"name": "chatroom_member"}]}
    if sql.startswith("PRAGMA"):
        return {"status": 0, "desc": "", "data": [{"name": "member_id"}, {"name": "display_name"}]}
    if "FROM chatroom_member" in sql:
        return {"status": 0, "desc": "", "data": [{"member_id": "wxid_abc", "display_name": "群友甲"}]}
    return {"status": -1, "desc": "?"}


h2.query_db = q_msg  # type: ignore[method-assign]
plan2 = contact_sync.discover(h2, verbose=False)
check("退化到 chatroom_member 且命中", (plan2["source"] or {}).get("table") == "chatroom_member",
      str(plan2["source"]))
check("member 表 SQL 用 member_id/display_name",
      "member_id" in (plan2["source"] or {}).get("sql", ""), (plan2["source"] or {}).get("sql", "")[:100])

print("\n[4] fetch_names + sync_labels：只填空白、不覆盖手工名")
(SC / "data" / "contact_source.json").unlink(missing_ok=True)
h3 = FakeDll()
names = contact_sync.fetch_names(h3)
check("拿到名字", len(names) >= 4, f"{len(names)} 个")
res = contact_sync.sync_labels(h3, SC / "data" / "labels.json")
saved = json.loads((SC / "data" / "labels.json").read_text(encoding="utf-8"))
print("   ", {k: v for k, v in res.items() if k != "sample"})
check("手工名未被覆盖（群）", saved["groups"]["195940014@chatroom"]["name"] == "幸福小家")
check("手工名未被覆盖（人）", saved["senders"]["wxid_8cjwgonnvyq822"]["name"] == "Mr.Gao")
check("空白名被填（群）", saved["groups"]["958062774@chatroom"]["name"] == "中澳一家亲",
      saved["groups"]["958062774@chatroom"]["name"])
check("空白名被填（人）", saved["senders"]["wxid_qukw64gnzycr12"]["name"] == "甜菜")
check("未标定的新 wxid 建条目", saved["senders"].get("wxid_newbie0001", {}).get("name") == "新朋友")
check("自动填的标 auto", saved["groups"]["958062774@chatroom"].get("auto") is True)
check("手工条目不标 auto", not saved["groups"]["195940014@chatroom"].get("auto"))
check("名字=wxid 的跳过", "wxid_samenam3" not in saved["senders"])
check("★重点保留", saved["groups"]["195940014@chatroom"].get("important") is True)

print("\n[5] 缓存来源失效时自动重新发现")
(SC / "data" / "contact_source.json").write_text(json.dumps(
    {"source": {"db": "不存在的.db", "table": "contact", "sql": "SELECT 1"}}, ensure_ascii=False),
    encoding="utf-8")
h4 = FakeDll()
names4 = contact_sync.fetch_names(h4)
check("坏缓存会用假 DLL 重新发现（仍拿到名字）", len(names4) >= 4, f"{len(names4)} 个")
check("重发现后缓存被修正",
      json.loads((SC / "data" / "contact_source.json").read_text(encoding="utf-8"))["source"]["db"] == "contact.db",
      str(json.loads((SC / "data" / "contact_source.json").read_text(encoding="utf-8")).get("source")))

print("\n[6] Web 接口")
app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()
r = c.get("/api/contacts/probe")
body = r.get_data(as_text=True)
check("probe 回 JSON（DLL 不在线也不 500）", "application/json" in (r.headers.get("Content-Type") or ""), body[:110])
check("probe 带 dbs/attempts 字段", "attempts" in body or "error" in body, body[:200])
r = c.post("/api/contacts/sync")
check("sync 回 JSON", "application/json" in (r.headers.get("Content-Type") or ""))
b = c.get("/labels").get_data(as_text=True)
check("标定页有两个按钮", "btn-sync-names" in b and "btn-probe-names" in b)
check("真实 labels.json 未被改动", REAL_LABELS.read_text(encoding="utf-8").find("wxid_newbie0001") < 0)

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("昵称自动关联（动态发现版）验证通过")
