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
        await msg.reply_text("这个抽奖还没开奖，等开完再看排名。")
        return

    if db.reservation_count(g.id):
        # 这场的中奖名单不是纯随机产生的，硬给一份随机排名等于编数据
        await msg.reply_text(
            f"🔍 <b>抽奖 #{g.id}</b>\n\n"
            f"奖品：{texts.esc(g.prize)}\n"
            f"参与人数：{db.participant_count(g.id)}　名额：{g.winners_count}\n\n"
            "本场未生成排名，中奖名单见开奖消息。",
            parse_mode=ParseMode.HTML,
        )
        return

    people, weighted = service.weighted_participants(g)
    ranked = sorted(
        people,
        key=lambda p: (-engine.score(g.seed, g.id, p.user_id, p.weight if weighted else 1), p.user_id),
    )
    lines = [
        f"🔍 <b>抽奖 #{g.id} 排名</b>",
        "",
        f"奖品：{texts.esc(g.prize)}",
        f"参与人数：{len(people)}　名额：{g.winners_count}",
        "",
        f"<b>前 {min(20, len(ranked))} 名：</b>",
    ]
    for i, person in enumerate(ranked[:20], 1):
        mark = "🏆" if i <= g.winners_count else "　"
        w = f"（权重 {person.weight}）" if weighted else ""
        lines.append(f"{mark}第 {i} 名　{texts.esc(person.full_name)}{w}")
    if len(ranked) > 20:
        lines.append(f"<i>……还有 {len(ranked) - 20} 人未显示</i>")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                         disable_web_page_preview=True)


# ---------------------------------------------------------------- /view

_STATUS_LABEL = {
    db.STATUS_ACTIVE: "进行中",
    db.STATUS_ENDED: "已开奖",
    db.STATUS_CANCELLED: "已取消",
    db.STATUS_DRAFT: "草稿（还没发布）",
}


def _mode_detail(g: db.Giveaway) -> str:
    if g.mode == db.MODE_KEYWORD:
        if not g.keywords:
            return "口令（还没设置口令）"
        words = "　".join(f"<code>{texts.esc(k)}</code>" for k in g.keywords)
        return f"口令（{len(g.keywords)} 个）：{words}"
    if g.mode == db.MODE_POINTS:
        return "发言积分（发言越多权重越高，不设上限）"
    if g.mode == db.MODE_BUTTON:
        return "点按钮报名"
    return texts.MODE_LABEL.get(g.mode, g.mode)


def _draw_condition(g: db.Giveaway) -> list[str]:
    out = []
    if g.end_at:
        out.append(f"到 {texts.fmt_time(g.end_at)} 自动开奖")
    if g.max_participants:
        out.append(f"满 {g.max_participants} 人立即开奖")
    if not out:
        out.append("由管理员手动开奖")
    return out


async def view_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, msg = update.effective_chat, update.effective_message
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text(texts.NOT_GROUP)
        return

    gid, err = _resolve(context, chat.id)
    if gid is None:
        await msg.reply_text(err + "\n用法：/view 12")
        return
    g = db.get(gid)
    if g is None or g.chat_id != chat.id or g.status == db.STATUS_DRAFT:
        await msg.reply_text("没找到这个编号的抽奖。用 /list 看本群的编号。")
        return

    people, weighted = service.weighted_participants(g)
    lines = [
        f"📋 <b>抽奖 #{g.id} 详情</b>",
        "",
        f"状态：{_STATUS_LABEL.get(g.status, g.status)}",
        f"奖品：{texts.esc(g.prize) or '（未填写）'}",
        f"玩法：{_mode_detail(g)}",
        f"名额：{g.winners_count} 名",
        "开奖条件：" + "；".join(_draw_condition(g)),
    ]
    if g.invite_weight:
        lines.append(f"邀请加成：每邀请 1 人 +{g.invite_weight} 权重（只算本场开始后拉进来的）")
    else:
        lines.append("邀请加成：关")

    reqs = []
    if g.require_channels:
        reqs.append("需先加入 " + "、".join(texts.esc(c) for c in g.require_channels))
    if g.require_username:
        reqs.append("需有用户名")
    if g.min_seen_hours:
        reqs.append(f"需在本群出现满 {texts.fmt_duration(g.min_seen_hours * 3600)}")
    lines.append("参与门槛：" + ("；".join(reqs) if reqs else "无"))

    lines += [
        "",
        f"参与人数：<b>{len(people)}</b>",
        f"创建时间：{texts.fmt_time(g.created_at)}",
    ]
    if g.ended_at:
        lines.append(f"结束时间：{texts.fmt_time(g.ended_at)}")

    if weighted and people and not db.reservation_count(g.id):
        top = sorted(people, key=lambda p: (-p.weight, p.user_id))[:5]
        total = sum(p.weight for p in people) or 1
        lines.append("")
        lines.append("<b>权重前 5：</b>")
        for i, p in enumerate(top, 1):
            share = p.weight / total * 100
            lines.append(f"{i}. {texts.esc(p.full_name)} — 权重 {p.weight}（约 {share:.1f}%）")

    if g.status == db.STATUS_ENDED:
        wins = db.winners(g.id)
        lines.append("")
        if wins:
            lines.append("<b>中奖者：</b>")
            for i, w in enumerate(wins, 1):
                lines.append(f"{i}. {texts.user_link(w.user_id, w.full_name, w.username)}")
        else:
            lines.append("<b>中奖者：</b>无（参与人数不足）")
        lines.append(f"<i>用 /verify {g.id} 看完整排名</i>")

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

    wins, _seed = engine.draw_names(names, count)
    await msg.reply_text(
        texts.manual_result("名单抽奖", wins, len(names)),
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
