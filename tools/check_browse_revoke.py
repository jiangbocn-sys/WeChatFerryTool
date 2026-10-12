"""验证浏览页撤回显示（走本地 HTTP，真实数据）。"""
import html
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

URL = "http://127.0.0.1:6060/browse?msg_type=10002&per_page=10"
body = urllib.request.urlopen(URL, timeout=20).read().decode("utf-8", "replace")

hits = re.findall(r"已撤回</span>(.{0,220}?)</div>", body, re.S)
print(f"  页面含「已撤回」{body.count('已撤回')} 处；抽 {min(6, len(hits))} 条：")
texts = []
for h in hits[:6]:
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    t = re.sub(r"\s+", " ", t).strip()
    texts.append(t)
    print("   ", t[:130])

joined = " ".join(texts)
print()
print("  含「原文：」        :", "原文：" in joined)
print("  含「原文未留存」    :", "原文未留存" in joined)
bad = [t for t in texts if t.startswith('"') or '"" ' in t]
print("  还有引号噪音的行    :", len(bad), bad[:2])
