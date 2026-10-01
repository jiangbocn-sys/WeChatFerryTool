#!/bin/bash
# Git Bash 一键启动脚本
# 启动顺序：daemon → 微信 → consumer
# 使用：在 Git Bash 里 ./start.sh
set -e

WCF_DIR="${WCF_DIR:-C:/tools/WeChatFerry}"
WECHAT="${WECHAT:-C:/Program Files/Tencent/WeChat/WeChat.exe}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$PROJECT_DIR/.venv/Scripts/python.exe"

cd "$PROJECT_DIR"

# 1. 启动 daemon（后台）
echo "[1/3] 启动 WechatFerry daemon..."
if [ -f "$WCF_DIR/wcf.exe" ]; then
    # 用 cmd 调起，start /B 是后台运行
    cmd //c start "" "$WCF_DIR\\wcf.exe"
else
    echo "警告：未找到 wcf.exe ($WCF_DIR)，请手动以管理员身份启动"
fi
sleep 3

# 2. 启动微信
echo "[2/3] 启动微信 PC..."
if [ -f "$WECHAT" ]; then
    cmd //c start "" "$WECHAT"
else
    echo "警告：未找到 WeChat.exe ($WECHAT)，请手动启动"
fi
sleep 5

# 3. 启动 consumer
echo "[3/3] 启动 consumer..."
if [ ! -f "$VENV_PY" ]; then
    echo "未找到 .venv，请先 python -m venv .venv && pip install -r requirements.txt"
    exit 1
fi
exec "$VENV_PY" -m consumer.main