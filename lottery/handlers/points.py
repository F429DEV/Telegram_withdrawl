"""积分板块的用户入口：签到、我的积分、排行榜。

群里直接打词就行（「签到」「我的积分」「排行榜」），也有对应的命令。
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from .. import db, points, texts

log = logging.getLogger(__name__)


def _in_group(update: Update) -> bool:
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if not _in_group(update):
        await msg.reply_text("签到要在群里，积分是按群算的。")
        return

    got = points.check_in(chat.id, user)
    rank, total = db.point_rank(chat.id, user.id)

    if got.ok:
        text = texts.checkin_ok(user.full_name, got.points, got.balance, got.streak, rank, total)
    elif got.reason == "今天已经签过了":
        text = texts.checkin_again(got.balance, got.streak, rank, total)
    else:
        text = texts.points_disabled() if "没开" in got.reason else got.reason
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def my_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if not _in_group(update):
        await msg.reply_text("积分是按群算的，到群里发「我的积分」。")
        return

    settings = db.point_settings(chat.id)
    if not settings["enabled"]:
        await msg.reply_text(texts.points_disabled())
        return

    wallet = db.wallet(chat.id, user.id)
    rank, total = db.point_rank(chat.id, user.id)
    await msg.reply_text(
        texts.points_card(
            user.full_name,
            wallet,
            rank,
            total,
            settings,
            db.daily(chat.id, user.id),
            db.checkin_streak(chat.id, user.id),
            len(db.user_redemptions(chat.id, user.id, limit=99)),
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if not _in_group(update):
        await msg.reply_text("排行榜是按群算的，到群里发「排行榜」。")
        return

    if not db.point_settings(chat.id)["enabled"]:
        await msg.reply_text(texts.points_disabled())
        return

    rank, total = db.point_rank(chat.id, user.id)
    await msg.reply_text(
        texts.points_top(
            db.point_top(chat.id, 10), rank, db.point_balance(chat.id, user.id), total
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
