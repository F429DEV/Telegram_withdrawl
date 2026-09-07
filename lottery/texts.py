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
    db.MODE_MANUAL: "管理员贴名单",
}

MODE_SHORT = {
    db.MODE_BUTTON: "按钮",
    db.MODE_KEYWORD: "口令",
    db.MODE_POINTS: "积分",
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
    prize = esc(g.prize) or "（未填写奖品）"
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
    parts.append(f"🏆 名额：{g.winners_count} 名")
    if g.invite_weight:
        parts.append(f"👥 每邀请 1 位新成员进群，中奖权重 +{g.invite_weight}")
    parts.extend(_conditions(g))
    parts.extend(_requirements(g))
    parts.append("")
    parts.append(f"✅ 当前参与：<b>{count}</b> 人")
    parts.append(f"<i>编号 #{g.id}</i>")
    return "\n".join(parts)


def result(g: db.Giveaway, wins: list[db.Participant], total: int) -> str:
    prize = esc(g.prize) or "（未填写奖品）"
    if not wins:
        return (
            f"🎁 <b>{prize}</b>\n\n"
            f"😶 参与人数不足，本次没有产生中奖者。\n"
            f"<i>编号 #{g.id}</i>"
        )
    lines = [f"🎉 <b>开奖：{prize}</b>", "", f"共 {total} 人参与，抽出 {len(wins)} 位："]
    for i, w in enumerate(wins, 1):
        at = f"（@{esc(w.username)}）" if w.username else ""
        lines.append(f"{i}. {user_link(w.user_id, w.full_name, w.username)} {at}")
    lines.append("")
    lines.append(f"🔐 随机种子：<code>{esc(g.seed)}</code>")
    lines.append(f"<i>编号 #{g.id}　用 /verify {g.id} 可复核开奖过程</i>")
    return "\n".join(lines)


def manual_result(prize: str, wins: list[str], total: int, seed: str) -> str:
    lines = [f"🎉 <b>开奖：{esc(prize)}</b>", "", f"从 {total} 个候选里抽出 {len(wins)} 位："]
    for i, name in enumerate(wins, 1):
        lines.append(f"{i}. {esc(name)}")
    lines.append("")
    lines.append(f"🔐 随机种子：<code>{esc(seed)}</code>")
    return "\n".join(lines)


# ---------------------------------------------------------------- 草稿卡片

def draft_card(g: db.Giveaway) -> str:
    prize = esc(g.prize) if g.prize else "<i>还没填，点「✏️ 奖品」设置</i>"
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
    "👋 我是群抽奖机器人。\n\n"
    "把我拉进群，给我「删除消息」和「置顶消息」权限（可选，但体验更好），"
    "然后群管理员发 /new 就能开抽奖了。\n\n"
    "发 /help 看全部命令。"
)

HELP = (
    "<b>🎁 抽奖机器人使用说明</b>\n\n"
    "<b>发起抽奖</b>\n"
    "/new — 打开配置面板，按按钮选玩法、名额、时长、门槛，最后点发布\n"
    "/new 奖品名 | 名额 | 时长 — 一行搞定，例：<code>/new 会员月卡 | 3 | 30m</code>\n"
    "　时长写法：30m / 2h / 1d，写 0 表示不限时\n"
    "/new 奖品 | 名额 | 时长 | 口令1 口令2 — 第四段写口令就直接开口令玩法，\n"
    "　例：<code>/new 会员月卡 | 3 | 30m | 抽 发财 666</code>，发中任意一个都算参与\n"
    "/pick 3 — 回复一条包含名单的消息（每行或逗号分隔一个），直接从名单里抽 3 个\n\n"
    "<b>管理进行中的抽奖</b>\n"
    "/list — 列出本群进行中的抽奖\n"
    "/view &lt;编号&gt; — 看某场抽奖的全部参数：玩法、名额、开奖条件、门槛、\n"
    "　邀请加成、参与人数、权重前 5、中奖者\n"
    "/end &lt;编号&gt; — 立即开奖\n"
    "/cancel &lt;编号&gt; — 取消抽奖\n"
    "/reroll &lt;编号&gt; — 重新抽（排除已中奖的人）\n"
    "/verify &lt;编号&gt; — 公示参与名单和随机种子，复核开奖是否公平\n\n"
    "<b>群设置</b>\n"
    "/settings — 谁能发起抽奖、默认要求加入哪些频道\n\n"
    "<b>玩法说明</b>\n"
    "• 按钮：发一条带按钮的消息，点一下就报名\n"
    "• 口令：在群里发指定口令即报名；可以一次设多个口令，发中任意一个都算\n"
    "• 积分：抽奖期间正常聊天自动参与，发言越多权重越高，不设上限\n"
    "• 名单：管理员贴一份名单，直接抽\n\n"
    "<b>邀请加成</b>\n"
    "创建时点「👥 邀请加成」，每把 1 个人拉进群，中奖权重就 +N。\n"
    "只算本场抽奖开始之后拉进来的人，同一个人只算一次（踢出再拉回来不重复计）。\n"
    "自己通过邀请链接进群不算别人邀请的。\n\n"
    "开奖用的是可验证的随机算法：种子会公示，任何人都能复算结果。"
)

NOT_GROUP = "这个命令要在群里用。把我加到群里，然后在群里发 /new。"
NOT_ADMIN = "只有群管理员能发起 / 管理抽奖。"
NO_DRAFT = "没找到你的草稿，重新发 /new 吧。"
PRIZE_PROMPT = "请<b>回复这条消息</b>，告诉我奖品是什么（60 秒内有效）。"
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
