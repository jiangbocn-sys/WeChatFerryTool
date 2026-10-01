"""主入口。

启动顺序：
1. 读 config.yaml
2. 连接 WechatFerry daemon (WebSocket)
3. 订阅消息 → 过滤 → 入库 → LLM 评分 → 高分推送

协议说明（基于 WechatFerry 3.x）：
- 启动时 client.send("callback", json.dumps({"type": "msg"})) 订阅消息
- daemon 主动推送 {"type":"msg", "data": { ... }}
- 文本消息字段：id / room (群名) / sender / senderId / content / type / ts
"""
import json
import logging
import signal
import sys
import time
from pathlib import Path

import websocket  # websocket-client
import yaml

from consumer.filter import Filter, FilterConfig
from consumer.notifier import BarkConfig, Notifier
from consumer.scorer import Scorer, ScorerConfig
from consumer.store import Store


# ------- logging --------
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "consumer.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("consumer.main")


# ------- 工具函数 --------
def load_config(path: str = "config.yaml") -> dict:
    p = Path(__file__).resolve().parent.parent / path
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------- 主循环 --------
class Consumer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.store = Store(cfg["storage"]["sqlite_path"])
        self.filter = Filter(FilterConfig.from_yaml(cfg["filter"]))
        self.scorer = Scorer(ScorerConfig(
            base_url=cfg["llm"]["base_url"],
            api_key=cfg["llm"]["api_key"],
            model=cfg["llm"]["model"],
        ))
        self.notifier = Notifier(BarkConfig(
            server=cfg["bark"]["server"],
            key=cfg["bark"]["key"],
        ) if cfg.get("bark", {}).get("enabled") else None)
        self.push_threshold = int(cfg["llm"].get("push_threshold", 4))
        self._stop = False

    def stop(self, *_) -> None:
        log.info("收到停止信号，退出中...")
        self._stop = True

    def handle_message(self, msg: dict) -> None:
        """处理一条 daemon 推送的消息事件。"""
        try:
            data = msg.get("data") or {}
            msg_type = data.get("type", 1)
            # 只处理文本（type=1）；图片/文件/系统消息忽略
            if msg_type != 1:
                return

            group_name = data.get("room") or "(私聊)"
            sender = data.get("sender") or ""
            sender_id = data.get("senderId") or ""
            content = (data.get("content") or "").strip()
            msg_id = str(data.get("id") or "")
            ts = int(data.get("ts") or time.time())

            if not content or not msg_id:
                return

            # 1. 过滤
            ok, reason = self.filter.match(
                group_name=group_name, sender=sender, content=content
            )
            if not ok:
                log.debug("跳过 [%s] %s: %s", group_name, sender, content[:30])
                return

            # 2. 入库
            row_id = self.store.insert_message(
                msg_id=msg_id,
                group_name=group_name,
                sender=sender,
                sender_id=sender_id,
                content=content,
                msg_type=msg_type,
                received_at=ts,
            )
            if row_id is None:
                log.debug("重复消息，跳过: %s", msg_id)
                return
            log.info("入库 [%s] %s: %s (reason=%s)", group_name, sender, content[:50], reason)

            # 3. LLM 评分
            score, score_reason = self.scorer.score(
                group_name=group_name, sender=sender, content=content
            )
            self.store.update_score(msg_id, score, score_reason)
            log.info("评分 msg=%s score=%d (%s)", msg_id, score, score_reason)

            # 4. 高分推送
            if score >= self.push_threshold:
                title = f"[{score}] {group_name}"
                body = f"{sender}: {content[:120]}"
                pushed = self.notifier.push(title=title, body=body, group=group_name)
                if pushed:
                    self.store.mark_pushed(msg_id)
                    log.info("已推送 msg=%s", msg_id)

        except Exception:  # noqa: BLE001
            log.exception("处理消息失败: %r", msg)

    def run(self) -> None:
        ws_url = self.cfg["wcf"]["ws_url"]
        log.info("连接 WechatFerry daemon: %s", ws_url)

        while not self._stop:
            try:
                ws = websocket.WebSocket()
                ws.connect(ws_url)
                # 订阅消息事件
                ws.send(json.dumps({
                    "type": "callback",
                    "payload": json.dumps({"type": "msg"}),
                }))
                log.info("已订阅消息事件，开始接收...")

                while not self._stop:
                    raw = ws.recv()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("非 JSON 消息: %s", raw[:200])
                        continue
                    if msg.get("type") == "msg":
                        self.handle_message(msg)
            except websocket.WebSocketException as e:
                log.warning("WebSocket 断开/异常: %s，5 秒后重连", e)
                time.sleep(5)
            except Exception:  # noqa: BLE001
                log.exception("主循环异常，5 秒后重连")
                time.sleep(5)


def main() -> None:
    cfg = load_config()
    consumer = Consumer(cfg)
    signal.signal(signal.SIGINT, consumer.stop)
    signal.signal(signal.SIGTERM, consumer.stop)
    consumer.run()


if __name__ == "__main__":
    main()