#!/usr/bin/env python3
"""Telegram 群抽奖机器人 —— 入口。

跑起来只要两步：
    1. cp .env.example .env  然后把 BOT_TOKEN 填进去
    2. python bot.py
"""

from __future__ import annotations

import logging

from telegram import BotCommand, BotCommandScopeAllGroupChats
from telegram.ext import Application, ContextTypes

from lottery import db, handlers, service, texts
from lottery.config import Config

log = logging.getLogger("lottery")

COMMANDS = [
    BotCommand("new", "发起抽奖"),
    BotCommand("list", "查看进行中的抽奖"),
    BotCommand("end", "立即开奖"),
    BotCommand("cancel", "取消抽奖"),
    BotCommand("reroll", "重新抽取"),
    BotCommand("verify", "复核开奖过程"),
    BotCommand("pick", "从名单里随机抽"),
    BotCommand("settings", "群设置"),
    BotCommand("help", "使用说明"),
]


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllGroupChats())
    await service.restore_jobs(app)
    me = await app.bot.get_me()
    log.info("机器人已启动：@%s（把它加进群，管理员发 /new 开始）", me.username)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("处理更新时出错", exc_info=context.error)


def main() -> None:
    cfg = Config.load()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=getattr(logging, cfg.log_level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db.init(cfg.db_path)
    texts.set_timezone(cfg.display_tz)

    app = Application.builder().token(cfg.bot_token).post_init(_post_init).build()
    app.bot_data["config"] = cfg
    handlers.register(app)
    app.add_error_handler(_on_error)

    log.info("数据库：%s", cfg.db_path)
    app.run_polling(allowed_updates=["message", "callback_query", "my_chat_member"],
                    drop_pending_updates=True)


if __name__ == "__main__":
    main()
