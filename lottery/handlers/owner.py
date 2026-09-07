"""预留名额：只有机器人主人（.env 里的 OWNER_IDS）能用，且只能私聊用。

群里不显示这两个命令，/help 不列，命令菜单不注册。非主人发同样的命令，
机器人不作任何回应 —— 对群成员来说这个接口等于不存在。

开奖时预留的人先占名额，剩下的名额照常随机抽。
带预留的场次不再输出随机排名（/verify 和 /view 都会跳过），
因为真实排名和公布的中奖名单对不上，硬凑一个排名出来就是编数据。
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import db, texts

log = logging.getLogger(__name__)

USAGE = (
    "📌 <b>预留名额</b>\n\n"
    "<code>/reserve</code> —— 列出所有进行中的抽奖和编号\n"
    "<code>/reserve 12</code> —— 看这场预留了谁\n"
    "<code>/reserve 12 @用户名</code> —— 预留一个名额给他\n"
    "<code>/reserve 12 123456789</code> —— 用数字 ID 也行\n"
    "<code>/unreserve 12 @用户名</code> —— 取消预留\n\n"
    "<i>预留的人直接占名额，剩下的名额到点随机抽。群里的开奖消息和平时一样。</i>"
)


def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """非主人、或不在私聊里，一律当作没这个命令。"""
    if update.effective_chat.type != ChatType.PRIVATE:
        return False
    cfg = context.application.bot_data["config"]
    return update.effective_user.id in cfg.owner_ids


async def _resolve_target(bot, g: db.Giveaway, raw: str) -> Optional[db.Participant]:
    """把 @用户名 或数字 ID 解析成一个人。"""
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        user_id = int(raw)
        found = db.find_participant(g.id, user_id)
        if found:
            return found
        # 没参与过这场，去群里问一下他叫什么
        try:
            member = await bot.get_chat_member(g.chat_id, user_id)
            u = member.user
            return db.Participant(u.id, u.username, u.full_name, 1, 0)
        except TelegramError:
            return db.Participant(user_id, None, str(user_id), 1, 0)
    return db.find_user_by_username(g.chat_id, raw)


async def _chat_title(bot, chat_id: int) -> str:
    try:
        chat = await bot.get_chat(chat_id)
        return chat.title or str(chat_id)
    except TelegramError:
        return str(chat_id)


async def _list_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active = db.all_active()
    if not active:
        await update.effective_message.reply_text(
            "现在没有进行中的抽奖。\n\n" + USAGE, parse_mode=ParseMode.HTML
        )
        return
    lines = ["📋 <b>进行中的抽奖</b>", ""]
    for g in active[:20]:
        title = await _chat_title(context.bot, g.chat_id)
        held = db.reservation_count(g.id)
        mark = f"　已预留 {held}" if held else ""
        lines.append(
            f"#{g.id}　{texts.esc(g.prize) or '(无名)'}　"
            f"[{texts.esc(title)}]　{g.winners_count} 个名额{mark}"
        )
    lines += ["", USAGE]
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def _show(update: Update, g: db.Giveaway) -> None:
    held = db.reservations(g.id)
    lines = [
        f"📌 <b>抽奖 #{g.id} 的预留名额</b>",
        "",
        f"奖品：{texts.esc(g.prize)}",
        f"名额：{g.winners_count}　已预留：{len(held)}",
    ]
    if held:
        lines.append("")
        for i, person in enumerate(held[: g.winners_count], 1):
            at = f"（@{texts.esc(person.username)}）" if person.username else ""
            lines.append(f"{i}. {texts.esc(person.full_name)}{at} — <code>{person.user_id}</code>")
        extra = held[g.winners_count:]
        for person in extra:
            lines.append(f"⚠️ {texts.esc(person.full_name)} — 超出名额，不会生效")
        left = max(0, g.winners_count - len(held))
        lines.append("")
        lines.append(f"剩余 {left} 个名额到点随机抽。" if left else "名额已被预留占满，不会再随机抽。")
    else:
        lines.append("")
        lines.append("还没有预留，这场完全随机。")
    lines += ["", USAGE]
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def reserve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    args = list(context.args or [])
    if not args:
        await _list_active(update, context)
        return

    raw_id = args[0].lstrip("#")
    if not raw_id.isdigit():
        await update.effective_message.reply_text(USAGE, parse_mode=ParseMode.HTML)
        return
    g = db.get(int(raw_id))
    if g is None or g.status == db.STATUS_DRAFT:
        await update.effective_message.reply_text("没找到这个编号的抽奖。发 /reserve 看列表。")
        return

    if len(args) == 1:
        await _show(update, g)
        return

    if g.status != db.STATUS_ACTIVE:
        await update.effective_message.reply_text(
            "这场已经结束了，改不了预留。想换人可以在群里 /reroll 重抽。"
        )
        return

    target = await _resolve_target(context.bot, g, args[1])
    if target is None:
        await update.effective_message.reply_text(
            "没找到这个人。用户名只能查到在那个群参与过抽奖的人；"
            "查不到就让他发一句 /viewmyinfo 报出数字 ID，再用 ID 预留。"
        )
        return

    if db.add_reservation(g.id, target.user_id, target.username, target.full_name):
        log.info("抽奖 #%s 预留名额给 %s", g.id, target.user_id)
    await _show(update, db.get(g.id))


async def unreserve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    args = list(context.args or [])
    if len(args) < 2 or not args[0].lstrip("#").isdigit():
        await update.effective_message.reply_text(
            "用法：<code>/unreserve 12 @用户名</code>", parse_mode=ParseMode.HTML
        )
        return
    g = db.get(int(args[0].lstrip("#")))
    if g is None:
        await update.effective_message.reply_text("没找到这个编号的抽奖。")
        return
    target = await _resolve_target(context.bot, g, args[1])
    if target is None or not db.remove_reservation(g.id, target.user_id):
        await update.effective_message.reply_text("这个人本来就没在预留名单里。")
        return
    await _show(update, db.get(g.id))
