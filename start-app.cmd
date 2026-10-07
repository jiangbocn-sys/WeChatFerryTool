@echo off
rem ===================================================================
rem  启动（跑你现有数据）—— 用源码方式启动，数据根 = 项目目录
rem
rem  为什么不用 dist\WeChatFerryApp\WeChatFerryApp.exe：
rem    打包 exe 的"数据根 = exe 所在目录"，它会在 dist 下新建一套空的
rem    accounts/data，看不到你现有的 config.yaml / messages.db / labels.json。
rem    那个形态是给"全新安装分发"用的。
rem
rem  关闭窗口 = 退出 app；日志在 accounts\ruibo_jiang_542e\logs\
rem ===================================================================
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 找不到 .venv\Scripts\python.exe
    echo        请在项目目录里先装好虚拟环境。
    pause
    exit /b 1
)
echo 正在启动 WeChatFerry 消息助手（源码方式，数据根=项目目录）...
echo   管理平台: http://127.0.0.1:6060
echo   日志:     accounts\ruibo_jiang_542e\logs\app.log
echo   退出:     托盘图标右键 -^> 退出；或直接关掉这个窗口
echo.
".venv\Scripts\python.exe" -m app.main %*
echo.
echo app 已退出（exit=%ERRORLEVEL%）
pause
