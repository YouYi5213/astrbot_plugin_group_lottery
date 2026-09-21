"""群抽奖系统 —— 开奖时间解析。

支持以下几种写法（均为本地时区）：

* ``+30m`` / ``+2h`` / ``+1d`` / ``+90s`` —— 相对当前时间
* ``20:00`` —— 今天该时刻，若已过则顺延到明天
* ``12-31 20:00`` —— 今年该日期，若已过则顺延到明年
* ``2026-12-31 20:00`` / ``2026/12/31 20:00`` —— 绝对时间
* ``12-31`` / ``2026-12-31`` —— 省略时刻时默认 20:00
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

# 省略时刻时使用的默认开奖时间
DEFAULT_HOUR = 20
DEFAULT_MINUTE = 0

_RELATIVE_RE = re.compile(r"^\+\s*(\d+)\s*(s|m|h|d|秒|分|分钟|小时|天)$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_MD_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_YMD_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")

_RELATIVE_UNITS = {
    "s": 1,
    "秒": 1,
    "m": 60,
    "分": 60,
    "分钟": 60,
    "h": 3600,
    "小时": 3600,
    "d": 86400,
    "天": 86400,
}

# 允许的最长定时跨度，避免手滑写出十年后的开奖时间
MAX_AHEAD_SECONDS = 365 * 86400


def _parse_clock(text: str) -> tuple[int, int]:
    """解析 ``HH:MM``，返回 (时, 分)。"""
    match = _CLOCK_RE.match(text)
    if not match:
        raise ValueError(f"无法识别的时间：{text}（示例：20:00 / +2h / 12-31 20:00）")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围：{text}")
    return hour, minute


def _split_date_and_clock(text: str) -> tuple[str, tuple[int, int]]:
    """把 ``12-31 20:00`` 拆成日期部分与时刻部分；无时刻时用默认 20:00。

    单独一个 ``20:00`` 会被识别为「只有时刻」，此时日期部分返回空串。
    """
    parts = text.split()
    if len(parts) == 1:
        if _CLOCK_RE.match(parts[0]):
            return "", _parse_clock(parts[0])
        return parts[0], (DEFAULT_HOUR, DEFAULT_MINUTE)
    if len(parts) == 2:
        return parts[0], _parse_clock(parts[1])
    raise ValueError(f"无法识别的时间：{text}（示例：12-31 20:00）")


def parse_draw_time(text: str, now: datetime | None = None) -> tuple[int, str]:
    """把用户输入解析成开奖时间戳。

    Args:
        text: 用户输入的时间表达式。
        now: 参考时刻，默认取当前本地时间（测试时可注入）。

    Returns:
        ``(unix 时间戳, 可读描述)``，描述形如 ``2026-01-01 20:00``。

    Raises:
        ValueError: 输入无法识别，或时间点已经过去 / 超出最大跨度。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("请提供开奖时间，例如：20:00 / +2h / 12-31 20:00")

    base = now or datetime.now()

    # 1) 相对时间
    relative = _RELATIVE_RE.match(raw)
    if relative:
        amount = int(relative.group(1))
        seconds = amount * _RELATIVE_UNITS[relative.group(2).lower()]
        if seconds <= 0:
            raise ValueError("相对时间必须大于 0")
        if seconds > MAX_AHEAD_SECONDS:
            raise ValueError("开奖时间最远只能设置到一年以内")
        target = base + timedelta(seconds=seconds)
        return int(target.timestamp()), target.strftime("%Y-%m-%d %H:%M")

    date_part, (hour, minute) = _split_date_and_clock(raw)

    # 2) 只有时刻：今天该时刻，已过则顺延到明天
    if not date_part:
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= base:
            target += timedelta(days=1)
        return int(target.timestamp()), target.strftime("%Y-%m-%d %H:%M")

    # 3) 完整日期
    ymd = _YMD_RE.match(date_part)
    if ymd:
        year, month, day = (int(x) for x in ymd.groups())
    else:
        md = _MD_RE.match(date_part)
        if not md:
            raise ValueError(
                f"无法识别的时间：{raw}（示例：20:00 / +2h / 12-31 20:00 / 2026-12-31 20:00）",
            )
        month, day = int(md.group(1)), int(md.group(2))
        # 只有月日：今年，已过则顺延到明年
        try:
            candidate = datetime(base.year, month, day, hour, minute)
        except ValueError as exc:
            raise ValueError(f"日期不存在：{date_part}") from exc
        year = base.year if candidate > base else base.year + 1
        try:
            target = datetime(year, month, day, hour, minute)
        except ValueError as exc:
            raise ValueError(f"日期不存在：{date_part}") from exc
        return int(target.timestamp()), target.strftime("%Y-%m-%d %H:%M")

    try:
        target = datetime(year, month, day, hour, minute)
    except ValueError as exc:
        raise ValueError(f"日期不存在：{date_part}") from exc

    if target <= base:
        raise ValueError(f"开奖时间必须晚于当前时间（当前 {base:%Y-%m-%d %H:%M}）")
    if (target - base).total_seconds() > MAX_AHEAD_SECONDS:
        raise ValueError("开奖时间最远只能设置到一年以内")
    return int(target.timestamp()), target.strftime("%Y-%m-%d %H:%M")


def format_ts(ts: int | None) -> str:
    """把时间戳格式化为 ``YYYY-MM-DD HH:MM``；空值返回空串。"""
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")


def humanize_remaining(ts: int | None, now: datetime | None = None) -> str:
    """把剩余时间描述成「3 小时 12 分后」这类文本；已到点返回「即将开奖」。"""
    if not ts:
        return ""
    delta = int(ts) - int((now or datetime.now()).timestamp())
    if delta <= 0:
        return "即将开奖"
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时后"
    if hours:
        return f"{hours} 小时 {minutes} 分后"
    if minutes:
        return f"{minutes} 分钟后"
    return f"{delta} 秒后"
