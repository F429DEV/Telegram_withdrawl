"""用假的 Bot 跑一遍开奖流程，确认各模块拼得起来。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram.error import BadRequest

from lottery import db, service


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self._next_id = 100

    async def send_message(self, chat_id, text, **kw):
        self._next_id += 1
        self.sent.append(text)
        return SimpleNamespace(message_id=self._next_id, text=text)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edited.append(text)
        return SimpleNamespace(message_id=message_id, text=text)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")


class BrokenEditBot(FakeBot):
    """模拟「抽奖卡片被人删了」：编辑一律报 message to edit not found。"""

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        raise BadRequest("Message to edit not found")


class FakeApp:
    job_queue = None
    bot_data: dict = {}


class TestFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "f.db")
        self.bot = FakeBot()
        self.app = FakeApp()

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    def _make(self, **fields):
        gid = db.create_draft(-100, 1, {"prize": "会员卡", "winners_count": 2, **fields})
        return db.get(gid)

    async def test_publish_and_finish(self):
        g = self._make()
        mid = await service.publish(self.bot, self.app, g)
        self.assertIsNotNone(mid)
        self.assertEqual(db.get(g.id).status, db.STATUS_ACTIVE)

        for uid in range(1, 6):
            db.add_participant(g.id, uid, f"u{uid}", f"用户{uid}")

        self.assertTrue(await service.finish(self.bot, g.id))
        g = db.get(g.id)
        self.assertEqual(g.status, db.STATUS_ENDED)
        self.assertIsNotNone(g.seed)
        self.assertEqual(len(db.winners(g.id)), 2)
        self.assertIn("开奖", self.bot.sent[-1])

        # 已结束的不能再开一次
        self.assertFalse(await service.finish(self.bot, g.id))

    async def test_finish_with_no_participants(self):
        g = self._make()
        await service.publish(self.bot, self.app, g)
        await service.finish(self.bot, g.id)
        self.assertEqual(db.winners(g.id), [])
        self.assertIn("没有产生中奖者", self.bot.sent[-1])

    async def test_cap_triggers_finish(self):
        g = self._make(max_participants=3)
        await service.publish(self.bot, self.app, g)
        for uid in range(1, 3):
            db.add_participant(g.id, uid, None, f"用户{uid}")
        self.assertFalse(await service.maybe_finish_by_cap(self.bot, self.app, db.get(g.id)))
        db.add_participant(g.id, 3, None, "用户3")
        self.assertTrue(await service.maybe_finish_by_cap(self.bot, self.app, db.get(g.id)))
        self.assertEqual(db.get(g.id).status, db.STATUS_ENDED)

    async def test_reroll_excludes_previous_winners(self):
        g = self._make(winners_count=1)
        await service.publish(self.bot, self.app, g)
        for uid in range(1, 6):
            db.add_participant(g.id, uid, None, f"用户{uid}")
        await service.finish(self.bot, g.id)
        first = db.winners(g.id)[0].user_id
        self.assertTrue(await service.reroll(self.bot, g.id))
        self.assertNotEqual(db.winners(g.id)[0].user_id, first)

    async def test_cancel(self):
        g = self._make()
        await service.publish(self.bot, self.app, g)
        self.assertTrue(await service.cancel(self.bot, g.id))
        self.assertEqual(db.get(g.id).status, db.STATUS_CANCELLED)
        self.assertFalse(await service.finish(self.bot, g.id))

    async def test_points_mode_uses_weight(self):
        g = self._make(mode=db.MODE_POINTS, winners_count=1)
        await service.publish(self.bot, self.app, g)
        db.add_participant(g.id, 1, None, "话痨", weight=50)
        for uid in range(2, 12):
            db.add_participant(g.id, uid, None, f"潜水{uid}")
        await service.finish(self.bot, g.id)
        self.assertEqual(len(db.winners(g.id)), 1)

    async def test_username_requirement_filters(self):
        g = self._make(require_username=True, winners_count=5)
        await service.publish(self.bot, self.app, g)
        db.add_participant(g.id, 1, "has_name", "甲")
        db.add_participant(g.id, 2, None, "乙")
        await service.finish(self.bot, g.id)
        winners = [w.user_id for w in db.winners(g.id)]
        self.assertEqual(winners, [1])
        self.assertIn("被剔除", self.bot.sent[-1])


class TestDeletedCard(unittest.IsolatedAsyncioTestCase):
    """卡片被删之后，机器人应该自愈而不是一直刷 WARNING。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "d.db")
        self.app = FakeApp()

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    async def test_refresh_reposts_when_card_deleted(self):
        bot = BrokenEditBot()
        gid = db.create_draft(-100, 1, {"prize": "会员卡"})
        db.update(gid, status=db.STATUS_ACTIVE, message_id=555)
        service._last_edit.clear()

        await service.refresh_card(bot, db.get(gid), force=True)

        new_id = db.get(gid).message_id
        self.assertNotEqual(new_id, 555, "卡片没了应该补发一条新的")
        self.assertEqual(len(bot.sent), 1)

    async def test_repost_failure_clears_message_id(self):
        class DeadBot(BrokenEditBot):
            async def send_message(self, chat_id, text, **kw):
                raise BadRequest("Chat not found")

        bot = DeadBot()
        gid = db.create_draft(-100, 1, {"prize": "会员卡"})
        db.update(gid, status=db.STATUS_ACTIVE, message_id=555)
        service._last_edit.clear()

        await service.refresh_card(bot, db.get(gid), force=True)
        self.assertIsNone(db.get(gid).message_id, "补发也失败时应清掉 message_id，别再反复重试")

    async def test_finish_still_announces_when_card_deleted(self):
        bot = BrokenEditBot()
        gid = db.create_draft(-100, 1, {"prize": "会员卡", "winners_count": 1})
        db.update(gid, status=db.STATUS_ACTIVE, message_id=555)
        db.add_participant(gid, 1, "a", "甲")

        self.assertTrue(await service.finish(bot, gid))
        self.assertEqual(len(db.winners(gid)), 1)
        self.assertIn("开奖", bot.sent[-1])


