#!/bin/bash
# Web 管理后台启动脚本
# 用法：./web.sh           # 启动 web（端口 6060，仅本地访问）
#       ./web.sh --port 8080
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$PROJECT_DIR/.venv/Scripts/python.exe"
LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$LOG_DIR/web.pid"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

if [ ! -f "$VENV_PY" ]; then
    echo "未找到 .venv，请先 python -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

echo $$ > "$PID_FILE"
exec "$VENV_PY" -m web.app "$@"
