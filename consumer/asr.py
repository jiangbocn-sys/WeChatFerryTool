"""语音转写客户端：调用 Mac mini 上的 whisper 服务。

接口约定（Mac 端实现，见 docs/mac_whisper_service.md）：
  POST <url>  multipart/form-data
    file: WAV 文件（16kHz 单声道）
    response_format=json, language=zh（可选参数，whisper.cpp server 支持）
  响应: JSON {"text": "识别文字"}（或纯文本）

兼容：whisper.cpp 自带 server（POST /inference）、自写 Flask/FastAPI 服务。
"""
import logging
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)


class ASRClient:
    def __init__(self, url: str, timeout_s: float = 60.0, enabled: bool = True):
        self.url = (url or "").strip()
        self.timeout_s = timeout_s
        self.enabled = enabled
        self._session = requests.Session()

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.url)

    def transcribe(self, wav_path: Path) -> Optional[str]:
        """转写 WAV 文件，返回文字；失败返回 None。"""
        if not self.ready:
            log.debug("ASR 未配置（url 为空或未启用），跳过转写")
            return None
        try:
            with wav_path.open("rb") as f:
                r = self._session.post(
                    self.url,
                    files={"file": (wav_path.name, f, "audio/wav")},
                    data={"response_format": "json", "language": "zh"},
                    timeout=self.timeout_s,
                )
            if not r.ok:
                log.warning("ASR 服务返回 %s: %s", r.status_code, r.text[:200])
                return None
            try:
                j = r.json()
                text = (j.get("text") or j.get("result") or j.get("transcription") or "").strip()
                return text or None
            except ValueError:
                text = r.text.strip()
                return text or None
        except requests.RequestException as e:
            log.warning("ASR 请求失败（Mac 服务是否在线？）: %s", e)
            return None