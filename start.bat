@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".env" (
    echo [错误] 没找到 .env
    echo 先把 .env.example 复制成 .env，并填入 @BotFather 给的 BOT_TOKEN。
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo 首次运行，正在创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        echo 请确认已安装 Python 3，且安装时勾选了 "Add Python to PATH"。
        echo.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\pip.exe" install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，检查一下网络。
        echo.
        pause
        exit /b 1
    )
    echo 虚拟环境就绪。
    echo.
)

echo 启动中，按 Ctrl+C 停止。日志同时写入 logs\bot.log
echo.
".venv\Scripts\python.exe" bot.py

echo.
echo 机器人已退出。
pause
