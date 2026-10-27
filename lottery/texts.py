"""所有面向用户的中文文案，集中放这里方便改口径。"""

from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import db

MODE_LABEL = {
    db.MODE_BUTTON: "点按钮报名",
    db.MODE_KEYWORD: "发口令报名",
    db.MODE_POINTS: "发言积分（说得越多机会越大）",
    db.MODE_MESSAGE: "消息抽奖（随机抽一条群消息，作者中奖）",
    db.MODE_MANUAL: "管理员贴名单",
}

MODE_SHORT = {
    db.MODE_BUTTON: "按钮",
    db.MODE_KEYWORD: "口令",
    db.MODE_POINTS: "积分",
    db.MODE_MESSAGE: "消息",
    db.MODE_MANUAL: "名单",
}

_TZ = "Asia/Shanghai"


def set_timezone(name: str) -> None:
    global _TZ
    try:
        ZoneInfo(name)
        _TZ = name
    except Exception:
        _TZ = "Asia/Shanghai"


def prize_label(g: db.Giveaway, placeholder: str = "（未填写奖品）") -> str:
    """奖品栏和积分奖品二选一，这里统一出一个展示用的名字（已转义）。"""
    if g.point_prize > 0:
        return f"{g.point_prize} 积分"
    return esc(g.prize) if g.prize else placeholder


def esc(s: Optional[str]) -> str:
    return html.escape(s or "", quote=False)


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(ZoneInfo(_TZ)).strftime(
        "%Y-%m-%d %H:%M"
    )


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def message_link(chat_id: int, message_id: int) -> Optional[str]:
    """超级群的消息深链：-1001234567890 -> https://t.me/c/1234567890/<mid>"""
    raw = str(chat_id)
    if not raw.startswith("-100"):
        return None
    return f"https://t.me/c/{raw[4:]}/{message_id}"


def user_link(user_id: int, full_name: str, username: Optional[str] = None) -> str:
    name = esc(full_name or (f"@{username}" if username else str(user_id)))
    return f'<a href="tg://user?id={user_id}">{name}</a>'


# ---------------------------------------------------------------- 抽奖卡片

def _conditions(g: db.Giveaway) -> list[str]:
    lines = []
    if g.end_at:
        remain = g.end_at - int(time.time())
        if remain > 0:
            lines.append(f"⏰ 开奖时间：{fmt_time(g.end_at)}（还剩 {fmt_duration(remain)}）")
        else:
            lines.append(f"⏰ 开奖时间：{fmt_time(g.end_at)}（即将开奖）")
    if g.max_participants:
        lines.append(f"👥 满 {g.max_participants} 人立即开奖")
    if not g.end_at and not g.max_participants:
        lines.append("🎬 由管理员手动开奖")
    return lines


def _requirements(g: db.Giveaway) -> list[str]:
    lines = []
    if g.require_channels:
        joined = "、".join(esc(c) for c in g.require_channels)
        lines.append(f"🔒 需先加入：{joined}")
    if g.require_username:
        lines.append("🔒 需设置 Telegram 用户名")
    if g.min_seen_hours:
        lines.append(f"🔒 需在本群出现满 {fmt_duration(g.min_seen_hours * 3600)}（防小号）")
    return lines


