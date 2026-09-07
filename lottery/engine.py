"""抽奖算法。

开奖时生成一个随机 seed，结果对 (seed, 参与者名单) 完全确定 —— 同样的输入
永远得到同样的排名，方便排查和复现。seed 存在数据库里，不对群公示。

做法是 A-Res 加权蓄水池抽样：
    u_i   = sha256(seed:giveaway_id:user_id) 归一化到 (0,1)
    key_i = u_i ** (1 / weight_i)
按 key 从大到小取前 k 名。weight 全为 1 时就是等概率均匀抽取。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Sequence, TypeVar

T = TypeVar("T")

_DENOM = float(1 << 64)


def new_seed() -> str:
    return secrets.token_hex(16)


def _unit(seed: str, giveaway_id: int, user_id: int) -> float:
    digest = hashlib.sha256(f"{seed}:{giveaway_id}:{user_id}".encode()).hexdigest()
    raw = int(digest[:16], 16)
    # 落在开区间 (0, 1)，避免 0 让 key 恒为 0
    return (raw + 0.5) / _DENOM


def score(seed: str, giveaway_id: int, user_id: int, weight: int = 1) -> float:
    w = max(1, int(weight))
    return _unit(seed, giveaway_id, user_id) ** (1.0 / w)


def draw(
    candidates: Sequence[T],
    winners_count: int,
    seed: str,
    giveaway_id: int,
    *,
    weighted: bool = False,
) -> list[T]:
    """从 candidates 里抽 winners_count 个。

    candidates 的元素需要有 user_id / weight 属性（db.Participant 就是）。
    结果按抽中顺序（第 1 名、第 2 名……）返回。
    """
    if winners_count <= 0 or not candidates:
        return []
    ranked = sorted(
        candidates,
        key=lambda p: (
            -score(seed, giveaway_id, p.user_id, p.weight if weighted else 1),
            p.user_id,
        ),
    )
    return list(ranked[:winners_count])


def draw_names(names: Sequence[str], winners_count: int, seed: str | None = None) -> tuple[list[str], str]:
    """手动名单模式：从一串名字里随机抽，返回 (中奖名单, seed)。"""
    seed = seed or new_seed()
    uniq: list[str] = []
    seen: set[str] = set()
    for n in names:
        n = n.strip()
        if n and n not in seen:
            seen.add(n)
            uniq.append(n)
    if winners_count <= 0 or not uniq:
        return [], seed
    ranked = sorted(
        enumerate(uniq),
        key=lambda item: hashlib.sha256(f"{seed}:{item[1]}".encode()).hexdigest(),
    )
    return [name for _, name in ranked[: min(winners_count, len(uniq))]], seed
