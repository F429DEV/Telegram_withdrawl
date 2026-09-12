"""群消息自动清理：该删的删，该留的留。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram.constants import ChatType

from lottery import db, ephemeral, service


class FakeJob:
    def __init__(self, name, data):
        self.name = name
        self.data = data
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, cb, when, data=None, name=None):
        job = FakeJob(name, data)
        self.jobs.append(job)
        return job

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]

    def alive(self):
        return [j for j in self.jobs if not j.removed]


def fake_message(chat_id, message_id, chat_type=ChatType.SUPERGROUP):
    return SimpleNamespace(
        chat_id=chat_id,
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type=chat_type),
    )


class TestEphemeral(unittest.TestCase):
    def setUp(self):
        self.jq = FakeJobQueue()
        ephemeral.bind(SimpleNamespace(job_queue=self.jq), 30)

    def tearDown(self):
        ephemeral.bind(SimpleNamespace(job_queue=None), 0)

    def test_group_message_is_scheduled(self):
        ephemeral.track(fake_message(-100, 5))
        self.assertEqual(len(self.jq.alive()), 1)
        self.assertEqual(self.jq.alive()[0].data, {"chat_id": -100, "message_id": 5})

    def test_private_message_is_not_scheduled(self):
        ephemeral.track(fake_message(777, 5, ChatType.PRIVATE))
        self.assertEqual(self.jq.alive(), [])

    def test_keep_cancels(self):
        msg = fake_message(-100, 5)
        ephemeral.track(msg)
        ephemeral.keep(msg)
        self.assertEqual(self.jq.alive(), [], "keep 之后不该还留着删除任务")

    def test_cancel_only_targets_that_message(self):
        ephemeral.track(fake_message(-100, 5))
        ephemeral.track(fake_message(-100, 6))
        ephemeral.cancel(-100, 5)
        left = self.jq.alive()
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0].data["message_id"], 6)

    def test_disabled_schedules_nothing(self):
        ephemeral.bind(SimpleNamespace(job_queue=self.jq), 0)
        ephemeral.track(fake_message(-100, 5))
        self.assertEqual(self.jq.alive(), [])

    def test_no_job_queue_is_harmless(self):
        ephemeral.bind(SimpleNamespace(job_queue=None), 30)
        ephemeral.track(fake_message(-100, 5))   # 不应抛异常
        ephemeral.cancel(-100, 5)


class SchedulingBot:
    """模拟 EphemeralBot：发出去的群消息都排一个删除任务。"""

    def __init__(self):
        self.next_id = 1000
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.next_id += 1
        self.sent.append(text)
        msg = fake_message(chat_id, self.next_id)
        ephemeral.track(msg)
        return msg

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        return fake_message(chat_id, message_id)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member")


class TestExemptions(unittest.IsolatedAsyncioTestCase):
    """抽奖卡片和开奖结果必须活下来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "e.db")
        self.jq = FakeJobQueue()
        self.app = SimpleNamespace(job_queue=None)
        ephemeral.bind(SimpleNamespace(job_queue=self.jq), 30)
        self.bot = SchedulingBot()

    def tearDown(self):
        ephemeral.bind(SimpleNamespace(job_queue=None), 0)
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    async def test_card_and_result_survive(self):
        gid = db.create_draft(-100, 1, {"prize": "会员卡", "winners_count": 1})
        g = db.get(gid)
        await service.publish(self.bot, self.app, g)
        self.assertEqual(self.jq.alive(), [], "抽奖卡片不该被排进删除队列")

        db.add_participant(gid, 7, None, "甲")
        await service.finish(self.bot, gid)
        self.assertEqual(self.jq.alive(), [], "开奖结果不该被排进删除队列")

    async def test_cancel_notice_is_ephemeral(self):
        gid = db.create_draft(-100, 1, {"prize": "会员卡"})
        await service.publish(self.bot, self.app, db.get(gid))
        await service.cancel(self.bot, gid)
        alive = self.jq.alive()
        self.assertEqual(len(alive), 1, "取消通知属于普通消息，应该 30 秒后删掉")


if __name__ == "__main__":
    unittest.main()
