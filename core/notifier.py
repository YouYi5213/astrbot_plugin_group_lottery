"""群抽奖系统 —— 消息构造与发送。

这里集中处理三件事：

1. **会话标识解析**：从 ``unified_msg_origin`` 里取出平台 ID / 群号 / 用户 ID；
2. **真实 @ 的能力探测**：OneBot 系（aiocqhttp）支持真实 At，其它平台降级为文本；
3. **私聊主动发送**：用 ``{platform_id}:FriendMessage:{user_id}`` 拼出私聊会话，
   交给 ``context.send_message`` 投递，失败时返回 False 由上层兜底。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from astrbot.api import logger

# 支持真实 @ 的适配器类型名（aiocqhttp 覆盖 NapCat / Lagrange / LLOneBot 等）
AT_CAPABLE_PLATFORMS = ("aiocqhttp",)


def platform_of(umo: str) -> str:
    """取 unified_msg_origin 里的平台 ID。"""
    return umo.split(":", 1)[0] if umo else ""


def group_id_of(umo: str) -> str:
    """取 unified_msg_origin 里的会话 ID（群聊时为群号）。

    AstrBot 开启 ``unique_session`` 后，群消息的会话段是
    ``{用户ID}_{群号}``（见 ``pipeline/waking_check/stage.py``），因此取最后
    一段才是真正的群号——各平台适配器的 ``send_by_session`` 也是这么切的。
    """
    parts = (umo or "").split(":")
    if len(parts) < 3:
        return parts[-1] if parts else ""
    return parts[2].split("_")[-1]


def group_umo(event: Any) -> str:
    """构造与 ``unique_session`` 无关的「群级」会话标识。

    抽奖是**群级**资源：同一个群里所有人都必须看到同一场抽奖。而
    ``event.unified_msg_origin`` 在开启 ``unique_session`` 时会变成按用户隔离的
    ``{platform}:GroupMessage:{用户ID}_{群号}``，直接拿它当键会让每个人各看到一场
    抽奖。这里统一改用 ``event.get_group_id()``（真实群号）拼出规范标识。

    Args:
        event: 当前消息事件。

    Returns:
        形如 ``aiocqhttp:GroupMessage:762429641`` 的会话标识；拿不到群号时
        （例如私聊）回退为事件自身的 ``unified_msg_origin``。
    """
    group_id = ""
    try:
        group_id = str(event.get_group_id() or "")
    except Exception:
        group_id = ""
    if not group_id:
        return str(getattr(event, "unified_msg_origin", "") or "")
    return f"{event.get_platform_id()}:GroupMessage:{group_id}"


def private_umo(platform_id: str, user_id: str) -> str:
    """拼出私聊会话标识，可直接传给 ``context.send_message``。

    Args:
        platform_id: 平台适配器 ID（``event.get_platform_id()``）。
        user_id: 用户 ID（aiocqhttp 为 QQ 号，qq_official 为 openid）。

    Returns:
        形如 ``aiocqhttp:FriendMessage:123456`` 的会话标识。
    """
    return f"{platform_id}:FriendMessage:{user_id}"


def _components():
    """获取消息组件模块，兼容新旧导入路径。"""
    try:
        import astrbot.api.message_components as mc

        return mc
    except Exception:  # pragma: no cover - 旧版本回退
        try:
            from astrbot.core.message import components as mc

            return mc
        except Exception:
            return None


def _message_chain():
    """获取 MessageChain 类，兼容新旧导入路径。"""
    try:
        from astrbot.api.event import MessageChain

        return MessageChain
    except Exception:  # pragma: no cover - 旧版本回退
        try:
            from astrbot.core.message.message_event_result import MessageChain

            return MessageChain
        except Exception:
            return None


def platform_supports_at(context: Any, umo: str) -> bool:
    """判断当前会话所在的平台是否支持真实 @。

    ``unified_msg_origin`` 的首段是平台适配器的**自定义 ID**（例如用户起的
    「EasonBot」），因此必须通过 ``context`` 找到适配器实例，再看它的类型名。

    Args:
        context: 插件 Context。
        umo: 会话标识。

    Returns:
        支持返回 True；找不到适配器实例时保守地返回 True（真发失败会自动降级）。
    """
    token = platform_of(umo)
    if token in AT_CAPABLE_PLATFORMS:
        return True
    try:
        manager = getattr(context, "platform_manager", None)
        insts = list(getattr(manager, "platform_insts", None) or []) if manager else []
        for inst in insts:
            try:
                meta = inst.meta()
            except Exception:
                continue
            if getattr(meta, "id", None) == token:
                return getattr(meta, "name", "") in AT_CAPABLE_PLATFORMS
    except Exception:
        pass
    return True


def build_at_chain(user_ids: Sequence[str], text: str) -> list[Any] | None:
    """构造「@A @B 文本」的消息链；组件不可用时返回 None。"""
    mc = _components()
    if mc is None:
        return None
    parts: list[Any] = []
    for uid in user_ids:
        try:
            parts.append(mc.At(qq=int(str(uid))))
        except Exception:
            try:
                parts.append(mc.At(qq=str(uid)))
            except Exception:
                continue
        try:
            parts.append(mc.Plain(" "))
        except Exception:
            pass
    try:
        parts.append(mc.Plain(text))
    except Exception:
        return None
    return parts


def _wrap(chain: list[Any]) -> Any:
    """把组件列表包成 ``context.send_message`` 接受的对象。"""
    chain_cls = _message_chain()
    if chain_cls is not None:
        try:
            return chain_cls(chain=chain)
        except Exception:
            pass
    return chain


async def send_group_text(
    context: Any,
    umo: str,
    text: str,
    at_user_ids: Sequence[str] = (),
    allow_at: bool = True,
) -> bool:
    """向群聊会话发送文本，可选在文首真实 @ 若干人。

    真实 @ 失败（平台不支持、被风控等）时自动退回纯文本，保证公告一定发得出去。

    Args:
        context: 插件 Context。
        umo: 目标会话标识。
        text: 正文。
        at_user_ids: 需要 @ 的用户 ID 列表。
        allow_at: 是否尝试真实 @。

    Returns:
        是否发送成功。
    """
    mc = _components()
    if mc is None:
        logger.warning("[群抽奖] 消息组件不可用，跳过发送")
        return False

    if allow_at and at_user_ids and platform_supports_at(context, umo):
        chain = build_at_chain(at_user_ids, text)
        if chain:
            try:
                await context.send_message(umo, _wrap(chain))
                return True
            except Exception as exc:
                logger.warning(f"[群抽奖] 真实 @ 发送失败，降级为纯文本：{exc}")

    try:
        await context.send_message(umo, _wrap([mc.Plain(text)]))
        return True
    except Exception as exc:
        logger.error(f"[群抽奖] 群消息发送失败：{exc}")
        return False


async def send_private_text(
    context: Any, platform_id: str, user_id: str, text: str
) -> bool:
    """向指定用户私聊发送文本。

    Args:
        context: 插件 Context。
        platform_id: 平台适配器 ID。
        user_id: 用户 ID。
        text: 正文。

    Returns:
        是否发送成功（用户未加好友、被风控、平台不支持主动私聊时返回 False）。
    """
    mc = _components()
    if mc is None or not platform_id or not user_id:
        return False
    umo = private_umo(platform_id, user_id)
    try:
        await context.send_message(umo, _wrap([mc.Plain(text)]))
        return True
    except Exception as exc:
        logger.warning(f"[群抽奖] 私聊发送失败（{umo}）：{exc}")
        return False


async def get_bot_groups(event: Any) -> dict[str, str] | None:
    """枚举机器人所在的群，用于校验「私聊里填的群号」是否真的可达。

    仅 OneBot 系（aiocqhttp）提供 ``get_group_list`` 接口。

    Args:
        event: 当前消息事件。

    Returns:
        ``{群号: 群名}``；平台不支持或调用失败时返回 ``None``（表示无法确认）。
    """
    try:
        if event.get_platform_name() != "aiocqhttp":
            return None
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        raw = await bot.get_group_list()
    except Exception as exc:
        logger.debug(f"[群抽奖] 获取群列表失败（可忽略）：{exc}")
        return None
    if not isinstance(raw, list):
        return None

    groups: dict[str, str] = {}
    for item in raw:
        try:
            groups[str(item.get("group_id"))] = str(item.get("group_name") or "")
        except Exception:
            continue
    return groups


async def try_recall_message(event: Any) -> bool:
    """尝试撤回触发本次事件的那条消息。

    仅在 OneBot 系适配器（aiocqhttp）且机器人有撤回权限时可用。典型用途：
    管理员误把密钥明文发到群里时，第一时间把它撤掉，缩小泄露面。

    Args:
        event: 当前消息事件。

    Returns:
        撤回成功返回 True，其余情况（平台不支持 / 无权限 / 消息过旧）返回 False。
    """
    try:
        if event.get_platform_name() != "aiocqhttp":
            return False
        bot = getattr(event, "bot", None)
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        if bot is None or not message_id:
            return False
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            pass
        await bot.delete_msg(message_id=message_id)
        return True
    except Exception as exc:
        logger.debug(f"[群抽奖] 撤回消息失败（可忽略）：{exc}")
        return False
