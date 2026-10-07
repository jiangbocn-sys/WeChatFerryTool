"""验证「离线导入微信库（昵称/群成员）」：解析、合并规则、Web 接口、重点人页。

不碰真实微信：用**假连接**（内存 sqlite，建同名表）测读取与合并逻辑；
用 monkeypatch 替换 `run_import` 测 Web 接口。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-offline-import")
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


from consumer import wechat_offline as wo  # noqa: E402

print("[1] 口令候选与 PRAGMA 转义（踩过坑：不转义会误判密钥无效）")
keysfile = SC / "keys.jsonl"
k1 = bytes(range(99))                      # 99B：模拟 x'…' 口令文本
k2 = bytes(range(64))                      # 64B：模拟裸密钥
keysfile.write_text("\n".join([
    json.dumps({"ts": 1, "len": 99, "key": k1.hex(), "db": "0x1"}),
    json.dumps({"ts": 2, "len": 64, "key": k2.hex(), "db": "0x2"}),
    "坏行不是 JSON",
]), encoding="utf-8")
cands = wo.key_candidates(keysfile)
check("候选包含原样/前67/前64", k1 in cands and k1[:67] in cands and k2[:64] in cands, f"{len(cands)} 个")
check("坏行被跳过（不抛异常）", True)
quoted = wo._key_pragma(b"x'abc'def", "pass")
check("单引号被转义成 ''", "''" in quoted and quoted.count("''") >= 2, quoted)
check("raw 模式用 x'<hex>'", wo._key_pragma(b"\x01\x02", "raw") == "PRAGMA key = \"x'0102'\"",
      wo._key_pragma(b"\x01\x02", "raw"))

print("\n[2] 读取联系人/群成员（用同名表的假库）")
fake = sqlite3.connect(":memory:")
fake.executescript("""
CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, local_type INTEGER, alias TEXT,
                      remark TEXT, nick_name TEXT);
CREATE TABLE name2id (username TEXT);
CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, owner TEXT);
CREATE TABLE chatroom_member (room_id INTEGER, member_id INTEGER);
INSERT INTO contact (username, local_type, alias, remark, nick_name) VALUES
  ('wxid_a', 1, 'alia', '备注A', '昵称A'),
  ('wxid_b', 1, '', '', '昵称B'),
  ('wxid_c', 1, '', '备注C', ''),
  ('wxid_none', 1, '', '', ''),
  ('room1@chatroom', 2, '', '测试群', '群昵称'),
  ('gh_xxx', 3, '', '', '公众号');
