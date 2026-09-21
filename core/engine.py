"""群抽奖系统 —— 开奖引擎（纯函数，便于单元测试）。

职责：从报名名单中随机抽取中奖者，并在密钥模式下把密钥池里的密钥
依次分配给中奖者。所有函数都不触碰数据库与网络。
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import KIND_KEY, MAX_KEYS

# 密钥分隔符：换行、逗号、分号、顿号、竖线、制表符。
# 注意不能用空格分隔——很多兑换码本身带空格或形如 "AAAA-BBBB CCCC"。
_KEY_SPLIT_RE = re.compile(r"[\r\n,，;；、|]+")


def parse_keys(text: str) -> list[str]:
    """把用户粘贴的整段文本拆成密钥列表（去重、去空、保序）。

    Args:
        text: 原始文本，可包含换行、逗号、分号等分隔符。

    Returns:
        清洗后的密钥列表，最多 ``MAX_KEYS`` 条。
    """
    if not text:
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw in _KEY_SPLIT_RE.split(text):
        item = raw.strip().strip("\t")
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)
        if len(items) >= MAX_KEYS:
            break
    return items


@dataclass
class DrawOutcome:
    """一次开奖的结算结果。"""

    winners: list[dict[str, Any]] = field(default_factory=list)
    """中奖者列表，每项含 user_id / name / rank / prize。"""

    participant_total: int = 0
    """参与本次开奖的报名总人数。"""

    seat_shortage: int = 0
    """报名人数不足导致空缺的名额数。"""

    key_shortage: int = 0
    """密钥池不足导致拿不到密钥的中奖者人数。"""

    @property
    def winner_count(self) -> int:
        """实际中奖人数。"""
        return len(self.winners)


def draw(
    participants: Sequence[dict[str, Any]],
    winner_count: int,
    keys: Sequence[str] | None = None,
    prize_kind: str = "contact",
    rng: random.Random | None = None,
) -> DrawOutcome:
    """执行一次开奖结算。

    Args:
        participants: 报名名单，每项至少含 ``user_id``，可含 ``name``。
        winner_count: 计划中奖名额。
        keys: 密钥池中尚未发放的密钥（按顺序取用），仅密钥模式使用。
        prize_kind: ``contact``（联系群主）或 ``key``（私聊发密钥）。
        rng: 随机源，测试时可注入固定种子。

    Returns:
        DrawOutcome 结算结果。名单为空时返回空结果。
    """
    pool = [p for p in participants if p.get("user_id")]
    total = len(pool)
    outcome = DrawOutcome(participant_total=total)
    if total == 0:
        return outcome

    seats = max(0, int(winner_count))
    if seats > total:
        outcome.seat_shortage = seats - total
        seats = total
    if seats == 0:
        return outcome

    picker = rng or random
    picked = picker.sample(pool, seats)

    key_pool = list(keys or []) if prize_kind == KIND_KEY else []
    for index, person in enumerate(picked):
        prize = key_pool[index] if index < len(key_pool) else ""
        if prize_kind == KIND_KEY and not prize:
            outcome.key_shortage += 1
        outcome.winners.append(
            {
                "user_id": str(person.get("user_id", "")),
                "name": person.get("name") or str(person.get("user_id", "")),
                "rank": index + 1,
                "prize": prize,
            },
        )
    return outcome


def available_seats(raffle: dict[str, Any], free_keys: int) -> int:
    """计算本场实际可开出的中奖名额（受报名人数与密钥池限制）。"""
    seats = max(1, int(raffle.get("winner_count") or 1))
    if raffle.get("prize_kind") == KIND_KEY:
        # 密钥模式：名额以密钥池为准，避免抽出拿不到密钥的中奖者
        seats = min(seats, max(0, int(free_keys)))
    return seats
