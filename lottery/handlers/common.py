"""基础命令：/start、/help，以及被拉进群时的自我介绍。"""

from __future__ import annotations

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from .. import db, service, texts


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text(texts.START_PRIVATE)
    else:
        await update.message.reply_text("我在。群管理员发 /new 就能开抽奖，/help 看说明。")


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
