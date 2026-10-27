"""SQLite 数据层。

设计要点：
- 一个连接全局复用（PTB 是单线程 asyncio，sqlite 调用都是毫秒级，够用）。
- 开启 WAL，进程被 kill 也不容易损坏。
- 草稿和正式抽奖放同一张表，用 status 区分：draft -> active -> ended/cancelled。
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------- 常量

MODE_BUTTON = "button"      # 点按钮报名
MODE_KEYWORD = "keyword"    # 群里发口令报名（可设多个口令，发中任意一个即参与）
MODE_POINTS = "points"      # 按发言条数加权
MODE_MESSAGE = "message"    # 随机抽一条群消息，作者中奖
MODE_MANUAL = "manual"      # 管理员贴名单

MODES = (MODE_BUTTON, MODE_KEYWORD, MODE_POINTS, MODE_MESSAGE, MODE_MANUAL)

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_ENDED = "ended"
STATUS_CANCELLED = "cancelled"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS giveaways (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id             INTEGER NOT NULL,
    message_id          INTEGER,
    creator_id          INTEGER NOT NULL,
    prize               TEXT    NOT NULL DEFAULT '',
    mode                TEXT    NOT NULL DEFAULT 'button',
    keyword             TEXT,          -- JSON 数组，存一个或多个口令
    winners_count       INTEGER NOT NULL DEFAULT 1,
    end_at              INTEGER,
    max_participants    INTEGER,
    require_channels    TEXT    NOT NULL DEFAULT '[]',
    require_username    INTEGER NOT NULL DEFAULT 0,
    min_seen_hours      INTEGER NOT NULL DEFAULT 0,
    weight_cap          INTEGER NOT NULL DEFAULT 0,   -- 已废弃：发言积分不再设上限
    invite_weight       INTEGER NOT NULL DEFAULT 0,   -- 每邀请 1 人加多少权重，0=不计
    point_prize         INTEGER NOT NULL DEFAULT 0,   -- 中奖者各得多少积分，和 prize 二选一
    status              TEXT    NOT NULL DEFAULT 'draft',
    seed                TEXT,
    created_at          INTEGER NOT NULL,
    ended_at            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_giveaways_chat_status ON giveaways(chat_id, status);

CREATE TABLE IF NOT EXISTS participants (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    username    TEXT,
    full_name   TEXT,
    weight      INTEGER NOT NULL DEFAULT 1,
    joined_at   INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id),
    FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS winners (
    giveaway_id INTEGER NOT NULL,
    rank        INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    username    TEXT,
    full_name   TEXT,
    PRIMARY KEY (giveaway_id, rank),
    FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
);

-- 机器人第一次在本群见到某人的时间，用来做「新号不能参与」的判断
CREATE TABLE IF NOT EXISTS user_seen (
    chat_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    first_seen_at INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL,
    msg_count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);

-- ============================ 积分板块 ============================

-- 每群一个钱包。A 群挣的分只能在 A 群用。
CREATE TABLE IF NOT EXISTS points (
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    balance      INTEGER NOT NULL DEFAULT 0,
    total_earned INTEGER NOT NULL DEFAULT 0,
    username     TEXT,
    full_name    TEXT,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_points_rank ON points(chat_id, balance DESC);

-- 流水。积分出了问题全靠这张表复盘。
CREATE TABLE IF NOT EXISTS point_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    delta         INTEGER NOT NULL,
    reason        TEXT    NOT NULL,
    balance_after INTEGER NOT NULL,
    note          TEXT,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_point_logs_user ON point_logs(chat_id, user_id, id DESC);

-- 每人每天的计数：发言拿了多少、签到没签、上次发言拿分是什么时候（冷却用）
CREATE TABLE IF NOT EXISTS point_daily (
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    day          TEXT    NOT NULL,
    chat_earned    INTEGER NOT NULL DEFAULT 0,
    checked_in     INTEGER NOT NULL DEFAULT 0,
    last_chat_at   INTEGER NOT NULL DEFAULT 0,
    last_text_hash TEXT,
    PRIMARY KEY (chat_id, user_id, day)
);

-- 每群的积分规则，管理员在 /settings 里调
CREATE TABLE IF NOT EXISTS point_settings (
    chat_id          INTEGER PRIMARY KEY,
    enabled          INTEGER NOT NULL DEFAULT 1,
    per_message      INTEGER NOT NULL DEFAULT 1,
    daily_cap        INTEGER NOT NULL DEFAULT 50,
    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    checkin_points   INTEGER NOT NULL DEFAULT 10,
    min_message_len  INTEGER NOT NULL DEFAULT 2,
    updated_at       INTEGER NOT NULL
);

-- ============================ 积分商城 ============================

CREATE TABLE IF NOT EXISTS shop_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    name           TEXT    NOT NULL,
    cost           INTEGER NOT NULL,
    kind           TEXT    NOT NULL DEFAULT 'physical',  -- physical=实物走兑换单, code=卡密自动发
    stock          INTEGER,            -- NULL = 不限量（实物）
    per_user_limit INTEGER,            -- NULL = 不限次
    note           TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shop_items_chat ON shop_items(chat_id, enabled);

-- 卡密库，kind=code 的奖品从这里出货
CREATE TABLE IF NOT EXISTS shop_codes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    code    TEXT    NOT NULL,
    used_by INTEGER,
    used_at INTEGER,
    FOREIGN KEY (item_id) REFERENCES shop_items(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_shop_codes_free ON shop_codes(item_id, used_by);

-- 兑换单。实物 pending -> done/refunded；卡密直接 delivered
CREATE TABLE IF NOT EXISTS redemptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    item_id    INTEGER,
    item_name  TEXT    NOT NULL,
    cost       INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    username   TEXT,
    full_name  TEXT,
    status     TEXT    NOT NULL DEFAULT 'pending',
    code       TEXT,
    created_at INTEGER NOT NULL,
    handled_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_redemptions_chat ON redemptions(chat_id, status, id DESC);

-- 消息抽奖用：抽奖期间群里的每条消息都记一行，开奖时随机抽一条，作者中奖。
CREATE TABLE IF NOT EXISTS messages (
    giveaway_id INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    username    TEXT,
    full_name   TEXT,
    excerpt     TEXT,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, message_id),
    FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(giveaway_id, user_id);

-- 机器人给每个人生成的专属邀请链接，用来把新成员算到邀请人头上。
CREATE TABLE IF NOT EXISTS invite_links (
    invite_link TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invite_links_owner ON invite_links(chat_id, user_id);

-- 预留名额。只由机器人主人私聊设置，群里看不到。
CREATE TABLE IF NOT EXISTS reservations (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    username    TEXT,
    full_name   TEXT,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id),
    FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
);

-- 谁把谁拉进了这个群。一个人在一个群里只算一次，
-- 踢出再拉回来不会重复计数。
CREATE TABLE IF NOT EXISTS invites (
    chat_id    INTEGER NOT NULL,
    invitee_id INTEGER NOT NULL,
    inviter_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, invitee_id)
);
CREATE INDEX IF NOT EXISTS idx_invites_inviter ON invites(chat_id, inviter_id, created_at);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id          INTEGER PRIMARY KEY,
    admin_only       INTEGER NOT NULL DEFAULT 1,
    default_channels TEXT    NOT NULL DEFAULT '[]',
    updated_at       INTEGER NOT NULL
);
"""


