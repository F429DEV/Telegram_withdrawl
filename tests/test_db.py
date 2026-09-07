"""数据层测试。"""

import tempfile
import unittest
from pathlib import Path

from lottery import db


class TestDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    def test_draft_lifecycle(self):
        gid = db.create_draft(-100, 5, {"prize": "会员卡", "winners_count": 3})
        g = db.get(gid)
        self.assertEqual(g.status, db.STATUS_DRAFT)
        self.assertEqual(g.prize, "会员卡")
        self.assertEqual(g.winners_count, 3)

        db.update(gid, status=db.STATUS_ACTIVE, message_id=42)
        self.assertEqual(len(db.active_in_chat(-100)), 1)
        self.assertEqual(db.get(gid).message_id, 42)

    def test_one_draft_per_creator(self):
        db.create_draft(-100, 5)
        db.create_draft(-100, 5)
        rows = db.conn().execute(
            "SELECT COUNT(*) c FROM giveaways WHERE status='draft'"
        ).fetchone()
        self.assertEqual(rows["c"], 1)

    def test_participants(self):
        gid = db.create_draft(-100, 5, {"prize": "x"})
        self.assertTrue(db.add_participant(gid, 1, "a", "甲"))
        self.assertFalse(db.add_participant(gid, 1, "a", "甲"))
        self.assertEqual(db.participant_count(gid), 1)
        self.assertTrue(db.is_participant(gid, 1))

        db.bump_weight(gid, 1)
        db.bump_weight(gid, 1)
        db.bump_weight(gid, 1)
        self.assertEqual(db.participants(gid)[0].weight, 4)

        self.assertTrue(db.remove_participant(gid, 1))
        self.assertEqual(db.participant_count(gid), 0)

    def test_winners_roundtrip(self):
        gid = db.create_draft(-100, 5, {"prize": "x"})
        db.add_participant(gid, 1, "a", "甲")
        db.add_participant(gid, 2, "b", "乙")
        db.save_winners(gid, db.participants(gid))
        self.assertEqual([w.user_id for w in db.winners(gid)], [1, 2])

    def test_require_channels_json(self):
        gid = db.create_draft(-100, 5)
        db.update(gid, require_channels=["@a", "@b"])
        self.assertEqual(db.get(gid).require_channels, ["@a", "@b"])

    def test_chat_settings(self):
        self.assertTrue(db.chat_settings(-100)["admin_only"])
        db.save_chat_settings(-100, admin_only=False, default_channels=["@c"])
        s = db.chat_settings(-100)
        self.assertFalse(s["admin_only"])
        self.assertEqual(s["default_channels"], ["@c"])

    def test_user_seen(self):
        db.touch_user(-100, 9)
        db.touch_user(-100, 9)
        self.assertIsNotNone(db.first_seen(-100, 9))
        row = db.conn().execute(
            "SELECT msg_count FROM user_seen WHERE chat_id=-100 AND user_id=9"
        ).fetchone()
        self.assertEqual(row["msg_count"], 2)


if __name__ == "__main__":
    unittest.main()