if __name__ == "__main__":
    unittest.main()


class TestInviteWeight(unittest.IsolatedAsyncioTestCase):
    """邀请加成：拉的人越多，权重越高。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "iw.db")
        self.bot = FakeBot()
        self.app = FakeApp()

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    async def test_invite_weight_adds_up(self):
        gid = db.create_draft(-100, 1, {"prize": "x", "winners_count": 1})
        db.update(gid, invite_weight=3, status=db.STATUS_ACTIVE)
        db.add_participant(gid, 1, None, "拉了两个")
        db.add_participant(gid, 2, None, "没拉人")
        db.record_invite(-100, 11, 1)
        db.record_invite(-100, 12, 1)

        people, weighted = service.weighted_participants(db.get(gid))
        self.assertTrue(weighted)
        by_id = {p.user_id: p.weight for p in people}
        self.assertEqual(by_id[1], 1 + 2 * 3)   # 底分 1 + 两次邀请
        self.assertEqual(by_id[2], 1)

    async def test_invites_before_giveaway_do_not_count(self):
        db.record_invite(-100, 11, 1)
        db.conn().execute("UPDATE invites SET created_at=1000")
        db.conn().commit()
        gid = db.create_draft(-100, 1, {"prize": "x"})
        db.update(gid, invite_weight=5, status=db.STATUS_ACTIVE)
        db.add_participant(gid, 1, None, "老早就拉的")

        people, _ = service.weighted_participants(db.get(gid))
        self.assertEqual(people[0].weight, 1, "本场开始之前的邀请不该算进来")

    async def test_no_invite_weight_means_unweighted(self):
        gid = db.create_draft(-100, 1, {"prize": "x"})
        db.update(gid, status=db.STATUS_ACTIVE)
        db.add_participant(gid, 1, None, "甲")
        db.record_invite(-100, 11, 1)
        people, weighted = service.weighted_participants(db.get(gid))
        self.assertFalse(weighted)
        self.assertEqual(people[0].weight, 1)

    async def test_points_and_invites_stack(self):
        gid = db.create_draft(-100, 1, {"prize": "x", "mode": db.MODE_POINTS})
        db.update(gid, invite_weight=2, status=db.STATUS_ACTIVE)
        db.add_participant(gid, 1, None, "又说又拉", weight=10)
        db.record_invite(-100, 11, 1)
        people, weighted = service.weighted_participants(db.get(gid))
        self.assertTrue(weighted)
        self.assertEqual(people[0].weight, 12)

    async def test_finish_uses_invite_weight(self):
        gid = db.create_draft(-100, 1, {"prize": "x", "winners_count": 1})
        db.update(gid, invite_weight=10, status=db.STATUS_ACTIVE, message_id=1)
        db.add_participant(gid, 1, None, "拉了很多")
        for invitee in range(100, 130):
            db.record_invite(-100, invitee, 1)
        for uid in range(2, 12):
            db.add_participant(gid, uid, None, f"路人{uid}")
        await service.finish(self.bot, gid)
        self.assertEqual(len(db.winners(gid)), 1)


class TestReservations(unittest.IsolatedAsyncioTestCase):
    """预留名额：指定的人必中，剩下的名额照常随机抽。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "r.db")
        self.bot = FakeBot()
        self.app = FakeApp()

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    def _active(self, winners=3):
        gid = db.create_draft(-100, 1, {"prize": "会员卡", "winners_count": winners})
        db.update(gid, status=db.STATUS_ACTIVE, message_id=1)
        for uid in range(1, 21):
            db.add_participant(gid, uid, f"u{uid}", f"用户{uid}")
        return gid

    async def test_reserved_always_wins(self):
        gid = self._active()
        db.add_reservation(gid, 7, "u7", "内定的")
        await service.finish(self.bot, gid)
        winners = [w.user_id for w in db.winners(gid)]
        self.assertEqual(winners[0], 7, "预留的人应该排在中奖名单最前")
        self.assertEqual(len(winners), 3)
        self.assertEqual(len(set(winners)), 3, "预留的人不该被重复抽中")

    async def test_reserved_not_in_random_pool(self):
        """预留的人已经占了名额，不能再进随机池里被抽第二次。"""
        gid = self._active(winners=2)
        db.add_reservation(gid, 5, "u5", "内定的")
        await service.finish(self.bot, gid)
        winners = [w.user_id for w in db.winners(gid)]
        self.assertEqual(winners.count(5), 1)

    async def test_reserved_can_be_outsider(self):
        """给没参与的人预留也能生效。"""
        gid = self._active(winners=1)
        db.add_reservation(gid, 999, None, "群外的人")
        await service.finish(self.bot, gid)
        self.assertEqual([w.user_id for w in db.winners(gid)], [999])

    async def test_reservations_capped_at_winner_count(self):
        gid = self._active(winners=2)
        for uid in (11, 12, 13, 14):
            db.add_reservation(gid, uid, None, f"内定{uid}")
        await service.finish(self.bot, gid)
        winners = [w.user_id for w in db.winners(gid)]
        self.assertEqual(winners, [11, 12], "超出名额的预留不该生效")

    async def test_no_reservation_is_pure_random(self):
        gid = self._active()
        await service.finish(self.bot, gid)
        self.assertEqual(len(db.winners(gid)), 3)

    async def test_reroll_keeps_reserved(self):
        gid = self._active(winners=2)
        db.add_reservation(gid, 7, "u7", "内定的")
        await service.finish(self.bot, gid)
        first = [w.user_id for w in db.winners(gid)]
        self.assertEqual(first[0], 7)

        await service.reroll(self.bot, gid)
        second = [w.user_id for w in db.winners(gid)]
        self.assertEqual(second[0], 7, "重抽不该把预留的人抽掉")
        self.assertNotEqual(second[1], first[1], "随机那一半应该换人")


