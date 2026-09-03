#!/usr/bin/env bash
# Linux / macOS 启动脚本：优先用项目自带的 .venv
set -e
cd "$(dirname "$0")"

[ -f .env ] || { echo "没找到 .env，先 cp .env.example .env 并填入 BOT_TOKEN"; exit 1; }

if [ ! -x .venv/bin/python ]; then
    echo "首次运行，正在创建虚拟环境 .venv ..."
    python3 -m venv .venv || {
        echo "创建失败。Debian/Ubuntu 上先装：sudo apt install -y python3-venv python3-full"
        exit 1
    }
    .venv/bin/pip install --upgrade pip -q
    .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python bot.py
