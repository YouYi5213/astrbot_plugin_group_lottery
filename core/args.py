"""群抽奖系统 —— 命令参数解析（纯函数，便于单元测试）。

这里只负责把用户输入的一行文本拆成结构化参数，不做任何 IO。

支持的发布写法（私聊时群号由 ``main._target`` 先行剥离）::

    抽奖 发布 <奖品名> [名额]
    抽奖 发布 <群号> <奖品名> [名额]
    抽奖 发布 <群号> <奖品名> [名额] 定时 20:00 满员 8 说明 手慢无

一行式里可选的选项关键字：``名额`` / ``定时`` / ``满员`` / ``私聊`` / ``说明``。
第一个选项关键字之前的部分是「奖品名 + 名额」，之后按关键字切分。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import MAX_WINNERS

# 一行式发布支持的选项关键字（也是「值」的终止符）
OPTION_KEYWORDS = ("名额", "定时", "满员", "私聊", "说明")

# 「关 / 开」类输入
OFF_WORDS = {"关", "关闭", "off", "false", "0", "取消", "无", "不"}
ON_WORDS = {"开", "开启", "on", "true", "1", "是"}

MAX_TITLE_LEN = 60
MAX_DESC_LEN = 300


class PublishParseError(ValueError):
    """发布命令参数不合法。"""


@dataclass
class PublishSpec:
    """一行式「发布」命令解析结果。"""

    title: str = ""
    winner_count: int | None = None
    draw_at_text: str = ""
    min_players: int | None = None
    private_notify: bool | None = None
    description: str = ""
    # 解析过程中发现但不致命的问题，交由上层提示
    warnings: list[str] = field(default_factory=list)


def _split_head_options(text: str) -> tuple[list[str], list[str]]:
    """按第一个选项关键字把文本切成「头部」与「选项区」。

    Args:
        text: 去掉子命令（与私聊群号）后的原始文本。

    Returns:
        ``(头部 token 列表, 选项区 token 列表)``；没有选项时选项区为空列表。
    """
    tokens = text.split()
    for index, token in enumerate(tokens):
        if token in OPTION_KEYWORDS:
            return tokens[:index], tokens[index:]
    return tokens, []


def _take_value(tokens: list[str], index: int) -> tuple[str, int]:
    """从 ``index`` 起收集参数值，直到下一个选项关键字。

    ``定时 12-31 20:00`` 这种带空格的值需要多个 token，因此一直收集到下一个
    关键字为止。

    Args:
        tokens: 选项区 token 列表。
        index: 值的起始下标。

    Returns:
        ``(值文本, 消耗掉的 token 数)``。
    """
    parts: list[str] = []
    while index < len(tokens) and tokens[index] not in OPTION_KEYWORDS:
        parts.append(tokens[index])
        index += 1
    return " ".join(parts).strip(), len(parts)


def parse_publish(text: str) -> PublishSpec:
    """解析「抽奖 发布」的参数。

    Args:
        text: 子命令之后的文本（私聊场景下群号已被剥离），例如
            ``"支付宝口令红包5元 2 定时 20:00 满员 8"``。

    Returns:
        解析结果；``winner_count`` 为 ``None`` 表示未指定。

    Raises:
        PublishParseError: 奖品名为空、名额或满员人数非法、选项缺值等。
    """
    raw = (text or "").strip()
    if not raw:
        raise PublishParseError("请填写奖品名称")

    head, options = _split_head_options(raw)
    if not head:
        raise PublishParseError("请填写奖品名称")

    spec = PublishSpec()

    # 头部末尾的纯数字是名额
    if len(head) >= 2 and head[-1].isdigit():
        spec.winner_count = int(head[-1])
        head = head[:-1]
    spec.title = " ".join(head).strip()
    if not spec.title:
        raise PublishParseError("请填写奖品名称")
    if len(spec.title) > MAX_TITLE_LEN:
        raise PublishParseError(f"奖品名称过长（{MAX_TITLE_LEN} 字以内）")
    if spec.winner_count is not None and not 1 <= spec.winner_count <= MAX_WINNERS:
        raise PublishParseError(f"中奖名额需在 1 - {MAX_WINNERS} 之间")

    _parse_options(spec, options)
    return spec


def _parse_options(spec: PublishSpec, tokens: list[str]) -> None:
    """解析选项区，就地写入 ``spec``。

    Raises:
        PublishParseError: 选项缺值或取值非法。
    """
    index = 0
    while index < len(tokens):
        keyword = tokens[index]
        value, consumed = _take_value(tokens, index + 1)
        index += 1 + consumed

        if keyword == "名额":
            spec.winner_count = _positive_int(value, "名额")
        elif keyword == "定时":
            if not value:
                raise PublishParseError(
                    "「定时」后面要跟时间，例如：定时 20:00 / 定时 +2h / 定时 12-31 20:00",
                )
            spec.draw_at_text = value
        elif keyword == "满员":
            if value.lower() in OFF_WORDS:
                spec.min_players = None
                continue
            count = _positive_int(value, "满员")
            if count < 2:
                raise PublishParseError("满员人数至少为 2")
            spec.min_players = count
        elif keyword == "私聊":
            lowered = value.lower()
            if lowered in ON_WORDS:
                spec.private_notify = True
            elif lowered in OFF_WORDS:
                spec.private_notify = False
            else:
                raise PublishParseError("「私聊」后面要跟「开」或「关」")
        elif keyword == "说明":
            if not value:
                raise PublishParseError("「说明」后面要跟文本")
            if len(value) > MAX_DESC_LEN:
                raise PublishParseError(f"说明过长（{MAX_DESC_LEN} 字以内）")
            spec.description = value
        else:  # pragma: no cover - 关键字表与分支一一对应
            spec.warnings.append(f"未知选项：{keyword}")


def _positive_int(value: str, label: str) -> int:
    """把选项值解析成正整数。

    Raises:
        PublishParseError: 不是数字或超出名额上限。
    """
    if not value.isdigit():
        raise PublishParseError(f"「{label}」后面要跟数字，例如：{label} 3")
    count = int(value)
    if not 1 <= count <= MAX_WINNERS:
        raise PublishParseError(f"中奖名额需在 1 - {MAX_WINNERS} 之间")
    return count
