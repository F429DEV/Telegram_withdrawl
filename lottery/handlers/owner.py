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
            f"#{g.id}　{texts.prize_label(g, '(无名)')}　"
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
        f"奖品：{texts.prize_label(g)}",
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

# ================================================================ 手动调整积分

ADJUST_USAGE = (
    "🛠 <b>手动调整积分</b>\n\n"
    "<code>/adjust</code> —— 列出机器人见过的群和编号\n"
    "<code>/adjust &lt;群ID&gt; &lt;用户ID或@用户名&gt; &lt;±数量&gt; [备注]</code>\n\n"
    "例：<code>/adjust -1001234567890 123456789 +100 补发上次漏的</code>\n"
    "例：<code>/adjust -1001234567890 @someone -50 误领扣回</code>\n\n"
    "<code>/ledger &lt;群ID&gt; &lt;用户&gt;</code> —— 查这个人的积分流水（含备注）\n\n"
    "<i>调整不会通知对方，备注只记在流水里，只有你能看到。</i>"
)


async def _resolve_member(bot, chat_id: int, raw: str) -> Optional[tuple]:
    """把 @用户名 或数字 ID 解析成 (user_id, 显示名, 用户名)。"""
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        user_id = int(raw)
        found = db.wallet(chat_id, user_id)
        if found:
            return found.user_id, found.full_name, found.username
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            return member.user.id, member.user.full_name, member.user.username
        except TelegramError:
            return user_id, str(user_id), None

    found = db.find_wallet_by_username(chat_id, raw)
    if found:
        return found.user_id, found.full_name, found.username
    # 没有积分记录的人，再去抽奖参与者里翻一下
    participant = db.find_user_by_username(chat_id, raw)
    if participant:
        return participant.user_id, participant.full_name, participant.username
    return None


async def _list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chats = db.known_chats()
    if not chats:
        await update.effective_message.reply_text(
            "机器人还没在任何群里有记录。\n\n" + ADJUST_USAGE, parse_mode=ParseMode.HTML
        )
        return
    lines = ["📋 <b>机器人见过的群</b>", ""]
    for chat_id in chats[:20]:
        title = await _chat_title(context.bot, chat_id)
        pt = db.point_settings(chat_id)
        state = "积分已开" if pt["enabled"] else "积分未开"
        lines.append(f"<code>{chat_id}</code>　{texts.esc(title)}　{state}")
    lines += ["", ADJUST_USAGE]
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def adjust_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    msg = update.effective_message
    args = list(context.args or [])

    if not args:
        await _list_chats(update, context)
        return
    if len(args) < 3:
        await msg.reply_text(ADJUST_USAGE, parse_mode=ParseMode.HTML)
        return

    raw_chat, raw_user, raw_delta = args[0], args[1], args[2]
    note = " ".join(args[3:]).strip()[:200]

    if not raw_chat.lstrip("-").isdigit():
        await msg.reply_text("群 ID 要是数字，超级群是 -100 开头。发 /adjust 看列表。")
        return
    chat_id = int(raw_chat)

    try:
        delta = int(raw_delta.replace("＋", "+").lstrip("+"))
    except ValueError:
        await msg.reply_text("数量要写成 +100 或 -50 这样。")
        return
    if delta == 0:
        await msg.reply_text("加 0 分没有意义。")
        return

    target = await _resolve_member(context.bot, chat_id, raw_user)
    if target is None:
        await msg.reply_text(
            "这个群里找不到这个人。用户名只能查到有积分记录或参与过抽奖的人，"
            "查不到就让他发一句 /viewmyinfo 报出数字 ID，再用 ID 调。"
        )
        return
    user_id, name, username = target

    before = db.point_balance(chat_id, user_id)
    after = db.add_points(
        chat_id, user_id, delta, db.REASON_ADMIN, username, name, note=note or None
    )
    real = after - before
    title = await _chat_title(context.bot, chat_id)

    lines = [
        "✅ <b>已调整</b>",
        "",
        f"群：{texts.esc(title)}",
        f"对象：{texts.esc(name)}" + (f"（@{texts.esc(username)}）" if username else "")
        + f"　<code>{user_id}</code>",
        f"变动：{real:+d} 分（{before} → {after}）",
    ]
    if real != delta:
        lines.append(f"<i>你填的是 {delta:+d}，余额不能扣成负数，实际只扣到 0。</i>")
    if note:
        lines.append(f"备注：{texts.esc(note)}")
    lines += ["", "<i>没有通知对方。备注只在流水里，群成员看不到。</i>"]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    log.info("主人调整积分：群 %s 用户 %s %+d 分（%s）", chat_id, user_id, real, note or "无备注")


async def ledger_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    msg = update.effective_message
    args = list(context.args or [])
    if len(args) < 2 or not args[0].lstrip("-").isdigit():
        await msg.reply_text(
            "用法：<code>/ledger &lt;群ID&gt; &lt;用户ID或@用户名&gt; [条数]</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = int(args[0])
    target = await _resolve_member(context.bot, chat_id, args[1])
    if target is None:
        await msg.reply_text("这个群里找不到这个人。")
        return
    user_id, name, username = target
    limit = int(args[2]) if len(args) > 2 and args[2].isdigit() else 15
    limit = max(1, min(50, limit))

    entries = db.point_ledger(chat_id, user_id, limit)
    title = await _chat_title(context.bot, chat_id)
    lines = [
        f"📒 <b>积分流水</b>　{texts.esc(name)}",
        "",
        f"群：{texts.esc(title)}",
        f"当前余额：<b>{db.point_balance(chat_id, user_id)}</b> 分　<code>{user_id}</code>",
        "",
    ]
    if not entries:
        lines.append("还没有任何积分记录。")
    else:
        for e in entries:
            label = db.REASON_LABEL.get(e.reason, e.reason)
            lines.append(
                f"{texts.fmt_time(e.created_at)}　<b>{e.delta:+d}</b>　"
                f"{label}　余额 {e.balance_after}"
            )
            if e.note:
                lines.append(f"　　└ {texts.esc(e.note)}")
    await msg.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )

