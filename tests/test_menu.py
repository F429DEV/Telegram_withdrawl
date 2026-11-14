"""按钮菜单：分层显示 + 每次点击都重新校验权限。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram.constants import ChatType

from lottery import db, ephemeral
from lottery.handlers import menu

CHAT = -1001234567890
MEMBER, ADMIN, OWNER = 1, 2, 3


class FakeQuery:
    def __init__(self, data, user_id, message):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, username="u", full_name="某人")
        self.message = message
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)


class FakeMessage:
    def __init__(self, chat_id=CHAT, chat_type=ChatType.SUPERGROUP):
        self.chat_id = chat_id
        self.message_id = 500
        self.chat = SimpleNamespace(id=chat_id, type=chat_type, title="群")
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return FakeMessage(self.chat_id, self.chat.type)


class FakeBot:
    """get_chat_member 决定谁是管理员。"""

    def __init__(self, admins=()):
        self.admins = set(admins)

    async def get_chat_member(self, chat_id, user_id):
        status = "administrator" if user_id in self.admins else "member"
        return SimpleNamespace(status=status)

    async def get_chat(self, chat_id):
        return SimpleNamespace(title="抽奖测试群")


def make_context(bot, owner_ids=()):
    return SimpleNamespace(
        bot=bot,
        args=[],
        application=SimpleNamespace(
            job_queue=None,
            bot_data={"config": SimpleNamespace(owner_ids=set(owner_ids))},
        ),
    )


def make_update(user_id, message, query=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="u", full_name="某人"),
        effective_chat=message.chat,
        effective_message=message,
        callback_query=query,
    )


class MenuBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.init(Path(self.tmp.name) / "m.db")
        db.set_timezone("Asia/Shanghai")
        ephemeral.bind(SimpleNamespace(job_queue=None), 0)
        eligibility_cache()

    def tearDown(self):
        db.conn().close()
        db._conn = None
        self.tmp.cleanup()

    def labels(self, kb):
        return [b.text for row in kb.inline_keyboard for b in row]


def eligibility_cache():
    """清掉管理员判断的缓存，免得测试之间互相污染。"""
    from lottery import eligibility

    eligibility._admin_cache.clear()


class TestTiers(MenuBase):
    def test_member_sees_no_admin_buttons(self):
        msg = FakeMessage()
        ctx = make_context(FakeBot(admins=[ADMIN]))
        asyncio.run(menu.menu_cmd(make_update(MEMBER, msg), ctx))
        kb_labels = self.labels(menu._group_kb("member"))
        self.assertNotIn("➕ 发起抽奖", kb_labels)
        self.assertNotIn("⚙️ 群设置", kb_labels)
        self.assertIn("📅 签到", kb_labels)

    def test_admin_sees_admin_buttons(self):
        kb_labels = self.labels(menu._group_kb("admin"))
        self.assertIn("➕ 发起抽奖", kb_labels)
        self.assertIn("⚙️ 群设置", kb_labels)

    def test_owner_tools_never_in_group_menu(self):
        """主人工具对群成员应该连存在都看不出来。"""
        for tier in ("member", "admin", "owner"):
            labels = " ".join(self.labels(menu._group_kb(tier)))
            self.assertNotIn("主人", labels, tier)
            self.assertNotIn("预留", labels, tier)
            self.assertNotIn("调分", labels, tier)

    def test_owner_tools_only_in_private_for_owner(self):
        self.assertIn("👑 主人工具", self.labels(menu._private_kb("owner")))
        self.assertNotIn("👑 主人工具", self.labels(menu._private_kb("member")))
        self.assertNotIn("👑 主人工具", self.labels(menu._private_kb("admin")))

    def test_tier_resolution(self):
        bot = FakeBot(admins=[ADMIN])
        msg = FakeMessage()
        for uid, want in ((MEMBER, "member"), (ADMIN, "admin"), (OWNER, "owner")):
            eligibility_cache()
            ctx = make_context(bot, owner_ids=[OWNER])
            got = asyncio.run(menu._tier(make_update(uid, msg), ctx))
            self.assertEqual(got, want, uid)

    def test_private_chat_member_is_never_admin(self):
        msg = FakeMessage(chat_id=MEMBER, chat_type=ChatType.PRIVATE)
        ctx = make_context(FakeBot(admins=[MEMBER]), owner_ids=[])
        self.assertEqual(asyncio.run(menu._tier(make_update(MEMBER, msg), ctx)), "member")


class TestCallbackPermissions(MenuBase):
    def _click(self, data, user_id, admins=(), owners=(), private=False):
        msg = (
            FakeMessage(chat_id=user_id, chat_type=ChatType.PRIVATE) if private
            else FakeMessage()
        )
        query = FakeQuery(data, user_id, msg)
        ctx = make_context(FakeBot(admins=admins), owner_ids=owners)
        upd = make_update(user_id, msg, query)
        asyncio.run(menu.menu_callback(upd, ctx))
        return query, msg

    def test_member_clicking_admin_button_is_blocked(self):
        """管理员开出来的菜单留在群里，别人点了也不能越权。"""
        for action in ("m:new", "m:settings"):
            query, msg = self._click(action, MEMBER, admins=[ADMIN])
            self.assertTrue(query.answers[-1][1], "应该是弹窗警告")
            self.assertEqual(msg.replies, [], action)

    def test_admin_clicking_admin_button_works(self):
        query, msg = self._click("m:settings", ADMIN, admins=[ADMIN])
        self.assertTrue(msg.replies, "应该真的把设置面板发出来")
        self.assertIn("群设置", msg.replies[0])

    def test_member_cannot_open_owner_tools(self):
        query, msg = self._click(
            "m:owner", ADMIN, admins=[ADMIN], owners=[OWNER], private=True
        )
        self.assertTrue(query.answers[-1][1])
        self.assertEqual(query.edits, [])

    def test_owner_opens_owner_tools_in_private(self):
        query, _ = self._click("m:owner", OWNER, owners=[OWNER], private=True)
        self.assertTrue(query.edits)
        self.assertIn("主人工具", query.edits[0])

    def test_owner_tools_do_nothing_in_a_group(self):
        """哪怕是主人自己在群里点，也不该有任何回显 —— 群里不暴露这些工具。"""
        for action in ("m:owner", "m:reserve", "m:adjust", "m:ledger"):
            query, msg = self._click(action, OWNER, owners=[OWNER])
            self.assertEqual(query.edits, [], action)
            self.assertEqual(msg.replies, [], action)

    def test_owner_tool_buttons_reach_the_real_handlers(self):
        db.create_draft(CHAT, OWNER, {"prize": "会员卡"})
        for action, expect in (
            ("m:reserve", "预留"),
            ("m:adjust", "调整"),
            ("m:ledger", "ledger"),
        ):
            query, msg = self._click(action, OWNER, owners=[OWNER], private=True)
            body = " ".join(msg.replies)
            self.assertIn(expect, body, action)

    def test_member_buttons_work_for_everyone(self):
        db.save_point_settings(CHAT, enabled=True, checkin_points=5)
        query, msg = self._click("m:checkin", MEMBER)
        self.assertTrue(msg.replies)
        self.assertIn("签到", msg.replies[0])

    def test_unknown_action_is_harmless(self):
        query, msg = self._click("m:nonsense", MEMBER)
        self.assertEqual(msg.replies, [])


if __name__ == "__main__":
    unittest.main()


class TestOwnerBoard(MenuBase):
    """主人私聊的常驻键盘。"""

    def _send(self, text, user_id, owners=(), private=True):
        msg = (
            FakeMessage(chat_id=user_id, chat_type=ChatType.PRIVATE) if private
            else FakeMessage()
        )
        msg.text = text
        msg.markups = []
        orig = msg.reply_text

        async def reply_text(body, **kw):
            msg.markups.append(kw.get("reply_markup"))
            return await orig(body, **kw)

        msg.reply_text = reply_text
        ctx = make_context(FakeBot(), owner_ids=owners)
        asyncio.run(menu.owner_board_message(make_update(user_id, msg), ctx))
        return msg

    def test_layout_has_the_three_tools(self):
        labels = [b.text for row in menu.owner_board().keyboard for b in row]
        self.assertIn(menu.BTN_RESERVE, labels)
        self.assertIn(menu.BTN_ADJUST, labels)
        self.assertIn(menu.BTN_LEDGER, labels)
        self.assertTrue(menu.owner_board().is_persistent, "要常驻，不能按一次就消失")

    def test_owner_pressing_a_button_reaches_the_handler(self):
        db.create_draft(CHAT, OWNER, {"prize": "会员卡"})
        msg = self._send(menu.BTN_RESERVE, OWNER, owners=[OWNER])
        self.assertTrue(msg.replies)
        self.assertIn("预留", " ".join(msg.replies))

    def test_non_owner_pressing_the_same_text_gets_nothing(self):
        msg = self._send(menu.BTN_ADJUST, MEMBER, owners=[OWNER])
        self.assertEqual(msg.replies, [], "别人原样打这几个字也不该有反应")

    def test_group_message_is_ignored(self):
        """常驻键盘只装在私聊；群里出现同样的文字一律不理。"""
        msg = self._send(menu.BTN_ADJUST, OWNER, owners=[OWNER], private=False)
        self.assertEqual(msg.replies, [])

    def test_unrelated_text_is_ignored(self):
        msg = self._send("今天天气不错", OWNER, owners=[OWNER])
        self.assertEqual(msg.replies, [])

    def test_hide_removes_the_keyboard(self):
        from telegram import ReplyKeyboardRemove

        msg = self._send(menu.BTN_HIDE, OWNER, owners=[OWNER])
        self.assertTrue(msg.replies)
        self.assertIsInstance(msg.markups[-1], ReplyKeyboardRemove)

    def test_menu_command_installs_the_board_for_owner_only(self):
        from telegram import ReplyKeyboardMarkup

        for uid, owners, want in ((OWNER, [OWNER], True), (MEMBER, [OWNER], False)):
            msg = FakeMessage(chat_id=uid, chat_type=ChatType.PRIVATE)
            msg.markups = []
            orig = msg.reply_text

            async def reply_text(body, _m=msg, _o=orig, **kw):
                _m.markups.append(kw.get("reply_markup"))
                return await _o(body, **kw)

            msg.reply_text = reply_text
            ctx = make_context(FakeBot(), owner_ids=owners)
            asyncio.run(menu.menu_cmd(make_update(uid, msg), ctx))
            got = any(isinstance(m, ReplyKeyboardMarkup) for m in msg.markups)
            self.assertEqual(got, want, f"user={uid}")

    def test_board_never_installed_in_a_group(self):
        from telegram import ReplyKeyboardMarkup

        msg = FakeMessage()
        msg.markups = []
        orig = msg.reply_text

        async def reply_text(body, **kw):
            msg.markups.append(kw.get("reply_markup"))
            return await orig(body, **kw)

        msg.reply_text = reply_text
        ctx = make_context(FakeBot(), owner_ids=[OWNER])
        asyncio.run(menu.menu_cmd(make_update(OWNER, msg), ctx))
        self.assertFalse(
            any(isinstance(m, ReplyKeyboardMarkup) for m in msg.markups),
            "群里装常驻键盘等于把主人工具摆给所有人看",
        )
