"""积分板块的业务逻辑：发言得分、每日签到、排名。

积分按群独立：A 群挣的分只能在 A 群花。
发言得分有三道闸，都在 /settings 的「积分设置」里调：
  冷却   —— 同一个人多久才能再拿一次
  每日上限 —— 一天靠发言最多拿多少
  反刷过滤 —— 太短的、纯表情的、和上一条重复的，不给分
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Optional

from telegram import User

from . import db

log = logging.getLogger(__name__)

# 聊天中的触发词，整条消息相等才算（避免「我的积分怎么查」被误判）
CHECKIN_WORDS = {"签到", "打卡"}
BALANCE_WORDS = {"我的积分", "积分", "我的分数", "查积分"}
SHOP_WORDS = {"积分商城", "商城", "积分兑换", "兑换"}
TOP_WORDS = {"积分排行", "积分排行榜", "排行榜", "积分榜"}

TRIGGER_WORDS = CHECKIN_WORDS | BALANCE_WORDS | SHOP_WORDS | TOP_WORDS

# 至少要有一个字母/数字/汉字，纯表情和纯标点不算有效发言
_MEANINGFUL = re.compile(r"[0-9A-Za-z一-鿿]")


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


def is_trigger(text: str) -> bool:
    return text.strip() in TRIGGER_WORDS


@dataclass
class Earned:
    points: int
    balance: int
    skipped: str = ""      # 没给分的原因，只用于日志排查


def earn_from_message(chat_id: int, user: User, text: str) -> Earned:
    """一条群消息该给多少分。返回 points=0 表示这条不给分。"""
    settings = db.point_settings(chat_id)
    if not settings["enabled"] or settings["per_message"] <= 0:
        return Earned(0, 0, "未开启")

    body = (text or "").strip()
    if len(body) < settings["min_message_len"]:
        return Earned(0, 0, "太短")
    if not _MEANINGFUL.search(body):
        return Earned(0, 0, "没有有效字符")
    if is_trigger(body):
        return Earned(0, 0, "触发词本身不给分")

    today = db.day_key()
    stat = db.daily(chat_id, user.id, today)

    cap = settings["daily_cap"]
    if cap > 0 and stat["chat_earned"] >= cap:
        return Earned(0, 0, "已到每日上限")

    cooldown = settings["cooldown_seconds"]
    if cooldown > 0 and db.now() - stat["last_chat_at"] < cooldown:
        return Earned(0, 0, "冷却中")

    digest = _text_hash(body)
    if stat["last_text_hash"] == digest:
        return Earned(0, 0, "和上一条重复")

    points = settings["per_message"]
    if cap > 0:
        points = min(points, cap - stat["chat_earned"])
    if points <= 0:
        return Earned(0, 0, "已到每日上限")

    db.bump_daily_chat(chat_id, user.id, points, text_hash=digest, day=today)
    balance = db.add_points(
        chat_id, user.id, points, db.REASON_CHAT, user.username, user.full_name
    )
    return Earned(points, balance)


@dataclass
class CheckIn:
    ok: bool
    points: int = 0
    balance: int = 0
    streak: int = 0
    reason: str = ""


def check_in(chat_id: int, user: User) -> CheckIn:
    settings = db.point_settings(chat_id)
    if not settings["enabled"]:
        return CheckIn(False, reason="本群没开积分")
    reward = settings["checkin_points"]
    if reward <= 0:
        return CheckIn(False, reason="本群签到不给分")

    if not db.mark_checkin(chat_id, user.id):
        return CheckIn(
            False,
            balance=db.point_balance(chat_id, user.id),
            streak=db.checkin_streak(chat_id, user.id),
            reason="今天已经签过了",
        )

    balance = db.add_points(
        chat_id, user.id, reward, db.REASON_CHECKIN, user.username, user.full_name
    )
    return CheckIn(True, reward, balance, db.checkin_streak(chat_id, user.id))
