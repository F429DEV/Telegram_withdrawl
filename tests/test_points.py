"""积分板块：发言得分、三道防刷闸、签到、排名。"""

import tempfile
import unittest
from pathlib import Path

from telegram import User

from lottery import db, points


def user(uid=1, name="甲", username="a"):
    return User(id=uid, first_name=name, is_bot=False, username=username)


class PointsBase(unittest.TestCase):
    CHAT = -100

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "p.db")
        db.set_timezone("Asia/Shanghai")

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    def settings(self, **kw):
        return db.save_point_settings(self.CHAT, **kw)


class TestEarnFromMessage(PointsBase):
    def test_default_settings_give_points(self):
        got = points.earn_from_message(self.CHAT, user(), "今天天气不错")
        self.assertEqual(got.points, 1)
        self.assertEqual(got.balance, 1)
        self.assertEqual(db.point_balance(self.CHAT, 1), 1)

    def test_disabled_gives_nothing(self):
        self.settings(enabled=False)
        self.assertEqual(points.earn_from_message(self.CHAT, user(), "你好啊").points, 0)

    def test_per_message_amount(self):
        self.settings(per_message=5, cooldown_seconds=0)
        self.assertEqual(points.earn_from_message(self.CHAT, user(), "第一句").points, 5)
        self.assertEqual(points.earn_from_message(self.CHAT, user(), "第二句").points, 5)
        self.assertEqual(db.point_balance(self.CHAT, 1), 10)

    def test_cooldown_blocks_second_message(self):
        self.settings(cooldown_seconds=60)
        self.assertEqual(points.earn_from_message(self.CHAT, user(), "第一句").points, 1)
        second = points.earn_from_message(self.CHAT, user(), "第二句")
        self.assertEqual(second.points, 0)
        self.assertEqual(second.skipped, "冷却中")

    def test_daily_cap(self):
        self.settings(per_message=3, daily_cap=7, cooldown_seconds=0)
        earned = [points.earn_from_message(self.CHAT, user(), f"话{i}").points for i in range(4)]
        self.assertEqual(earned, [3, 3, 1, 0], "最后一次只补到上限，之后不再给")
        self.assertEqual(db.point_balance(self.CHAT, 1), 7)

    def test_too_short(self):
        self.settings(min_message_len=4, cooldown_seconds=0)
        self.assertEqual(points.earn_from_message(self.CHAT, user(), "嗯嗯").skipped, "太短")

    def test_emoji_only_gives_nothing(self):
        self.settings(cooldown_seconds=0, min_message_len=1)
        self.assertEqual(
            points.earn_from_message(self.CHAT, user(), "😂😂😂").skipped, "没有有效字符"
        )
        self.assertEqual(points.earn_from_message(self.CHAT, user(), "。。。！！").points, 0)

    def test_repeated_text_gives_nothing(self):
        self.settings(cooldown_seconds=0)
        self.assertEqual(points.earn_from_message(self.CHAT, user(), "复制粘贴").points, 1)
        again = points.earn_from_message(self.CHAT, user(), "  复制粘贴 ")
        self.assertEqual(again.skipped, "和上一条重复", "大小写和空白都该忽略")
        self.assertEqual(points.earn_from_message(self.CHAT, user(), "换一句话").points, 1)

    def test_trigger_words_do_not_earn(self):
        self.settings(cooldown_seconds=0, min_message_len=1)
        for word in ("签到", "我的积分", "积分商城", "排行榜"):
            self.assertEqual(
                points.earn_from_message(self.CHAT, user(), word).skipped,
                "触发词本身不给分",
                word,
            )

    def test_wallets_are_per_chat(self):
        self.settings(cooldown_seconds=0)
        db.save_point_settings(-200, cooldown_seconds=0)
        points.earn_from_message(self.CHAT, user(), "甲群说话")
        points.earn_from_message(-200, user(), "乙群说话")
        self.assertEqual(db.point_balance(self.CHAT, 1), 1)
        self.assertEqual(db.point_balance(-200, 1), 1)