INSERT INTO name2id (username) VALUES ('wxid_a'), ('wxid_b'), ('wxid_c'), ('ruibo_jiang');
INSERT INTO chat_room (id, username, owner) VALUES (10, 'room1@chatroom', 'ruibo_jiang');
INSERT INTO chatroom_member (room_id, member_id) VALUES (10, 1), (10, 2), (10, 4), (10, 2);
""")
contacts = wo.read_contacts(fake)
check("备注优先", contacts.get("wxid_a") == "备注A", str(contacts.get("wxid_a")))
check("无备注用昵称", contacts.get("wxid_b") == "昵称B")
check("无昵称用备注", contacts.get("wxid_c") == "备注C")
check("名字等于 id 的跳过", "wxid_none" not in contacts)
check("群名也读出来", contacts.get("room1@chatroom") == "测试群")
members = wo.read_group_members(fake)
check("群成员解析", members.get("room1@chatroom") == ["wxid_a", "wxid_b", "ruibo_jiang"],
      str(members.get("room1@chatroom")))
check("重复成员去重", len(members.get("room1@chatroom", [])) == 3)

print("\n[3] 合并进标定：只补空白 + 只给已知 wxid 建条目")
labels_path = SC / "data" / "labels.json"
labels_path.write_text(json.dumps({
    "groups": {"room1@chatroom": {"name": "我改过的群名", "important": True}},
    "senders": {"wxid_c": {"name": "", "important": False}, "wxid_a": {"name": "手工名A"}},
}, ensure_ascii=False, indent=2), encoding="utf-8")
res = wo.merge_into_labels(contacts, labels_path, known={"wxid_c", "wxid_b", "room1@chatroom"},
                           overwrite=True)
saved = json.loads(labels_path.read_text(encoding="utf-8"))
check("覆盖模式：手工名被微信库名字覆盖（wxid_a: 手工名A → 备注A）",
      saved["senders"]["wxid_a"]["name"] == "备注A", str(saved["senders"]["wxid_a"]))
check("原名存进 name_prev 可回退", saved["senders"]["wxid_a"].get("name_prev") == "手工名A",
      str(saved["senders"]["wxid_a"]))
check("★重点保留", saved["groups"]["room1@chatroom"].get("important") is True)
check("群名被覆盖（我改过的群名 → 测试群）",
      saved["groups"]["room1@chatroom"]["name"] == "测试群", str(saved["groups"]["room1@chatroom"]))
check("覆盖数被统计", res.get("overwritten", 0) >= 2, str(res.get("overwritten")))
check("空白名被补上", saved["senders"]["wxid_c"]["name"] == "备注C", str(saved["senders"]["wxid_c"]))
check("已知 wxid 建了新条目", saved["senders"].get("wxid_b", {}).get("name") == "昵称B")
check("来源标记 wechat_db", saved["senders"]["wxid_b"].get("source") == "wechat_db")
check("未知 wxid 不建条目（避免 5000 陌生人塞爆标定）", "wxid_none" not in saved["senders"]
      and "gh_xxx" not in saved["senders"], f"created={res['created']} skipped={res['skipped_unknown']}")
check("跳过数被统计", res["skipped_unknown"] >= 1, str(res["skipped_unknown"]))

print("\n[3b] overwrite=False 时只补空白、不动已有名字")
labels2 = SC / "data" / "labels2.json"
labels2.write_text(json.dumps({
    "groups": {}, "senders": {"wxid_a": {"name": "手工名A"}, "wxid_c": {"name": ""}},
}, ensure_ascii=False, indent=2), encoding="utf-8")
res2 = wo.merge_into_labels(contacts, labels2, known={"wxid_a", "wxid_c"}, overwrite=False)
saved2 = json.loads(labels2.read_text(encoding="utf-8"))
check("不覆盖：手工名保留", saved2["senders"]["wxid_a"]["name"] == "手工名A",
      str(saved2["senders"]["wxid_a"]))
check("不覆盖：仍然补空白", saved2["senders"]["wxid_c"]["name"] == "备注C")
check("不覆盖：没有 name_prev", "name_prev" not in saved2["senders"]["wxid_a"])
check("不覆盖：overwritten=0", res2.get("overwritten", 0) == 0, str(res2.get("overwritten")))

print("\n[4] Web 接口")
import web.app as webapp  # noqa: E402
app = webapp.create_app()
app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = app.test_client()

r = c.get("/api/contacts/wechat-db")
j = r.get_json()
check("GET 状态 -> JSON", r.status_code == 200 and j is not None, str(r.status_code))
check("报告 keys 文件是否存在", "keys_exist" in j and "keys_file" in j, json.dumps(j, ensure_ascii=False)[:110])

# 替换 run_import：不真读库
called = {}


def fake_run_import(*a, **kw):
    called["yes"] = True
    return {"ok": True, "counts": {"contacts": 5492, "groups": 32, "members": 3366},
            "filled": 5, "created": 7, "skipped_unknown": 5480,
            "labels_path": str(labels_path), "cache": str(SC / "data" / "wechat_contacts.json"),
            "sample": {}}


wo.run_import = fake_run_import          # type: ignore[assignment]
r = c.post("/api/contacts/wechat-db")
j = r.get_json()
check("POST 导入 -> JSON 且 ok", r.status_code == 200 and j.get("ok") is True, json.dumps(j, ensure_ascii=False)[:140])
check("确实调用了 run_import", called.get("yes") is True)
check("返回统计", j["counts"]["contacts"] == 5492)


def boom(*a, **kw):
    raise RuntimeError("缺少 sqlcipher3（离线解密用）")


wo.run_import = boom                     # type: ignore[assignment]
r = c.post("/api/contacts/wechat-db")
j = r.get_json()
check("依赖缺失时给可读错误（不是 500）", r.status_code == 200 and j.get("ok") is False
      and "sqlcipher3" in j.get("error", ""), json.dumps(j, ensure_ascii=False)[:120])

print("\n[5] 重点人页应显示『本群成员』")
(SC / "data" / "wechat_contacts.json").write_text(json.dumps({
    "imported_at": 1, "counts": {"contacts": 3, "groups": 1, "members": 2},
    "contacts": {"wxid_a": "备注A", "wxid_b": "昵称B", "wxid_zzz": "陌生人Z"},
    "groups": {"room1@chatroom": "测试群"},
    "members": {"room1@chatroom": ["wxid_a", "wxid_b"]},
}, ensure_ascii=False), encoding="utf-8")
page = c.get("/config/focus/room1@chatroom")
html = page.get_data(as_text=True)
check("重点人页 200", page.status_code == 200, str(page.status_code))
check("本群成员出现（wxid_a）", "wxid_a" in html)
check("有『本群』标记", "本群" in html)
check("非成员且未标记的不出现（wxid_zzz）", "wxid_zzz" not in html)
check("标定名（已被导入覆盖为备注A）优先显示", "备注A" in html)
check("无标定名时用离线缓存名兜底（wxid_b → 昵称B）", "昵称B" in html)

b = c.get("/labels").get_data(as_text=True)
check("标定页有离线导入按钮", "btn-wechat-db" in b and "从微信库导入" in b)
check("真实 labels.json 未被改动", "备注A" not in REAL_LABELS.read_text(encoding="utf-8"))

shutil.rmtree(SC, ignore_errors=True)
print("=" * 60)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("离线导入昵称/群成员 验证通过")
