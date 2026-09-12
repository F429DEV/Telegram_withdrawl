"""基础命令：/start、/help，以及被拉进群时的自我介绍。"""

from __future__ import annotations

from typing import Optional

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import db, service, texts
from . import participate


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("我在。群管理员发 /new 就能开抽奖，/help 看说明。")
        return

    # 从群里「点我私聊」过来的：直接把那个群的专属邀请链接发给他
    payload = (context.args or [""])[0]
    if payload.startswith("inv_"):
        raw = payload[4:]
        try:
            group_id = int(raw)
        except ValueError:
            group_id = None
        if group_id is not None:
            title = ""
            try:
                title = (await context.bot.get_chat(group_id)).title or ""
            except TelegramError:
                pass
            if await participate.send_invite_dm(
                context.bot, group_id, update.effective_user.id, title
            ):
                return
            await update.message.reply_text(
                "没能生成邀请链接 —— 我在那个群里可能没有「邀请用户」权限。"
            )
            return

    await update.message.reply_text(texts.START_PRIVATE)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        texts.HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


_CHAT_TYPE_LABEL = {
    ChatType.PRIVATE: "私聊",
    ChatType.GROUP: "普通群",
    ChatType.SUPERGROUP: "超级群",
    ChatType.CHANNEL: "频道",
}


def _win_history(user_id: int, chat_id: Optional[int]) -> list[str]:
    """历史中奖记录。chat_id 为 None 时查所有群。"""
    records, total = db.wins_for_user(user_id, chat_id, limit=5)
    if not total:
        return ["", "🏆 <b>历史中奖</b>", "还没中过奖，继续参与 🍀"]
    scope = "本群" if chat_id is not None else "全部"
    out = ["", f"🏆 <b>历史中奖</b>（{scope}共 {total} 次）"]
    for r in records:
        when = texts.fmt_time(r.ended_at) if r.ended_at else "时间不详"
        out.append(f"• #{r.giveaway_id}「{texts.esc(r.prize) or '未填奖品'}」{when}")
    if total > len(records):
        out.append(f"<i>…… 还有 {total - len(records)} 次没列出</i>")
    return out


async def viewmyinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """报出自己的 Telegram ID，以及在本群的记录。"""
    user, chat, msg = update.effective_user, update.effective_chat, update.effective_message

    lines = [
        "🪪 <b>你的信息</b>",
        "",
        f"用户 ID：<code>{user.id}</code>",
        f"用户名：{'@' + user.username if user.username else '（没设置）'}",
        f"昵称：{texts.esc(user.full_name)}",
    ]

    if chat.type == ChatType.PRIVATE:
        lines += _win_history(user.id, None)
        lines += [
            "",
            "<i>把这个命令发在群里，还能看到群 ID 和你在那个群的记录。</i>",
        ]
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    lines += [
        "",
        "📍 <b>当前会话</b>",
        f"名称：{texts.esc(chat.title or '')}",
        f"类型：{_CHAT_TYPE_LABEL.get(chat.type, chat.type)}",
        f"群 ID：<code>{chat.id}</code>",
    ]

    stats = db.user_stats(chat.id, user.id)
    invited = db.invite_count(chat.id, user.id)
    lines += ["", "📊 <b>你在本群的记录</b>"]
    if stats:
        lines.append(f"首次出现：{texts.fmt_time(stats['first_seen_at'])}")
        lines.append(f"累计发言：{stats['msg_count']} 条")
    else:
        lines.append("首次出现：还没记录到（机器人加群后你发过言才会开始记）")
    lines.append(f"邀请进群：{invited} 人")

    mine = []
    for g in db.active_in_chat(chat.id):
        if not db.is_participant(g.id, user.id):
            continue
        people, weighted = service.weighted_participants(g)
        me = next((p for p in people if p.user_id == user.id), None)
        if me is None:
            continue
        if weighted:
            total = sum(p.weight for p in people) or 1
            share = me.weight / total * 100
            mine.append(
                f"#{g.id}「{texts.esc(g.prize)}」权重 {me.weight}"
                f"，占全场约 {share:.1f}%"
            )
        else:
            mine.append(
                f"#{g.id}「{texts.esc(g.prize)}」已报名"
                f"（{len(people)} 人参与，人人机会均等）"
            )
    if mine:
        lines += ["", "🎁 <b>你正在参与</b>"]
        lines += [f"• {item}" for item in mine]

    lines += _win_history(user.id, chat.id)

    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                         disable_web_page_preview=True)


async def greet_new_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """机器人被加进群时打个招呼。"""
    msg = update.message
    if not msg or not msg.new_chat_members:
        return
    me = context.bot.id
    if any(u.id == me for u in msg.new_chat_members):
        await msg.reply_text(
            "🎁 抽奖机器人已就位。\n\n"
            "群管理员发 /new 打开配置面板即可开始，/help 查看全部命令。\n"
            "建议给我「删除消息」权限，这样口令报名的刷屏消息我可以自动清掉。"
        )