def card(g: db.Giveaway, count: int) -> str:
    prize = prize_label(g)
    parts = [f"🎁 <b>{prize}</b>", ""]
    if g.mode == db.MODE_BUTTON:
        parts.append("参与方式：点下面的「我要参与」按钮")
    elif g.mode == db.MODE_KEYWORD:
        if len(g.keywords) == 1:
            parts.append(f"参与方式：在本群发送口令 <code>{esc(g.keywords[0])}</code>")
        else:
            words = "　".join(f"<code>{esc(k)}</code>" for k in g.keywords)
            parts.append(f"参与方式：在本群发送下面<b>任意一个</b>口令即可参与")
            parts.append(f"🔑 {words}")
    elif g.mode == db.MODE_POINTS:
        parts.append("参与方式：在本群正常发言即自动参与，发言越多中奖权重越高（不设上限）")
    elif g.mode == db.MODE_MESSAGE:
        parts.append("参与方式：在本群正常聊天即可，开奖时随机抽一条消息，发这条的人中奖")
        parts.append("说得越多，被抽中的机会越大")
    parts.append(f"🏆 名额：{g.winners_count} 名")
    if g.point_prize > 0:
        parts.append(f"🏅 中奖者各得 <b>{g.point_prize}</b> 积分，开奖后直接记入本群积分")
    if g.invite_weight:
        parts.append(f"👥 每邀请 1 位新成员进群，中奖权重 +{g.invite_weight}")
    parts.extend(_conditions(g))
    parts.extend(_requirements(g))
    parts.append("")
    parts.append(f"✅ 当前参与：<b>{count}</b> 人")
    parts.append(f"<i>编号 #{g.id}</i>")
    return "\n".join(parts)


def result(
    g: db.Giveaway,
    wins: list[db.Participant],
    total: int,
    quotes: Optional[dict] = None,
    chat_id: Optional[int] = None,
) -> str:
    prize = prize_label(g)
    if not wins:
        return (
            f"🎁 <b>{prize}</b>\n\n"
            f"😶 参与人数不足，本次没有产生中奖者。\n"
            f"<i>编号 #{g.id}</i>"
        )
    quotes = quotes or {}
    if g.mode == db.MODE_MESSAGE:
        head = f"共 {total} 人参与，随机抽中这 {len(wins)} 条消息："
    else:
        head = f"共 {total} 人参与，抽出 {len(wins)} 位："
    lines = [f"🎉 <b>开奖：{prize}</b>", "", head]
    for i, w in enumerate(wins, 1):
        at = f"（@{esc(w.username)}）" if w.username else ""
        lines.append(f"{i}. {user_link(w.user_id, w.full_name, w.username)} {at}")
        quoted = quotes.get(w.user_id)
        if quoted is not None:
            excerpt = esc(quoted.excerpt).strip() or "（非文字消息）"
            link = message_link(chat_id, quoted.message_id) if chat_id else None
            tail = f' <a href="{link}">跳转</a>' if link else ""
            lines.append(f"　　💬 <i>{excerpt}</i>{tail}")
    if g.point_prize > 0:
        lines.append("")
        lines.append(f"🏅 每人 <b>{g.point_prize}</b> 积分已记入本群积分，发「我的积分」可查")
    lines.append("")
    lines.append(f"<i>编号 #{g.id}　用 /verify {g.id} 看结果</i>")
    return "\n".join(lines)