KEYWORD_LIMIT = 20          # 一场抽奖最多设几个口令
KEYWORD_MAX_LEN = 32        # 单个口令最长多少字符


def parse_keywords(raw: Optional[str]) -> list[str]:
    """口令列存的是 JSON 数组；老数据是一个裸字符串，这里一并兼容。"""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return [raw]
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
    return [raw]


def clean_keywords(words: Iterable[str]) -> list[str]:
    """去空、去重（忽略大小写）、限长、限个数，顺序保持用户输入的顺序。"""
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        word = str(word).strip()[:KEYWORD_MAX_LEN]
        if not word:
            continue
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(word)
        if len(out) >= KEYWORD_LIMIT:
            break
    return out


def matches_keyword(keywords: list[str], text: str) -> Optional[str]:
    """整条消息等于某个口令才算（忽略首尾空白和英文大小写）。返回命中的那个口令。"""
    text = (text or "").strip().casefold()
    if not text:
        return None
    for word in keywords:
        if word.strip().casefold() == text:
            return word
    return None


# ---------------------------------------------------------------- 数据对象

@dataclass
class Giveaway:
    id: int
    chat_id: int
    message_id: Optional[int]
    creator_id: int
    prize: str
    mode: str
    keywords: list[str]
    winners_count: int
    end_at: Optional[int]
    max_participants: Optional[int]
    require_channels: list[str]
    require_username: bool
    min_seen_hours: int
    weight_cap: int
    invite_weight: int
    point_prize: int
    status: str
    seed: Optional[str]
    created_at: int
    ended_at: Optional[int]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Giveaway":
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            creator_id=row["creator_id"],
            prize=row["prize"],
            mode=row["mode"],
            keywords=parse_keywords(row["keyword"]),
            winners_count=row["winners_count"],
            end_at=row["end_at"],
            max_participants=row["max_participants"],
            require_channels=json.loads(row["require_channels"] or "[]"),
            require_username=bool(row["require_username"]),
            min_seen_hours=row["min_seen_hours"],
            weight_cap=row["weight_cap"],
            invite_weight=row["invite_weight"],
            point_prize=row["point_prize"],
            status=row["status"],
            seed=row["seed"],
            created_at=row["created_at"],
            ended_at=row["ended_at"],
        )


@dataclass
class Participant:
    user_id: int
    username: Optional[str]
    full_name: str
    weight: int
    joined_at: int


# ---------------------------------------------------------------- 连接

_conn: Optional[sqlite3.Connection] = None


# 版本升级时给老库补上的列：表名 -> {列名: 列定义}
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "giveaways": {
        "invite_weight": "INTEGER NOT NULL DEFAULT 0",
        "point_prize": "INTEGER NOT NULL DEFAULT 0",
    },
    "point_logs": {
        "note": "TEXT",
    },
}


