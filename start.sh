#!/bin/bash
# Git Bash 一键启动脚本
# 启动顺序：微信 → consumer（version.dll 已被微信启动时自动加载）
# 使用：在 Git Bash 里 ./start.sh
set -e

WECHAT="${WECHAT:-C:/Program Files/Tencent/Weixin/Weixin.exe}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$PROJECT_DIR/.venv/Scripts/python.exe"
LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$LOG_DIR/consumer.pid"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

# 0. 检查 version.dll 是否在 WeChat 目录（必需，否则 hook 不生效）
echo "[1/3] 检查 version.dll..."
if [ -f "$(dirname "$WECHAT")/version.dll" ]; then
    echo "OK"
else
    echo "警告：未找到 version.dll，请先复制 aixed 提供的 version.dll 到 $(dirname "$WECHAT")"
    echo "否则 DLL 不会自动加载，consumer 启动后会报'连不上 DLL'"
fi

# 1. 启动微信
echo "[2/3] 启动微信 PC..."
if [ -f "$WECHAT" ]; then
    cmd //c start "" "$WECHAT"
else
    echo "警告：未找到 Weixin.exe ($WECHAT)，请手动启动"
fi
sleep 5

# 2. 启动 consumer
echo "[3/3] 启动 consumer..."
if [ ! -f "$VENV_PY" ]; then
    echo "未找到 .venv，请先 python -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

# 把当前 bash PID 写入文件，exec 后这个 PID 就属于 consumer 进程
# stop.sh 据此精确停止，不会误杀其他 python.exe
echo $$ > "$PID_FILE"
exec "$VENV_PY" -m consumer.main
