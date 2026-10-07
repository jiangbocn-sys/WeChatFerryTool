"""给 hook 重新注册回调地址（幂等），并打印当前状态。

用途：微信重启后 hook 是重新加载的，回调地址可能需要重新登记一次。
只发 HTTP 请求，不改配置、不写库。
"""
import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOOK = "http://127.0.0.1:30001"
TARGET = "http://127.0.0.1:8888/hook/callback"


def post(path: str, payload: dict) -> str:
    req = urllib.request.Request(HOOK + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"HTTP {r.status} {r.read().decode('utf-8', 'replace')[:300]}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:  # noqa: BLE001
        return f"ERR {type(e).__name__}: {e}"


def get(path: str) -> str:
    try:
        with urllib.request.urlopen(HOOK + path, timeout=10) as r:
            return f"HTTP {r.status} {r.read().decode('utf-8', 'replace')[:300]}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:  # noqa: BLE001
        return f"ERR {type(e).__name__}: {e}"


if __name__ == "__main__":
    print("hook 存活   :", get("/QueryDB/status"))
    print("注册回调    :", post("/set_callback", {"url": TARGET}))
