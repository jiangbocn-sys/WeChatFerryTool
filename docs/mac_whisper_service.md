# Mac mini Whisper 转写服务 · 接口约定

Windows 端（consumer）会把微信语音的 WAV 文件 POST 到这个服务，拿回转写文字。

## 接口契约

```
POST http://<mac-ip>:<port>/inference
Content-Type: multipart/form-data

表单字段：
  file             WAV 文件（16kHz 单声道，Windows 端已转好）
  response_format  "json"（可选）
  language         "zh"（可选）

成功响应（200）：
  {"text": "识别出来的文字"}
  （也兼容 {"result": ...} / {"transcription": ...} / 纯文本）
```

Windows 端配置（`config.yaml`）：
```yaml
asr:
  enabled: true
  url: "http://<mac-ip>:<port>/inference"
  timeout_s: 60
```

---

## 方案 A：whisper.cpp 自带 server（最省事）

```bash
# 编译或下载 whisper.cpp 后：
./whisper-server \
  -m models/ggml-large-v3.bin \
  --host 0.0.0.0 \
  --port 8080 \
  --language zh
```

- 自带 `/inference` 接口，契约完全匹配（`file` + `response_format=json`）
- 不需要写任何代码
- 注意 `--host 0.0.0.0` 才能被局域网访问

## 方案 B：Python 服务（如果你装的是 openai-whisper / mlx-whisper）

```python
# mac_whisper_server.py   →   pip install flask
# 用法: python mac_whisper_server.py   （默认端口 8080）

import subprocess, tempfile, os
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.post("/inference")
def inference():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        f.save(tmp.name)
        path = tmp.name
    try:
        # 用 openai-whisper 的命令行（large 模型）：
        out = subprocess.run(
            ["whisper", path, "--model", "large", "--language", "zh",
             "--output_format", "txt", "--output_dir", "/tmp/whisper_out"],
            capture_output=True, text=True, timeout=300,
        )
        txt_path = "/tmp/whisper_out/" + os.path.basename(path).rsplit(".", 1)[0] + ".txt"
        text = open(txt_path, encoding="utf-8").read().strip() if os.path.exists(txt_path) else ""
        return jsonify({"text": text})
    finally:
        os.unlink(path)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
```

> 如果是 mlx-whisper（Apple Silicon 更快），把 subprocess 那段换成：
> ```python
> import mlx_whisper
> result = mlx_whisper.transcribe(path, path_or_hf_repo="mlx-community/whisper-large-v3-mlx", language="zh")
> return jsonify({"text": result["text"].strip()})
> ```

---

## 安全提示

服务只放在局域网内使用（不要映射到公网）。如果担心局域网内其他人调用，可以在 Mac 防火墙里限制来源 IP 为本机 Windows 的 IP。

## Windows 端验证（服务就绪后）

```bash
curl -F "file=@data/voices/<某条语音>.wav" -F "response_format=json" http://<mac-ip>:8080/inference
# 期望返回 {"text": "..."}
```

或跑项目里的批量补转脚本：
```bash
.venv/Scripts/python.exe scripts/transcribe_pending.py
```