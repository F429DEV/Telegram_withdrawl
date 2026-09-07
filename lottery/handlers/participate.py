"""参与抽奖：按钮报名、口令报名、发言积分统计。"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import db, eligibility, service, texts

log = logging.getLogger(__name__)


async def _delete_later(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    try:
        await context.bot.delete_message(data["chat_id"], data["message_id"])
    except TelegramError:
        pass


def _schedule_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int,
                     delay: int = 8) -> None:
    if context.application.job_queue is None:
        return
    context.application.job_queue.run_once(
        _delete_later, when=delay, data={"chat_id": chat_id, "message_id": message_id}
    )


# ---------------------------------------------------------------- 按钮

async def join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, gid_raw, action = query.data.split(":", 2)
    g = db.get(int(gid_raw))
    user = query.from_user

    if g is None:
        await query.answer("这个抽奖不存在了。", show_alert=True)
        return

    if action == "who":
        await query.answer(
            f"当前 {db.participant_count(g.id)} 人参与。", show_alert=False
        )
        return

    if action == "verify":
        wins = db.winners(g.id)
        names = "\n".join(f"{i}. {w.full_name}" for i, w in enumerate(wins, 1)) or "无"
        await query.answer(
            f"种子 {g.seed}\n中奖：\n{names}\n\n群里发 /verify {g.id} 看完整名单。",
            show_alert=True,
        )
        return

    if g.status != db.STATUS_ACTIVE:
        await query.answer("这个抽奖已经结束了。", show_alert=True)
        return

    if action == "out":
        if db.remove_participant(g.id, user.id):
            await query.answer("已退出，祝下次好运。")
            await service.refresh_card(context.bot, db.get(g.id))
        else:
            await query.answer("你本来就没参与。")
        return

    if action != "in":
        await query.answer()
        return

    if db.is_participant(g.id, user.id):
        await query.answer("你已经报过名了，等开奖就行 🍀")
        return

    ok, reason = await eligibility.check(context.bot, g, user)
    if not ok:
        await query.answer(reason, show_alert=True)
        return

    db.add_participant(g.id, user.id, user.username, user.full_name)
    count = db.participant_count(g.id)
    await query.answer(f"报名成功！你是第 {count} 位 🎉")

    g = db.get(g.id)
    if await service.maybe_finish_by_cap(context.bot, context.application, g):
        return
    await service.refresh_card(context.bot, g)


# ---------------------------------------------------------------- 群消息

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """有人进群：记一笔「谁拉的谁」，给邀请加成用。"""
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return
    inviter = msg.from_user
    if inviter is None:
        return
    chat_id = update.effective_chat.id
    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        # 自己通过邀请链接进群时，from_user 就是他本人 —— 这不算邀请
        if member.id == inviter.id:
            continue
        if db.record_invite(chat_id, member.id, inviter.id):
            log.info("群 %s：%s 邀请了 %s", chat_id, inviter.id, member.id)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """每条群消息都会走这里：记录活跃度、处理口令、累计积分。"""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or user is None or user.is_bot:
        return

    # 正在回复配置面板的 ForceReply 提示，不当成口令/积分消息处理
    if msg.reply_to_message:
        rid = msg.reply_to_message.message_id
        bot_data = context.application.bot_data
        if rid in bot_data.get("pending", {}) or rid in bot_data.get("pending_settings", {}):
            return

    db.touch_user(chat.id, user.id)

    text = (msg.text or msg.caption or "").strip()

    # 口令报名
    if text:
        for g in db.active_keyword_giveaways(chat.id):
            hit = db.matches_keyword(g.keywords, text)
            if hit is None:
                continue
            if db.is_participant(g.id, user.id):
                continue
            ok, reason = await eligibility.check(context.bot, g, user)
            if not ok:
                tip = await msg.reply_text(f"@{user.username or user.full_name} {reason}")
                _schedule_delete(context, chat.id, tip.message_id)
                continue
            db.add_participant(g.id, user.id, user.username, user.full_name)
            count = db.participant_count(g.id)
            tip = await msg.reply_text(
                f"✅ 已记下，你是第 {count} 位参与 #{g.id}「{texts.esc(g.prize)}」的人。",
                parse_mode=ParseMode.HTML,
            )
            _schedule_delete(context, chat.id, tip.message_id)
            g = db.get(g.id)
            if not await service.maybe_finish_by_cap(context.bot, context.application, g):
                await service.refresh_card(context.bot, g)

    # 发言积分
    for g in db.active_points_giveaways(chat.id):
        if db.is_participant(g.id, user.id):
            db.bump_weight(g.id, user.id)
            continue
        ok, _ = await eligibility.check(context.bot, g, user)
        if not ok:
            continue
        db.add_participant(g.id, user.id, user.username, user.full_name)
        g = db.get(g.id)
        if not await service.maybe_finish_by_cap(context.bot, context.application, g):
            await service.refresh_card(context.bot, g)