def _migrate(connection: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，这里手动补。"""
    for table, columns in _ADDED_COLUMNS.items():
        have = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue  # 表还不存在，建表语句里已经带上了这些列
        for column, definition in columns.items():
            if column not in have:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    connection.commit()


def init(db_path: Path) -> sqlite3.Connection:
    global _conn
    _conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    _migrate(_conn)
    _conn.commit()
    return _conn


def conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("数据库尚未初始化，请先调用 db.init()")
    return _conn


def _exec(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    cur = conn().execute(sql, tuple(params))
    conn().commit()
    return cur


def now() -> int:
    return int(time.time())


_TZ = "Asia/Shanghai"


def set_timezone(name: str) -> None:
    """签到和每日上限按这个时区切天，和消息里显示的时间保持一致。"""
    global _TZ
    try:
        ZoneInfo(name)
        _TZ = name
    except Exception:
        _TZ = "Asia/Shanghai"


def day_key(ts: Optional[int] = None) -> str:
    """YYYY-MM-DD，按显示时区。用字符串存，跨夏令时也不会算错天。"""
    moment = datetime.fromtimestamp(ts if ts is not None else now(), timezone.utc)
    return moment.astimezone(ZoneInfo(_TZ)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 抽奖 CRUD

def create_draft(chat_id: int, creator_id: int, defaults: dict[str, Any] | None = None) -> int:
    """新建一个草稿。同一个人在同一个群只保留最新一份草稿。"""
    _exec(
        "DELETE FROM giveaways WHERE chat_id=? AND creator_id=? AND status=?",
        (chat_id, creator_id, STATUS_DRAFT),
    )
    d = defaults or {}
    cur = _exec(
        """INSERT INTO giveaways
           (chat_id, creator_id, prize, mode, keyword, winners_count, end_at, max_participants,
            require_channels, require_username, min_seen_hours, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            chat_id,
            creator_id,
            d.get("prize", ""),
            d.get("mode", MODE_BUTTON),
            json.dumps(clean_keywords(d["keyword"]), ensure_ascii=False)
            if d.get("keyword") else None,
            d.get("winners_count", 1),
            d.get("end_at"),
            d.get("max_participants"),
            json.dumps(d.get("require_channels", []), ensure_ascii=False),
            int(d.get("require_username", False)),
            d.get("min_seen_hours", 0),
            STATUS_DRAFT,
            now(),
        ),
    )
    return int(cur.lastrowid)


def get(giveaway_id: int) -> Optional[Giveaway]:
    row = conn().execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
    return Giveaway.from_row(row) if row else None


def get_draft(chat_id: int, creator_id: int) -> Optional[Giveaway]:
    row = conn().execute(
        "SELECT * FROM giveaways WHERE chat_id=? AND creator_id=? AND status=? "
        "ORDER BY id DESC LIMIT 1",
        (chat_id, creator_id, STATUS_DRAFT),
    ).fetchone()
    return Giveaway.from_row(row) if row else None


_UPDATABLE = {
    "message_id", "prize", "mode", "keyword", "winners_count", "end_at",
    "max_participants", "require_channels", "require_username",
    "min_seen_hours", "weight_cap", "invite_weight", "point_prize",
    "status", "seed", "ended_at",
}


def update(giveaway_id: int, **fields: Any) -> None:
    sets, values = [], []
    for key, value in fields.items():
        if key not in _UPDATABLE:
            raise KeyError(f"不可更新的字段: {key}")
        if key == "require_channels":
            value = json.dumps(value or [], ensure_ascii=False)
        if key == "keyword" and isinstance(value, (list, tuple)):
            value = json.dumps(clean_keywords(value), ensure_ascii=False) if value else None
        if isinstance(value, bool):
            value = int(value)
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    values.append(giveaway_id)
    _exec(f"UPDATE giveaways SET {', '.join(sets)} WHERE id=?", values)


def active_in_chat(chat_id: int) -> list[Giveaway]:
    rows = conn().execute(
        "SELECT * FROM giveaways WHERE chat_id=? AND status=? ORDER BY id",
        (chat_id, STATUS_ACTIVE),
    ).fetchall()
    return [Giveaway.from_row(r) for r in rows]


def all_active() -> list[Giveaway]:
    rows = conn().execute(
        "SELECT * FROM giveaways WHERE status=? ORDER BY id", (STATUS_ACTIVE,)
    ).fetchall()
    return [Giveaway.from_row(r) for r in rows]


def active_keyword_giveaways(chat_id: int) -> list[Giveaway]:
    rows = conn().execute(
        "SELECT * FROM giveaways WHERE chat_id=? AND status=? AND mode=?",
        (chat_id, STATUS_ACTIVE, MODE_KEYWORD),
    ).fetchall()
    return [Giveaway.from_row(r) for r in rows]


def active_points_giveaways(chat_id: int) -> list[Giveaway]:
    rows = conn().execute(
        "SELECT * FROM giveaways WHERE chat_id=? AND status=? AND mode=?",
        (chat_id, STATUS_ACTIVE, MODE_POINTS),
    ).fetchall()
    return [Giveaway.from_row(r) for r in rows]


def active_by_mode(chat_id: int, mode: str) -> list[Giveaway]:
    rows = conn().execute(
        "SELECT * FROM giveaways WHERE chat_id=? AND status=? AND mode=?",
        (chat_id, STATUS_ACTIVE, mode),
    ).fetchall()
    return [Giveaway.from_row(r) for r in rows]


def recent_in_chat(chat_id: int, limit: int = 10) -> list[Giveaway]:
    rows = conn().execute(
        "SELECT * FROM giveaways WHERE chat_id=? AND status!=? ORDER BY id DESC LIMIT ?",
        (chat_id, STATUS_DRAFT, limit),
    ).fetchall()
    return [Giveaway.from_row(r) for r in rows]


# ---------------------------------------------------------------- 参与者

def add_participant(
    giveaway_id: int, user_id: int, username: Optional[str], full_name: str, weight: int = 1
) -> bool:
    """返回 True 表示是新报名，False 表示之前已经报过。"""
    cur = _exec(
        "INSERT OR IGNORE INTO participants "
        "(giveaway_id, user_id, username, full_name, weight, joined_at) VALUES (?,?,?,?,?,?)",
        (giveaway_id, user_id, username, full_name, weight, now()),
    )
    return cur.rowcount > 0