def manual_result(prize: str, wins: list[str], total: int, seed: str = "") -> str:
    lines = [f"🎉 <b>开奖：{esc(prize)}</b>", "", f"从 {total} 个候选里抽出 {len(wins)} 位："]
    for i, name in enumerate(wins, 1):
        lines.append(f"{i}. {esc(name)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 草稿卡片

def draft_card(g: db.Giveaway) -> str:
    prize = prize_label(g, "<i>还没填，点「✏️ 奖品」或「🏅 积分奖品」设置</i>")
    lines = [
        "🛠 <b>新建抽奖</b>（只有你能看到这些按钮的操作权限）",
        "",
        f"奖品：{prize}",
        f"玩法：{MODE_LABEL[g.mode]}",
        f"名额：{g.winners_count} 名",
    ]
    if g.mode == db.MODE_KEYWORD:
        if g.keywords:
            kw = "　".join(f"<code>{esc(k)}</code>" for k in g.keywords)
            if len(g.keywords) > 1:
                kw += f"（{len(g.keywords)} 个，发中任意一个都算）"
        else:
            kw = "<i>还没设置</i>"
        lines.append(f"口令：{kw}")
    if g.end_at:
        lines.append(f"时长：到 {fmt_time(g.end_at)} 自动开奖")
    else:
        lines.append("时长：不限时")
    lines.append(f"人数上限：{g.max_participants} 人自动开奖" if g.max_participants else "人数上限：不限")
    lines.append(f"邀请加成：每邀 1 人 +{g.invite_weight} 权重" if g.invite_weight else "邀请加成：关")
    if g.point_prize > 0:
        lines.append(f"<i>奖品是 {g.point_prize} 积分，开奖后自动入账。设文字奖品会把它清掉。</i>")
    reqs = _requirements(g)
    lines.append("门槛：" + ("；".join(r[2:] for r in reqs) if reqs else "无"))
    lines.append("")
    lines.append("设置好之后点「🚀 发布」。")
    return "\n".join(lines)


def requirement_card(g: db.Giveaway) -> str:
    channels = "、".join(esc(c) for c in g.require_channels) if g.require_channels else "无"
    return "\n".join(
        [
            "🔒 <b>参与门槛</b>",
            "",
            f"必须已加入：{channels}",
            f"必须有用户名：{'是' if g.require_username else '否'}",
            f"防小号：{'在本群出现满 ' + fmt_duration(g.min_seen_hours * 3600) if g.min_seen_hours else '不限'}",
            "",
            "<i>频道校验需要把机器人也加进那个频道/群，否则无法查询成员身份。</i>",
        ]
    )


# ---------------------------------------------------------------- 提示语

START_PRIVATE = (
    "👋 我是群机器人，目前有三个板块：\n\n"
    "🎁 <b>抽奖</b>　五种玩法，管理员发 /new 就能开\n"
    "🏅 <b>积分</b>　发言攒分、每日签到、群内排名\n"
    "🛒 <b>积分商城</b>　用积分换奖品\n\n"
    "把我拉进群，管理员发 /settings 配一下就能用。\n"
    "发 /help 看总览。"
)

HELP_OVERVIEW = (
    "🤖 <b>机器人功能总览</b>\n\n"
    "<b>🎁 抽奖</b>　五种玩法、三种开奖条件、可配门槛和邀请加成\n"
    "　/help 抽奖\n\n"
    "<b>🏅 积分</b>　发言攒分、每日签到、群内排名\n"
    "　群里直接发「签到」「我的积分」「排行榜」\n"
    "　/help 积分\n\n"
    "<b>🛒 积分商城</b>　用积分换奖品，卡密自动发货\n"
    "　群里直接发「积分商城」\n"
    "　/help 商城\n\n"
    "<b>其他</b>\n"
    "/viewmyinfo — 自己的 ID、本群记录、积分、历史中奖\n"
    "/invite — 拿自己的专属邀请链接（私信发给你）\n"
    "/settings — 群设置（管理员，三个板块都在里面）\n\n"
    "<i>机器人在群里发的消息 30 秒后自动清理，抽奖卡片和开奖结果除外。</i>"
)

HELP_LOTTERY = (
    "<b>🎁 抽奖</b>\n\n"
    "<b>发起</b>\n"
    "/new — 打开配置面板，按按钮选玩法、名额、时长、门槛，最后点发布\n"
    "/new 奖品名 | 名额 | 时长 — 一行搞定，例：<code>/new 会员月卡 | 3 | 30m</code>\n"
    "　时长写法：30m / 2h / 1d，写 0 表示不限时\n"
    "/new 奖品 | 名额 | 时长 | 口令1 口令2 — 第四段写口令就直接开口令玩法\n"
    "/pick 3 — 回复一条含名单的消息，从名单里抽 3 个\n\n"
    "<b>管理</b>\n"
    "/list — 本群进行中的抽奖\n"
    "/view &lt;编号&gt; — 某场的全部参数、完整参与名单、中奖者\n"
    "/end &lt;编号&gt; — 立即开奖　/cancel &lt;编号&gt; — 取消\n"
    "/reroll &lt;编号&gt; — 重抽（排除已中奖的人）\n"
    "/verify &lt;编号&gt; — 结果摘要\n\n"
    "<b>五种玩法</b>\n"
    "• 按钮：发一条带按钮的消息，点一下就报名\n"
    "• 口令：在群里发指定口令即报名；可设多个，发中任意一个都算\n"
    "• 积分：抽奖期间正常聊天自动参与，发言越多权重越高，不设上限\n"
    "• 消息：开奖时随机抽一条消息，发这条的人中奖\n"
    "• 名单：管理员贴一份名单，直接抽\n\n"
    "<b>奖品：文字 或 积分</b>\n"
    "「✏️ 奖品」写实物/虚拟奖品的名字，「🏅 积分奖品」直接发积分。\n"
    "两者<b>二选一</b>，设一个会把另一个清掉。积分奖品开奖后自动入账，\n"
    "走的是和签到、发言同一套积分，中奖者发「我的积分」就能看到。\n\n"
    "<b>邀请加成</b>\n"
    "创建时点「👥 邀请加成」，每邀请 1 人进群权重 +N。\n"
    "参与者发 /invite 拿专属链接，别人从那条链接进群才算数。\n\n"
    "<i>注意：抽奖里的「积分玩法」只数本场发言，和下面的积分板块是两套东西。</i>"
)

HELP_POINTS = (
    "<b>🏅 积分</b>\n\n"
    "积分<b>按群独立</b>，A 群攒的分只能在 A 群花。\n\n"
    "<b>怎么攒</b>\n"
    "• 发言 —— 每条有效发言给 N 分，管理员可调\n"
    "• 签到 —— 群里发「签到」或 /checkin，每天一次\n"
    "• 中抽奖 —— 抽奖的奖品设成「🏅 积分奖品」时，中奖者直接入账\n\n"
    "<b>怎么查</b>\n"
    "• 发「我的积分」或 /mypoints —— 余额、累计、群内排名、今日进度\n"
    "• 发「排行榜」或 /top —— 前 10 名\n\n"
    "<b>发言分有三道闸</b>（都在 /settings → 🏅 积分设置 里调）\n"
    "• 冷却：同一个人多久才能再拿一次\n"
    "• 每日上限：一天靠发言最多拿多少\n"
    "• 最短长度：太短的不给分\n"
    "另外纯表情、纯标点、和上一条重复的消息一律不给分，这条写死了。\n\n"
    "<i>攒够了发「积分商城」换东西。</i>"
)

HELP_SHOP = (
    "<b>🛒 积分商城</b>\n\n"
    "群里发「积分商城」或 /shop 看货架，点按钮兑换，再确认一次就扣分。\n\n"
    "<b>两类奖品</b>\n"
    "• 卡密 —— 管理员预先导入一批码，兑换后机器人<b>立刻私信</b>给你\n"
    "　（没跟机器人私聊过的话，码会用弹窗给你，记得马上复制）\n"
    "• 实物 —— 扣分后生成兑换单，管理员收到通知去发货\n\n"
    "<b>管理员</b>（/settings → 🛒 商城管理）\n"
    "• ➕ 上架奖品：<code>名称 | 积分 | 实物或卡密 | 库存</code>\n"
    "• 点奖品进去改价格、库存、每人限兑、上下架、导入卡密\n"
    "• 📦 待发货：发完点「已发货」，发不了点「退积分」把分退回去\n\n"
    "<i>兑换单退回时积分和库存都会还原。</i>"
)

HELP_SECTIONS = {
    "抽奖": HELP_LOTTERY, "lottery": HELP_LOTTERY, "draw": HELP_LOTTERY,
    "积分": HELP_POINTS, "points": HELP_POINTS,
    "商城": HELP_SHOP, "积分商城": HELP_SHOP, "shop": HELP_SHOP,
}

NOT_GROUP = "这个命令要在群里用。把我加到群里，然后在群里发 /new。"
NOT_ADMIN = "只有群管理员能发起 / 管理抽奖。"
NO_DRAFT = "没找到你的草稿，重新发 /new 吧。"
PRIZE_PROMPT = "请<b>回复这条消息</b>，告诉我奖品是什么（60 秒内有效）。"
POINT_PRIZE_PROMPT = (
    "请<b>回复这条消息</b>，输入每位中奖者能拿到的<b>积分</b>数，例如：<code>100</code>\n"
    "<i>开奖后自动记入本群积分，中奖者发「我的积分」就能看到。</i>\n"
    "<i>积分奖品和文字奖品二选一 —— 设了这个会把奖品栏清空。填 0 取消。</i>"
)
KEYWORD_PROMPT = (
    "请<b>回复这条消息</b>，输入参与口令。\n"
    "想设多个就用空格或逗号分开，发中<b>任意一个</b>都算参与，例如：\n"
    "<code>抽 发财 666</code>\n"
    "<i>最多 20 个，每个不超过 32 字。必须整条消息刚好等于某个口令才算，"
    "夹在句子里不会误报名。</i>"
)
CHANNEL_PROMPT = (
    "请<b>回复这条消息</b>，输入要求加入的频道/群，多个用空格分开，"
    "例如：<code>@my_channel @my_group</code>\n"
    "输入 <code>清空</code> 可以取消这项要求。"
)
WINNERS_PROMPT = "请<b>回复这条消息</b>，输入中奖名额（1-100 的数字）。"

# ================================================================ 积分板块

def _rank_label(rank: int, total: int) -> str:
    if rank <= 0:
        return "还没上榜"
    return f"第 {rank} 名 / 共 {total} 人"


def checkin_ok(name: str, got: int, balance: int, streak: int,
               rank: int, total: int) -> str:
    lines = [f"✅ <b>签到成功</b>　+{got} 分", ""]
    if streak > 1:
        lines.append(f"已连续签到 <b>{streak}</b> 天")
    lines.append(f"当前 <b>{balance}</b> 分　{_rank_label(rank, total)}")
    return "\n".join(lines)


def checkin_again(balance: int, streak: int, rank: int, total: int) -> str:
    tail = f"（连签 {streak} 天）" if streak > 1 else ""
    return (
        f"今天已经签过了，明天再来。{tail}\n"
        f"当前 <b>{balance}</b> 分　{_rank_label(rank, total)}"
    )


def points_card(
    name: str,
    w: Optional[db.Wallet],
    rank: int,
    total: int,
    settings: dict,
    today: dict,
    streak: int,
    redeemed: int,
) -> str:
    balance = w.balance if w else 0
    earned = w.total_earned if w else 0
    lines = [
        f"🏅 <b>{esc(name)} 的积分</b>",
        "",
        f"当前余额：<b>{balance}</b> 分",
        f"累计获得：{earned} 分",
        f"群内排名：{_rank_label(rank, total)}",
        "",
    ]
    cap = settings["daily_cap"]
    got_today = today["chat_earned"]
    lines.append(
        f"今日发言已得：{got_today} / {cap} 分" if cap > 0
        else f"今日发言已得：{got_today} 分（不限）"
    )
    if settings["checkin_points"] > 0:
        if today["checked_in"]:
            extra = f"（连签 {streak} 天）" if streak > 1 else ""
            lines.append(f"今日签到：已签{extra}")
        else:
            lines.append(f"今日签到：<b>还没签</b>，发「签到」拿 {settings['checkin_points']} 分")
    if redeemed:
        lines.append(f"累计兑换：{redeemed} 次")
    lines.append("")
    lines.append("<i>发「积分商城」看能换什么，发「排行榜」看谁最多</i>")
    return "\n".join(lines)


def points_top(rows: list, me_rank: int, me_balance: int, total: int) -> str:
    if not rows:
        return "🏆 <b>积分排行</b>\n\n本群还没有人有积分。发言和签到都能攒。"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"🏆 <b>积分排行</b>（共 {total} 人上榜）", ""]
    for i, w in enumerate(rows, 1):
        mark = medals.get(i, f"{i}.")
        lines.append(f"{mark} {esc(w.full_name) or w.user_id} — <b>{w.balance}</b> 分")
    lines.append("")
    lines.append(
        f"你：{_rank_label(me_rank, total)}，{me_balance} 分" if me_rank > 0
        else "你还没上榜，发言和签到都能攒分"
    )
    return "\n".join(lines)


