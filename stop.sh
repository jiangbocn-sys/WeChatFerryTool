#!/bin/bash
# 停掉所有相关进程
# 使用：在 Git Bash 里 ./stop.sh

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$PROJECT_DIR/logs/consumer.pid"

echo "停止 consumer..."
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$PID" ]; then
        taskkill //PID "$PID" //F 2>/dev/null \
            && echo "已停止 consumer (PID $PID)" \
            || echo "(PID $PID 已退出)"
    fi
    rm -f "$PID_FILE"
else
    echo "(未找到 PID 文件，请确认 consumer 是通过 start.sh 启动的)"
fi

echo "停止 web 管理后台..."
WEB_PID_FILE="$PROJECT_DIR/logs/web.pid"
if [ -f "$WEB_PID_FILE" ]; then
    WPID=$(cat "$WEB_PID_FILE" 2>/dev/null)
    if [ -n "$WPID" ]; then
        taskkill //PID "$WPID" //F 2>/dev/null \
            && echo "已停止 web (PID $WPID)" \
            || echo "(PID $WPID 已退出)"
    fi
    rm -f "$WEB_PID_FILE"
else
    echo "(未找到 web PID 文件，跳过)"
fi

echo "停止微信..."
taskkill //IM Weixin.exe //F 2>/dev/null || taskkill //IM WeChat.exe //F 2>/dev/null || echo "(未运行)"

echo "完成"