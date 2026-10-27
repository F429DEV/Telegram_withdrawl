"""/settings —— 群设置主菜单，分三个板块：抽奖、积分、商城。

一个入口分层进去，而不是 /settings /points /shop 三个命令：
管理员只要记一个词，板块以后再加也不用改用法。

文字输入沿用 ForceReply 那套：弹一条「回复这条消息」的提示，
把 提示消息id -> 要填哪个字段 记在 bot_data 里，收到回复后对号入座。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from telegram import ForceReply, InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import db, eligibility, shop, texts

log = logging.getLogger(__name__)

# 各项设置的档位，点一下换下一档
PER_MESSAGE = [0, 1, 2, 3, 5, 10]
DAILY_CAP = [0, 10, 20, 50, 100, 200, 500]
COOLDOWN = [0, 15, 30, 60, 120, 300, 600]
CHECKIN = [0, 5, 10, 20, 50, 100]
MIN_LEN = [1, 2, 4, 6, 10]

SPLIT = re.compile(r"[|｜]")


def _cycle(values: list, current) -> Any:
    try:
        idx = values.index(current)
    except ValueError:
        idx = -1
    return values[(idx + 1) % len(values)]


def _pending(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault("pending_settings", {})


async def _ask(
    context: ContextTypes.DEFAULT_TYPE, query, field: str, prompt: str
) -> None:
    """弹一条 ForceReply 提示，等管理员回复。"""
    await query.answer()
    mention = texts.user_link(query.from_user.id, query.from_user.full_name)
    sent = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"{mention} {prompt}",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True),
        reply_to_message_id=query.message.message_id,
        allow_sending_without_reply=True,
    )
    _pending(context)[sent.message_id] = {
        "user_id": query.from_user.id,
        "chat_id": query.message.chat_id,
        "panel_message_id": query.message.message_id,
        "field": field,
    }


# ================================================================ 主菜单

def _main_text(chat_id: int) -> str:
    lot = db.chat_settings(chat_id)
    pt = db.point_settings(chat_id)
    items = len(db.shop_list(chat_id, only_enabled=False))
    pending = db.pending_count(chat_id)
    return "\n".join(
        [
            "⚙️ <b>群设置</b>",
            "",
            f"🎁 抽奖　发起权限：{'仅管理员' if lot['admin_only'] else '所有人'}",
            f"🏅 积分　{'已开启' if pt['enabled'] else '已关闭'}"
            + (f"，每条发言 {pt['per_message']} 分，签到 {pt['checkin_points']} 分"
               if pt["enabled"] else ""),
            f"🛒 商城　{items} 件奖品"
            + (f"，<b>{pending} 张待发货</b>" if pending else ""),
        ]
    )


def _main_kb(chat_id: int) -> Markup:
    pending = db.pending_count(chat_id)
    shop_label = "🛒 商城管理" + (f"（{pending} 张待发货）" if pending else "")
    return Markup(
        [
            [Btn("🎁 抽奖设置", callback_data="st:main:lot")],
            [Btn("🏅 积分设置", callback_data="st:main:pt")],
            [Btn(shop_label, callback_data="st:main:shop")],
            [Btn("关闭", callback_data="st:main:close")],
        ]
    )


# ================================================================ 抽奖

def _lot_text(chat_id: int) -> str:
    s = db.chat_settings(chat_id)
    channels = "、".join(texts.esc(c) for c in s["default_channels"]) or "无"
    return "\n".join(
        [
            "🎁 <b>抽奖设置</b>",
            "",
            f"谁能发起 / 管理抽奖：{'仅管理员' if s['admin_only'] else '所有人'}",
            f"新抽奖默认要求加入：{channels}",
            "",
            "<i>默认频道只影响之后新建的抽奖，已发布的不受影响。</i>",
        ]
    )


def _lot_kb(chat_id: int) -> Markup:
    s = db.chat_settings(chat_id)
    return Markup(
        [
            [Btn(f"发起权限：{'仅管理员' if s['admin_only'] else '所有人'}",
                 callback_data="st:lot:who")],
            [Btn("📢 默认必须加入的频道/群", callback_data="st:lot:channels")],
            [Btn("⬅️ 返回", callback_data="st:lot:back")],
        ]
    )


# ================================================================ 积分

def _secs(value: int) -> str:
    if value <= 0:
        return "不限"
    return f"{value} 秒" if value < 60 else texts.fmt_duration(value)


def _pt_text(chat_id: int) -> str:
    s = db.point_settings(chat_id)
    if not s["enabled"]:
        return (
            "🏅 <b>积分设置</b>\n\n当前<b>已关闭</b>。\n"
            "开启后群成员发言和签到就能攒分，可以在商城里兑奖品。"
        )
    return "\n".join(
        [
            "🏅 <b>积分设置</b>",
            "",
            f"每条有效发言：<b>{s['per_message']}</b> 分" if s["per_message"] > 0
            else "每条有效发言：不给分",
            f"每日发言上限：{s['daily_cap'] or '不限'} 分",
            f"发言冷却：{_secs(s['cooldown_seconds'])}",
            f"每日签到：{s['checkin_points'] or '不给分'} 分",
            f"最短计分长度：{s['min_message_len']} 字",
            "",
            "<i>冷却、每日上限、最短长度这三项是防刷的 —— 都设成不限的话，"
            "刷屏攒分会很划算。</i>",
            "<i>纯表情、纯标点、和上一条重复的消息一律不给分，这个写死了。</i>",
        ]
    )


def _pt_kb(chat_id: int) -> Markup:
    s = db.point_settings(chat_id)
    if not s["enabled"]:
        return Markup(
            [
                [Btn("开启积分功能", callback_data="st:pt:toggle")],
                [Btn("⬅️ 返回", callback_data="st:pt:back")],
            ]
        )
    return Markup(
        [
            [Btn(f"💬 每条发言：{s['per_message']} 分", callback_data="st:pt:per"),
             Btn(f"📅 签到：{s['checkin_points']} 分", callback_data="st:pt:checkin")],
            [Btn(f"🧢 每日上限：{s['daily_cap'] or '不限'}", callback_data="st:pt:cap"),
             Btn(f"⏱ 冷却：{_secs(s['cooldown_seconds'])}", callback_data="st:pt:cd")],
            [Btn(f"✂️ 最短 {s['min_message_len']} 字", callback_data="st:pt:minlen"),
             Btn("✏️ 自定义数值", callback_data="st:pt:custom")],
            [Btn("关闭积分功能", callback_data="st:pt:toggle")],
            [Btn("⬅️ 返回", callback_data="st:pt:back")],
        ]
    )


PT_CUSTOM_PROMPT = (
    "请<b>回复这条消息</b>，用「键=值」写要改的项，空格分开，只写要改的：\n"
    "<code>每条=2 签到=8 上限=60 冷却=30 最短=3</code>\n"
    "<i>上限 / 冷却 写 0 表示不限。</i>"
)

PT_CUSTOM_KEYS = {
    "每条": "per_message",
    "签到": "checkin_points",
    "上限": "daily_cap",
    "冷却": "cooldown_seconds",
    "最短": "min_message_len",
}


# ================================================================ 商城

def _shop_text(chat_id: int) -> str:
    items = db.shop_list(chat_id, only_enabled=False)
    pending = db.pending_count(chat_id)
    lines = ["🛒 <b>商城管理</b>", ""]
    if not items:
        lines.append("货架还是空的，点「➕ 上架奖品」加第一件。")
    else:
        for item in items:
            stock = shop.available_stock(item)
            bits = [f"{item.cost} 分",
                    "卡密" if item.is_code else "实物",
                    "不限量" if stock is None else f"剩 {stock}"]
            if item.per_user_limit:
                bits.append(f"限 {item.per_user_limit} 次/人")
            if not item.enabled:
                bits.append("已下架")
            lines.append(f"• <b>{texts.esc(item.name)}</b> — " + " · ".join(bits))
    if pending:
        lines += ["", f"📦 有 <b>{pending}</b> 张兑换单等着发货。"]
    return "\n".join(lines)


def _shop_kb(chat_id: int) -> Markup:
    rows = [[Btn("➕ 上架奖品", callback_data="st:shop:add")]]
    for item in db.shop_list(chat_id, only_enabled=False):
        mark = "" if item.enabled else "（已下架）"
        rows.append([Btn(f"⚙️ {item.name}{mark}"[:40], callback_data=f"st:item:{item.id}")])
    pending = db.pending_count(chat_id)
    if pending:
        rows.append([Btn(f"📦 待发货（{pending}）", callback_data="st:shop:pending")])
    rows.append([Btn("⬅️ 返回", callback_data="st:shop:back")])
    return Markup(rows)


SHOP_ADD_PROMPT = (
    "请<b>回复这条消息</b>，一行写完奖品，用 | 分隔：\n"
    "<code>名称 | 积分 | 实物或卡密 | 库存</code>\n\n"
    "例：<code>会员月卡 | 100 | 卡密 | </code>（卡密的库存由导入的码数决定，留空）\n"
    "例：<code>定制马克杯 | 300 | 实物 | 5</code>\n"
    "例：<code>群内表扬 | 10 | 实物 | 不限</code>"
)


def _item_text(item: db.ShopItem) -> str:
    stock = shop.available_stock(item)
    lines = [
        f"⚙️ <b>{texts.esc(item.name)}</b>",
        "",
        f"消耗积分：{item.cost}",
        f"类型：{'卡密（兑换后自动私信）' if item.is_code else '实物（生成兑换单，人工发货）'}",
        f"库存：{'不限量' if stock is None else stock}",
        f"每人限兑：{item.per_user_limit or '不限'}",
        f"状态：{'在售' if item.enabled else '已下架'}",
    ]
    if item.note:
        lines.append(f"说明：{texts.esc(item.note)}")
    if item.is_code:
        lines += ["", f"<i>卡密库里还有 {db.shop_free_codes(item.id)} 个没用过的码。</i>"]
    return "\n".join(lines)


def _item_kb(item: db.ShopItem) -> Markup:
    rows = [[Btn("✏️ 改名称 / 价格 / 库存 / 限兑", callback_data=f"st:it:{item.id}:edit")]]
    if item.is_code:
        rows.append([Btn("🎟 导入卡密", callback_data=f"st:it:{item.id}:codes")])
    rows.append(
        [
            Btn("下架" if item.enabled else "上架", callback_data=f"st:it:{item.id}:toggle"),
            Btn("🗑 删除", callback_data=f"st:it:{item.id}:del"),
        ]
    )
    rows.append([Btn("⬅️ 返回", callback_data=f"st:it:{item.id}:back")])
    return Markup(rows)


def _item_edit_prompt(item: db.ShopItem) -> str:
    stock = "不限" if item.stock is None else item.stock
    limit = "不限" if item.per_user_limit is None else item.per_user_limit
    return (
        "请<b>回复这条消息</b>，按原格式写新值，用 | 分隔：\n"
        "<code>名称 | 积分 | 库存 | 每人限兑</code>\n\n"
        f"当前：<code>{texts.esc(item.name)} | {item.cost} | {stock} | {limit}</code>\n"
        "<i>库存和限兑写「不限」就是不限制。卡密类的库存由码数决定，这里写什么都不生效。</i>"
    )


def _pending_text(chat_id: int) -> str:
    rows = db.redemptions_by_status(chat_id, db.STATUS_PENDING, limit=10)
    if not rows:
        return "📦 <b>待发货</b>\n\n没有待处理的兑换单。"
    lines = ["📦 <b>待发货</b>", ""]
    for r in rows:
        who = texts.esc(r.full_name) or str(r.user_id)
        at = f" @{texts.esc(r.username)}" if r.username else ""
        lines.append(
            f"<b>#{r.id}</b>　{texts.esc(r.item_name)}　{r.cost} 分\n"
            f"　　{who}{at}　<code>{r.user_id}</code>　{texts.fmt_time(r.created_at)}"
        )
    lines += ["", "<i>发完货点「已发货」，发不了点「退积分」把分退回去。</i>"]
    return "\n".join(lines)


def _pending_kb(chat_id: int) -> Markup:
    rows = []
    for r in db.redemptions_by_status(chat_id, db.STATUS_PENDING, limit=10):
        rows.append(
            [
                Btn(f"✅ #{r.id} 已发货", callback_data=f"st:rd:{r.id}:done"),
                Btn(f"↩️ #{r.id} 退积分", callback_data=f"st:rd:{r.id}:refund"),
            ]
        )
    rows.append([Btn("⬅️ 返回", callback_data="st:rd:0:back")])
    return Markup(rows)


# ================================================================ 入口

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text(texts.NOT_GROUP)
        return
    cfg = context.application.bot_data["config"]
    if not await eligibility.is_admin(context.bot, chat.id, user.id, cfg.owner_ids):
        await msg.reply_text(texts.NOT_ADMIN)
        return
    await msg.reply_text(
        _main_text(chat.id), parse_mode=ParseMode.HTML, reply_markup=_main_kb(chat.id)
    )


async def _render(query, text: str, kb: Markup) -> None:
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True
        )
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            log.debug("刷新设置面板失败: %s", exc)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    cfg = context.application.bot_data["config"]
    if not await eligibility.is_admin(context.bot, chat.id, query.from_user.id, cfg.owner_ids):
        await query.answer(texts.NOT_ADMIN, show_alert=True)
        return

    parts = query.data.split(":")
    section, action = parts[1], parts[2] if len(parts) > 2 else ""

    # ---- 主菜单 ----
    if section == "main":
        if action == "close":
            await query.answer()
            try:
                await query.message.delete()
            except TelegramError:
                pass
            return
        await query.answer()
        if action == "lot":
            await _render(query, _lot_text(chat.id), _lot_kb(chat.id))
        elif action == "pt":
            await _render(query, _pt_text(chat.id), _pt_kb(chat.id))
        elif action == "shop":
            await _render(query, _shop_text(chat.id), _shop_kb(chat.id))
        return

    # ---- 抽奖 ----
    if section == "lot":
        if action == "channels":
            await _ask(context, query, "lot_channels", texts.CHANNEL_PROMPT)
            return
        if action == "who":
            s = db.chat_settings(chat.id)
            db.save_chat_settings(chat.id, admin_only=not s["admin_only"])
        await query.answer()
        if action == "back":
            await _render(query, _main_text(chat.id), _main_kb(chat.id))
        else:
            await _render(query, _lot_text(chat.id), _lot_kb(chat.id))
        return

    # ---- 积分 ----
    if section == "pt":
        s = db.point_settings(chat.id)
        if action == "custom":
            await _ask(context, query, "pt_custom", PT_CUSTOM_PROMPT)
            return
        if action == "toggle":
            db.save_point_settings(chat.id, enabled=not s["enabled"])
        elif action == "per":
            db.save_point_settings(chat.id, per_message=_cycle(PER_MESSAGE, s["per_message"]))
        elif action == "cap":
            db.save_point_settings(chat.id, daily_cap=_cycle(DAILY_CAP, s["daily_cap"]))
        elif action == "cd":
            db.save_point_settings(
                chat.id, cooldown_seconds=_cycle(COOLDOWN, s["cooldown_seconds"])
            )
        elif action == "checkin":
            db.save_point_settings(
                chat.id, checkin_points=_cycle(CHECKIN, s["checkin_points"])
            )
        elif action == "minlen":
            db.save_point_settings(
                chat.id, min_message_len=_cycle(MIN_LEN, s["min_message_len"])
            )
        await query.answer()
        if action == "back":
            await _render(query, _main_text(chat.id), _main_kb(chat.id))
        else:
            await _render(query, _pt_text(chat.id), _pt_kb(chat.id))
        return

    # ---- 商城总览 ----
    if section == "shop":
        if action == "add":
            await _ask(context, query, "shop_add", SHOP_ADD_PROMPT)
            return
        await query.answer()
        if action == "pending":
            await _render(query, _pending_text(chat.id), _pending_kb(chat.id))
        elif action == "back":
            await _render(query, _main_text(chat.id), _main_kb(chat.id))
        else:
            await _render(query, _shop_text(chat.id), _shop_kb(chat.id))
        return

    # ---- 单个奖品 ----
    if section == "item":
        item = db.shop_item(int(parts[2]))
        if item is None or item.chat_id != chat.id:
            await query.answer("这个奖品不在了。", show_alert=True)
            await _render(query, _shop_text(chat.id), _shop_kb(chat.id))
            return
        await query.answer()
        await _render(query, _item_text(item), _item_kb(item))
        return

    if section == "it":
        item_id, sub = int(parts[2]), parts[3]
        item = db.shop_item(item_id)
        if item is None or item.chat_id != chat.id:
            await query.answer("这个奖品不在了。", show_alert=True)
            await _render(query, _shop_text(chat.id), _shop_kb(chat.id))
            return
        if sub == "edit":
            await _ask(context, query, f"shop_edit:{item_id}", _item_edit_prompt(item))
            return
        if sub == "codes":
            await _ask(
                context, query, f"shop_codes:{item_id}",
                "请<b>回复这条消息</b>，一行一个卡密，想导多少贴多少。",
            )
            return
        if sub == "toggle":
            db.shop_update_item(item_id, enabled=not item.enabled)
            await query.answer("已下架" if item.enabled else "已上架")
            await _render(query, _item_text(db.shop_item(item_id)), _item_kb(db.shop_item(item_id)))
            return
        if sub == "del":
            db.shop_delete_item(item_id)
            await query.answer(f"已删除「{item.name}」")
            await _render(query, _shop_text(chat.id), _shop_kb(chat.id))
            return
        await query.answer()
        await _render(query, _shop_text(chat.id), _shop_kb(chat.id))
        return

    # ---- 兑换单 ----
    if section == "rd":
        rid, sub = int(parts[2]), parts[3]
        if sub == "back":
            await query.answer()
            await _render(query, _shop_text(chat.id), _shop_kb(chat.id))
            return
        if sub == "done":
            ok, reason = shop.mark_done(rid)
            await query.answer(f"#{rid} 标记为已发货" if ok else reason, show_alert=not ok)
        elif sub == "refund":
            ok, reason = shop.refund(rid)
            await query.answer(f"#{rid} 已退回积分" if ok else reason, show_alert=not ok)
        else:
            await query.answer()
        await _render(query, _pending_text(chat.id), _pending_kb(chat.id))
        return

    await query.answer()


# ================================================================ 文字输入

def _parse_amount(raw: str, unlimited_ok: bool = True) -> Optional[int]:
    raw = raw.strip()
    if unlimited_ok and raw in ("不限", "无限", "-", "", "不限量"):
        return None
    if raw.isdigit():
        return int(raw)
    return None


async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        return
    pending = _pending(context)
    info = pending.get(msg.reply_to_message.message_id)
    if not info or info["user_id"] != update.effective_user.id:
        return
    pending.pop(msg.reply_to_message.message_id, None)

    chat_id = info["chat_id"]
    field = info["field"]
    value = (msg.text or "").strip()
    note = ""

    if field == "lot_channels":
        if value in ("清空", "无", "-"):
            db.save_chat_settings(chat_id, default_channels=[])
        else:
            channels = [c if c.startswith(("@", "-")) else "@" + c for c in value.split()][:5]
            db.save_chat_settings(chat_id, default_channels=channels)
        text, kb = _lot_text(chat_id), _lot_kb(chat_id)

    elif field == "pt_custom":
        changes: dict[str, int] = {}
        for chunk in re.split(r"[\s,，]+", value):
            if "=" not in chunk and "＝" not in chunk:
                continue
            key, _, raw = chunk.replace("＝", "=").partition("=")
            mapped = PT_CUSTOM_KEYS.get(key.strip())
            if mapped and raw.strip().isdigit():
                changes[mapped] = int(raw.strip())
        if changes:
            db.save_point_settings(chat_id, **changes)
            note = "已更新：" + "、".join(changes)
        else:
            note = "没看懂，格式是 每条=2 签到=8"
        text, kb = _pt_text(chat_id), _pt_kb(chat_id)

    elif field == "shop_add":
        parts = [p.strip() for p in SPLIT.split(value)]
        if len(parts) < 2 or not parts[0] or not parts[1].isdigit():
            note = "格式不对，至少要「名称 | 积分」"
        else:
            kind = db.KIND_CODE if len(parts) > 2 and "卡密" in parts[2] else db.KIND_PHYSICAL
            stock = _parse_amount(parts[3]) if len(parts) > 3 else None
            item_id = db.shop_add_item(
                chat_id, parts[0], int(parts[1]), kind,
                stock=None if kind == db.KIND_CODE else stock,
            )
            note = f"已上架「{parts[0]}」"
            if kind == db.KIND_CODE:
                note += "，记得进去导入卡密"
            log.info("群 %s 上架奖品 #%s %s", chat_id, item_id, parts[0])
        text, kb = _shop_text(chat_id), _shop_kb(chat_id)

    elif field.startswith("shop_edit:"):
        item_id = int(field.split(":")[1])
        item = db.shop_item(item_id)
        parts = [p.strip() for p in SPLIT.split(value)]
        if item is None:
            note = "这个奖品不在了"
            text, kb = _shop_text(chat_id), _shop_kb(chat_id)
        elif len(parts) < 2 or not parts[0] or not parts[1].isdigit():
            note = "格式不对，至少要「名称 | 积分」"
            text, kb = _item_text(item), _item_kb(item)
        else:
            changes: dict[str, Any] = {"name": parts[0][:80], "cost": max(1, int(parts[1]))}
            if len(parts) > 2:
                changes["stock"] = _parse_amount(parts[2])
            if len(parts) > 3:
                changes["per_user_limit"] = _parse_amount(parts[3])
            db.shop_update_item(item_id, **changes)
            note = "已更新"
            item = db.shop_item(item_id)
            text, kb = _item_text(item), _item_kb(item)

    elif field.startswith("shop_codes:"):
        item_id = int(field.split(":")[1])
        item = db.shop_item(item_id)
        if item is None:
            note = "这个奖品不在了"
            text, kb = _shop_text(chat_id), _shop_kb(chat_id)
        else:
            added = db.shop_add_codes(item_id, value.splitlines())
            note = f"导入了 {added} 个卡密"
            item = db.shop_item(item_id)
            text, kb = _item_text(item), _item_kb(item)
    else:
        return

    for m in (msg.reply_to_message, msg):
        try:
            await m.delete()
        except TelegramError:
            pass

    if note:
        text = f"{text}\n\n<i>✅ {texts.esc(note)}</i>"
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=info["panel_message_id"],
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        log.debug("回写设置面板失败: %s", exc)
