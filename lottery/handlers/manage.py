"""管理命令：/list /end /cancel /reroll /verify /settings /pick。"""

from __future__ import annotations

import re
from typing import Optional

from telegram import ForceReply, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import db, eligibility, engine, service, texts


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text(texts.NOT_GROUP)
        return False
    cfg = context.application.bot_data["config"]
    if not await eligibility.can_manage(context.bot, chat, user, cfg.owner_ids):
        await msg.reply_text(texts.NOT_ADMIN)
        return False
    return True


def _resolve(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> tuple[Optional[int], str]:
    """从命令参数里取抽奖编号；只有一个进行中的抽奖时可以省略。"""
    if context.args:
        raw = context.args[0].lstrip("#")
        if raw.isdigit():
            return int(raw), ""
        return None, "编号要是数字，比如 /end 12"
    active = db.active_in_chat(chat_id)
    if len(active) == 1:
        return active[0].id, ""
    if not active:
        return None, "本群没有进行中的抽奖。"
    return None, "本群有多个进行中的抽奖，请带上编号，例如 /end 12（用 /list 查编号）。"


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, msg = update.effective_chat, update.effective_message
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text(texts.NOT_GROUP)
        return
    active = db.active_in_chat(chat.id)
    if not active:
        recent = db.recent_in_chat(chat.id, 5)
        if not recent:
            await msg.reply_text("本群还没开过抽奖。管理员发 /new 开一个吧。")
            return
        lines = ["本群没有进行中的抽奖。最近结束的：", ""]
        for g in recent:
            state = "已开奖" if g.status == db.STATUS_ENDED else "已取消"
            lines.append(f"#{g.id} {texts.esc(g.prize)} — {state}")
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return
    lines = ["<b>进行中的抽奖</b>", ""]
    for g in active:
        bits = [f"#{g.id}", texts.esc(g.prize) or "(无名)", f"{texts.MODE_SHORT[g.mode]}玩法",
                f"{db.participant_count(g.id)} 人参与", f"{g.winners_count} 个名额"]
        if g.end_at:
            bits.append(f"{texts.fmt_time(g.end_at)} 开奖")
        lines.append("• " + " ｜ ".join(bits))
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def end_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    gid, err = _resolve(context, update.effective_chat.id)
    if gid is None:
        await update.effective_message.reply_text(err)
        return
    g = db.get(gid)
    if g is None or g.chat_id != update.effective_chat.id:
        await update.effective_message.reply_text("没找到这个编号的抽奖。")
        return
    service.unschedule(context.application, gid)
    if not await service.finish(context.bot, gid, reason="🎬 由管理员手动开奖"):
        await update.effective_message.reply_text("这个抽奖不在进行中。")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    gid, err = _resolve(context, update.effective_chat.id)
    if gid is None:
        await update.effective_message.reply_text(err)
        return
    g = db.get(gid)
    if g is None or g.chat_id != update.effective_chat.id:
        await update.effective_message.reply_text("没找到这个编号的抽奖。")
        return
    service.unschedule(context.application, gid)
    if not await service.cancel(context.bot, gid):
        await update.effective_message.reply_text("这个抽奖不在进行中。")


async def reroll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    msg = update.effective_message
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await msg.reply_text("用法：/reroll 12（编号可以在开奖消息里看到）")
        return
    gid = int(context.args[0].lstrip("#"))
    g = db.get(gid)
    if g is None or g.chat_id != update.effective_chat.id:
        await msg.reply_text("没找到这个编号的抽奖。")
        return
    if not await service.reroll(context.bot, gid):
        await msg.reply_text("只能对已经开过奖的抽奖重抽。")


async def verify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await msg.reply_text("用法：/verify 12")
        return
    gid = int(context.args[0].lstrip("#"))
    g = db.get(gid)
    if g is None or g.chat_id != update.effective_chat.id:
        await msg.reply_text("没找到这个编号的抽奖。")
        return
    if g.status != db.STATUS_ENDED or not g.seed:
        await msg.reply_text("这个抽奖还没开奖，暂时没有可复核的种子。")
        return

    people = db.participants(g.id)
    weighted = g.mode == db.MODE_POINTS
    ranked = sorted(
        people,
        key=lambda p: (-engine.score(g.seed, g.id, p.user_id, p.weight if weighted else 1), p.user_id),
    )
    lines = [
        f"🔍 <b>复核抽奖 #{g.id}</b>",
        "",
        f"奖品：{texts.esc(g.prize)}",
        f"参与人数：{len(people)}　名额：{g.winners_count}",
        f"随机种子：<code>{texts.esc(g.seed)}</code>",
        "",
        "算法：<code>score = (sha256(种子:抽奖号:用户id) 归一化) ** (1/权重)</code>，"
        "按 score 从大到小取前 N 名。种子在开奖那一刻才生成并公示，"
        "拿同样的名单和种子谁都能算出同样结果。",
        "",
        "<b>排名前 20：</b>",
    ]
    for i, p in enumerate(ranked[:20], 1):
        s = engine.score(g.seed, g.id, p.user_id, p.weight if weighted else 1)
        mark = "🏆" if i <= g.winners_count else "　"
        w = f" ×{p.weight}" if weighted else ""
        lines.append(f"{mark}{i}. {texts.esc(p.full_name)}{w} — {s:.6f}")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                         disable_web_page_preview=True)


