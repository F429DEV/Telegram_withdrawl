"""内联键盘。callback_data 统一用 '前缀:抽奖id:动作[:参数]'。"""

from __future__ import annotations

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup

from . import db, texts

# 时长档位（秒），0 表示不限时
DURATIONS = [0, 300, 600, 1800, 3600, 6 * 3600, 12 * 3600, 24 * 3600, 3 * 24 * 3600]
# 人数上限档位，0 表示不限
CAPS = [0, 10, 20, 50, 100, 200, 500]
# 防小号档位（小时）
SEEN_HOURS = [0, 1, 6, 24, 72, 168]


def _dur_label(seconds: int) -> str:
    return "不限时" if not seconds else texts.fmt_duration(seconds)


def draft_kb(g: db.Giveaway, remaining: int | None) -> Markup:
    rows = [
        [Btn(f"🎮 玩法：{texts.MODE_SHORT[g.mode]}", callback_data=f"d:{g.id}:mode")],
        [
            Btn("➖", callback_data=f"d:{g.id}:win-"),
            Btn(f"🏆 名额 {g.winners_count}", callback_data=f"d:{g.id}:win?"),
            Btn("➕", callback_data=f"d:{g.id}:win+"),
        ],
        [
            Btn(f"⏰ 时长：{_dur_label(remaining or 0)}", callback_data=f"d:{g.id}:dur"),
            Btn(
                f"👥 上限：{g.max_participants or '不限'}",
                callback_data=f"d:{g.id}:cap",
            ),
        ],
    ]
    third = [Btn("✏️ 奖品", callback_data=f"d:{g.id}:prize")]
    if g.mode == db.MODE_KEYWORD:
        third.append(Btn("🔑 口令", callback_data=f"d:{g.id}:keyword"))
    third.append(Btn("🔒 门槛", callback_data=f"d:{g.id}:req"))
    rows.append(third)
    rows.append(
        [
            Btn("🚀 发布", callback_data=f"d:{g.id}:publish"),
            Btn("🗑 取消", callback_data=f"d:{g.id}:discard"),
        ]
    )
    return Markup(rows)


def requirement_kb(g: db.Giveaway) -> Markup:
    return Markup(
        [
            [Btn("📢 必须加入的频道/群", callback_data=f"r:{g.id}:channels")],
            [
                Btn(
                    f"🆔 必须有用户名：{'开' if g.require_username else '关'}",
                    callback_data=f"r:{g.id}:username",
                )
            ],
            [
                Btn(
                    f"🕒 防小号：{texts.fmt_duration(g.min_seen_hours * 3600) if g.min_seen_hours else '关'}",
                    callback_data=f"r:{g.id}:seen",
                )
            ],
            [Btn("⬅️ 返回", callback_data=f"r:{g.id}:back")],
        ]
    )


def join_kb(g: db.Giveaway, count: int) -> Markup | None:
    if g.mode != db.MODE_BUTTON:
        return Markup([[Btn(f"👀 查看参与情况（{count}）", callback_data=f"j:{g.id}:who")]])
    return Markup(
        [
            [Btn(f"🎉 我要参与（{count}）", callback_data=f"j:{g.id}:in")],
            [Btn("↩️ 退出", callback_data=f"j:{g.id}:out")],
        ]
    )


def ended_kb(g: db.Giveaway) -> Markup:
    return Markup([[Btn("🔍 复核开奖", callback_data=f"j:{g.id}:verify")]])
