"""对 aixed/WeChat-Hook DLL HTTP API 的封装。

DLL 注入到 WeChat 进程后，在 127.0.0.1:30001 起一个 HTTP 服务，
主要 endpoint（来自 Postman 集合 + 实际验证）：
- GET  /QueryDB/status
- POST /set_callback         body: {"url": "http://..."}
- POST /GetSelfProfile
- POST /QueryDB/execute

我们只用到 status / set_callback / GetSelfProfile。其他保留以备扩展。
"""
import logging
from dataclasses import dataclass

import requests


log = logging.getLogger(__name__)


class HookError(Exception):
    pass


@dataclass
class HookConfig:
    api_base: str = "http://127.0.0.1:30001"
    timeout_s: float = 5.0


class HookClient:
    def __init__(self, cfg: HookConfig):
        self.cfg = cfg
        self._session = requests.Session()

    def _post(self, path: str, json_body: dict | None = None) -> dict:
        url = f"{self.cfg.api_base}{path}"
        try:
            r = self._session.post(
                url,
                json=json_body,
                timeout=self.cfg.timeout_s,
            )
        except requests.RequestException as e:
            raise HookError(f"POST {url} 网络异常: {e}") from e
        if not r.ok:
            raise HookError(f"POST {url} -> {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise HookError(f"POST {url} 响应非 JSON: {r.text[:200]}") from e

    def _get(self, path: str) -> dict:
        url = f"{self.cfg.api_base}{path}"
        try:
            r = self._session.get(url, timeout=self.cfg.timeout_s)
        except requests.RequestException as e:
            raise HookError(f"GET {url} 网络异常: {e}") from e
        if not r.ok:
            raise HookError(f"GET {url} -> {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise HookError(f"GET {url} 响应非 JSON: {r.text[:200]}") from e

    # ---- 业务方法 ----

    def status(self) -> dict:
        """返回 {"IsLogin": int, "hWeixin": int}。注意：IsLogin 字段在某些 WeChat 版本上是 0 但实际已登录，"""
        return self._get("/QueryDB/status")

    def set_callback(self, callback_url: str) -> dict:
        """告诉 DLL 把消息推到哪里。ret=0 表示成功。"""
        return self._post("/set_callback", {"url": callback_url})

    def get_self_profile(self) -> dict:
        """返回当前登录账号资料。

        注意：这个 endpoint 在新版 WeChat 上有 bug，可能返回的是最近活跃的联系人
        而不是真正的 self profile。我们用它做 best-effort，self_wxid 还是要靠配置。
        """
        return self._post("/GetSelfProfile", {})

    def query_db(self, sql: str) -> dict:
        """执行 SQL（极少数场景需要，比如查好友/群列表）。DLL 文档说只读。"""
        return self._post("/QueryDB/execute", {"sql": sql})

    def send_text(self, to_wxid: str, msg: str) -> dict:
        """发文本消息。

        to_wxid:
          - 群：roomid（如 "XXX@chatroom"）
          - 私聊：对方 wxid
        返回 ret=0 表示成功（注意：aixed DLL 不论成功失败都返 0，需要看群里实际收到才知真发了）。
        """
        # aixed DLL 期望字段名是 wxidorgid，不是 to_wxid（看 SendTextMsg.cpp 源码）
        return self._post("/SendTextMsg", {"wxidorgid": to_wxid, "msg": msg})