# ---------------------------------------------------------------- 手动名单

_SPLIT = re.compile(r"[\n,，、;；]+")


async def pick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    msg = update.effective_message
    args = list(context.args or [])
    count = 1
    if args and args[0].isdigit():
        count = max(1, min(100, int(args[0])))
        args = args[1:]

    source = ""
    if msg.reply_to_message:
        source = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    if not source and args:
        source = " ".join(args)

    names = [n.strip() for n in _SPLIT.split(source) if n.strip()]
    if len(names) < 1:
        await msg.reply_text(
            "用法：把名单发在群里，然后<b>回复那条消息</b>发 <code>/pick 3</code>。\n"
            "名单每行一个，或者用逗号 / 顿号分隔。",
            parse_mode=ParseMode.HTML,
        )
        return

    wins, seed = engine.draw_names(names, count)
    await msg.reply_text(
        texts.manual_result("名单抽奖", wins, len(names), seed),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------- 群设置

def _settings_kb(settings: dict) -> Markup:
    return Markup(
        [
            [Btn(f"发起权限：{'仅管理员' if settings['admin_only'] else '所有人'}",
                 callback_data="s:0:who")],
            [Btn("默认必须加入的频道/群", callback_data="s:0:channels")],
            [Btn("关闭", callback_data="s:0:close")],
        ]
    )


def _settings_text(settings: dict) -> str:
    channels = "、".join(texts.esc(c) for c in settings["default_channels"]) or "无"
    return (
        "⚙️ <b>本群抽奖设置</b>\n\n"
        f"谁能发起 / 管理抽奖：{'仅管理员' if settings['admin_only'] else '所有人'}\n"
        f"新抽奖默认要求加入：{channels}\n\n"
        "<i>默认频道只影响之后新建的抽奖，已发布的不受影响。</i>"
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings = db.chat_settings(update.effective_chat.id)
    await update.effective_message.reply_text(
        _settings_text(settings), parse_mode=ParseMode.HTML, reply_markup=_settings_kb(settings)
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    cfg = context.application.bot_data["config"]
    if not await eligibility.is_admin(context.bot, chat.id, query.from_user.id, cfg.owner_ids):
        await query.answer(texts.NOT_ADMIN, show_alert=True)
        return
    action = query.data.split(":", 2)[2]
    settings = db.chat_settings(chat.id)

    if action == "who":
        db.save_chat_settings(chat.id, admin_only=not settings["admin_only"])
    elif action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return
    elif action == "channels":
        await query.answer()
        mention = texts.user_link(query.from_user.id, query.from_user.full_name)
        sent = await context.bot.send_message(
            chat_id=chat.id,
            text=f"{mention} {texts.CHANNEL_PROMPT}",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True),
            reply_to_message_id=query.message.message_id,
            allow_sending_without_reply=True,
        )
        context.application.bot_data.setdefault("pending_settings", {})[sent.message_id] = {
            "user_id": query.from_user.id,
            "chat_id": chat.id,
            "panel_message_id": query.message.message_id,
        }
        return

    await query.answer()
    settings = db.chat_settings(chat.id)
    try:
        await query.edit_message_text(
            _settings_text(settings), parse_mode=ParseMode.HTML, reply_markup=_settings_kb(settings)
        )
    except TelegramError:
        pass


async def handle_settings_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        return
    pending = context.application.bot_data.setdefault("pending_settings", {})
    info = pending.get(msg.reply_to_message.message_id)
    if not info or info["user_id"] != update.effective_user.id:
        return
    pending.pop(msg.reply_to_message.message_id, None)

    value = (msg.text or "").strip()
    if value in ("清空", "无", "-"):
        channels: list[str] = []
    else:
        channels = [c if c.startswith(("@", "-")) else "@" + c for c in value.split()][:5]
    db.save_chat_settings(info["chat_id"], default_channels=channels)

    for m in (msg.reply_to_message, msg):
        try:
            await m.delete()
        except TelegramError:
            pass

    settings = db.chat_settings(info["chat_id"])
    try:
        await context.bot.edit_message_text(
            chat_id=info["chat_id"],
            message_id=info["panel_message_id"],
            text=_settings_text(settings),
            parse_mode=ParseMode.HTML,
            reply_markup=_settings_kb(settings),
        )
    except TelegramError:
        pass
