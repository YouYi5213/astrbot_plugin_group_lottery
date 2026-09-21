"""群抽奖系统 —— 文案渲染（纯函数，便于单元测试）。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .models import KIND_KEY
from .timeparse import format_ts, humanize_remaining


def raffle_no(raffle: dict[str, Any] | None) -> int:
    """取该场抽奖在**本群内**的期号（从 1 开始）。

    ``raffles.id`` 是跨群全局自增的内部主键，对用户没有意义；展示一律用按群
    独立编号的 ``seq``。老数据缺 ``seq`` 时回退到 ``id``，保证不会显示成 0。

    Args:
        raffle: 抽奖行（或含 ``seq`` / ``raffle_seq`` 的衍生行）。

    Returns:
        群内期号；拿不到时返回 0。
    """
    if not raffle:
        return 0
    for key in ("seq", "raffle_seq", "id", "raffle_id"):
        value = raffle.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def raffle_label(raffle: dict[str, Any] | None) -> str:
    """生成「第 N 期」文案。"""
    return f"第 {raffle_no(raffle)} 期"


# 帮助文本：主命令 + 子命令说明（权限, 用法, 说明）
HELP_ROWS: list[tuple[str, str, str]] = [
    ("所有人", "抽奖 参与", "报名参加当前抽奖（也可用「抽奖 报名」）"),
    ("所有人", "抽奖 退出", "取消自己的报名"),
    ("所有人", "抽奖 状态", "查看当前抽奖的奖品、名额、开奖时间与报名人数"),
    ("所有人", "抽奖 名单", "查看已报名成员"),
    ("所有人", "抽奖 记录", "查看本群最近的开奖结果"),
    ("所有人", "抽奖 领取", "补领自己还没收到的密钥（私聊机器人发送即可）"),
    ("管理员", "抽奖 发布 <奖品名> [名额] [选项…]", "发布一场新抽奖，选项可一次写完"),
    (
        "管理员",
        "私聊：抽奖 发布 <群号> <奖品名> [名额] [选项…]",
        "在指定群发布并自动播报，如「抽奖 发布 754797467 月卡 2 定时 20:00 满员 8」",
    ),
    (
        "管理员",
        "选项：名额 / 定时 / 满员 / 私聊 / 说明",
        "名额 3 · 定时 20:00 · 满员 8 · 私聊 开 · 说明 手慢无",
    ),
    (
        "管理员",
        "私聊：抽奖 密钥 <群号>",
        "在私聊里另起一行粘贴密钥，一行一条；密钥不会经过群聊",
    ),
    (
        "管理员",
        "抽奖 密钥 查看 / 清空",
        "群里可用：私聊查看剩余密钥 / 清空未发放的密钥",
    ),
    ("管理员", "抽奖 名额 <数字>", "修改中奖名额"),
    (
        "管理员",
        "抽奖 定时 <时间>",
        "设置自动开奖时间，如 20:00 / +2h / 12-31 20:00；「抽奖 定时 关」取消",
    ),
    ("管理员", "抽奖 满员 <人数>", "报名满 N 人立即开奖；「抽奖 满员 关」取消"),
    ("管理员", "抽奖 私聊 开|关", "密钥模式是否私聊中奖者发奖"),
    ("管理员", "抽奖 说明 <文本>", "补充奖品说明，会显示在状态里"),
    ("管理员", "抽奖 开奖", "立即开奖并公布结果"),
    ("管理员", "抽奖 取消", "取消当前抽奖（不清除历史记录）"),
    ("管理员", "抽奖 帮助", "查看本帮助"),
]


def fill(template: str, values: dict[str, Any]) -> str:
    """把模板里的 ``<key>`` 占位符替换成实际内容。

    Args:
        template: 含占位符的模板文本。
        values: 占位符名 -> 值。

    Returns:
        替换后的文本；未知占位符保持原样，便于排查。
    """
    result = template or ""
    for key, value in values.items():
        result = result.replace(f"<{key}>", "" if value is None else str(value))
    return result


def winners_text(winners: Sequence[dict[str, Any]], with_at: bool = False) -> str:
    """把中奖者列表拼成「张三、李四」或「@张三、@李四」。"""
    names = [str(w.get("name") or w.get("user_id") or "") for w in winners]
    names = [n for n in names if n]
    if not names:
        return "（无人中奖）"
    return "、".join(f"@{n}" if with_at else n for n in names)


def build_announce(
    *,
    raffle: dict[str, Any],
    winners: Sequence[dict[str, Any]],
    template: str,
    contact: str,
    group_id: str = "",
    winners_display: str | None = None,
    drawn_at: int | None = None,
    notes: Iterable[str] = (),
) -> str:
    """生成群内开奖公告。

    Args:
        raffle: 抽奖记录。
        winners: 中奖者列表。
        template: 公告模板（来自插件配置）。
        contact: 联系方式。
        group_id: 群号，用于 ``<group>`` 占位符。
        winners_display: 中奖名单的展示文本；默认自动生成。
        drawn_at: 开奖时间戳。
        notes: 追加在公告末尾的补充说明（如密钥不足提醒）。

    Returns:
        完整的公告文本。
    """
    display = winners_display
    if display is None:
        display = winners_text(winners)

    body = fill(
        template,
        {
            "winners": display,
            "count": len(winners),
            "prize": raffle.get("title", ""),
            "group": group_id or raffle.get("group_id", ""),
            "time": format_ts(drawn_at),
            "contact": contact,
            "raffle_id": raffle.get("id", ""),
        },
    ).strip()

    lines = [f"🎊 开奖结果 ·「{raffle.get('title', '')}」", "", body]
    extra = [n for n in notes if n]
    if extra:
        lines.append("")
        lines.extend(f"（{n}）" for n in extra)
    return "\n".join(lines).strip()


def build_private(
    *,
    raffle: dict[str, Any],
    winner: dict[str, Any],
    template: str,
    contact: str,
    group_id: str = "",
    drawn_at: int | None = None,
) -> str:
    """生成私聊发奖文案。"""
    return fill(
        template,
        {
            "key": winner.get("prize", ""),
            "prize": raffle.get("title", ""),
            "name": winner.get("name") or winner.get("user_id", ""),
            "group": group_id or raffle.get("group_id", ""),
            "time": format_ts(drawn_at),
            "contact": contact,
            "raffle_id": raffle.get("id", ""),
        },
    ).strip()


def build_status(
    *,
    raffle: dict[str, Any] | None,
    participant_count: int = 0,
    free_keys: int = 0,
) -> str:
    """生成「抽奖 状态」的文本。"""
    if not raffle:
        return "当前没有进行中的抽奖。管理员可发送「抽奖 发布 <奖品名>」发起一场。"

    lines = [
        f"🎁 当前抽奖 ·「{raffle.get('title', '')}」（{raffle_label(raffle)}）",
        f"中奖名额：{raffle.get('winner_count', 1)} 名",
        f"已报名：{participant_count} 人",
    ]

    if raffle.get("prize_kind") == KIND_KEY:
        need = max(1, int(raffle.get("winner_count") or 1))
        lines.append(f"奖品形式：私聊发密钥（密钥池剩余 {free_keys} 条）")
        if free_keys < need:
            lines.append(f"⚠️ 密钥池不足，还需要 {need - free_keys} 条密钥才能开满名额")

    if raffle.get("description"):
        lines.append(f"说明：{raffle['description']}")

    draw_at = raffle.get("draw_at")
    if draw_at:
        lines.append(f"自动开奖：{format_ts(draw_at)}（{humanize_remaining(draw_at)}）")
    else:
        lines.append("自动开奖：未设置（由管理员手动开奖）")

    min_players = raffle.get("min_players")
    if min_players:
        lines.append(f"满员开奖：报名满 {min_players} 人立即开奖")

    lines.append("参与方式：发送「抽奖 参与」")
    return "\n".join(lines)


def build_participants(
    names: Sequence[str], group_id: str = "", limit: int = 50
) -> str:
    """生成「抽奖 名单」的文本。"""
    if not names:
        return "还没有人报名，发送「抽奖 参与」即可参加。"
    shown = list(names[:limit])
    lines = [f"📝 群 {group_id} 已报名 {len(names)} 人："]
    lines.extend(f"{i}. {name}" for i, name in enumerate(shown, 1))
    if len(names) > limit:
        lines.append(f"…等共 {len(names)} 人（仅显示前 {limit} 位）")
    return "\n".join(lines)


def build_records(
    rows: Sequence[dict[str, Any]], group_id: str = "", limit: int = 10
) -> str:
    """生成「抽奖 记录」的文本。"""
    if not rows:
        return "本群还没有开奖记录。"
    lines = [f"📜 群 {group_id} 最近 {min(len(rows), limit)} 次开奖："]
    for row in list(rows)[:limit]:
        when = format_ts(row.get("created_at"))
        name = row.get("name") or row.get("user_id")
        prize = f" · {row['prize']}" if row.get("prize") else ""
        lines.append(
            f"· [{when}] {raffle_label(row)}「{row.get('title', '')}」→ {name}{prize}"
        )
    return "\n".join(lines)


def build_publish_notice(
    raffle: dict[str, Any], group_id: str = "", group_name: str = ""
) -> str:
    """生成「新抽奖发布」群公告。

    管理员从私聊发布抽奖时，群里看不到任何命令痕迹，因此需要单独播报一条。
    """
    lines = [
        "🎁 新抽奖开始啦！",
        "",
        f"奖品：{raffle.get('title', '')}",
        f"名额：{raffle.get('winner_count', 1)} 名",
    ]
    if raffle.get("description"):
        lines.append(f"说明：{raffle['description']}")
    if raffle.get("draw_at"):
        lines.append(f"开奖时间：{format_ts(raffle['draw_at'])}")
    else:
        lines.append("开奖时间：由管理员手动开奖")
    if raffle.get("min_players"):
        lines.append(f"满员提前开奖：报名满 {raffle['min_players']} 人")
    lines.append("")
    lines.append("参与方式：发送「抽奖 参与」")
    if group_name:
        lines.append(f"（群 {group_id} · {group_name}）")
    return "\n".join(lines)


def build_help(prefix: str = "/") -> str:
    """生成纯文本帮助（图片帮助渲染失败时的兜底）。"""
    lines = [
        "🎁 群抽奖系统 · 命令帮助",
        f"主命令：抽奖（英文别名 lottery），例如 {prefix}抽奖 发布 一等奖",
        "",
        "【群里用】直接在当前群操作，不需要写群号：",
    ]
    for perm, usage, desc in HELP_ROWS:
        if not usage.startswith("私聊："):
            lines.append(f"[{perm}] {usage} —— {desc}")
    lines.append("")
    lines.append(
        "【私聊用】把群号写在子命令后面，可跨群管理（需 AstrBot 全局管理员）："
    )
    lines.append("[管理员] 抽奖 发布 <群号> <奖品名> [名额] —— 在指定群发布抽奖并播报")
    lines.append("[管理员] 抽奖 密钥 <群号> —— 下一行起粘贴密钥，一行一条")
    lines.append("[管理员] 抽奖 状态|名单|开奖|取消|定时|满员|名额|说明|私聊 <群号>")
    lines.append("")
    lines.append("提示：密钥只能私聊机器人设置，群聊里发送会被拒绝并尝试撤回；")
    lines.append("开奖后机器人会把专属密钥私聊发给中奖者；")
    lines.append("如果没收到私聊，中奖者可在群里或私聊发送「抽奖 领取」补领。")
    return "\n".join(lines)