def bump_weight(giveaway_id: int, user_id: int) -> None:
    """发言积分不设上限，说一句加一分。"""
    _exec(
        "UPDATE participants SET weight = weight + 1 WHERE giveaway_id=? AND user_id=?",
        (giveaway_id, user_id),
    )


def remove_participant(giveaway_id: int, user_id: int) -> bool:
    cur = _exec(
        "DELETE FROM participants WHERE giveaway_id=? AND user_id=?", (giveaway_id, user_id)
    )
    return cur.rowcount > 0


def is_participant(giveaway_id: int, user_id: int) -> bool:
    row = conn().execute(
        "SELECT 1 FROM participants WHERE giveaway_id=? AND user_id=?", (giveaway_id, user_id)
    ).fetchone()
    return row is not None


def participants(giveaway_id: int) -> list[Participant]:
    rows = conn().execute(
        "SELECT user_id, username, full_name, weight, joined_at FROM participants "
        "WHERE giveaway_id=? ORDER BY joined_at, user_id",
        (giveaway_id,),
    ).fetchall()
    return [
        Participant(r["user_id"], r["username"], r["full_name"] or "", r["weight"], r["joined_at"])
        for r in rows
    ]


def participant_count(giveaway_id: int) -> int:
    row = conn().execute(
        "SELECT COUNT(*) AS c FROM participants WHERE giveaway_id=?", (giveaway_id,)
    ).fetchone()
    return int(row["c"])


# ---------------------------------------------------------------- 中奖者

def save_winners(giveaway_id: int, winners: list[Participant]) -> None:
    _exec("DELETE FROM winners WHERE giveaway_id=?", (giveaway_id,))
    conn().executemany(
        "INSERT INTO winners (giveaway_id, rank, user_id, username, full_name) VALUES (?,?,?,?,?)",
        [(giveaway_id, i + 1, w.user_id, w.username, w.full_name) for i, w in enumerate(winners)],
    )
    conn().commit()


@dataclass
class WinRecord:
    giveaway_id: int
    chat_id: int
    prize: str
    ended_at: Optional[int]


def wins_for_user(
    user_id: int, chat_id: Optional[int] = None, limit: int = 10
) -> tuple[list[WinRecord], int]:
    """某人的中奖记录。chat_id 给了就只查那个群。返回 (最近若干条, 总次数)。"""
    where = "w.user_id=?"
    params: list[Any] = [user_id]
    if chat_id is not None:
        where += " AND g.chat_id=?"
        params.append(chat_id)

    total = int(conn().execute(
        f"SELECT COUNT(*) AS c FROM winners w JOIN giveaways g ON g.id=w.giveaway_id "
        f"WHERE {where}",
        tuple(params),
    ).fetchone()["c"])

    rows = conn().execute(
        f"""SELECT g.id, g.chat_id, g.prize, g.ended_at
            FROM winners w JOIN giveaways g ON g.id = w.giveaway_id
            WHERE {where}
            ORDER BY COALESCE(g.ended_at, 0) DESC, g.id DESC
            LIMIT ?""",
        tuple(params) + (limit,),
    ).fetchall()
    return [
        WinRecord(r["id"], r["chat_id"], r["prize"] or "", r["ended_at"]) for r in rows
    ], total


def winners(giveaway_id: int) -> list[Participant]:
    rows = conn().execute(
        "SELECT user_id, username, full_name FROM winners WHERE giveaway_id=? ORDER BY rank",
        (giveaway_id,),
    ).fetchall()
    return [Participant(r["user_id"], r["username"], r["full_name"] or "", 1, 0) for r in rows]


# ---------------------------------------------------------------- 用户活跃度

def touch_user(chat_id: int, user_id: int) -> None:
    ts = now()
    _exec(
        """INSERT INTO user_seen (chat_id, user_id, first_seen_at, last_seen_at, msg_count)
           VALUES (?,?,?,?,1)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
             last_seen_at=excluded.last_seen_at,
             msg_count=msg_count+1""",
        (chat_id, user_id, ts, ts),
    )


def user_stats(chat_id: int, user_id: int) -> Optional[dict[str, int]]:
    """某人在本群的记录：第一次被看到的时间、累计发言条数。没见过返回 None。"""
    row = conn().execute(
        "SELECT first_seen_at, last_seen_at, msg_count FROM user_seen "
        "WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "first_seen_at": int(row["first_seen_at"]),
        "last_seen_at": int(row["last_seen_at"]),
        "msg_count": int(row["msg_count"]),
    }


def first_seen(chat_id: int, user_id: int) -> Optional[int]:
    row = conn().execute(
        "SELECT first_seen_at FROM user_seen WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    return int(row["first_seen_at"]) if row else None


# ---------------------------------------------------------------- 消息抽奖

EXCERPT_LEN = 120


@dataclass
class ChatMessage:
    message_id: int
    user_id: int
    username: Optional[str]
    full_name: str
    excerpt: str


def record_message(
    giveaway_id: int,
    message_id: int,
    user_id: int,
    username: Optional[str],
    full_name: str,
    text: str,
) -> None:
    _exec(
        "INSERT OR IGNORE INTO messages "
        "(giveaway_id, message_id, user_id, username, full_name, excerpt, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (giveaway_id, message_id, user_id, username, full_name,
         (text or "")[:EXCERPT_LEN], now()),
    )


def messages(giveaway_id: int) -> list[ChatMessage]:
    rows = conn().execute(
        "SELECT message_id, user_id, username, full_name, excerpt FROM messages "
        "WHERE giveaway_id=? ORDER BY message_id",
        (giveaway_id,),
    ).fetchall()
    return [
        ChatMessage(r["message_id"], r["user_id"], r["username"],
                    r["full_name"] or "", r["excerpt"] or "")
        for r in rows
    ]


