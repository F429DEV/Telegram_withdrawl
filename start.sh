#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -f .env ] || { echo "没找到 .env，先 cp .env.example .env 并填入 BOT_TOKEN"; exit 1; }
exec python3 bot.py
