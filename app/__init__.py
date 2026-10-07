"""WeChatFerry 消息助手 —— 应用层。

包含：
  supervisor.py  微信生命周期 / keyhook 注入 / 回调注册 / 恢复正常
  main.py        应用装配与启停编排
  tray.py        系统托盘（缺依赖时优雅降级）
"""