def points_disabled() -> str:
    return "本群还没开启积分功能。管理员可以在 /settings → 🏅 积分设置 里打开。"


# ================================================================ 积分商城

def _stock_label(item: db.ShopItem, stock: Optional[int]) -> str:
    if stock is None:
        return "不限量"
    return f"剩 {stock}" if stock > 0 else "已兑完"


def _kind_label(item: db.ShopItem) -> str:
    return "卡密自动发" if item.is_code else "需人工发货"


def shop_card(balance: int, rows: list) -> str:
    """rows: [(ShopItem, 可兑数量 or None)]"""
    if not rows:
        return (
            "🛒 <b>积分商城</b>\n\n货架还是空的。\n"
            "<i>管理员可以在 /settings → 🛒 商城管理 里上架奖品。</i>"
        )
    lines = [f"🛒 <b>积分商城</b>", "", f"你有 <b>{balance}</b> 分", ""]
    for i, (item, stock) in enumerate(rows, 1):
        bits = [f"<b>{item.cost}</b> 分", _kind_label(item), _stock_label(item, stock)]
        if item.per_user_limit:
            bits.append(f"每人限 {item.per_user_limit} 次")
        lines.append(f"{i}. {esc(item.name)} — " + " · ".join(bits))
        if item.note:
            lines.append(f"　　<i>{esc(item.note)}</i>")
    lines.append("")
    lines.append("<i>点下面的按钮兑换</i>")
    return "\n".join(lines)


