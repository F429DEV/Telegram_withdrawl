"""/menu —— 按权限分层的按钮菜单，不用记命令。

分三层，看谁在用：
  群成员     签到、积分、排行、商城、抽奖列表、邀请链接、我的信息
  群管理员   以上 + 发起抽奖、群设置
  主人       以上 + 主人工具（**只在私聊里出现**）

主人工具不进群里的菜单：/reserve /adjust /ledger 这几个对群成员
应该连存在都看不出来，摆在群里的按钮上就暴露了。

建菜单时按点命令那个人的权限来，但每次点按钮都会重新校验一遍 ——
管理员开出来的菜单留在群里，别人点了也越不了权。
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import db, eligibility, ephemeral, texts
from . import common, create, manage, owner, participate, points, settings, shop

log = logging.getLogger(__name__)

# 只有主人能点、且只在私聊里生效的按钮
_OWNER_ACTIONS = {"owner", "reserve", "adjust", "ledger"}


def _in_group(update: Update) -> bool:
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    cfg = context.application.bot_data["config"]
    return update.effective_user.id in cfg.owner_ids


async def _tier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """member / admin / owner —— 决定菜单上有哪些按钮。"""
    if _is_owner(update, context):
        return "owner"
    if _in_group(update):
        cfg = context.application.bot_data["config"]
        if await eligibility.is_admin(
            context.bot, update.effective_chat.id, update.effective_user.id, cfg.owner_ids
        ):
            return "admin"
    return "member"


# ---------------------------------------------------------------- 菜单内容

def _group_text(chat_id: int, tier: str) -> str:
    pt = db.point_settings(chat_id)
    lines = ["🤖 <b>功能菜单</b>", "", "点按钮就行，不用记命令。"]
    if not pt["enabled"]:
        lines.append("<i>本群积分功能没开，积分和商城的按钮暂时用不了。</i>")
    if tier in ("admin", "owner"):
        lines.append("<i>下面一排是管理员专用，别人点会被挡住。</i>")
    lines.append("")
    lines.append("<i>想看文字说明发 /help</i>")
    return "\n".join(lines)


def _group_kb(tier: str) -> Markup:
    rows = [
        [Btn("📅 签到", callback_data="m:checkin"),
         Btn("🏅 我的积分", callback_data="m:points")],
        [Btn("🏆 排行榜", callback_data="m:top"),
         Btn("🛒 积分商城", callback_data="m:shop")],
        [Btn("🎁 进行中的抽奖", callback_data="m:lotteries"),
         Btn("🔗 我的邀请链接", callback_data="m:invite")],
        [Btn("🪪 我的信息", callback_data="m:me"),
         Btn("❓ 帮助", callback_data="m:help")],
    ]
    if tier in ("admin", "owner"):
        rows.append(
            [Btn("➕ 发起抽奖", callback_data="m:new"),
             Btn("⚙️ 群设置", callback_data="m:settings")]
        )
    return Markup(rows)


def _private_text(tier: str) -> str:
    lines = [
        "🤖 <b>功能菜单</b>",
        "",
        "积分、抽奖、商城都是<b>按群</b>算的，到群里发 /menu 才能用。",
    ]
    if tier == "owner":
        lines += ["", "你是机器人主人，下面有只能私聊用的工具。"]
    return "\n".join(lines)


def _private_kb(tier: str) -> Markup:
    rows = [[Btn("❓ 帮助", callback_data="m:help")]]
    if tier == "owner":
        rows.insert(0, [Btn("👑 主人工具", callback_data="m:owner")])
    return Markup(rows)


# ---------------------------------------------------------------- 主人常驻键盘
#
# 内联按钮会被后面的消息顶走，翻回去找很烦。常驻键盘挂在输入框上方，
# 不管聊到哪儿都在手边。
#
# 只在主人的私聊里装：常驻键盘是「这个会话」级别的，群里装会对所有人显示，
# 等于把主人工具摆到群成员面前。
BTN_RESERVE = "📌 预留名额"
BTN_ADJUST = "🛠 手动调分"
BTN_LEDGER = "📒 积分流水"
BTN_MENU = "🤖 功能菜单"
BTN_HIDE = "⌨️ 收起键盘"

OWNER_BOARD = {
    BTN_RESERVE: "reserve",
    BTN_ADJUST: "adjust",
    BTN_LEDGER: "ledger",
    BTN_MENU: "menu",
    BTN_HIDE: "hide",
}


def owner_board() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_RESERVE), KeyboardButton(BTN_ADJUST)],
            [KeyboardButton(BTN_LEDGER), KeyboardButton(BTN_MENU)],
            [KeyboardButton(BTN_HIDE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="点按钮，或直接打命令",
    )


OWNER_TOOLS = (
    "👑 <b>主人工具</b>\n\n"
    "只能在私聊里用。群里发这些命令、或者别人发，机器人都没有任何反应。\n\n"
    "点下面的按钮会先列出可操作的对象（抽奖 / 群），"
    "具体那一步还是要打一行命令 —— 预留给谁、给谁加多少分，按钮没法代你填。\n\n"
    "<i>这几条不在命令菜单里，也不在 /help 里。</i>"
)


def _owner_kb() -> Markup:
    return Markup(
        [
            [Btn("📌 预留名额", callback_data="m:reserve")],
            [Btn("🛠 手动调分", callback_data="m:adjust")],
            [Btn("📒 积分流水", callback_data="m:ledger")],
            [Btn("⬅️ 返回", callback_data="m:main")],
        ]
    )


# ---------------------------------------------------------------- 入口

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tier = await _tier(update, context)
    if _in_group(update):
        text, kb = _group_text(update.effective_chat.id, tier), _group_kb(tier)
    else:
        text, kb = _private_text(tier), _private_kb(tier)
    ephemeral.panel(
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb,
            disable_web_page_preview=True,
        )
    )
    # 主人的私聊再额外挂一排常驻键盘，之后不管聊到哪儿按钮都在
    if tier == "owner" and not _in_group(update):
        await update.effective_message.reply_text(
            "下面这排按钮会一直留在输入框上方。", reply_markup=owner_board()
        )


async def owner_board_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """常驻键盘按下去发的是普通文字，这里接住它。只认主人、只认私聊。"""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    action = OWNER_BOARD.get(msg.text.strip())
    if action is None or _in_group(update) or not _is_owner(update, context):
        return

    if action == "hide":
        await msg.reply_text("键盘收起来了。发 /menu 可以再叫出来。",
                             reply_markup=ReplyKeyboardRemove())
        return
    if action == "menu":
        await menu_cmd(update, context)
        return

    context.args = []
    handler = {
        "reserve": owner.reserve_cmd,
        "adjust": owner.adjust_cmd,
        "ledger": owner.ledger_cmd,
    }[action]
    await handler(update, context)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    tier = await _tier(update, context)

    # 管理员专用的按钮，每次点都重新校验 —— 菜单是谁开的不作数
    if action in ("new", "settings") and tier not in ("admin", "owner"):
        await query.answer(texts.NOT_ADMIN, show_alert=True)
        return
    if action in _OWNER_ACTIONS and tier != "owner":
        await query.answer("这个按钮不是给你的。", show_alert=True)
        return
    if action in _OWNER_ACTIONS and _in_group(update):
        # 兜底：主人工具一律不在群里动作，连回显都不要
        await query.answer()
        return

    if action == "main":
        await query.answer()
        if _in_group(update):
            text, kb = _group_text(update.effective_chat.id, tier), _group_kb(tier)
        else:
            text, kb = _private_text(tier), _private_kb(tier)
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=kb,
                disable_web_page_preview=True,
            )
        except TelegramError:
            pass
        return

    if action == "owner":
        await query.answer()
        try:
            await query.edit_message_text(
                OWNER_TOOLS, parse_mode=ParseMode.HTML,
                reply_markup=_owner_kb(),
            )
        except TelegramError:
            pass
        return

    # 下面这些直接复用各板块已有的处理函数：
    # 回调里 effective_user 是点按钮的人，effective_message 是菜单那条消息，
    # 它们内部都是 reply_text，所以结果会跟在菜单下面，行为和打命令一致。
    await query.answer()
    context.args = []
    dispatch = {
        "checkin": points.checkin,
        "points": points.my_points,
        "top": points.leaderboard,
        "shop": shop.shop_cmd,
        "lotteries": manage.list_cmd,
        "invite": participate.invite_cmd,
        "me": common.viewmyinfo,
        "help": common.help_cmd,
        "new": create.new_giveaway,
        "settings": settings.settings_cmd,
        # 主人工具：不带参数调用，各自会列出可操作的对象和用法
        "reserve": owner.reserve_cmd,
        "adjust": owner.adjust_cmd,
        "ledger": owner.ledger_cmd,
    }
    handler = dispatch.get(action)
    if handler is None:
        return
    await handler(update, context)