class TestMessageMode(unittest.IsolatedAsyncioTestCase):
    """消息抽奖：随机抽一条群消息，作者中奖。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "msg.db")
        self.bot = FakeBot()
        self.app = FakeApp()

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    def _make(self, winners=1):
        gid = db.create_draft(-100, 1, {"prize": "会员卡", "mode": db.MODE_MESSAGE,
                                        "winners_count": winners})
        db.update(gid, status=db.STATUS_ACTIVE, message_id=1)
        return gid

    async def test_winner_comes_from_a_recorded_message(self):
        gid = self._make()
        mid = 1000
        for uid in range(1, 6):
            db.add_participant(gid, uid, f"u{uid}", f"用户{uid}")
            for _ in range(3):
                mid += 1
                db.record_message(gid, mid, uid, f"u{uid}", f"用户{uid}", f"消息{mid}")
        await service.finish(self.bot, gid)
        wins = db.winners(gid)
        self.assertEqual(len(wins), 1)
        self.assertIn(wins[0].user_id, set(range(1, 6)))
        self.assertIn("💬", self.bot.sent[-1], "开奖消息应该引用中奖的那条消息")

    async def test_multiple_winners_are_distinct_people(self):
        gid = self._make(winners=3)
        mid = 2000
        for uid in range(1, 6):
            db.add_participant(gid, uid, None, f"用户{uid}")
            for _ in range(10):
                mid += 1
                db.record_message(gid, mid, uid, None, f"用户{uid}", "水")
        await service.finish(self.bot, gid)
        ids = [w.user_id for w in db.winners(gid)]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3, "同一个人不该占两个名额")

    async def test_more_messages_means_better_odds(self):
        """发 30 条的人，应该明显比只发 1 条的人容易中。"""
        hits = 0
        rounds = 200
        for r in range(rounds):
            gid = self._make()
            db.add_participant(gid, 1, None, "话痨")
            db.add_participant(gid, 2, None, "潜水")
            mid = r * 1000
            for _ in range(30):
                mid += 1
                db.record_message(gid, mid, 1, None, "话痨", "水")
            mid += 1
            db.record_message(gid, mid, 2, None, "潜水", "冒泡")
            await service.finish(self.bot, gid)
            if db.winners(gid)[0].user_id == 1:
                hits += 1
        self.assertGreater(hits / rounds, 0.8, f"话痨中奖率只有 {hits / rounds}")

    async def test_no_messages_means_no_winner(self):
        gid = self._make()
        db.add_participant(gid, 1, None, "只报名没说话")
        await service.finish(self.bot, gid)
        self.assertEqual(db.winners(gid), [])

    async def test_reserved_slot_still_wins_in_message_mode(self):
        gid = self._make(winners=2)
        mid = 3000
        for uid in range(1, 6):
            db.add_participant(gid, uid, None, f"用户{uid}")
            mid += 1
            db.record_message(gid, mid, uid, None, f"用户{uid}", "水")
        db.add_reservation(gid, 99, None, "内定的")
        await service.finish(self.bot, gid)
        ids = [w.user_id for w in db.winners(gid)]
        self.assertEqual(ids[0], 99)
        self.assertEqual(len(ids), 2)
