#!/bin/bash
# 停掉所有相关进程
# 使用：在 Git Bash 里 ./stop.sh

echo "停止 consumer..."
taskkill //IM python.exe //FI "WINDOWTITLE eq consumer*" 2>/dev/null || true
taskkill //IM python.exe //FI "MEMUSAGE gt 50000" 2>/dev/null || true

echo "停止 WechatFerry daemon..."
taskkill //IM wcf.exe //F 2>/dev/null || echo "(未运行)"

echo "停止微信..."
taskkill //IM WeChat.exe //F 2>/dev/null || echo "(未运行)"

echo "完成"