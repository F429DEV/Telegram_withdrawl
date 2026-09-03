"""抽奖的业务流程：发布、刷新卡片、开奖、定时调度。"""

from __future__ import annotations

import logging
import time
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, ContextTypes

from . import db, eligibility, engine, keyboards, texts

log = logging.getLogger(__name__)

# 卡片刷新节流：抽奖 id -> 上次编辑时间
_last_edit: dict[int, float] = {}
_EDIT_INTERVAL = 3.0

JOB_PREFIX = "giveaway_end_"

# Telegram 表示「这条消息没了 / 编辑不了」的几种说法
_GONE_HINTS = (
    "message to edit not found",
    "message can't be edited",
    "message identifier is not specified",
)


def _is_gone(exc: Exception) -> bool:
    return any(h in str(exc).lower() for h in _GONE_HINTS)


# ---------------------------------------------------------------- 发布

async def publish(
    bot: Bot, app: Application, g: db.Giveaway, edit_message_id: Optional[int] = None
) -> Optional[int]:
    """把草稿变成正式抽奖，发出（或就地改写）抽奖卡片。返回消息 id。"""
    db.update(g.id, status=db.STATUS_ACTIVE)
    g = db.get(g.id)
    count = db.participant_count(g.id)
    message_id = None
    if edit_message_id:
        try:
            await bot.edit_message_text(
                chat_id=g.chat_id,
                message_id=edit_message_id,
                text=texts.card(g, count),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboards.join_kb(g, count),
            )
            message_id = edit_message_id
        except TelegramError as exc:
            log.warning("就地发布抽奖 #%s 失败，改为新发一条: %s", g.id, exc)
    if message_id is None:
        msg = await bot.send_message(
            chat_id=g.chat_id,
            text=texts.card(g, count),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.join_kb(g, count),
        )
        message_id = msg.message_id
    db.update(g.id, message_id=message_id)
    schedule_end(app, db.get(g.id))
    return message_id


# ---------------------------------------------------------------- 卡片刷新

async def _repost_card(bot: Bot, g: db.Giveaway) -> None:
    """原来的抽奖卡片被删了，补发一条新的，并把 message_id 指过去。"""
    count = db.participant_count(g.id)
    try:
        msg = await bot.send_message(
            chat_id=g.chat_id,
            text=texts.card(g, count),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.join_kb(g, count),
        )
    except TelegramError as exc:
        # 连发都发不出去（被踢出群、没发言权限……），清掉 message_id 别再刷屏日志
        db.update(g.id, message_id=None)
        log.warning("抽奖 #%s 的卡片没了，补发也失败：%s", g.id, exc)
        return
    db.update(g.id, message_id=msg.message_id)
    log.info("抽奖 #%s 的卡片已被删除，已补发一条新的（message_id=%s）", g.id, msg.message_id)


async def refresh_card(bot: Bot, g: db.Giveaway, force: bool = False) -> None:
    if not g.message_id or g.status != db.STATUS_ACTIVE:
        return
    now = time.monotonic()
    if not force and now - _last_edit.get(g.id, 0.0) < _EDIT_INTERVAL:
        return
    _last_edit[g.id] = now
    count = db.participant_count(g.id)
    try:
        await bot.edit_message_text(
            chat_id=g.chat_id,
            message_id=g.message_id,
            text=texts.card(g, count),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.join_kb(g, count),
        )
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        if _is_gone(exc):
            await _repost_card(bot, db.get(g.id))
            return
        log.warning("刷新抽奖 #%s 卡片失败: %s", g.id, exc)
    except TelegramError as exc:
        log.warning("刷新抽奖 #%s 卡片失败: %s", g.id, exc)


# ---------------------------------------------------------------- 开奖

