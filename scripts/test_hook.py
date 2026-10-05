"""测试 aixed/WeChat-Hook DLL 的本地探针。

流程：
1. 启一个 HTTP server 在 8888 端口，接收 DLL POST 过来的回调
2. 每收到一条回调，打印完整 payload 到日志文件 + stdout
3. 提供 CLI 子命令：
   - status    检查 127.0.0.1:30001/QueryDB/status
   - register  调用 /set_callback 让 DLL 把消息推到这里
   - serve     启 server（默认就是这个模式）

不依赖 flask，只用标准库。
"""
import argparse
import json
import logging
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests


# ------- 配置 -------
HOOK_API_BASE = "http://127.0.0.1:30001"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8888
CALLBACK_PATH = "/hook/callback"

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "hook_probe.log"


# ------- 日志 -------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("hook_probe")


# ------- 回调接收 -------
class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass  # 关掉默认 access log，我们自己打

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as e:  # noqa: BLE001
            log.warning("非 JSON 回调: %s (err=%s)", raw[:200], e)
            payload = {"_raw": raw.decode("utf-8", errors="replace")}

        ts = datetime.now().isoformat(timespec="seconds")
        log.info("=== 回调 [%s] path=%s ===", ts, self.path)
        log.info(json.dumps(payload, ensure_ascii=False, indent=2))

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):  # noqa: N802
        # 健康检查
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"hook_probe ok\n")


def serve() -> None:
    server = ThreadingHTTPServer((CALLBACK_HOST, CALLBACK_PORT), CallbackHandler)
    log.info(
        "回调 server 已启动: http://%s:%d%s  (日志: %s)",
        CALLBACK_HOST, CALLBACK_PORT, CALLBACK_PATH, LOG_FILE,
    )
    log.info("下一步: 在另一终端跑 register 子命令让 DLL 推送消息到这里")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("停止")
        server.shutdown()


# ------- DLL 调用 -------
def status() -> None:
    try:
        r = requests.get(f"{HOOK_API_BASE}/QueryDB/status", timeout=5)
        log.info("GET /QueryDB/status -> %d %s", r.status_code, r.text[:500])
    except Exception as e:  # noqa: BLE001
        log.error("status 失败: %s（dll 是否启动？微信是否在运行？）", e)


def register() -> None:
    url = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
    try:
        r = requests.post(
            f"{HOOK_API_BASE}/set_callback",
            json={"url": url},
            timeout=5,
        )
        log.info("POST /set_callback %s -> %d %s", url, r.status_code, r.text[:500])
    except Exception as e:  # noqa: BLE001
        log.error("set_callback 失败: %s", e)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="启回调 server")
    sub.add_parser("status", help="查 DLL 模块状态")
    sub.add_parser("register", help="注册回调地址")
    args = parser.parse_args()

    {"serve": serve, "status": status, "register": register}[args.cmd]()


if __name__ == "__main__":
    main()
