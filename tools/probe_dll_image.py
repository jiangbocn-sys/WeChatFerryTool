"""探针 9：DLL 能不能帮我们"拿到图"——两条现成能力的实测。

  1. `POST /Decode_Pic`：hook 自带的图片解码接口（源码 SendTextMsg.cpp 里有注册）。
     预期入参是 XML 或路径，具体契约未知 → 多试几种 body。
  2. `POST /ForwardXMLMsg`：hook 的"原文转发消息"，若它内部会下载/解密资源，
     就能借它把图片落到本地（副作用：会往会话里发一条消息——**只在私聊自己**上试，
     或者干脆不试，先看 1 的结果）。

只发本地 HTTP 请求，不写库、不动微信数据。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOOK = "http://127.0.0.1:30001"
IMG_XML = ('<?xml version="1.0"?><msg><img aeskey="c8bd25997eb553482d4fc7816f9846bd" '
           'encryver="1" cdnthumbaeskey="c8bd25997eb553482d4fc7816f9846bd" cdnthumburl="305f" '
           'cdnthumblength="4700" cdnthumbheight="480" cdnthumbwidth="214" length="489072" '
           'hevc_mid_size="60593" md5="c98dc3582663eb2caa102af42300c8f6" /></msg>')


def post(path: str, payload, raw: bool = False, timeout: int = 20):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(HOOK + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            if raw:
                return f"HTTP {r.status} bytes={len(body)} head={body[:12].hex(' ')}"
            return f"HTTP {r.status} {body.decode('utf-8', 'replace')[:220]}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {e.read().decode('utf-8', 'replace')[:220]}"
    except Exception as e:  # noqa: BLE001
        return f"ERR {type(e).__name__}: {e}"


print("=== 1. /Decode_Pic 的几种可能契约 ===")
cases = [
    ("JSON {img:xml}", {"img": IMG_XML}),
    ("JSON {xml:xml}", {"xml": IMG_XML}),
    ("JSON {path:...}", {"path": r"D:\JiangBo\Documents\xwechat_files\ruibo_jiang_542e\msg\attach\dd12d75e3cf9932ab2a6743476e0d35d\2026-10\Img\ddece4375134faa154cd693fe53a6d48.dat"}),
    ("JSON {msgid:...}", {"msgid": "5439543700231738180"}),
    ("JSON {data:xml}", {"data": IMG_XML}),
    ("裸 XML", IMG_XML.encode("utf-8")),
    ("空 body", {}),
]
for label, body in cases:
    print(f"  {label:<18} -> {post('/Decode_Pic', body, raw=True)}")

print("\n=== 2. /ForwardXMLMsg 是否可用（只探测响应，不真发给自己以外的人）===")
# 先试一个"注定失败"的调用，只看它怎么回（判断接口在不在、参数要什么）
print("  空 body          ->", post("/ForwardXMLMsg", {}))
print("  {to:自己, xml}    -> 跳过（会真的发消息），需要你同意再试")

print("\n=== 3. 其它可能存在的接口探测（看返回码区分 404 / 200）===")
for p in ("/get_status", "/GetSelfProfile", "/QueryDB/status", "/SendTextMsg", "/Decode_Pic",
          "/ForwardXMLMsg", "/AttachFile", "/GetImage", "/DownloadImage"):
    r = post(p, {}) if p != "/GetSelfProfile" else post(p, {})
    print(f"  POST {p:<18} {r[:80]}")
