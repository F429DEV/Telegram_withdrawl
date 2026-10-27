"""积分商城：扣分、库存、限兑、卡密自动发货、退单。"""

import tempfile
import unittest
from pathlib import Path

from telegram import User

from lottery import db, shop


def user(uid=1, name="甲", username="a"):
    return User(id=uid, first_name=name, is_bot=False, username=username)


class ShopBase(unittest.TestCase):
    CHAT = -100

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "s.db")

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    def give(self, amount, uid=1):
        db.add_points(self.CHAT, uid, amount, db.REASON_ADMIN, "a", "甲")


class TestPhysical(ShopBase):
    def test_redeem_creates_pending_order(self):
        item = db.shop_add_item(self.CHAT, "马克杯", 30, db.KIND_PHYSICAL, stock=2)
        self.give(50)
        got = shop.redeem(self.CHAT, user(), item)
        self.assertTrue(got.ok)
        self.assertEqual(got.balance, 20)
        self.assertIsNone(got.code)
        record = db.redemption(got.redemption_id)
        self.assertEqual(record.status, db.STATUS_PENDING)
        self.assertEqual(db.shop_item(item).stock, 1, "实物库存要减 1")
        self.assertEqual(db.pending_count(self.CHAT), 1)

    def test_not_enough_points(self):
        item = db.shop_add_item(self.CHAT, "马克杯", 30)
        self.give(10)
        got = shop.redeem(self.CHAT, user(), item)
        self.assertFalse(got.ok)
        self.assertIn("积分不够", got.reason)
        self.assertEqual(db.point_balance(self.CHAT, 1), 10, "失败不该扣分")

    def test_out_of_stock(self):
        item = db.shop_add_item(self.CHAT, "马克杯", 10, stock=1)
        self.give(100)
        self.assertTrue(shop.redeem(self.CHAT, user(), item).ok)
        second = shop.redeem(self.CHAT, user(), item)
        self.assertFalse(second.ok)
        self.assertIn("兑完", second.reason)
        self.assertEqual(db.point_balance(self.CHAT, 1), 90)

    def test_unlimited_stock(self):
        item = db.shop_add_item(self.CHAT, "口头表扬", 1, stock=None)
        self.give(10)
        for _ in range(5):
            self.assertTrue(shop.redeem(self.CHAT, user(), item).ok)
        self.assertIsNone(db.shop_item(item).stock)

    def test_per_user_limit(self):
        item = db.shop_add_item(self.CHAT, "限兑一次", 5, per_user_limit=1)
        self.give(100)
        self.assertTrue(shop.redeem(self.CHAT, user(), item).ok)
        again = shop.redeem(self.CHAT, user(), item)
        self.assertFalse(again.ok)
        self.assertIn("限兑", again.reason)
        self.give(100, uid=2)
        self.assertTrue(
            shop.redeem(self.CHAT, user(2, "乙", "b"), item).ok,
            "限兑是按人算的，别人还能兑",
        )

    def test_disabled_item(self):
        item = db.shop_add_item(self.CHAT, "下架的", 5)
        db.shop_update_item(item, enabled=False)
        self.give(100)
        self.assertIn("下架", shop.redeem(self.CHAT, user(), item).reason)

    def test_other_chat_item_is_invisible(self):
        item = db.shop_add_item(-999, "别群的", 5)
        self.give(100)
        self.assertFalse(shop.redeem(self.CHAT, user(), item).ok)

    def test_mark_done(self):
        item = db.shop_add_item(self.CHAT, "马克杯", 10)
        self.give(100)
        rid = shop.redeem(self.CHAT, user(), item).redemption_id
        self.assertEqual(shop.mark_done(rid), (True, ""))
        self.assertEqual(db.redemption(rid).status, db.STATUS_DONE)
        self.assertFalse(shop.mark_done(rid)[0], "不能重复处理")

    def test_refund_restores_points_and_stock(self):
        item = db.shop_add_item(self.CHAT, "马克杯", 30, stock=1)
        self.give(50)
        rid = shop.redeem(self.CHAT, user(), item).redemption_id
        self.assertEqual(db.point_balance(self.CHAT, 1), 20)
        self.assertEqual(db.shop_item(item).stock, 0)

        self.assertEqual(shop.refund(rid), (True, ""))
        self.assertEqual(db.point_balance(self.CHAT, 1), 50, "积分要退回")
        self.assertEqual(db.shop_item(item).stock, 1, "库存要加回")
        self.assertEqual(db.redemption(rid).status, db.STATUS_REFUNDED)

    def test_refund_frees_per_user_limit(self):
        item = db.shop_add_item(self.CHAT, "限兑一次", 5, per_user_limit=1)
        self.give(100)
        rid = shop.redeem(self.CHAT, user(), item).redemption_id
        shop.refund(rid)
        self.assertTrue(shop.redeem(self.CHAT, user(), item).ok, "退单后应该能再兑")


class TestCodes(ShopBase):
    def test_code_is_delivered_immediately(self):
        item = db.shop_add_item(self.CHAT, "会员卡", 20, db.KIND_CODE)
        self.assertEqual(db.shop_add_codes(item, ["AAA-111", " BBB-222 ", "", "CCC-333"]), 3)
        self.give(100)

        got = shop.redeem(self.CHAT, user(), item)
        self.assertTrue(got.ok)
        self.assertEqual(got.code, "AAA-111")
        self.assertEqual(db.redemption(got.redemption_id).status, db.STATUS_DELIVERED)
        self.assertEqual(db.pending_count(self.CHAT), 0, "卡密不进待发货队列")
        self.assertEqual(db.shop_free_codes(item), 2)

    def test_each_code_used_once(self):
        item = db.shop_add_item(self.CHAT, "会员卡", 1, db.KIND_CODE)
        db.shop_add_codes(item, ["X1", "X2"])
        self.give(100, uid=1)
        self.give(100, uid=2)
        codes = {shop.redeem(self.CHAT, user(i), item).code for i in (1, 2)}
        self.assertEqual(codes, {"X1", "X2"})

    def test_no_codes_left(self):
        item = db.shop_add_item(self.CHAT, "会员卡", 1, db.KIND_CODE)
        db.shop_add_codes(item, ["ONLY"])
        self.give(100, uid=1)
        self.give(100, uid=2)
        shop.redeem(self.CHAT, user(1), item)
        second = shop.redeem(self.CHAT, user(2), item)
        self.assertFalse(second.ok)
        self.assertEqual(db.point_balance(self.CHAT, 2), 100, "取不到码不能扣分")

    def test_code_item_with_no_codes_does_not_charge(self):
        item = db.shop_add_item(self.CHAT, "还没导码", 10, db.KIND_CODE)
        self.give(100)
        got = shop.redeem(self.CHAT, user(), item)
        self.assertFalse(got.ok)
        self.assertEqual(db.point_balance(self.CHAT, 1), 100, "取不到码就不能扣分")

    def test_available_stock_reads_code_pool(self):
        item_id = db.shop_add_item(self.CHAT, "会员卡", 1, db.KIND_CODE)
        db.shop_add_codes(item_id, ["A", "B", "C"])
        self.assertEqual(shop.available_stock(db.shop_item(item_id)), 3)


if __name__ == "__main__":
    unittest.main()
