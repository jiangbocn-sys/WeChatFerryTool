"""摸底：语音消息体里的 aeskey + voiceurl 能否自己解出 SILK。

背景：PC 微信不把语音落盘到可捕获的位置（实测播放也不生成文件），
但**每条语音消息的 XML 里带着 `aeskey` 和 `voiceurl`（一大段十六进制）**。
如果 voiceurl 就是密文/可下载的数据，那就能完全绕开微信客户端取到语音。

本脚本只做只读探测 + 在内存里试解密，不改任何数据、不写任何文件。
"""
import re
import sqlite3
import shutil
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SRC = Path(r"accounts\ruibo_jiang_542e\data")
work = Path(os.environ.get("TEMP", r"C:\Windows\Temp")) / "wft-voice-probe"
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True)
for s in ("", "-wal", "-shm"):
    p = Path(str(SRC / "messages.db") + s)
    if p.exists():
        shutil.copy2(p, work / ("messages.db" + s))

conn = sqlite3.connect(work / "messages.db")
rows = conn.execute(
    "SELECT msg_id, content, received_at FROM messages WHERE msg_type=34 "
    "AND content LIKE '%voiceurl%' ORDER BY received_at DESC LIMIT 8"
).fetchall()
print(f"带 voiceurl 的语音消息: {len(rows)} 条\n")

for msg_id, content, ts in rows:
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', content or ""))
    vurl = attrs.get("voiceurl", "")
    aes = attrs.get("aeskey", "")
    ln = attrs.get("length", "")
    vlen = attrs.get("voicelength", "")
    silkl = attrs.get("silklength", "")
    fmt = attrs.get("voiceformat", "")
    raw = bytes.fromhex(vurl) if vurl and len(vurl) % 2 == 0 else b""
    print(f"msg={str(msg_id)[:22]:<24} fmt={fmt} length={ln} voicelength={vlen} silklength={silkl}")
    print(f"   aeskey(hex)={aes}  ({len(aes)//2} 字节)")
    print(f"   voiceurl: hex 长度={len(vurl)} -> 解码 {len(raw)} 字节")
    print(f"   前 16 字节: {raw[:16].hex()}")
    print(f"   后 8 字节 : {raw[-8:].hex()}")
    # 看有没有明显的头部特征
    for magic, name in ((b"SILK", "SILK"), (b"\x02#!SILK", "#!SILK"), (b"\x02#!", "#!"),
                        (b"\x1f\x8b", "gzip"), (b"\x78\x9c", "zlib")):
        if raw.startswith(magic):
            print(f"   ⚠️ 以 {name} 魔数开头")
    print()

# 对比：已成功转写的那些语音，其解密后的 SILK 长什么样（我们的 .bin 就是 silk）
voices = SRC / "voices"
print("=== 已捕获成功的 .bin 头部（作为对照：真 SILK 长什么样）===")
for f in sorted(voices.glob("*.bin"))[:5]:
    b = f.read_bytes()[:16]
    print(f"  {f.name:<40} {len(f.read_bytes()):>7} B  头: {b.hex()}")
conn.close()
shutil.rmtree(work, ignore_errors=True)
