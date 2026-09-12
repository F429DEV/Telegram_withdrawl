"""参与抽奖：按钮报名、口令报名、发言积分统计。"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import db, eligibility, service, texts

# 算「在群里」的几种状态
_IN_GROUP = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
    ChatMemberStatus.RESTRICTED,
}

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
            f"中奖：\n{names}\n\n群里发 /verify {g.id} 看结果。",
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

async def _ensure_link(bot, chat_id: int, user_id: int) -> Optional[str]:
    """拿到（必要时生成）某人在某群的专属邀请链接。生成不了返回 None。"""
    link = db.existing_invite_link(chat_id, user_id)
    if link:
        return link
    try:
        created = await bot.create_chat_invite_link(chat_id, name=f"ref-{user_id}"[:32])
    except TelegramError as exc:
        log.warning("群 %s 生成邀请链接失败: %s", chat_id, exc)
        return None
    db.save_invite_link(chat_id, user_id, created.invite_link)
    return created.invite_link


async def send_invite_dm(bot, chat_id: int, user_id: int, chat_title: str = "") -> bool:
    """把专属链接私发给本人。对方没和机器人私聊过的话会失败，返回 False。"""
    link = await _ensure_link(bot, chat_id, user_id)
    if link is None:
        return False
    invited = db.invite_count(chat_id, user_id)
    where = f"「{texts.esc(chat_title)}」" if chat_title else " 那个群 "
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🔗 <b>你在{where}的专属邀请链接</b>\n\n"
                f"{texts.esc(link)}\n\n"
                f"把它发给朋友，谁从这条链接进群就算你邀请的。\n"
                f"你目前已邀请 <b>{invited}</b> 人。\n\n"
                f"<i>开了邀请加成的抽奖，每邀 1 人中奖权重就 +N，"
                f"在群里发 /viewmyinfo 看自己当前的权重。</i>"
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return True
    except TelegramError as exc:
        log.info("给 %s 私发邀请链接失败（多半是没私聊过机器人）: %s", user_id, exc)
        return False


async def invite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/invite —— 在群里发，机器人把专属邀请链接私信给你。

    链接不发在群里：一来避免刷屏，二来贴在群里的链接谁都能复制走，
    别人用它拉人就变成给发链接的人刷邀请数了。
    """
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text(
            "请到群里发 /invite，我会把那个群的专属链接私信给你。"
        )
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if await send_invite_dm(context.bot, chat.id, user.id, chat.title or ""):
        tip = await msg.reply_text("🔗 已经私信发给你了，去看看私聊。")
        _schedule_delete(context, chat.id, tip.message_id)
        return

    # 私发不出去：要么没权限生成链接，要么对方没先私聊过机器人
    if db.existing_invite_link(chat.id, user.id) is None and \
            await _ensure_link(context.bot, chat.id, user.id) is None:
        await msg.reply_text(
            "生成失败：我需要「邀请用户」这项管理员权限才能发专属链接，"
            "让群主到管理员设置里勾上。"
        )
        return

    me = await context.bot.get_me()
    deep_link = f"https://t.me/{me.username}?start=inv_{chat.id}"
    tip = await msg.reply_text(
        f"我私信不了你 —— Telegram 不允许机器人主动私聊没说过话的人。\n"
        f"点这里跟我说句话，链接会自动发给你：{deep_link}",
        disable_web_page_preview=True,
    )
    _schedule_delete(context, chat.id, tip.message_id, delay=30)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """成员状态变化：只关心「通过某条专属邀请链接进了群」。"""
    cmu = update.chat_member
    if cmu is None:
        return
    was_in = cmu.old_chat_member.status in _IN_GROUP
    now_in = cmu.new_chat_member.status in _IN_GROUP
    if was_in or not now_in:
        return
    link = cmu.invite_link
    if link is None:
        return
    inviter_id = db.invite_link_owner(link.invite_link)
    if inviter_id is None:
        return
    joined = cmu.new_chat_member.user
    if joined.is_bot:
        return
    if db.record_invite(cmu.chat.id, joined.id, inviter_id):
        log.info("群 %s：%s 通过专属链接邀请了 %s", cmu.chat.id, inviter_id, joined.id)


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

    # 消息抽奖：每条消息都记下来，开奖时随机抽一条
    for g in db.active_by_mode(chat.id, db.MODE_MESSAGE):
        if not db.is_participant(g.id, user.id):
            ok, _ = await eligibility.check(context.bot, g, user)
            if not ok:
                continue
            db.add_participant(g.id, user.id, user.username, user.full_name)
        else:
            db.bump_weight(g.id, user.id)
        db.record_message(
            g.id, msg.message_id, user.id, user.username, user.full_name, text
        )
        fresh = db.get(g.id)
        if not await service.maybe_finish_by_cap(context.bot, context.application, fresh):
            await service.refresh_card(context.bot, fresh)

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
