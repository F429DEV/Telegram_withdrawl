"""创建抽奖：/new 命令 + 配置面板的按钮交互 + 文字输入。"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from telegram import ForceReply, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import db, eligibility, keyboards, service, texts

log = logging.getLogger(__name__)

# 面板里可选的玩法（手动名单走 /pick，不进面板）
PANEL_MODES = [db.MODE_BUTTON, db.MODE_KEYWORD, db.MODE_POINTS]

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.I)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 60}


def parse_duration(raw: str) -> Optional[int]:
    """'30m' -> 1800；'0' -> 0（不限时）；无法识别返回 None。"""
    m = _DURATION_RE.match(raw or "")
    if not m:
        return None
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]


def _cycle(values: list[Any], current: Any) -> Any:
    try:
        idx = values.index(current)
    except ValueError:
        idx = 0
    return values[(idx + 1) % len(values)]


def _nearest_duration(remaining: int) -> int:
    return min(keyboards.DURATIONS, key=lambda d: abs(d - remaining))


def _remaining(g: db.Giveaway) -> int:
    return max(0, g.end_at - db.now()) if g.end_at else 0


def _pending(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault("pending", {})


def _drop_pending(context: ContextTypes.DEFAULT_TYPE, giveaway_id: int) -> None:
    """抽奖已发布或已丢弃，把它遗留的「等你回复」提示作废。"""
    pending = _pending(context)
    for message_id in [k for k, v in pending.items() if v.get("giveaway_id") == giveaway_id]:
        pending.pop(message_id, None)


async def _render_draft(context: ContextTypes.DEFAULT_TYPE, g: db.Giveaway) -> None:
    if not g.message_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=g.chat_id,
            message_id=g.message_id,
            text=texts.draft_card(g),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.draft_kb(g, _remaining(g)),
        )
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            log.debug("刷新草稿面板失败: %s", exc)


async def _render_requirements(context: ContextTypes.DEFAULT_TYPE, g: db.Giveaway) -> None:
    if not g.message_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=g.chat_id,
            message_id=g.message_id,
            text=texts.requirement_card(g),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.requirement_kb(g),
        )
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            log.debug("刷新门槛面板失败: %s", exc)


# ---------------------------------------------------------------- /new

async def new_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text(texts.NOT_GROUP)
        return

    cfg = context.application.bot_data["config"]
    if not await eligibility.can_manage(context.bot, chat, user, cfg.owner_ids):
        await msg.reply_text(texts.NOT_ADMIN)
        return

    if len(db.active_in_chat(chat.id)) >= cfg.max_active_per_chat:
        await msg.reply_text(
            f"本群同时进行的抽奖已达上限（{cfg.max_active_per_chat} 个），"
            "先用 /end 或 /cancel 结束几个再来。"
        )
        return

    defaults: dict[str, Any] = {}
    raw = " ".join(context.args) if context.args else ""
    quick = False
    if raw:
        parts = [p.strip() for p in re.split(r"[|｜]", raw)]
        defaults["prize"] = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            defaults["winners_count"] = max(1, min(100, int(parts[1])))
        if len(parts) > 2:
            secs = parse_duration(parts[2])
            if secs:
                defaults["end_at"] = db.now() + secs
        if len(parts) > 3 and parts[3]:
            # 第四段是口令，写了就直接开口令玩法
            words = db.clean_keywords(re.split(r"[\s,，、;；]+", parts[3]))
            if words:
                defaults["mode"] = db.MODE_KEYWORD
                defaults["keyword"] = words
        quick = bool(defaults.get("prize"))

    settings = db.chat_settings(chat.id)
    if settings["default_channels"]:
        defaults.setdefault("require_channels", settings["default_channels"])

    gid = db.create_draft(chat.id, user.id, defaults)
    g = db.get(gid)

    if quick:
        await service.publish(context.bot, context.application, g)
        try:
            await msg.delete()
        except TelegramError:
            pass
        return

    sent = await msg.reply_text(
        texts.draft_card(g),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.draft_kb(g, _remaining(g)),
    )
    db.update(gid, message_id=sent.message_id)


# ---------------------------------------------------------------- 面板按钮

async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, gid_raw, action = query.data.split(":", 2)
    g = db.get(int(gid_raw))
    cfg = context.application.bot_data["config"]

    if g is None or g.status != db.STATUS_DRAFT:
        await query.answer("这个面板已经失效了，重新发 /new 吧。", show_alert=True)
        return
    if query.from_user.id != g.creator_id and query.from_user.id not in cfg.owner_ids:
        await query.answer("这是别人的配置面板，你可以自己发 /new。", show_alert=True)
        return

    if action == "mode":
        db.update(g.id, mode=_cycle(PANEL_MODES, g.mode))
    elif action == "win+":
        db.update(g.id, winners_count=min(100, g.winners_count + 1))
    elif action == "win-":
        db.update(g.id, winners_count=max(1, g.winners_count - 1))
    elif action == "win?":
        await _ask(context, query, g, "winners_count", texts.WINNERS_PROMPT)
        return
    elif action == "dur":
        nxt = _cycle(keyboards.DURATIONS, _nearest_duration(_remaining(g)))
        db.update(g.id, end_at=(db.now() + nxt) if nxt else None)
    elif action == "cap":
        nxt = _cycle(keyboards.CAPS, g.max_participants or 0)
        db.update(g.id, max_participants=nxt or None)
    elif action == "prize":
        await _ask(context, query, g, "prize", texts.PRIZE_PROMPT)
        return
    elif action == "keyword":
        await _ask(context, query, g, "keyword", texts.KEYWORD_PROMPT)
        return
    elif action == "req":
        await query.answer()
        await _render_requirements(context, db.get(g.id))
        return
    elif action == "discard":
        db.update(g.id, status=db.STATUS_CANCELLED)
        _drop_pending(context, g.id)
        await query.answer("已取消")
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return
    elif action == "publish":
        await _publish(update, context, g)
        return
    else:
        await query.answer()
        return

    await query.answer()
    await _render_draft(context, db.get(g.id))


async def requirement_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, gid_raw, action = query.data.split(":", 2)
    g = db.get(int(gid_raw))
    cfg = context.application.bot_data["config"]

    if g is None or g.status != db.STATUS_DRAFT:
        await query.answer("这个面板已经失效了。", show_alert=True)
        return
    if query.from_user.id != g.creator_id and query.from_user.id not in cfg.owner_ids:
        await query.answer("这是别人的配置面板。", show_alert=True)
        return

    if action == "username":
        db.update(g.id, require_username=not g.require_username)
    elif action == "seen":
        db.update(g.id, min_seen_hours=_cycle(keyboards.SEEN_HOURS, g.min_seen_hours))
    elif action == "channels":
        await _ask(context, query, g, "require_channels", texts.CHANNEL_PROMPT)
        return
    elif action == "back":
        await query.answer()
        await _render_draft(context, db.get(g.id))
        return

    await query.answer()
    await _render_requirements(context, db.get(g.id))


async def _publish(update: Update, context: ContextTypes.DEFAULT_TYPE, g: db.Giveaway) -> None:
    query = update.callback_query
    if not g.prize.strip():
        await query.answer("先点「✏️ 奖品」写上抽什么。", show_alert=True)
        return
    if g.mode == db.MODE_KEYWORD and not g.keywords:
        await query.answer("口令玩法要先点「🔑 口令」设置口令。", show_alert=True)
        return
    await query.answer("已发布 🚀")
    _drop_pending(context, g.id)
    await service.publish(context.bot, context.application, g, edit_message_id=g.message_id)


# ---------------------------------------------------------------- 文字输入

async def _ask(
    context: ContextTypes.DEFAULT_TYPE, query, g: db.Giveaway, field: str, prompt: str
) -> None:
    await query.answer()
    # 文本里带上 @提及，ForceReply 的 selective 才能只弹给发起人
    mention = texts.user_link(query.from_user.id, query.from_user.full_name)
    sent = await context.bot.send_message(
        chat_id=g.chat_id,
        text=f"{mention} {prompt}",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder="在这里输入"),
        reply_to_message_id=g.message_id,
        allow_sending_without_reply=True,
    )
    _pending(context)[sent.message_id] = {
        "user_id": query.from_user.id,
        "giveaway_id": g.id,
        "field": field,
        "panel": "req" if field == "require_channels" else "draft",
    }


async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理对 ForceReply 提示的回复。"""
    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        return
    pending = _pending(context)
    info = pending.get(msg.reply_to_message.message_id)
    if not info or info["user_id"] != update.effective_user.id:
        return
    pending.pop(msg.reply_to_message.message_id, None)

    g = db.get(info["giveaway_id"])
    if g is None or g.status != db.STATUS_DRAFT:
        # 抽奖已经发布 / 取消了，这条提示是过期的：清掉就行，绝不能再去编辑那条卡片
        for m in (msg.reply_to_message, msg):
            try:
                await m.delete()
            except TelegramError:
                pass
        return
    value = (msg.text or "").strip()
    field = info["field"]

    if field == "prize":
        db.update(g.id, prize=value[:200])
    elif field == "keyword":
        # 空格、逗号、顿号、分号都能用来分隔多个口令
        words = db.clean_keywords(re.split(r"[\s,，、;；]+", value)) if value else []
        db.update(g.id, keyword=words or None)
    elif field == "winners_count":
        if value.isdigit():
            db.update(g.id, winners_count=max(1, min(100, int(value))))
    elif field == "require_channels":
        if value in ("清空", "无", "-"):
            db.update(g.id, require_channels=[])
        else:
            channels = [c if c.startswith(("@", "-")) else "@" + c for c in value.split()][:5]
            db.update(g.id, require_channels=channels)

    for m in (msg.reply_to_message, msg):
        try:
            await m.delete()
        except TelegramError:
            pass

    g = db.get(g.id)
    if info["panel"] == "req":
        await _render_requirements(context, g)
    else:
        await _render_draft(context, g)
