"""积分商城的业务逻辑：兑换、退单。

奖品分两类，建的时候选：
  实物（physical）—— 扣分后生成兑换单，私信通知管理员去发货
  卡密（code）    —— 管理员预先导入一批码，用户兑换后机器人直接私信给他

扣分之前先把货取到手：卡密是先取码再扣分，取不到码就不扣。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from telegram import User

from . import db

log = logging.getLogger(__name__)


@dataclass
class RedeemResult:
    ok: bool
    reason: str = ""
    item: Optional[db.ShopItem] = None
    redemption_id: int = 0
    code: Optional[str] = None
    balance: int = 0


def available_stock(item: db.ShopItem) -> Optional[int]:
    """还能兑多少份。None = 不限量。"""
    if item.is_code:
        return db.shop_free_codes(item.id)
    return item.stock


def redeem(chat_id: int, user: User, item_id: int) -> RedeemResult:
    item = db.shop_item(item_id)
    if item is None or item.chat_id != chat_id:
        return RedeemResult(False, "这个奖品不存在了。")
    if not item.enabled:
        return RedeemResult(False, "这个奖品已经下架了。", item)

    balance = db.point_balance(chat_id, user.id)
    if balance < item.cost:
        return RedeemResult(
            False, f"积分不够：需要 {item.cost}，你有 {balance}。", item, balance=balance
        )

    if item.per_user_limit is not None:
        mine = db.user_redeemed_count(chat_id, item.id, user.id)
        if mine >= item.per_user_limit:
            return RedeemResult(
                False, f"这个奖品每人限兑 {item.per_user_limit} 次，你已经兑过了。",
                item, balance=balance,
            )

    stock = available_stock(item)
    if stock is not None and stock <= 0:
        return RedeemResult(False, "这个奖品已经兑完了。", item, balance=balance)

    code: Optional[str] = None
    if item.is_code:
        # 先把码取到手再扣分 —— 反过来的话取不到码就白扣了
        code = db.shop_take_code(item.id, user.id)
        if code is None:
            return RedeemResult(False, "这个奖品刚好被兑完了。", item, balance=balance)

    after = db.add_points(
        chat_id, user.id, -item.cost, db.REASON_REDEEM, user.username, user.full_name
    )

    if not item.is_code and item.stock is not None:
        db.shop_update_item(item.id, stock=max(0, item.stock - 1))

    status = db.STATUS_DELIVERED if item.is_code else db.STATUS_PENDING
    rid = db.redemption_create(
        chat_id, item, user.id, user.username, user.full_name, status, code
    )
    log.info("群 %s：%s 用 %s 分兑换了「%s」(#%s)", chat_id, user.id, item.cost, item.name, rid)
    return RedeemResult(True, "", item, rid, code, after)


def refund(redemption_id: int) -> tuple[bool, str]:
    """退单：积分退回、实物库存加回、状态改成 refunded。"""
    record = db.redemption(redemption_id)
    if record is None:
        return False, "找不到这张兑换单。"
    if record.status != db.STATUS_PENDING:
        return False, "只有待处理的兑换单能退。"

    if not db.redemption_set_status(redemption_id, db.STATUS_REFUNDED):
        return False, "这张单刚刚被别人处理了。"

    db.add_points(
        record.chat_id, record.user_id, record.cost, db.REASON_REFUND,
        record.username, record.full_name,
    )
    if record.item_id:
        item = db.shop_item(record.item_id)
        if item is not None and not item.is_code and item.stock is not None:
            db.shop_update_item(item.id, stock=item.stock + 1)
    return True, ""


def mark_done(redemption_id: int) -> tuple[bool, str]:
    record = db.redemption(redemption_id)
    if record is None:
        return False, "找不到这张兑换单。"
    if record.status != db.STATUS_PENDING:
        return False, "这张单不是待处理状态。"
    if not db.redemption_set_status(redemption_id, db.STATUS_DONE):
        return False, "这张单刚刚被别人处理了。"
    return True, ""