def message_count(giveaway_id: int) -> int:
    row = conn().execute(
        "SELECT COUNT(*) AS c FROM messages WHERE giveaway_id=?", (giveaway_id,)
    ).fetchone()
    return int(row["c"])


# ---------------------------------------------------------------- 邀请链接

def save_invite_link(chat_id: int, user_id: int, link: str) -> None:
    _exec(
        "INSERT OR REPLACE INTO invite_links (invite_link, chat_id, user_id, created_at) "
        "VALUES (?,?,?,?)",
        (link, chat_id, user_id, now()),
    )


def invite_link_owner(link: str) -> Optional[int]:
    row = conn().execute(
        "SELECT user_id FROM invite_links WHERE invite_link=?", (link,)
    ).fetchone()
    return int(row["user_id"]) if row else None


def existing_invite_link(chat_id: int, user_id: int) -> Optional[str]:
    row = conn().execute(
        "SELECT invite_link FROM invite_links WHERE chat_id=? AND user_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (chat_id, user_id),
    ).fetchone()
    return row["invite_link"] if row else None


# ---------------------------------------------------------------- 预留名额

def add_reservation(
    giveaway_id: int, user_id: int, username: Optional[str], full_name: str
) -> bool:
    cur = _exec(
        "INSERT OR IGNORE INTO reservations "
        "(giveaway_id, user_id, username, full_name, created_at) VALUES (?,?,?,?,?)",
        (giveaway_id, user_id, username, full_name, now()),
    )
    return cur.rowcount > 0


def remove_reservation(giveaway_id: int, user_id: int) -> bool:
    cur = _exec(
        "DELETE FROM reservations WHERE giveaway_id=? AND user_id=?", (giveaway_id, user_id)
    )
    return cur.rowcount > 0


def reservations(giveaway_id: int) -> list[Participant]:
    """按设置的先后顺序返回预留名单。

    用 rowid 排而不是 created_at —— 同一秒内连着设几个的话，
    created_at 会打平，顺序就乱了，而顺序决定超出名额时留下哪几个。
    """
    rows = conn().execute(
        "SELECT user_id, username, full_name, created_at FROM reservations "
        "WHERE giveaway_id=? ORDER BY rowid",
        (giveaway_id,),
    ).fetchall()
    return [
        Participant(r["user_id"], r["username"], r["full_name"] or "", 1, r["created_at"])
        for r in rows
    ]


def reservation_count(giveaway_id: int) -> int:
    row = conn().execute(
        "SELECT COUNT(*) AS c FROM reservations WHERE giveaway_id=?", (giveaway_id,)
    ).fetchone()
    return int(row["c"])


def find_user_by_username(chat_id: int, username: str) -> Optional[Participant]:
    """在这个群参与过任何一场抽奖的人里，按用户名找人。"""
    row = conn().execute(
        """SELECT p.user_id, p.username, p.full_name FROM participants p
           JOIN giveaways g ON g.id = p.giveaway_id
           WHERE g.chat_id = ? AND LOWER(p.username) = LOWER(?)
           ORDER BY p.joined_at DESC LIMIT 1""",
        (chat_id, username.lstrip("@")),
    ).fetchone()
    if row is None:
        return None
    return Participant(row["user_id"], row["username"], row["full_name"] or "", 1, 0)