class TestCheckIn(PointsBase):
    def test_first_checkin_pays(self):
        self.settings(checkin_points=10)
        got = points.check_in(self.CHAT, user())
        self.assertTrue(got.ok)
        self.assertEqual(got.points, 10)
        self.assertEqual(got.balance, 10)
        self.assertEqual(got.streak, 1)

    def test_second_checkin_same_day_is_rejected(self):
        self.settings(checkin_points=10)
        points.check_in(self.CHAT, user())
        again = points.check_in(self.CHAT, user())
        self.assertFalse(again.ok)
        self.assertEqual(again.reason, "今天已经签过了")
        self.assertEqual(db.point_balance(self.CHAT, 1), 10, "不该重复发分")

    def test_streak_counts_consecutive_days(self):
        self.settings(checkin_points=1)
        from datetime import date, timedelta

        today = date.fromisoformat(db.day_key())
        for back in (3, 2, 1):
            db.mark_checkin(self.CHAT, 1, (today - timedelta(days=back)).isoformat())
        points.check_in(self.CHAT, user())
        self.assertEqual(db.checkin_streak(self.CHAT, 1), 4)

    def test_streak_breaks_on_gap(self):
        self.settings(checkin_points=1)
        from datetime import date, timedelta

        today = date.fromisoformat(db.day_key())
        db.mark_checkin(self.CHAT, 1, (today - timedelta(days=5)).isoformat())
        points.check_in(self.CHAT, user())
        self.assertEqual(db.checkin_streak(self.CHAT, 1), 1)

    def test_disabled_group(self):
        self.settings(enabled=False)
        self.assertFalse(points.check_in(self.CHAT, user()).ok)


class TestRankAndLedger(PointsBase):
    def test_rank_and_total(self):
        db.add_points(self.CHAT, 1, 100, db.REASON_ADMIN, "a", "甲")
        db.add_points(self.CHAT, 2, 50, db.REASON_ADMIN, "b", "乙")
        db.add_points(self.CHAT, 3, 50, db.REASON_ADMIN, "c", "丙")
        self.assertEqual(db.point_rank(self.CHAT, 1), (1, 3))
        self.assertEqual(db.point_rank(self.CHAT, 2), (2, 3), "并列都算第 2")
        self.assertEqual(db.point_rank(self.CHAT, 3), (2, 3))
        self.assertEqual(db.point_rank(self.CHAT, 9), (0, 3), "没积分的没有名次")

    def test_top_order(self):
        for uid, amount in ((1, 10), (2, 30), (3, 20)):
            db.add_points(self.CHAT, uid, amount, db.REASON_ADMIN, None, f"用户{uid}")
        self.assertEqual([w.user_id for w in db.point_top(self.CHAT, 2)], [2, 3])

    def test_balance_never_goes_negative(self):
        db.add_points(self.CHAT, 1, 10, db.REASON_ADMIN)
        self.assertEqual(db.add_points(self.CHAT, 1, -999, db.REASON_REDEEM), 0)

    def test_total_earned_ignores_spending(self):
        db.add_points(self.CHAT, 1, 100, db.REASON_CHAT)
        db.add_points(self.CHAT, 1, -40, db.REASON_REDEEM)
        w = db.wallet(self.CHAT, 1)
        self.assertEqual((w.balance, w.total_earned), (60, 100))

    def test_ledger_records_every_move(self):
        db.add_points(self.CHAT, 1, 10, db.REASON_CHECKIN)
        db.add_points(self.CHAT, 1, -4, db.REASON_REDEEM)
        rows = db.conn().execute(
            "SELECT delta, reason, balance_after FROM point_logs "
            "WHERE chat_id=? AND user_id=? ORDER BY id", (self.CHAT, 1)
        ).fetchall()
        self.assertEqual([(r["delta"], r["reason"], r["balance_after"]) for r in rows],
                         [(10, db.REASON_CHECKIN, 10), (-4, db.REASON_REDEEM, 6)])


if __name__ == "__main__":
    unittest.main()
