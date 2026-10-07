"""验证标定热重载：labels.json 一改就生效，不用重启 app。"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PROJ = Path(r"D:\projects\WeChatFerryTool")
SC = Path(r"D:\projects\.wft-reload-test")

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


LABELS = SC / "data" / "labels.json"
LABELS.write_text(json.dumps({
    "groups": {"195940014@chatroom": {"name": "幸福小家", "important": True}},
    "senders": {"wxid_aaa111": {"name": "老张", "important": False}},
}, ensure_ascii=False, indent=2), encoding="utf-8")

import consumer.main as cm  # noqa: E402

# 只测热重载逻辑：直接构造一个轻量对象，避免启动整个 consumer
class Fake(cm.Consumer):
    def __init__(self):                                    # noqa: D107
        self._labels_mtime = None
        self.labels = self._load_labels()


f = Fake()
check("初始加载 1 群 / 1 人", len(f.labels["groups"]) == 1 and len(f.labels["senders"]) == 1,
      f"{len(f.labels['groups'])}/{len(f.labels['senders'])}")
check("首次检查不算变化", f._maybe_reload_labels() is False)

# 模拟 Web 上挖掘/套用：加人、补名
import time
time.sleep(0.02)
LABELS.write_text(json.dumps({
    "groups": {"195940014@chatroom": {"name": "幸福小家", "important": True},
               "958062774@chatroom": {"name": "中澳一家亲", "important": False, "auto": True, "source": "harvest"}},
    "senders": {"wxid_aaa111": {"name": "老张", "important": False},
                "wxid_bbb222": {"name": "小李", "important": False, "auto": True, "source": "harvest"}},
}, ensure_ascii=False, indent=2), encoding="utf-8")

check("文件改了 → 检测到变化", f._maybe_reload_labels() is True)
check("新群已生效", f.labels["groups"].get("958062774@chatroom", {}).get("name") == "中澳一家亲")
check("新联系人已生效", f.labels["senders"].get("wxid_bbb222", {}).get("name") == "小李")
check("旧数据仍在", f.labels["senders"]["wxid_aaa111"]["name"] == "老张")
check("再检查不重复重载", f._maybe_reload_labels() is False)

# 坏 JSON 不该炸
time.sleep(0.02)
LABELS.write_text("{ this is broken", encoding="utf-8")
ok = True
try:
    f._maybe_reload_labels()
except Exception as e:  # noqa: BLE001
    ok = False
    print("   异常:", e)
check("坏 JSON 不抛异常", ok)
check("坏 JSON 时退回空标定（与原有行为一致）", f.labels.get("groups") == {})

shutil.rmtree(SC, ignore_errors=True)
print("=" * 55)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("标定热重载 验证通过")
