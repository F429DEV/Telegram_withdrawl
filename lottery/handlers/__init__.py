"""把所有处理器注册到 Application 上。"""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import common, create, manage, owner, participate

GROUPS = filters.ChatType.GROUPS


async def _reply_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ForceReply 的回复可能属于创建面板，也可能属于群设置面板，两边都问一遍。"""
    await create.handle_reply(update, context)
    await manage.handle_settings_reply(update, context)


def register(app: Application) -> None:
    # ---- 命令 ----
    app.add_handler(CommandHandler("start", common.start))
    app.add_handler(CommandHandler("help", common.help_cmd))
    app.add_handler(CommandHandler("viewmyinfo", common.viewmyinfo))
    app.add_handler(CommandHandler(["new", "draw"], create.new_giveaway))
    app.add_handler(CommandHandler("list", manage.list_cmd))
    app.add_handler(CommandHandler("view", manage.view_cmd))
    app.add_handler(CommandHandler("end", manage.end_cmd))
    app.add_handler(CommandHandler("cancel", manage.cancel_cmd))
    app.add_handler(CommandHandler("reroll", manage.reroll_cmd))
    app.add_handler(CommandHandler("verify", manage.verify_cmd))
    app.add_handler(CommandHandler("pick", manage.pick_cmd))
    app.add_handler(CommandHandler("settings", manage.settings_cmd))

    # ---- 主人私聊专用，不进命令菜单，群里不响应 ----
    app.add_handler(
        CommandHandler("reserve", owner.reserve_cmd, filters=filters.ChatType.PRIVATE)
    )
    app.add_handler(
        CommandHandler("unreserve", owner.unreserve_cmd, filters=filters.ChatType.PRIVATE)
    )

    # ---- 内联按钮 ----
    app.add_handler(CallbackQueryHandler(create.draft_callback, pattern=r"^d:\d+:"))
    app.add_handler(CallbackQueryHandler(create.requirement_callback, pattern=r"^r:\d+:"))
    app.add_handler(CallbackQueryHandler(participate.join_callback, pattern=r"^j:\d+:"))
    app.add_handler(CallbackQueryHandler(manage.settings_callback, pattern=r"^s:\d+:"))

    # ---- 对 ForceReply 的回复（要排在普通群消息前面）----
    app.add_handler(
        MessageHandler(GROUPS & filters.REPLY & filters.TEXT & ~filters.COMMAND, _reply_router)
    )

    # ---- 进群问候 ----
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, common.greet_new_group))
    # ---- 记录「谁拉了谁」，另开一个 group 才不会被上面的问候截胡 ----
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, participate.on_new_members),
        group=2,
    )

    # ---- 所有群消息：活跃度、口令、积分（单独一个 group，保证前面的也能跑）----
    app.add_handler(
        MessageHandler(
            GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL, participate.on_group_message
        ),
        group=1,
    )
