"""参与资格校验：管理员判断、频道成员、防小号。"""

from __future__ import annotations

import time
from typing import Iterable

from telegram import Chat, User
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from . import db

_MEMBER_OK = {
    ChatMemberStatus.OWNER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
}

# (chat_id, user_id) -> (是否管理员, 过期时间)
_admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}
_ADMIN_TTL = 60.0


async def is_admin(bot, chat_id: int, user_id: int, owner_ids: Iterable[int] = ()) -> bool:
    if user_id in set(owner_ids):
        return True
    key = (chat_id, user_id)
    hit = _admin_cache.get(key)
    now = time.monotonic()
    if hit and hit[1] > now:
        return hit[0]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        ok = member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except TelegramError:
        ok = False
    _admin_cache[key] = (ok, now + _ADMIN_TTL)
    return ok


async def can_manage(bot, chat: Chat, user: User, owner_ids: Iterable[int] = ()) -> bool:
    """能不能发起 / 管理抽奖。群设置里可以放开给所有人。"""
    settings = db.chat_settings(chat.id)
    if not settings["admin_only"]:
        return True
    return await is_admin(bot, chat.id, user.id, owner_ids)


async def check(bot, g: db.Giveaway, user: User) -> tuple[bool, str]:
    """返回 (是否有资格, 不合格时给用户看的原因)。"""
    if user.is_bot:
        return False, "机器人不能参与抽奖。"

    if g.require_username and not user.username:
        return False, "需要先在 Telegram 设置里给自己起一个用户名（@xxx）才能参与。"

    if g.min_seen_hours:
        seen = db.first_seen(g.chat_id, user.id)
        if seen is None:
            return False, (
                f"为防小号，需要你在本群出现满 {g.min_seen_hours} 小时才能参与。"
                "先在群里聊两句，我就记录到你了。"
            )
        waited = (int(time.time()) - seen) / 3600
        if waited < g.min_seen_hours:
            left = g.min_seen_hours - waited
            return False, f"为防小号，还需要再等 {left:.1f} 小时才能参与。"

    for channel in g.require_channels:
        try:
            member = await bot.get_chat_member(channel, user.id)
        except TelegramError:
            # 机器人不在那个频道，或频道标识写错了 —— 不因为配置问题卡住用户
            continue
        if member.status not in _MEMBER_OK:
            return False, f"需要先加入 {channel} 才能参与。"

    return True, ""


async def filter_participants(
    bot, g: db.Giveaway, people: list[db.Participant]
) -> tuple[list[db.Participant], list[db.Participant]]:
    """开奖前批量复核（口令 / 积分模式用）。返回 (合格, 不合格)。"""
    if not (g.require_channels or g.require_username or g.min_seen_hours):
        return people, []
    ok, bad = [], []
    for p in people:
        passed = True
        if g.require_username and not p.username:
            passed = False
        if passed and g.min_seen_hours:
            seen = db.first_seen(g.chat_id, p.user_id)
            if seen is None or (int(time.time()) - seen) / 3600 < g.min_seen_hours:
                passed = False
        if passed:
            for channel in g.require_channels:
                try:
                    member = await bot.get_chat_member(channel, p.user_id)
                except TelegramError:
                    continue
                if member.status not in _MEMBER_OK:
                    passed = False
                    break
        (ok if passed else bad).append(p)
    return ok, bad