async def finish(bot: Bot, giveaway_id: int, *, reason: str = "") -> bool:
    g = db.get(giveaway_id)
    if g is None or g.status != db.STATUS_ACTIVE:
        return False

    people = db.participants(g.id)
    ok, rejected = await eligibility.filter_participants(bot, g, people)

    seed = engine.new_seed()
    wins = engine.draw(
        ok, g.winners_count, seed, g.id, weighted=(g.mode == db.MODE_POINTS)
    )
    db.update(g.id, status=db.STATUS_ENDED, ended_at=db.now(), seed=seed)
    db.save_winners(g.id, wins)
    g = db.get(g.id)

    # 原卡片改成「已结束」
    if g.message_id:
        try:
            await bot.edit_message_text(
                chat_id=g.chat_id,
                message_id=g.message_id,
                text=texts.card(g, len(people)) + "\n\n<b>🔚 已开奖</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboards.ended_kb(g),
            )
        except TelegramError as exc:
            if _is_gone(exc):
                log.info("抽奖 #%s 的卡片已被删除，跳过收尾编辑，开奖结果照常发。", g.id)
                db.update(g.id, message_id=None)
                g = db.get(g.id)
            else:
                log.warning("收尾编辑抽奖 #%s 失败: %s", g.id, exc)

    text = texts.result(g, wins, len(ok))
    if rejected:
        text += f"\n<i>（{len(rejected)} 人因不满足门槛被剔除）</i>"
    if reason:
        text += f"\n<i>{texts.esc(reason)}</i>"
    try:
        await bot.send_message(
            chat_id=g.chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=g.message_id,
            allow_sending_without_reply=True,
        )
    except TelegramError as exc:
        log.warning("发送开奖结果 #%s 失败: %s", g.id, exc)
    return True


async def reroll(bot: Bot, giveaway_id: int) -> bool:
    """重抽：排除上一轮已中奖的人，重新生成种子。"""
    g = db.get(giveaway_id)
    if g is None or g.status != db.STATUS_ENDED:
        return False
    old = {w.user_id for w in db.winners(g.id)}
    people = [p for p in db.participants(g.id) if p.user_id not in old]
    ok, _ = await eligibility.filter_participants(bot, g, people)
    seed = engine.new_seed()
    wins = engine.draw(ok, g.winners_count, seed, g.id, weighted=(g.mode == db.MODE_POINTS))
    db.update(g.id, seed=seed)
    db.save_winners(g.id, wins)
    g = db.get(g.id)
    await bot.send_message(
        chat_id=g.chat_id,
        text="🔁 <b>重新抽取</b>（已排除上一轮中奖者）\n\n" + texts.result(g, wins, len(ok)),
        parse_mode=ParseMode.HTML,
        reply_to_message_id=g.message_id,
        allow_sending_without_reply=True,
    )
    return True


async def cancel(bot: Bot, giveaway_id: int, note: str = "") -> bool:
    g = db.get(giveaway_id)
    if g is None or g.status != db.STATUS_ACTIVE:
        return False
    db.update(g.id, status=db.STATUS_CANCELLED, ended_at=db.now())
    if g.message_id:
        try:
            await bot.edit_message_text(
                chat_id=g.chat_id,
                message_id=g.message_id,
                text=texts.card(g, db.participant_count(g.id)) + "\n\n<b>❌ 已取消</b>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
    await bot.send_message(
        chat_id=g.chat_id,
        text=f"❌ 抽奖 #{g.id} 已取消。{texts.esc(note)}",
        parse_mode=ParseMode.HTML,
    )
    return True


# ---------------------------------------------------------------- 调度

async def _end_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    giveaway_id = context.job.data["giveaway_id"]
    await finish(context.bot, giveaway_id, reason="⏰ 到点自动开奖")


def schedule_end(app: Application, g: db.Giveaway) -> None:
    if app.job_queue is None or g.status != db.STATUS_ACTIVE or not g.end_at:
        return
    name = f"{JOB_PREFIX}{g.id}"
    for job in app.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    delay = max(1, g.end_at - db.now())
    app.job_queue.run_once(_end_job, when=delay, data={"giveaway_id": g.id}, name=name)


def unschedule(app: Application, giveaway_id: int) -> None:
    if app.job_queue is None:
        return
    for job in app.job_queue.get_jobs_by_name(f"{JOB_PREFIX}{giveaway_id}"):
        job.schedule_removal()


async def restore_jobs(app: Application) -> None:
    """进程重启后，把还没开奖的定时任务重新挂上；已经过期的立刻开奖。"""
    for g in db.all_active():
        if not g.end_at:
            continue
        if g.end_at <= db.now():
            await finish(app.bot, g.id, reason="⏰ 机器人重启后补开")
        else:
            schedule_end(app, g)


async def maybe_finish_by_cap(bot: Bot, app: Application, g: db.Giveaway) -> bool:
    if not g.max_participants:
        return False
    if db.participant_count(g.id) < g.max_participants:
        return False
    unschedule(app, g.id)
    return await finish(bot, g.id, reason="👥 人数已满，提前开奖")