class TestKeywords(unittest.TestCase):
    """多口令：设一组词，发中任意一个都算参与。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "k.db")

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    def test_clean_dedupes_and_limits(self):
        words = db.clean_keywords([" 抽 ", "抽", "", "发财", "ABC", "abc", "x" * 50])
        self.assertEqual(words, ["抽", "发财", "ABC", "x" * db.KEYWORD_MAX_LEN])

    def test_clean_caps_count(self):
        self.assertEqual(len(db.clean_keywords([str(i) for i in range(50)])), db.KEYWORD_LIMIT)

    def test_roundtrip_multiple(self):
        gid = db.create_draft(-100, 1, {"prize": "x", "keyword": ["抽", "发财", "666"]})
        self.assertEqual(db.get(gid).keywords, ["抽", "发财", "666"])
        db.update(gid, keyword=["只剩一个"])
        self.assertEqual(db.get(gid).keywords, ["只剩一个"])
        db.update(gid, keyword=None)
        self.assertEqual(db.get(gid).keywords, [])

    def test_backward_compat_with_plain_text(self):
        """老版本存的是裸字符串，升级后要还能读出来。"""
        gid = db.create_draft(-100, 1, {"prize": "x"})
        db.conn().execute("UPDATE giveaways SET keyword='抽' WHERE id=?", (gid,))
        db.conn().commit()
        self.assertEqual(db.get(gid).keywords, ["抽"])

    def test_matching(self):
        words = ["抽", "发财", "GoGo"]
        self.assertEqual(db.matches_keyword(words, "抽"), "抽")
        self.assertEqual(db.matches_keyword(words, "  发财 "), "发财")
        self.assertEqual(db.matches_keyword(words, "gogo"), "GoGo")   # 英文忽略大小写
        self.assertIsNone(db.matches_keyword(words, "我要抽奖"))        # 夹在句子里不算
        self.assertIsNone(db.matches_keyword(words, "抽抽"))
        self.assertIsNone(db.matches_keyword(words, ""))
        self.assertIsNone(db.matches_keyword([], "抽"))


class TestInvites(unittest.TestCase):
    """邀请记录：一个人在一个群只算一次，自己邀请自己不算。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "i.db")

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    def test_record_and_count(self):
        self.assertTrue(db.record_invite(-100, 2, 1))
        self.assertTrue(db.record_invite(-100, 3, 1))
        self.assertEqual(db.invite_count(-100, 1), 2)
        self.assertEqual(db.invite_count(-100, 9), 0)

    def test_same_invitee_counted_once(self):
        """踢出去再拉回来不能刷权重。"""
        self.assertTrue(db.record_invite(-100, 2, 1))
        self.assertFalse(db.record_invite(-100, 2, 1))
        self.assertFalse(db.record_invite(-100, 2, 5))   # 换个人拉也不再计
        self.assertEqual(db.invite_count(-100, 1), 1)

    def test_self_invite_ignored(self):
        self.assertFalse(db.record_invite(-100, 1, 1))
        self.assertEqual(db.invite_count(-100, 1), 0)

    def test_scoped_by_chat(self):
        db.record_invite(-100, 2, 1)
        db.record_invite(-200, 2, 1)
        self.assertEqual(db.invite_count(-100, 1), 1)
        self.assertEqual(db.invite_count(-200, 1), 1)

    def test_since_window(self):
        db.record_invite(-100, 2, 1)
        db.conn().execute("UPDATE invites SET created_at=1000 WHERE invitee_id=2")
        db.conn().commit()
        db.record_invite(-100, 3, 1)
        self.assertEqual(db.invite_count(-100, 1), 2)
        self.assertEqual(db.invite_count(-100, 1, since=2000), 1)

    def test_points_weight_uncapped(self):
        gid = db.create_draft(-100, 1, {"prize": "x", "mode": db.MODE_POINTS})
        db.add_participant(gid, 7, None, "话痨")
        for _ in range(200):
            db.bump_weight(gid, 7)
        self.assertEqual(db.participants(gid)[0].weight, 201)

    def test_migration_adds_invite_weight(self):
        """老库没有 invite_weight 列，init 时要能补上。"""
        db.conn().close()
        db._conn = None
        path = Path(self.tmp.name) / "old.db"
        import sqlite3
        raw = sqlite3.connect(str(path))
        raw.executescript(db.SCHEMA.replace(
            "    invite_weight       INTEGER NOT NULL DEFAULT 0,   -- 每邀请 1 人加多少权重，0=不计\n", ""
        ))
        raw.commit()
        raw.close()
        db.init(path)
        cols = {r["name"] for r in db.conn().execute("PRAGMA table_info(giveaways)")}
        self.assertIn("invite_weight", cols)
