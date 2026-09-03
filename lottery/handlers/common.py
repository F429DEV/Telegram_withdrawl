"""基础命令：/start、/help，以及被拉进群时的自我介绍。"""

from __future__ import annotations

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from .. import texts


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text(texts.START_PRIVATE)
    else:
        await update.message.reply_text("我在。群管理员发 /new 就能开抽奖，/help 看说明。")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        texts.HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def greet_new_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """机器人被加进群时打个招呼。"""
    msg = update.message
    if not msg or not msg.new_chat_members:
        return
    me = context.bot.id
    if any(u.id == me for u in msg.new_chat_members):
        await msg.reply_text(
            "🎁 抽奖机器人已就位。\n\n"
            "群管理员发 /new 打开配置面板即可开始，/help 查看全部命令。\n"
            "建议给我「删除消息」权限，这样口令报名的刷屏消息我可以自动清掉。"
        )
