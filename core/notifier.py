"""群抽奖系统 —— 消息构造与发送。

这里集中处理三件事：

1. **会话标识解析**：从 ``unified_msg_origin`` 里取出平台 ID / 群号 / 用户 ID；
2. **真实 @ 的能力探测**：OneBot 系（aiocqhttp）支持真实 At，其它平台降级为文本；
3. **私聊主动发送**：先走 ``{platform_id}:FriendMessage:{user_id}`` 这个通用私聊
   会话；发不出去时（对方不是好友）再用 OneBot 的「群临时会话」兜底重试。
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


def build_at_all_chain(text: str) -> list[Any] | None:
    """构造「@全体成员 + 正文」的消息组件列表。

    Args:
        text: 正文。

    Returns:
        组件列表；平台组件不可用或缺少 ``AtAll`` 时返回 ``None``。
    """
    mc = _components()
    if mc is None:
        return None
    at_all = getattr(mc, "AtAll", None)
    if at_all is None:
        return None
    try:
        return [at_all(), mc.Plain(" " + text)]
    except Exception:
        return None


async def bot_is_group_admin(event: Any, group_id: str) -> bool:
    """判断**机器人自己**在指定群里是不是管理员 / 群主。

    QQ 只有管理员能 @全体成员，且每天有次数限制，所以先问清楚再决定要不要 @。

    仅 OneBot 系（aiocqhttp）提供 ``get_group_member_info``；其它平台一律返回
    ``False``，即降级为普通公告。

    Args:
        event: 当前消息事件（群聊 / 私聊均可，私聊也能拿到 ``bot`` 与 ``self_id``）。
        group_id: 目标群号。

    Returns:
        机器人是否具备 @全体成员 的权限。
    """
    try:
        if event.get_platform_name() != "aiocqhttp":
            return False
        bot = getattr(event, "bot", None)
        self_id = str(event.get_self_id() or "")
        if bot is None or not str(group_id).isdigit() or not self_id.isdigit():
            return False
        try:
            info = await bot.get_group_member_info(
                group_id=int(group_id),
                user_id=int(self_id),
                no_cache=True,
            )
        except TypeError:
            # 少数协议端不认 no_cache 参数
            info = await bot.get_group_member_info(
                group_id=int(group_id),
                user_id=int(self_id),
            )
    except Exception as exc:
        logger.debug(f"[群抽奖] 查询机器人群身份失败（按非管理员处理）：{exc}")
        return False
    role = str((info or {}).get("role") or "")
    return role in ("owner", "admin")


async def send_group_text(
    context: Any,
    umo: str,
    text: str,
    at_user_ids: Sequence[str] = (),
    allow_at: bool = True,
    at_all: bool = False,
) -> bool:
    """向群聊会话发送文本，可选在文首 @全体成员 或 @ 若干人。

    真实 @ 失败（平台不支持、机器人不是管理员、@全体成员 配额用尽、被风控等）
    时自动退回纯文本，保证公告一定发得出去。

    Args:
        context: 插件 Context。
        umo: 目标会话标识。
        text: 正文。
        at_user_ids: 需要 @ 的用户 ID 列表。
        allow_at: 是否尝试真实 @。
        at_all: 是否尝试 @全体成员（优先级高于 ``at_user_ids``）。

    Returns:
        是否发送成功。
    """
    mc = _components()
    if mc is None:
        logger.warning("[群抽奖] 消息组件不可用，跳过发送")
        return False

    if at_all and platform_supports_at(context, umo):
        chain = build_at_all_chain(text)
        if chain:
            try:
                if await context.send_message(umo, _wrap(chain)) is not False:
                    return True
            except Exception as exc:
                logger.warning(f"[群抽奖] @全体成员 发送失败，降级为普通公告：{exc}")

    if allow_at and at_user_ids and platform_supports_at(context, umo):
        chain = build_at_chain(at_user_ids, text)
        if chain:
            try:
                if await context.send_message(umo, _wrap(chain)) is not False:
                    return True
            except Exception as exc:
                logger.warning(f"[群抽奖] 真实 @ 发送失败，降级为纯文本：{exc}")

    try:
        sent = await context.send_message(umo, _wrap([mc.Plain(text)]))
    except Exception as exc:
        logger.error(f"[群抽奖] 群消息发送失败：{exc}")
        return False
    if sent is False:
        logger.error(f"[群抽奖] 群消息发送失败：找不到平台适配器 {platform_of(umo)}")
        return False
    return True


def resolve_bot(context: Any, platform_id: str) -> Any | None:
    """按平台 ID 找到底层 OneBot 客户端实例。

    定时开奖与面板触发开奖时手边没有 ``event``，也就拿不到 ``event.bot``；
    这里改从 ``context.platform_manager.platform_insts`` 里按平台 ID 找适配器，
    取出它内部的 ``CQHttp`` 实例（``AiocqhttpAdapter.bot``）。

    Args:
        context: 插件 Context。
        platform_id: 平台适配器 ID（``event.get_platform_id()``）。

    Returns:
        aiocqhttp 的 ``CQHttp`` 实例；找不到、平台不是 aiocqhttp 或适配器没暴露
        ``bot`` 时返回 ``None``（调用方据此退回通用私聊会话）。
    """
    if not platform_id:
        return None
    try:
        manager = getattr(context, "platform_manager", None)
        insts = list(getattr(manager, "platform_insts", None) or []) if manager else []
    except Exception:
        return None
    for inst in insts:
        try:
            meta = inst.meta()
        except Exception:
            continue
        if getattr(meta, "id", None) != platform_id:
            continue
        # 只有 OneBot 系适配器才认 send_private_msg 的 group_id 参数
        if getattr(meta, "name", "") != "aiocqhttp":
            return None
        return getattr(inst, "bot", None)
    return None


async def send_private_text(
    context: Any,
    platform_id: str,
    user_id: str,
    text: str,
    *,
    bot: Any = None,
    group_id: str = "",
) -> bool:
    """向指定用户私聊发送文本。

    **非好友也能送达**：QQ 里机器人给非好友发私聊会直接被拒，但 OneBot 协议端
    （NapCat 等）支持「群临时会话」—— ``send_private_msg`` 带上 ``group_id``
    时，协议端先看对方是不是好友（是则走 C2C），不是好友就用该群发起临时会话。

    因此这里**先走通用私聊会话**（和以前完全一致，好友路径不受影响、也不绕过
    AstrBot 的消息管道），只有它发不出去时才用群临时会话兜底重试。

    Args:
        context: 插件 Context。
        platform_id: 平台适配器 ID。
        user_id: 用户 ID。
        text: 正文。
        bot: OneBot 客户端实例；留空且给了 ``group_id`` 时自动按平台 ID 查找。
        group_id: 用户所在的群号，用于发起群临时会话。

    Returns:
        是否投递成功。判定依据有两条：

        1. ``context.send_message`` 返回 False —— 没找到匹配的平台适配器；
        2. 抛异常 —— aiocqhttp 下「未加好友」会让 ``bot.send_private_msg`` 抛
           ``ActionFailed``，而适配器不做捕获。

        注意 ``Context.send_message`` 的返回值语义是「**是否找到平台**」而非
        「是否送达」；适配器若自行吞掉错误，两条通道都会误判成功。
    """
    mc = _components()
    if mc is None or not platform_id or not user_id:
        return False

    umo = private_umo(platform_id, user_id)
    try:
        sent = await context.send_message(umo, _wrap([mc.Plain(text)]))
    except Exception as exc:
        logger.debug(f"[群抽奖] 通用私聊失败（{umo}）：{exc} —— 改用群临时会话重试")
        sent = False
    if sent is not False:
        return True
    if not group_id:
        logger.warning(f"[群抽奖] 私聊发送失败（{umo}）")
        return False

    # 多半是「对方不是好友」：带上群号发起群临时会话
    if bot is None:
        bot = resolve_bot(context, platform_id)
    if bot is None or not str(group_id).isdigit() or not str(user_id).isdigit():
        logger.warning(f"[群抽奖] 私聊发送失败（{umo}），且无法发起群临时会话")
        return False
    try:
        # 消息用 OneBot 的数组段格式，避免正文被当成 CQ 码二次解析
        await bot.send_private_msg(
            user_id=int(user_id),
            group_id=int(group_id),
            message=[{"type": "text", "data": {"text": text}}],
        )
    except Exception as exc:
        logger.warning(
            f"[群抽奖] 群临时会话发送失败（群 {group_id} → {user_id}）：{exc}"
        )
        return False
    logger.info(f"[群抽奖] 已通过群 {group_id} 的临时会话把消息发给 {user_id}")
    return True


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
