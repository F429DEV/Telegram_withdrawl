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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------- 常量

MODE_BUTTON = "button"      # 点按钮报名
MODE_KEYWORD = "keyword"    # 群里发口令报名（可设多个口令，发中任意一个即参与）
MODE_POINTS = "points"      # 按发言条数加权
MODE_MANUAL = "manual"      # 管理员贴名单

MODES = (MODE_BUTTON, MODE_KEYWORD, MODE_POINTS, MODE_MANUAL)

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


# 版本升级时给老库补上的列：列名 -> 建列语句
_ADDED_COLUMNS = {
    "invite_weight": "ALTER TABLE giveaways ADD COLUMN invite_weight INTEGER NOT NULL DEFAULT 0",
}


def _migrate(connection: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，这里手动补。"""
    have = {row["name"] for row in connection.execute("PRAGMA table_info(giveaways)")}
    for column, sql in _ADDED_COLUMNS.items():
        if column not in have:
            connection.execute(sql)
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
    "min_seen_hours", "weight_cap", "invite_weight", "status", "seed", "ended_at",
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
