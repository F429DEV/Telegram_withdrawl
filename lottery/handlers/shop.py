"""积分商城的用户入口：浏览货架、点按钮兑换。

兑换要两步确认 —— 扣分不可撤销（卡密发出去就收不回），
一次误触就少几百分，值得多一次点击。

确认卡片是单独一条群消息，只有本人能点，30 秒后随自动清理一起消失。
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import db, keyboards, shop, texts

log = logging.getLogger(__name__)


def _rows(chat_id: int) -> list:
    return [(item, shop.available_stock(item)) for item in db.shop_list(chat_id)]


async def shop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text("积分商城在群里用，积分和奖品都是按群算的。")
        return
    if not db.point_settings(chat.id)["enabled"]:
        await msg.reply_text(texts.points_disabled())
        return

    rows = _rows(chat.id)
    balance = db.point_balance(chat.id, user.id)
    await msg.reply_text(
        texts.shop_card(balance, rows),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.shop_kb(rows),
        disable_web_page_preview=True,
    )


async def _notify_owners(
    context: ContextTypes.DEFAULT_TYPE, record: db.Redemption, chat_title: str
) -> None:
    """实物兑换单：私信通知主人去发货。"""
    cfg = context.application.bot_data["config"]
    if not cfg.owner_ids:
        log.warning(
            "兑换单 #%s 需要人工发货，但 .env 没配 OWNER_IDS，没法通知。"
            "管理员可以在 /settings → 商城管理 → 待处理 里看到。",
            record.id,
        )
        return
    text = texts.redemption_notice(record, chat_title)
    for owner_id in cfg.owner_ids:
        try:
            await context.bot.send_message(
                chat_id=owner_id, text=text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            log.warning("兑换单 #%s 通知主人 %s 失败: %s", record.id, owner_id, exc)


async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    item_id, action = int(parts[1]), parts[2]
    chat, user = update.effective_chat, query.from_user

    item = db.shop_item(item_id)
    if item is None or item.chat_id != chat.id:
        await query.answer("这个奖品不在了。", show_alert=True)
        return

    # ---- 第一步：弹出只有本人能点的确认卡片 ----
    if action == "ask":
        balance = db.point_balance(chat.id, user.id)
        if balance < item.cost:
            await query.answer(
                f"积分不够：「{item.name}」需要 {item.cost} 分，你有 {balance} 分。",
                show_alert=True,
            )
            return
        await query.answer()
        mention = texts.user_link(user.id, user.full_name, user.username)
        await context.bot.send_message(
            chat_id=chat.id,
            text=texts.redeem_confirm(mention, item, balance),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.redeem_confirm_kb(item.id, user.id),
            disable_web_page_preview=True,
        )
        return

    # ---- 第二步：只有发起人能确认或取消 ----
    owner_id = int(parts[3]) if len(parts) > 3 else 0
    if user.id != owner_id:
        await query.answer("这是别人的兑换确认，你可以自己发「积分商城」。", show_alert=True)
        return

    if action == "no":
        await query.answer("已取消")
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return

    if action != "buy":
        await query.answer()
        return

    result = shop.redeem(chat.id, user, item.id)
    if not result.ok:
        await query.answer(result.reason, show_alert=True)
        return

    record = db.redemption(result.redemption_id)

    if item.is_code:
        delivered = False
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=texts.code_dm(item, result.code or ""),
                parse_mode=ParseMode.HTML,
            )
            delivered = True
        except TelegramError as exc:
            log.info("卡密私信失败，改用弹窗给 %s: %s", user.id, exc)

        if delivered:
            await query.answer("兑换成功，卡密已私信给你 🎟")
            body = texts.redeem_done_code(item, result.balance)
        else:
            # 私信发不出去（没私聊过机器人）就用弹窗兜底 —— 只有他自己看得见
            await query.answer(
                f"兑换成功！你的卡密：\n{result.code}\n\n请立刻复制保存，这个弹窗关掉就没了。",
                show_alert=True,
            )
            body = (
                f"✅ 兑换成功「{texts.esc(item.name)}」-{item.cost} 分，"
                f"你还有 {result.balance} 分。\n"
                f"<i>卡密已通过弹窗给你了（我没法私信你，先跟我私聊说句话，下次就能收到私信）。</i>"
            )
    else:
        await query.answer("兑换成功，等管理员发货 📦")
        body = texts.redeem_done_physical(result.redemption_id, item, result.balance)
        if record is not None:
            await _notify_owners(context, record, chat.title or "")

    try:
        await query.edit_message_text(
            body, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except TelegramError:
        pass
