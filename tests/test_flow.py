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
