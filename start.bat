@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .env (
    echo 没找到 .env，先复制 .env.example 为 .env 并填入 BOT_TOKEN。
    pause
    exit /b 1
)
python bot.py
pause
