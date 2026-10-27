#!/usr/bin/env python3
"""Telegram 群抽奖机器人 —— 入口。

跑起来只要两步：
    1. cp .env.example .env  然后把 BOT_TOKEN 填进去
    2. python bot.py
"""

from __future__ import annotations

import logging
import logging.handlers

from telegram import BotCommand, BotCommandScopeAllGroupChats
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from lottery import db, ephemeral, handlers, service, texts
from lottery.config import Config

log = logging.getLogger("lottery")

COMMANDS = [
    BotCommand("new", "发起抽奖"),
    BotCommand("list", "查看进行中的抽奖"),
    BotCommand("view", "查看某个抽奖的详细参数"),
    BotCommand("end", "立即开奖"),
    BotCommand("cancel", "取消抽奖"),
    BotCommand("reroll", "重新抽取"),
    BotCommand("verify", "查看开奖排名"),
    BotCommand("pick", "从名单里随机抽"),
    BotCommand("settings", "群设置"),
    BotCommand("viewmyinfo", "查看自己的 ID 和本群记录"),
    BotCommand("invite", "拿到自己的专属邀请链接"),
    BotCommand("help", "使用说明"),
]


async def _notify_catch_up(app: Application, caught_up: list) -> None:
    """机器人停机期间该开没开的抽奖，启动时补开了 —— 把清单私信给主人。

    这是停机唯一会留下后果的地方：中奖名单迟到了，奖品还得人去发。
    """
    cfg = app.bot_data["config"]
    if not caught_up:
        return
    if not cfg.owner_ids:
        log.warning(
            "启动时补开了 %s 场抽奖，但 .env 里没配 OWNER_IDS，没法通知任何人。",
            len(caught_up),
        )
        return

    now = db.now()
    lines = [
        f"⚠️ <b>重启后补开了 {len(caught_up)} 场抽奖</b>",
        "",
        "这几场本该在机器人停机期间开奖，刚才一并补开了：",
    ]
    for g in caught_up:
        late = texts.fmt_duration(now - g.end_at) if g.end_at else "未知"
        when = texts.fmt_time(g.end_at) if g.end_at else "未知"
        lines.append(
            f"• #{g.id}「{texts.esc(g.prize) or '未填奖品'}」"
            f"原定 {when}，迟了 {late}"
        )
    lines += ["", "<i>中奖名单已经发到各群，奖品发放记得跟进。</i>"]
    text = "\n".join(lines)

    for owner_id in cfg.owner_ids:
        try:
            await app.bot.send_message(
                chat_id=owner_id, text=text, parse_mode=ParseMode.HTML
            )
        except TelegramError as exc:
            log.warning("补开清单发给主人 %s 失败: %s", owner_id, exc)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllGroupChats())
    caught_up = await service.restore_jobs(app)
    if caught_up:
        log.warning("启动时补开了 %s 场过期抽奖", len(caught_up))
        await _notify_catch_up(app, caught_up)

    cfg = app.bot_data["config"]
    if cfg.heartbeat_file:
        service.start_heartbeat(app, cfg.heartbeat_file, cfg.heartbeat_seconds)

    me = await app.bot.get_me()
    log.info("机器人已启动：@%s（把它加进群，管理员发 /new 开始）", me.username)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("处理更新时出错", exc_info=context.error)


def _setup_logging(cfg: Config) -> None:
    """控制台 + 滚动日志文件。文件满 LOG_MAX_MB 自动切一份，保留 LOG_BACKUPS 份。"""
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if cfg.log_file:
        rotating = logging.handlers.RotatingFileHandler(
            cfg.log_file,
            maxBytes=cfg.log_max_mb * 1024 * 1024,
            backupCount=cfg.log_backups,
            encoding="utf-8",
        )
        rotating.setFormatter(fmt)
        root.addHandler(rotating)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def main() -> None:
    cfg = Config.load()
    _setup_logging(cfg)

    db.init(cfg.db_path)
    texts.set_timezone(cfg.display_tz)

    app = (
        Application.builder()
        .bot(ephemeral.EphemeralBot(cfg.bot_token))
        .post_init(_post_init)
        .build()
    )
    app.bot_data["config"] = cfg
    ephemeral.bind(app, cfg.ephemeral_seconds)
    handlers.register(app)
    app.add_error_handler(_on_error)

    log.info("数据库：%s", cfg.db_path)
    if cfg.heartbeat_file:
        log.info("心跳文件：%s（每 %s 秒）", cfg.heartbeat_file, cfg.heartbeat_seconds)
    if cfg.ephemeral_seconds > 0:
        log.info("群消息自动清理：%s 秒（抽奖卡片 / 开奖结果 / 创建面板除外）",
                 cfg.ephemeral_seconds)
    else:
        log.info("群消息自动清理：已关闭")
    if cfg.log_file:
        log.info("日志文件：%s（%s MB 滚动，保留 %s 份）",
                 cfg.log_file, cfg.log_max_mb, cfg.log_backups)
    # chat_member 必须显式声明，否则收不到「谁通过哪条邀请链接进群」
    app.run_polling(
        allowed_updates=["message", "callback_query", "my_chat_member", "chat_member"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
