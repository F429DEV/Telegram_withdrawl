"""群里机器人自己发的消息，默认 30 秒后自动删掉，免得刷屏。

做法是包一层 Bot：所有 send_message 发出去的群消息都自动排一个删除任务。
要保留的消息（抽奖卡片、开奖结果、创建面板）由调用方在发完之后立刻 cancel()。
私聊消息一律不删。

机器人删自己发的消息不需要管理员权限，所以这套不依赖群里给不给「删除消息」。
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Message
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes, ExtBot

log = logging.getLogger(__name__)

DEFAULT_SECONDS = 30

_app: Optional[Application] = None
_seconds: int = DEFAULT_SECONDS

_GROUPS = (ChatType.GROUP, ChatType.SUPERGROUP)


def bind(app: Application, seconds: int = DEFAULT_SECONDS) -> None:
    """启动时调一次。seconds <= 0 表示关掉自动删除。"""
    global _app, _seconds
    _app = app
    _seconds = max(0, int(seconds))


def enabled() -> bool:
    return _seconds > 0 and _app is not None and _app.job_queue is not None


def _job_name(chat_id: int, message_id: int) -> str:
    return f"ephemeral_{chat_id}_{message_id}"


async def _delete_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    try:
        await context.bot.delete_message(data["chat_id"], data["message_id"])
    except TelegramError as exc:
        # 已经被别人删了、超过 48 小时删不了 —— 都不值得吵
        log.debug("自动删除消息失败 %s: %s", data, exc)


def schedule(chat_id: int, message_id: int, delay: Optional[int] = None) -> None:
    if not enabled():
        return
    _app.job_queue.run_once(
        _delete_job,
        when=delay if delay is not None else _seconds,
        data={"chat_id": chat_id, "message_id": message_id},
        name=_job_name(chat_id, message_id),
    )


def cancel(chat_id: int, message_id: Optional[int]) -> None:
    """这条消息要留着 —— 把已排的删除任务撤掉。"""
    if message_id is None or _app is None or _app.job_queue is None:
        return
    for job in _app.job_queue.get_jobs_by_name(_job_name(chat_id, message_id)):
        job.schedule_removal()


def keep(message: Optional[Message]) -> Optional[Message]:
    """语法糖：keep(await msg.reply_text(...))"""
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is not None:
        cancel(chat_id, message_id)
    return message


def track(message: Optional[Message]) -> None:
    if message is None or message.chat is None:
        return
    if message.chat.type not in _GROUPS:
        return
    schedule(message.chat_id, message.message_id)


class EphemeralBot(ExtBot):
    """发出去的群消息自动排删除任务。"""

    async def send_message(self, *args, **kwargs):  # type: ignore[override]
        message = await super().send_message(*args, **kwargs)
        track(message)
        return message