def redeem_confirm(mention: str, item: db.ShopItem, balance: int) -> str:
    return (
        f"{mention} 确认用 <b>{item.cost}</b> 分兑换「{esc(item.name)}」？\n"
        f"你当前 {balance} 分，兑换后剩 {balance - item.cost} 分。"
    )


def redeem_done_physical(rid: int, item: db.ShopItem, balance: int) -> str:
    return (
        f"✅ 兑换成功，已登记兑换单 <b>#{rid}</b>\n"
        f"「{esc(item.name)}」-{item.cost} 分，你还有 {balance} 分。\n"
        f"<i>管理员已收到通知，等发货就行。</i>"
    )


def redeem_done_code(item: db.ShopItem, balance: int) -> str:
    return (
        f"✅ 兑换成功，卡密已私信发给你\n"
        f"「{esc(item.name)}」-{item.cost} 分，你还有 {balance} 分。"
    )


def code_dm(item: db.ShopItem, code: str) -> str:
    return (
        f"🎟 <b>你兑换的「{esc(item.name)}」</b>\n\n"
        f"<code>{esc(code)}</code>\n\n"
        f"<i>这条消息不会自动删除，但建议你自己保存一份。</i>"
    )


def redemption_notice(record: db.Redemption, chat_title: str) -> str:
    who = user_link(record.user_id, record.full_name, record.username)
    at = f"（@{esc(record.username)}）" if record.username else ""
    return (
        f"📦 <b>新兑换单 #{record.id}</b>\n\n"
        f"群：{esc(chat_title) or record.chat_id}\n"
        f"奖品：{esc(record.item_name)}\n"
        f"消耗：{record.cost} 分\n"
        f"兑换人：{who}{at}　<code>{record.user_id}</code>\n\n"
        f"<i>发完货到 /settings → 🛒 商城管理 → 待处理 里标一下。</i>"
    )