def find_participant(giveaway_id: int, user_id: int) -> Optional[Participant]:
    row = conn().execute(
        "SELECT user_id, username, full_name, weight, joined_at FROM participants "
        "WHERE giveaway_id=? AND user_id=?",
        (giveaway_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return Participant(
        row["user_id"], row["username"], row["full_name"] or "", row["weight"], row["joined_at"]
    )


# ---------------------------------------------------------------- 邀请

def record_invite(chat_id: int, invitee_id: int, inviter_id: int) -> bool:
    """记一笔邀请。返回 True 表示这是一笔新邀请。

    同一个人在同一个群只记第一次 —— 否则「踢出去再拉回来」就能无限刷权重。
    """
    if invitee_id == inviter_id:
        return False
    cur = _exec(
        "INSERT OR IGNORE INTO invites (chat_id, invitee_id, inviter_id, created_at) "
        "VALUES (?,?,?,?)",
        (chat_id, invitee_id, inviter_id, now()),
    )
    return cur.rowcount > 0


def invite_count(chat_id: int, inviter_id: int, since: Optional[int] = None) -> int:
    """某人在本群邀请了多少人；since 给了就只数那个时间点之后的。"""
    if since is None:
        row = conn().execute(
            "SELECT COUNT(*) AS c FROM invites WHERE chat_id=? AND inviter_id=?",
            (chat_id, inviter_id),
        ).fetchone()
    else:
        row = conn().execute(
            "SELECT COUNT(*) AS c FROM invites WHERE chat_id=? AND inviter_id=? AND created_at>=?",
            (chat_id, inviter_id, since),
        ).fetchone()
    return int(row["c"])


# ---------------------------------------------------------------- 群设置

def chat_settings(chat_id: int) -> dict[str, Any]:
    row = conn().execute("SELECT * FROM chat_settings WHERE chat_id=?", (chat_id,)).fetchone()
    if row is None:
        return {"chat_id": chat_id, "admin_only": True, "default_channels": []}
    return {
        "chat_id": chat_id,
        "admin_only": bool(row["admin_only"]),
        "default_channels": json.loads(row["default_channels"] or "[]"),
    }


def save_chat_settings(chat_id: int, *, admin_only: bool | None = None,
                       default_channels: list[str] | None = None) -> None:
    cur = chat_settings(chat_id)
    if admin_only is not None:
        cur["admin_only"] = admin_only
    if default_channels is not None:
        cur["default_channels"] = default_channels
    _exec(
        """INSERT INTO chat_settings (chat_id, admin_only, default_channels, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET
             admin_only=excluded.admin_only,
             default_channels=excluded.default_channels,
             updated_at=excluded.updated_at""",
        (chat_id, int(cur["admin_only"]), json.dumps(cur["default_channels"], ensure_ascii=False), now()),
    )

# ================================================================ 积分板块

POINT_DEFAULTS = {
    "enabled": True,
    "per_message": 1,        # 每条有效发言给多少分
    "daily_cap": 50,         # 靠发言每天最多拿多少分，0 = 不限
    "cooldown_seconds": 60,  # 同一个人多久才能再拿一次发言分，0 = 不限
    "checkin_points": 10,    # 每日签到给多少分
    "min_message_len": 2,    # 短于这个长度的消息不给分
}

REASON_CHAT = "chat"
REASON_CHECKIN = "checkin"
REASON_REDEEM = "redeem"
REASON_REFUND = "refund"
REASON_ADMIN = "admin"
REASON_PRIZE = "prize"


@dataclass
class Wallet:
    user_id: int
    balance: int
    total_earned: int
    username: Optional[str]
    full_name: str


def point_settings(chat_id: int) -> dict[str, Any]:
    row = conn().execute(
        "SELECT * FROM point_settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if row is None:
        return {"chat_id": chat_id, **POINT_DEFAULTS}
    out: dict[str, Any] = {"chat_id": chat_id}
    for key in POINT_DEFAULTS:
        out[key] = bool(row[key]) if key == "enabled" else int(row[key])
    return out


def save_point_settings(chat_id: int, **changes: Any) -> dict[str, Any]:
    current = point_settings(chat_id)
    for key, value in changes.items():
        if key not in POINT_DEFAULTS:
            raise KeyError(f"未知的积分设置: {key}")
        current[key] = value
    _exec(
        """INSERT INTO point_settings
           (chat_id, enabled, per_message, daily_cap, cooldown_seconds,
            checkin_points, min_message_len, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET
             enabled=excluded.enabled,
             per_message=excluded.per_message,
             daily_cap=excluded.daily_cap,
             cooldown_seconds=excluded.cooldown_seconds,
             checkin_points=excluded.checkin_points,
             min_message_len=excluded.min_message_len,
             updated_at=excluded.updated_at""",
        (
            chat_id,
            int(bool(current["enabled"])),
            int(current["per_message"]),
            int(current["daily_cap"]),
            int(current["cooldown_seconds"]),
            int(current["checkin_points"]),
            int(current["min_message_len"]),
            now(),
        ),
    )
    return current


# ---------------------------------------------------------------- 钱包

def add_points(
    chat_id: int,
    user_id: int,
    delta: int,
    reason: str,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    note: Optional[str] = None,
) -> int:
    """加减积分，返回操作后的余额。余额不会被扣成负数。

    note 只写进流水，不对群成员展示 —— 主人手动调分时写在这里。
    """
    row = conn().execute(
        "SELECT balance FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    before = int(row["balance"]) if row else 0
    after = max(0, before + delta)
    real_delta = after - before
    earned = max(0, real_delta)

    _exec(
        """INSERT INTO points
           (chat_id, user_id, balance, total_earned, username, full_name, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
             balance=excluded.balance,
             total_earned=points.total_earned+?,
             username=COALESCE(excluded.username, points.username),
             full_name=COALESCE(excluded.full_name, points.full_name),
             updated_at=excluded.updated_at""",
        (chat_id, user_id, after, earned, username, full_name, now(), earned),
    )
    _exec(
        "INSERT INTO point_logs "
        "(chat_id, user_id, delta, reason, balance_after, note, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (chat_id, user_id, real_delta, reason, after, (note or None), now()),
    )
    return after


def point_balance(chat_id: int, user_id: int) -> int:
    row = conn().execute(
        "SELECT balance FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    return int(row["balance"]) if row else 0


def wallet(chat_id: int, user_id: int) -> Optional[Wallet]:
    row = conn().execute(
        "SELECT user_id, balance, total_earned, username, full_name FROM points "
        "WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return Wallet(row["user_id"], int(row["balance"]), int(row["total_earned"]),
                  row["username"], row["full_name"] or "")


def point_rank(chat_id: int, user_id: int) -> tuple[int, int]:
    """返回 (名次, 本群有积分的人数)。余额相同算并列。"""
    total = int(conn().execute(
        "SELECT COUNT(*) AS c FROM points WHERE chat_id=? AND balance>0", (chat_id,)
    ).fetchone()["c"])
    mine = point_balance(chat_id, user_id)
    if mine <= 0:
        return 0, total
    ahead = int(conn().execute(
        "SELECT COUNT(*) AS c FROM points WHERE chat_id=? AND balance>?", (chat_id, mine)
    ).fetchone()["c"])
    return ahead + 1, total


def point_top(chat_id: int, limit: int = 10) -> list[Wallet]:
    rows = conn().execute(
        "SELECT user_id, balance, total_earned, username, full_name FROM points "
        "WHERE chat_id=? AND balance>0 ORDER BY balance DESC, user_id LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    return [
        Wallet(r["user_id"], int(r["balance"]), int(r["total_earned"]),
               r["username"], r["full_name"] or "")
        for r in rows
    ]


@dataclass
class LedgerEntry:
    delta: int
    reason: str
    balance_after: int
    note: str
    created_at: int


REASON_LABEL = {
    REASON_CHAT: "发言",
    REASON_CHECKIN: "签到",
    REASON_REDEEM: "兑换",
    REASON_REFUND: "退单",
    REASON_PRIZE: "抽奖奖品",
    REASON_ADMIN: "手动调整",
}


def point_ledger(chat_id: int, user_id: int, limit: int = 15) -> list[LedgerEntry]:
    rows = conn().execute(
        "SELECT delta, reason, balance_after, note, created_at FROM point_logs "
        "WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, user_id, limit),
    ).fetchall()
    return [
        LedgerEntry(int(r["delta"]), r["reason"], int(r["balance_after"]),
                    r["note"] or "", int(r["created_at"]))
        for r in rows
    ]


def known_chats() -> list[int]:
    """机器人见过的群：有积分记录的、有抽奖的、改过设置的，都算。"""
    rows = conn().execute(
        """SELECT chat_id FROM points
           UNION SELECT chat_id FROM giveaways
           UNION SELECT chat_id FROM point_settings
           UNION SELECT chat_id FROM chat_settings"""
    ).fetchall()
    return sorted({int(r["chat_id"]) for r in rows})


def find_wallet_by_username(chat_id: int, username: str) -> Optional[Wallet]:
    row = conn().execute(
        "SELECT user_id, balance, total_earned, username, full_name FROM points "
        "WHERE chat_id=? AND LOWER(username)=LOWER(?) LIMIT 1",
        (chat_id, username.lstrip("@")),
    ).fetchone()
    if row is None:
        return None
    return Wallet(row["user_id"], int(row["balance"]), int(row["total_earned"]),
                  row["username"], row["full_name"] or "")


# ---------------------------------------------------------------- 每日计数

def daily(chat_id: int, user_id: int, day: Optional[str] = None) -> dict[str, int]:
    day = day or day_key()
    row = conn().execute(
        "SELECT chat_earned, checked_in, last_chat_at, last_text_hash FROM point_daily "
        "WHERE chat_id=? AND user_id=? AND day=?",
        (chat_id, user_id, day),
    ).fetchone()
    if row is None:
        return {"chat_earned": 0, "checked_in": 0, "last_chat_at": 0, "last_text_hash": None}
    return {
        "chat_earned": int(row["chat_earned"]),
        "checked_in": int(row["checked_in"]),
        "last_chat_at": int(row["last_chat_at"]),
        "last_text_hash": row["last_text_hash"],
    }


def bump_daily_chat(
    chat_id: int, user_id: int, earned: int,
    text_hash: Optional[str] = None, day: Optional[str] = None,
) -> None:
    day = day or day_key()
    _exec(
        """INSERT INTO point_daily
           (chat_id, user_id, day, chat_earned, last_chat_at, last_text_hash)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
             chat_earned=point_daily.chat_earned+excluded.chat_earned,
             last_chat_at=excluded.last_chat_at,
             last_text_hash=excluded.last_text_hash""",
        (chat_id, user_id, day, earned, now(), text_hash),
    )


def mark_checkin(chat_id: int, user_id: int, day: Optional[str] = None) -> bool:
    """标记今天已签到。返回 False 表示今天已经签过了。"""
    day = day or day_key()
    cur = _exec(
        """INSERT INTO point_daily (chat_id, user_id, day, checked_in)
           VALUES (?,?,?,1)
           ON CONFLICT(chat_id, user_id, day) DO UPDATE SET checked_in=1
           WHERE point_daily.checked_in=0""",
        (chat_id, user_id, day),
    )
    return cur.rowcount > 0


def checkin_streak(chat_id: int, user_id: int) -> int:
    """连续签到天数（含今天）。"""
    rows = conn().execute(
        "SELECT day FROM point_daily WHERE chat_id=? AND user_id=? AND checked_in=1 "
        "ORDER BY day DESC LIMIT 400",
        (chat_id, user_id),
    ).fetchall()
    if not rows:
        return 0
    from datetime import date, timedelta

    days = [date.fromisoformat(r["day"]) for r in rows]
    today = date.fromisoformat(day_key())
    if days[0] not in (today, today - timedelta(days=1)):
        return 0
    streak, cursor = 1, days[0]
    for d in days[1:]:
        if d == cursor - timedelta(days=1):
            streak += 1
            cursor = d
        else:
            break
    return streak


# ================================================================ 积分商城

KIND_PHYSICAL = "physical"
KIND_CODE = "code"

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_REFUNDED = "refunded"
STATUS_DELIVERED = "delivered"


@dataclass
class ShopItem:
    id: int
    chat_id: int
    name: str
    cost: int
    kind: str
    stock: Optional[int]
    per_user_limit: Optional[int]
    note: str
    enabled: bool

    @property
    def is_code(self) -> bool:
        return self.kind == KIND_CODE


def _shop_item(row: sqlite3.Row) -> ShopItem:
    return ShopItem(
        row["id"], row["chat_id"], row["name"], int(row["cost"]), row["kind"],
        None if row["stock"] is None else int(row["stock"]),
        None if row["per_user_limit"] is None else int(row["per_user_limit"]),
        row["note"] or "", bool(row["enabled"]),
    )


def shop_add_item(
    chat_id: int, name: str, cost: int, kind: str = KIND_PHYSICAL,
    stock: Optional[int] = None, per_user_limit: Optional[int] = None, note: str = "",
) -> int:
    cur = _exec(
        "INSERT INTO shop_items "
        "(chat_id, name, cost, kind, stock, per_user_limit, note, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,1,?)",
        (chat_id, name[:80], max(1, int(cost)), kind, stock, per_user_limit, note[:200], now()),
    )
    return int(cur.lastrowid)


def shop_item(item_id: int) -> Optional[ShopItem]:
    row = conn().execute("SELECT * FROM shop_items WHERE id=?", (item_id,)).fetchone()
    return _shop_item(row) if row else None


def shop_list(chat_id: int, only_enabled: bool = True) -> list[ShopItem]:
    sql = "SELECT * FROM shop_items WHERE chat_id=?"
    params: list[Any] = [chat_id]
    if only_enabled:
        sql += " AND enabled=1"
    sql += " ORDER BY cost, id"
    return [_shop_item(r) for r in conn().execute(sql, tuple(params)).fetchall()]


_SHOP_UPDATABLE = {"name", "cost", "kind", "stock", "per_user_limit", "note", "enabled"}


def shop_update_item(item_id: int, **fields: Any) -> None:
    sets, values = [], []
    for key, value in fields.items():
        if key not in _SHOP_UPDATABLE:
            raise KeyError(f"不可更新的奖品字段: {key}")
        if isinstance(value, bool):
            value = int(value)
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    values.append(item_id)
    _exec(f"UPDATE shop_items SET {', '.join(sets)} WHERE id=?", values)


def shop_delete_item(item_id: int) -> None:
    _exec("DELETE FROM shop_codes WHERE item_id=?", (item_id,))
    _exec("DELETE FROM shop_items WHERE id=?", (item_id,))


def shop_add_codes(item_id: int, codes: Iterable[str]) -> int:
    added = 0
    for code in codes:
        code = str(code).strip()
        if not code:
            continue
        _exec("INSERT INTO shop_codes (item_id, code) VALUES (?,?)", (item_id, code[:200]))
        added += 1
    return added


def shop_free_codes(item_id: int) -> int:
    row = conn().execute(
        "SELECT COUNT(*) AS c FROM shop_codes WHERE item_id=? AND used_by IS NULL", (item_id,)
    ).fetchone()
    return int(row["c"])


def shop_take_code(item_id: int, user_id: int) -> Optional[str]:
    """取一个还没用过的卡密并标记已用。没库存返回 None。"""
    row = conn().execute(
        "SELECT id, code FROM shop_codes WHERE item_id=? AND used_by IS NULL ORDER BY id LIMIT 1",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    cur = _exec(
        "UPDATE shop_codes SET used_by=?, used_at=? WHERE id=? AND used_by IS NULL",
        (user_id, now(), row["id"]),
    )
    return row["code"] if cur.rowcount > 0 else None


# ---------------------------------------------------------------- 兑换单

@dataclass
class Redemption:
    id: int
    chat_id: int
    item_id: Optional[int]
    item_name: str
    cost: int
    user_id: int
    username: Optional[str]
    full_name: str
    status: str
    code: Optional[str]
    created_at: int


def _redemption(row: sqlite3.Row) -> Redemption:
    return Redemption(
        row["id"], row["chat_id"], row["item_id"], row["item_name"], int(row["cost"]),
        row["user_id"], row["username"], row["full_name"] or "", row["status"],
        row["code"], int(row["created_at"]),
    )


def redemption_create(
    chat_id: int, item: ShopItem, user_id: int, username: Optional[str], full_name: str,
    status: str, code: Optional[str] = None,
) -> int:
    cur = _exec(
        "INSERT INTO redemptions "
        "(chat_id, item_id, item_name, cost, user_id, username, full_name, status, code, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chat_id, item.id, item.name, item.cost, user_id, username, full_name,
         status, code, now()),
    )
    return int(cur.lastrowid)


def redemption(redemption_id: int) -> Optional[Redemption]:
    row = conn().execute("SELECT * FROM redemptions WHERE id=?", (redemption_id,)).fetchone()
    return _redemption(row) if row else None


def redemptions_by_status(chat_id: int, status: str, limit: int = 20) -> list[Redemption]:
    rows = conn().execute(
        "SELECT * FROM redemptions WHERE chat_id=? AND status=? ORDER BY id LIMIT ?",
        (chat_id, status, limit),
    ).fetchall()
    return [_redemption(r) for r in rows]


def pending_count(chat_id: int) -> int:
    row = conn().execute(
        "SELECT COUNT(*) AS c FROM redemptions WHERE chat_id=? AND status=?",
        (chat_id, STATUS_PENDING),
    ).fetchone()
    return int(row["c"])


def redemption_set_status(redemption_id: int, status: str) -> bool:
    cur = _exec(
        "UPDATE redemptions SET status=?, handled_at=? WHERE id=? AND status=?",
        (status, now(), redemption_id, STATUS_PENDING),
    )
    return cur.rowcount > 0


def user_redeemed_count(chat_id: int, item_id: int, user_id: int) -> int:
    row = conn().execute(
        "SELECT COUNT(*) AS c FROM redemptions "
        "WHERE chat_id=? AND item_id=? AND user_id=? AND status!=?",
        (chat_id, item_id, user_id, STATUS_REFUNDED),
    ).fetchone()
    return int(row["c"])


def user_redemptions(chat_id: int, user_id: int, limit: int = 10) -> list[Redemption]:
    rows = conn().execute(
        "SELECT * FROM redemptions WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, user_id, limit),
    ).fetchall()
    return [_redemption(r) for r in rows]
