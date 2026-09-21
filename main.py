"""群抽奖系统 —— AstrBot 插件入口。

功能概览：

* **发布抽奖**：管理员在群里发布一场抽奖，设置奖品名、中奖名额、密钥池。
* **报名参与**：群友发送「抽奖 参与」报名，可随时退出。
* **开奖方式**：管理员手动开奖 / 到点定时自动开奖 / 报名满员提前开奖。
* **开奖通知**：群内公布中奖名单并 @ 中奖者，提示联系群主领取。
* **密钥奖品**：机器人把专属密钥私聊发给每位中奖者；私聊失败时中奖者可用
  「抽奖 领取」自助补领，密钥绝不在群里明文出现。

数据全部落在 ``data/plugin_data/astrbot_plugin_group_lottery/lottery.db``。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncGenerator
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core import texts
from .core.db import LotteryDB
from .core.engine import available_seats, draw, parse_keys
from .core.models import (
    KIND_CONTACT,
    KIND_KEY,
    MAX_WINNERS,
    STATUS_CANCELLED,
    STATUS_DRAWN,
    TRIGGER_FULL,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULED,
)
from .core.notifier import (
    get_bot_groups,
    group_id_of,
    group_umo,
    platform_of,
    platform_supports_at,
    send_group_text,
    send_private_text,
    try_recall_message,
)
from .core.timeparse import parse_draw_time
from .web_api import LotteryWebApi

PLUGIN_NAME = "astrbot_plugin_group_lottery"

# 主命令词（含英文别名），用于从 message_str 里剥掉命令头
_CMD_HEAD_RE = re.compile(r"^/?(?:抽奖|抽獎|lottery|raffle)(?=\s|$)", re.IGNORECASE)

# 不带唤醒前缀的兜底正则（由 allow_no_prefix 配置控制是否生效）
_NO_PREFIX_RE = r"^/?(?:抽奖|抽獎|lottery|raffle)(?:\s|$)"

# 子命令 -> (处理方法名, 是否需要管理员)
_SUBCOMMANDS: dict[str, tuple[str, bool]] = {
    "帮助": ("help", False),
    "help": ("help", False),
    "菜单": ("help", False),
    "参与": ("join", False),
    "报名": ("join", False),
    "join": ("join", False),
    "退出": ("quit", False),
    "取消报名": ("quit", False),
    "quit": ("quit", False),
    "状态": ("status", False),
    "status": ("status", False),
    "名单": ("list", False),
    "list": ("list", False),
    "记录": ("records", False),
    "历史": ("records", False),
    "history": ("records", False),
    "领取": ("claim", False),
    "补领": ("claim", False),
    "claim": ("claim", False),
    "发布": ("publish", True),
    "create": ("publish", True),
    "密钥": ("keys", True),
    "秘钥": ("keys", True),
    "keys": ("keys", True),
    "名额": ("seats", True),
    "seats": ("seats", True),
    "定时": ("schedule", True),
    "schedule": ("schedule", True),
    "满员": ("full", True),
    "full": ("full", True),
    "私聊": ("private", True),
    "private": ("private", True),
    "说明": ("describe", True),
    "desc": ("describe", True),
    "开奖": ("draw_now", True),
    "draw": ("draw_now", True),
    "取消": ("cancel", True),
    "cancel": ("cancel", True),
}

# 私聊里可用的子命令：个人类（不需要群号）+ 管理类（首参为群号）
_PRIVATE_PERSONAL = {"help", "claim", "records"}
_PRIVATE_GROUP_SCOPED = {
    "publish",
    "keys",
    "status",
    "list",
    "seats",
    "schedule",
    "full",
    "private",
    "describe",
    "draw_now",
    "cancel",
}

_ON_OFF_TRUE = {"开", "on", "是", "1", "true", "启用", "yes"}
_ON_OFF_FALSE = {"关", "off", "否", "0", "false", "停用", "no"}
# 「关闭」类输入（定时 / 满员等开关用）
_OFF_WORDS = {"关", "off", "关闭", "取消", "无", "none", "clear"}
# 密钥池专用的清空词（刻意不含 "0" / "无"，避免把内容恰为这些字符的密钥误清空）
_KEY_CLEAR_WORDS = {"清空", "clear", "重置", "reset"}


def _parse_on_off(text: str) -> bool:
    """解析「开 / 关」类开关输入。"""
    value = (text or "").strip().lower()
    if value in _ON_OFF_TRUE:
        return True
    if value in _ON_OFF_FALSE:
        return False
    raise ValueError("请用「开」或「关」")


def _take_group_id(tail: str) -> tuple[str, str]:
    """从私聊命令的剩余文本里取出开头的群号。

    群号是第一段空白之前的内容，其余原样保留（换行不丢，便于粘贴密钥）。

    Args:
        tail: 子命令之后的原始文本，例如 ``"123456\\nABCD-1111\\nEFGH-2222"``。

    Returns:
        ``(群号, 剩余内容)``；没有内容时返回 ``("", "")``。
    """
    text = (tail or "").strip()
    if not text:
        return "", ""
    match = re.match(r"^(\S+)[ \t]*(.*)$", text, re.DOTALL)
    if not match:
        return "", ""
    return match.group(1), match.group(2).strip()


class GroupLotteryPlugin(Star):
    """群抽奖系统主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.db = LotteryDB(self.data_dir / "lottery.db")

        self._terminating = False
        self._loop_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()

        self._register_web_api()

        # 热重载场景下 __init__ 运行在事件循环里，可以直接拉起后台任务；
        # 冷启动时交给 on_astrbot_loaded 生命周期钩子。
        try:
            asyncio.get_running_loop()
            self._start_loop()
        except RuntimeError:
            pass

        logger.info("[群抽奖] 插件已加载")

    # ============================================================ 生命周期

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot 完全就绪后拉起自动开奖调度循环。"""
        self._start_loop()

    async def terminate(self) -> None:
        """插件卸载 / 重载时取消后台任务并关闭数据库。"""
        self._terminating = True
        for task in [self._loop_task, *list(self._bg_tasks)]:
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._bg_tasks.clear()
        self.db.close()
        logger.info("[群抽奖] 插件已卸载")

    def _register_web_api(self) -> None:
        """注册管理面板用到的 Web API。"""
        try:
            api = LotteryWebApi(self)
            prefix = f"/{PLUGIN_NAME}"
            self.context.register_web_api(
                f"{prefix}/overview",
                api.overview,
                ["GET"],
                "总览统计",
            )
            self.context.register_web_api(
                f"{prefix}/raffles",
                api.list_raffles,
                ["GET"],
                "抽奖场次列表",
            )
            self.context.register_web_api(
                f"{prefix}/raffle/<raffle_id>",
                api.raffle_detail,
                ["GET"],
                "抽奖详情",
            )
            self.context.register_web_api(
                f"{prefix}/raffle/keys",
                api.save_keys,
                ["POST"],
                "保存密钥池",
            )
            self.context.register_web_api(
                f"{prefix}/raffle/draw",
                api.draw_now,
                ["POST"],
                "手动开奖",
            )
            self.context.register_web_api(
                f"{prefix}/raffle/cancel",
                api.cancel,
                ["POST"],
                "取消抽奖",
            )
            self.context.register_web_api(
                f"{prefix}/winners",
                api.list_winners,
                ["GET"],
                "中奖记录",
            )
            self.context.register_web_api(
                f"{prefix}/groups",
                api.list_groups,
                ["GET"],
                "群列表",
            )
        except Exception as exc:  # pragma: no cover - 旧版本 AstrBot 无此能力
            logger.warning(f"[群抽奖] Web API 注册失败：{exc}")

    # ============================================================ 配置读取

    def _cfg(self, key: str, default: Any = None) -> Any:
        """安全读取插件配置。"""
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _contact(self) -> str:
        """联系方式文案。"""
        return str(self._cfg("contact", "群主") or "群主")

    def _announce_template(self) -> str:
        """群公告模板。"""
        return str(
            self._cfg("announce_template")
            or "🎉 恭喜 <winners> 中奖！\n奖品：<prize>（共 <count> 名）\n请尽快联系 <contact> 领取奖励。",
        )

    def _private_template(self) -> str:
        """私聊发奖模板。"""
        return str(
            self._cfg("private_template")
            or "🎉 恭喜你在「<prize>」抽奖中中奖！\n\n你的专属密钥：\n<key>",
        )

    def _at_enabled(self) -> bool:
        """是否 @ 中奖者。"""
        return bool(self._cfg("at_winners", True))

    def _group_allowed(self, umo: str) -> bool:
        """群黑白名单判定。"""
        gid = group_id_of(umo)
        if not gid:
            return True
        mode = str(self._cfg("group_mode", "blacklist") or "blacklist")
        try:
            whitelist = {str(x) for x in (self._cfg("group_whitelist", []) or [])}
            blacklist = {str(x) for x in (self._cfg("group_blacklist", []) or [])}
        except Exception:
            whitelist, blacklist = set(), set()
        if mode == "whitelist":
            return gid in whitelist
        return gid not in blacklist

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """管理员判定：AstrBot 管理员 + 配置补充 + 群主/群管理员。"""
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            uid = str(event.get_sender_id())
            admins = {str(x) for x in (self._cfg("admins", []) or [])}
            if uid and uid in admins:
                return True
        except Exception:
            pass
        try:
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            role = str(getattr(sender, "role", "") or "").lower()
            if role in ("owner", "admin", "groupowner", "groupadmin"):
                return True
        except Exception:
            pass
        return False

    def _is_global_admin(self, event: AstrMessageEvent) -> bool:
        """仅 AstrBot 全局管理员 / 插件配置管理员。

        私聊里拿不到「群主 / 群管理员」身份，因此涉及跨群资源的操作
        （例如按场次号设置密钥）只认这一档权限。
        """
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            uid = str(event.get_sender_id())
            return bool(uid) and uid in {
                str(x) for x in (self._cfg("admins", []) or [])
            }
        except Exception:
            return False

    # ============================================================ 命令入口

    @filter.command("抽奖", alias={"抽獎", "lottery", "raffle"}, priority=1)
    async def cmd_lottery(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """群抽奖系统主命令（发送「抽奖 帮助」查看全部用法）"""
        async for result in self._dispatch(event):
            yield result

    @filter.regex(_NO_PREFIX_RE, priority=0)
    async def cmd_lottery_no_prefix(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """不带唤醒前缀时的兜底入口（可在配置里关闭）"""
        # 已被标准指令 handler 接管的，直接让路，避免重复响应
        if event.get_extra("lottery_handled"):
            return
        if not bool(self._cfg("allow_no_prefix", True)):
            return
        async for result in self._dispatch(event):
            yield result

    # ---------------------------------------------------------- 命令派发

    def _split_command(self, event: AstrMessageEvent) -> tuple[str, str]:
        """把消息拆成 (子命令, 子命令之后的原始文本)。

        原始文本保留换行，便于「抽奖 密钥」一次粘贴多条密钥。
        """
        raw = (event.message_str or "").strip().lstrip("/")
        match = _CMD_HEAD_RE.match(raw)
        if match:
            raw = raw[match.end() :].strip()
        if not raw:
            return "", ""
        head, _, tail = raw.partition(" ")
        # 子命令本身可能带换行（如「抽奖\n参与」），统一取第一行
        head = head.splitlines()[0].strip() if head else ""
        return head, tail.strip()

    async def _dispatch(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """统一派发子命令，并在结束后终止事件传播。"""
        event.set_extra("lottery_handled", True)
        try:
            sub, tail = self._split_command(event)
            entry = _SUBCOMMANDS.get(sub)
            if entry is None:
                if sub:
                    yield event.plain_result(
                        f"未知子命令：{sub}\n发送「抽奖 帮助」查看全部用法。",
                    )
                else:
                    yield event.plain_result(texts.build_help())
                return

            handler_name, need_admin = entry
            is_private = bool(event.is_private_chat())

            if is_private and handler_name not in (
                _PRIVATE_PERSONAL | _PRIVATE_GROUP_SCOPED
            ):
                yield event.plain_result(
                    "这个操作需要在群里使用。\n"
                    "（私聊里可用：抽奖 发布/密钥/状态/开奖 <群号>，以及 抽奖 领取 / 抽奖 记录 / 抽奖 帮助）",
                )
                return

            # 私聊里跨群操作属于高危动作，只认全局管理员
            if (
                is_private
                and handler_name in _PRIVATE_GROUP_SCOPED
                and not self._is_global_admin(event)
            ):
                yield event.plain_result(
                    "私聊里的跨群操作仅限 AstrBot 全局管理员或插件配置里的管理员。\n"
                    "群主 / 群管理员请直接在本群使用「抽奖 发布」等命令。",
                )
                return

            if not is_private and not self._group_allowed(group_umo(event)):
                return

            if need_admin and not is_private and not self._is_admin(event):
                yield event.plain_result("仅群主 / 管理员可执行该操作。")
                return

            handler = getattr(self, f"_h_{handler_name}")
            async for result in handler(event, tail):
                yield result
        except ValueError as exc:
            yield event.plain_result(f"⚠️ 参数错误：{exc}")
        except Exception as exc:
            logger.error(f"[群抽奖] 命令执行失败：{exc}", exc_info=True)
            yield event.plain_result(f"⚠️ 执行失败：{exc}")
        finally:
            try:
                event.stop_event()
            except Exception:
                pass

    def _open_raffle(self, umo: str) -> dict[str, Any] | None:
        """取当前会话进行中的抽奖。"""
        return self.db.get_open_raffle(umo)

    def _require_open_raffle(self, umo: str) -> dict[str, Any]:
        """取当前抽奖，没有则抛错。"""
        raffle = self._open_raffle(umo)
        if not raffle:
            raise ValueError(
                "该群当前没有进行中的抽奖，可先用「抽奖 发布 <奖品名>」发起一场"
            )
        return raffle

    async def _ensure_bot_in_group(self, event: AstrMessageEvent, group_id: str) -> str:
        """确认机器人在指定群里，返回群名（拿不到时返回空串）。

        Args:
            event: 当前私聊事件。
            group_id: 管理员填写的群号。

        Returns:
            群名称，未知时为空字符串。

        Raises:
            ValueError: 机器人不在该群，或当前平台无法确认。
        """
        groups = await get_bot_groups(event)
        if groups is not None:
            if group_id not in groups:
                raise ValueError(
                    f"机器人不在群 {group_id} 里（或群号有误），无法在该群发布抽奖。",
                )
            return groups.get(group_id) or ""

        # 平台不支持枚举群列表：退化为「该群此前已经有过抽奖记录」
        known = {str(g.get("group_id")) for g in self.db.known_groups()}
        if group_id in known:
            return ""
        raise ValueError(
            f"当前平台无法确认机器人是否在群 {group_id} 中。\n"
            "请先在该群里发送一次「抽奖 发布 <奖品名>」，之后就能从私聊按群号管理了。",
        )

    async def _target(
        self, event: AstrMessageEvent, tail: str
    ) -> tuple[str, str, str, str]:
        """解析本次操作的目标群与剩余参数。

        群聊里目标就是当前会话，参数原样使用；私聊里要求参数以群号开头，
        并校验机器人确实在该群中。

        Args:
            event: 当前消息事件。
            tail: 子命令之后的原始文本。

        Returns:
            ``(umo, 群号, 群名, 剩余参数)``。

        Raises:
            ValueError: 私聊里没写群号、群号非法，或机器人不在该群。
        """
        if not event.is_private_chat():
            umo = group_umo(event)
            return umo, group_id_of(umo), "", (tail or "").strip()

        group_id, body = _take_group_id(tail)
        if not group_id:
            raise ValueError(
                "私聊里请先写群号，例如：\n"
                "抽奖 发布 123456 月卡 3\n"
                "抽奖 密钥 123456\n（下一行起粘贴密钥）\n"
                "抽奖 开奖 123456",
            )
        if not group_id.isdigit():
            raise ValueError(f"群号必须是纯数字，收到的是「{group_id}」")

        group_name = await self._ensure_bot_in_group(event, group_id)
        umo = f"{event.get_platform_id()}:GroupMessage:{group_id}"
        return umo, group_id, group_name, body

    # ---------------------------------------------------------- 通用子命令

    async def _h_help(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """查看命令帮助。"""
        yield event.plain_result(texts.build_help())

    async def _h_status(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """查看指定群当前抽奖状态。"""
        umo, group_id, group_name, _body = await self._target(event, tail)
        raffle = self._open_raffle(umo)
        if not raffle:
            yield event.plain_result(
                f"群 {group_id} 当前没有进行中的抽奖。"
                + (
                    f"\n可在群里发送「抽奖 发布 <奖品名>」发起一场，"
                    f"或私聊发送「抽奖 发布 {group_id} <奖品名>」。"
                    if event.is_private_chat()
                    else ""
                ),
            )
            return
        header = f"📍 群 {group_id}"
        if group_name:
            header += f"（{group_name}）"
        yield event.plain_result(
            header
            + "\n"
            + texts.build_status(
                raffle=raffle,
                participant_count=self.db.count_participants(int(raffle["id"])),
                free_keys=self.db.count_keys(int(raffle["id"]), only_free=True),
            ),
        )

    async def _h_list(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """查看指定群的报名名单。"""
        umo, group_id, _group_name, _body = await self._target(event, tail)
        raffle = self._open_raffle(umo)
        if not raffle:
            yield event.plain_result(f"群 {group_id} 当前没有进行中的抽奖。")
            return
        rows = self.db.list_participants(int(raffle["id"]))
        names = [str(r.get("name") or r.get("user_id")) for r in rows]
        yield event.plain_result(texts.build_participants(names, group_id=group_id))

    async def _h_records(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """查看本群最近的开奖记录。"""
        uid = str(event.get_sender_id())
        if event.is_private_chat():
            rows = self.db.list_winners(user_id=uid, limit=10)
            if not rows:
                yield event.plain_result("你还没有中奖记录。")
                return
            lines = ["📜 你最近的中奖记录："]
            for row in rows:
                key = f" · {row['prize']}" if row.get("prize") else ""
                lines.append(
                    f"· 第 {row['raffle_id']} 期「{row.get('title', '')}」"
                    f"（群 {row.get('group_id', '')}）{key}",
                )
            yield event.plain_result("\n".join(lines))
            return
        rows = self.db.list_winners(umo=group_umo(event), limit=30)
        yield event.plain_result(
            texts.build_records(rows, group_id=group_id_of(group_umo(event))),
        )

    async def _h_join(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """报名参加当前抽奖。"""
        umo = group_umo(event)
        raffle = self._open_raffle(umo)
        if not raffle:
            yield event.plain_result("当前没有进行中的抽奖，等管理员发布后再来吧～")
            return

        raffle_id = int(raffle["id"])
        uid = str(event.get_sender_id())
        name = event.get_sender_name() or uid
        created = self.db.add_participant(raffle_id, uid, name)
        total = self.db.count_participants(raffle_id)

        if created:
            yield event.plain_result(
                f"✅ 报名成功！你是第 {total} 位参与者。\n"
                f"当前抽奖：「{raffle.get('title', '')}」，中奖名额 {raffle.get('winner_count', 1)} 名。",
            )
        else:
            yield event.plain_result(f"你已经报过名啦～当前共 {total} 人参与。")

        # 满员提前开奖
        min_players = raffle.get("min_players")
        if min_players and total >= int(min_players):
            yield event.plain_result(f"🎯 报名已达 {min_players} 人，马上开奖！")
            error = await self.do_draw(raffle_id, trigger=TRIGGER_FULL)
            if error:
                logger.warning(f"[群抽奖] 满员开奖失败：{error}")

    async def _h_quit(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """取消自己的报名。"""
        umo = group_umo(event)
        raffle = self._open_raffle(umo)
        if not raffle:
            yield event.plain_result("当前没有进行中的抽奖。")
            return
        removed = self.db.remove_participant(
            int(raffle["id"]), str(event.get_sender_id())
        )
        yield event.plain_result("已取消报名。" if removed else "你当前没有报名记录。")

    async def _h_claim(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """补领 / 查看自己的密钥奖品。"""
        uid = str(event.get_sender_id())
        umo = None if event.is_private_chat() else group_umo(event)
        rows = self.db.list_winners(user_id=uid, umo=umo, limit=10)

        if not rows:
            yield event.plain_result("你还没有中奖记录。")
            return

        claimable = [r for r in rows if r.get("prize") and not r.get("notified")]
        if not claimable:
            # 没有待领取的：只回报中奖概览，避免在群里重复泄露密钥
            lines = ["📜 你的中奖记录（均已领取）："]
            for row in rows:
                lines.append(f"· 第 {row['raffle_id']} 期「{row.get('title', '')}」")
            lines.append("如需重新获取密钥，请私聊机器人发送「抽奖 领取」。")
            yield event.plain_result("\n".join(lines))
            return

        platform_id = event.get_platform_id()
        delivered: list[int] = []
        failed = 0
        for row in claimable:
            text = texts.build_private(
                raffle=row,
                winner={"prize": row.get("prize", ""), "name": row.get("name", "")},
                template=self._private_template(),
                contact=self._contact(),
                group_id=str(row.get("group_id", "")),
                drawn_at=row.get("created_at"),
            )
            if event.is_private_chat():
                yield event.plain_result(text)
                delivered.append(int(row["id"]))
            elif await send_private_text(self.context, platform_id, uid, text):
                delivered.append(int(row["id"]))
            else:
                failed += 1

        self.db.mark_claimed(delivered)

        if event.is_private_chat():
            return
        if failed:
            yield event.plain_result(
                "⚠️ 私聊发送失败，请先添加机器人为好友，然后私聊发送「抽奖 领取」。",
            )
        else:
            yield event.plain_result("✅ 已私聊发送你的密钥，请查收。")

    # ---------------------------------------------------------- 管理子命令

    async def _h_publish(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """发布一场新抽奖。

        群里：``抽奖 发布 <奖品名> [名额]``
        私聊：``抽奖 发布 <群号> <奖品名> [名额]``，并在该群播报一条开奖信息。
        """
        umo, group_id, group_name, body = await self._target(event, tail)
        from_private = event.is_private_chat()

        existing = self._open_raffle(umo)
        if existing:
            yield event.plain_result(
                f"群 {group_id} 已有一场进行中的抽奖："
                f"「{existing.get('title', '')}」（第 {existing['id']} 期）。\n"
                f"请先「抽奖 开奖」或「抽奖 取消」。",
            )
            return

        if not body:
            raise ValueError(
                "用法：抽奖 发布 <奖品名> [名额]，例如「抽奖 发布 月卡 3」"
                + (
                    "\n私聊里请写成：抽奖 发布 <群号> <奖品名> [名额]"
                    if from_private
                    else ""
                ),
            )

        winner_count = 1
        parts = body.split()
        if len(parts) >= 2 and parts[-1].isdigit():
            winner_count = int(parts[-1])
            body = " ".join(parts[:-1]).strip()
        if not body:
            raise ValueError("请填写奖品名称")
        if not 1 <= winner_count <= MAX_WINNERS:
            raise ValueError(f"中奖名额需在 1 - {MAX_WINNERS} 之间")

        raffle_id = self.db.create_raffle(
            umo=umo,
            group_id=group_id,
            title=body,
            winner_count=winner_count,
            created_by=str(event.get_sender_id()),
        )
        raffle = self.db.get_raffle(raffle_id) or {}

        # 私聊发布时群里看不到任何痕迹，需要单独播报一条
        announced = False
        if from_private:
            announced = await send_group_text(
                self.context,
                umo,
                texts.build_publish_notice(raffle, group_id, group_name),
                allow_at=False,
            )

        where = f"群 {group_id}" + (f"（{group_name}）" if group_name else "")
        lines = [
            f"🎁 抽奖已发布！（第 {raffle_id} 期 · {where}）",
            f"奖品：{body}",
            f"名额：{winner_count} 名",
        ]
        if from_private:
            lines.append(
                "✅ 已在该群播报抽奖信息。"
                if announced
                else "⚠️ 群内播报发送失败，请检查机器人是否在群内。"
            )
        lines.append("")
        lines.append("群友发送「抽奖 参与」即可报名。接下来可以：")
        lines.append(
            f"· 私聊发送「抽奖 密钥 {group_id}」再粘贴密钥 —— 设置私聊发放的密钥池"
            if from_private
            else "· 私聊机器人发送「抽奖 密钥 <群号>」再粘贴密钥 —— 设置密钥池（密钥不能发在群里）"
        )
        for usage, desc in (
            (
                f"抽奖 定时 {group_id} 20:00" if from_private else "抽奖 定时 20:00",
                "到点自动开奖",
            ),
            (
                f"抽奖 满员 {group_id} 10" if from_private else "抽奖 满员 10",
                "报名满 10 人提前开奖",
            ),
            (f"抽奖 开奖 {group_id}" if from_private else "抽奖 开奖", "立即开奖"),
        ):
            lines.append(f"· {usage} —— {desc}")
        yield event.plain_result("\n".join(lines))

    async def _h_keys(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """密钥池管理。

        **密钥内容只能私聊设置**：群里发「抽奖 密钥 <内容>」会把密钥明文暴露给
        全群，因此群里只允许「查看」「清空」这两个不含密钥内容的操作，其余输入
        一律拒绝并尝试撤回。

        私聊写法：``抽奖 密钥 <群号>``，下一行起粘贴密钥。
        """
        from_private = event.is_private_chat()
        umo, group_id, group_name, body = await self._target(event, tail)

        if (
            not from_private
            and body not in ("查看", "list", "ls")
            and body not in _KEY_CLEAR_WORDS
        ):
            # 群里带了密钥内容（或干脆没写参数）→ 拒绝，并尽量把这条消息撤回
            recalled = await try_recall_message(event)
            prefix = "🚫 密钥不能在群里发送"
            if body:
                prefix += "，本次内容已被拒绝" + (
                    "，原消息已尝试撤回。" if recalled else "。"
                )
            else:
                prefix += "。"
            yield event.plain_result(
                f"{prefix}\n\n"
                "请私聊机器人发送：\n"
                f"抽奖 密钥 {group_id}\n"
                "<粘贴密钥，一行一条>\n\n"
                "群里可用的密钥命令：抽奖 密钥 查看 / 抽奖 密钥 清空",
            )
            return

        if body in ("查看", "list", "ls"):
            raffle = self._require_open_raffle(umo)
            async for result in self._send_remaining_keys(
                event, int(raffle["id"]), group_id
            ):
                yield result
            return

        if body in _KEY_CLEAR_WORDS:
            raffle = self._require_open_raffle(umo)
            raffle_id = int(raffle["id"])
            removed = self.db.clear_keys(raffle_id)
            self.db.update_raffle(raffle_id, prize_kind=KIND_CONTACT, private_notify=0)
            yield event.plain_result(
                f"🧹 已清空群 {group_id} 第 {raffle_id} 期 {removed} 条未发放密钥，"
                "该场已切回「联系群主领取」模式。"
            )
            return

        if not body:
            raise ValueError(
                "没有读到密钥内容。\n"
                f"请先发「抽奖 密钥 {group_id}」，然后在下一行起粘贴密钥，例如：\n"
                f"抽奖 密钥 {group_id}\n"
                "ABCD-1111-2222\n"
                "EFGH-3333-4444\n\n"
                "· 密钥前加 + 表示追加到现有密钥池\n"
                f"· 抽奖 密钥 {group_id} 查看 —— 查看剩余密钥\n"
                f"· 抽奖 密钥 {group_id} 清空 —— 清空未发放密钥",
            )

        raffle = self._require_open_raffle(umo)
        raffle_id = int(raffle["id"])

        append = body.startswith("+")
        if append:
            body = body[1:].strip()
        keys = parse_keys(body)
        if not keys:
            raise ValueError("没有解析到有效密钥，请检查内容")

        inserted = self.db.add_keys(raffle_id, keys, append=append)
        total = self.db.count_keys(raffle_id, only_free=True)
        need = max(1, int(raffle.get("winner_count") or 1))
        self.db.update_raffle(raffle_id, prize_kind=KIND_KEY, private_notify=1)

        where = f"群 {group_id}" + (f"（{group_name}）" if group_name else "")
        verb = "追加" if append else "设置"
        lines = [
            f"🔑 {where} 第 {raffle_id} 期已{verb} {inserted} 条密钥，当前剩余 {total} 条。",
            "该场已切换为「私聊发密钥」模式，开奖后机器人会私聊把密钥发给中奖者。",
        ]
        if total < need:
            lines.append(
                f"⚠️ 名额为 {need} 名，密钥还差 {need - total} 条，建议继续补充。"
            )
        yield event.plain_result("\n".join(lines))

    async def _send_remaining_keys(
        self, event: AstrMessageEvent, raffle_id: int, group_id: str = ""
    ) -> AsyncGenerator[Any, None]:
        """把剩余密钥私聊发给操作者（群里只回报结果，不显示明文）。"""
        rows = self.db.list_keys(raffle_id)
        free = [r for r in rows if not r.get("assigned_to")]
        if not free:
            yield event.plain_result(f"第 {raffle_id} 期密钥池为空。")
            return
        scope = f"群 {group_id} 第 {raffle_id} 期" if group_id else f"第 {raffle_id} 期"
        lines = [f"🔑 {scope}剩余密钥（{len(free)}/{len(rows)}）："]
        lines.extend(f"{i}. {r['content']}" for i, r in enumerate(free, 1))
        sent = await send_private_text(
            self.context,
            event.get_platform_id(),
            str(event.get_sender_id()),
            "\n".join(lines),
        )
        yield event.plain_result(
            "✅ 剩余密钥已私聊发送，请查收。"
            if sent
            else "⚠️ 私聊发送失败，请先添加机器人为好友后重试。",
        )

    async def _h_seats(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """修改中奖名额：抽奖 名额 <数字>（私聊：抽奖 名额 <群号> <数字>）"""
        umo, group_id, _name, body = await self._target(event, tail)
        raffle = self._require_open_raffle(umo)
        if not body.isdigit():
            raise ValueError(
                "用法：抽奖 名额 <数字>"
                + (
                    f"，私聊里写作：抽奖 名额 {group_id} <数字>"
                    if event.is_private_chat()
                    else ""
                )
            )
        count = int(body)
        if not 1 <= count <= MAX_WINNERS:
            raise ValueError(f"中奖名额需在 1 - {MAX_WINNERS} 之间")
        self.db.update_raffle(int(raffle["id"]), winner_count=count)
        yield event.plain_result(f"✅ 群 {group_id} 中奖名额已设为 {count} 名。")

    async def _h_schedule(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """设置定时自动开奖：抽奖 定时 20:00 / +2h / 12-31 20:00 / 关"""
        umo, group_id, _name, body = await self._target(event, tail)
        raffle = self._require_open_raffle(umo)
        suffix = (
            f"（私聊写法：抽奖 定时 {group_id} <时间>）"
            if event.is_private_chat()
            else ""
        )
        if not body:
            raise ValueError(
                "用法：抽奖 定时 <时间>\n"
                "· 20:00 —— 今天 20:00（已过则明天）\n"
                "· +2h / +30m / +1d —— 相对当前时间\n"
                "· 12-31 20:00 / 2026-12-31 20:00 —— 指定日期\n"
                "· 抽奖 定时 关 —— 取消自动开奖" + suffix,
            )
        if body in _OFF_WORDS:
            self.db.update_raffle(int(raffle["id"]), draw_at=None)
            yield event.plain_result(
                f"✅ 已取消群 {group_id} 的定时自动开奖，改为手动开奖。"
            )
            return

        timestamp, readable = parse_draw_time(body)
        self.db.update_raffle(int(raffle["id"]), draw_at=timestamp)
        yield event.plain_result(
            f"⏰ 群 {group_id} 自动开奖时间已设为 {readable}，到点后机器人会自动开奖并公布名单。"
        )

    async def _h_full(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """设置满员提前开奖：抽奖 满员 <人数> / 关"""
        umo, group_id, _name, body = await self._target(event, tail)
        raffle = self._require_open_raffle(umo)
        suffix = (
            f"（私聊写法：抽奖 满员 {group_id} <人数>）"
            if event.is_private_chat()
            else ""
        )
        if not body:
            raise ValueError(
                f"用法：抽奖 满员 <人数>（报名满 N 人立即开奖）｜抽奖 满员 关{suffix}"
            )
        if body in _OFF_WORDS:
            self.db.update_raffle(int(raffle["id"]), min_players=None)
            yield event.plain_result(f"✅ 已关闭群 {group_id} 的满员提前开奖。")
            return
        if not body.isdigit():
            raise ValueError("用法：抽奖 满员 <人数>，人数需为数字")
        count = int(body)
        if count < 2:
            raise ValueError("满员人数至少为 2")
        self.db.update_raffle(int(raffle["id"]), min_players=count)
        current = self.db.count_participants(int(raffle["id"]))
        yield event.plain_result(
            f"🎯 群 {group_id} 已设置报名满 {count} 人立即开奖（当前 {current} 人）。",
        )

    async def _h_private(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """开关私聊发奖：抽奖 私聊 开|关（私聊：抽奖 私聊 <群号> 开|关）"""
        umo, group_id, _name, body = await self._target(event, tail)
        raffle = self._require_open_raffle(umo)
        enabled = _parse_on_off(body)
        raffle_id = int(raffle["id"])
        if enabled and self.db.count_keys(raffle_id, only_free=True) <= 0:
            raise ValueError(
                f"密钥池为空，请先私聊发送「抽奖 密钥 {group_id}」并粘贴密钥",
            )
        self.db.update_raffle(
            raffle_id,
            private_notify=1 if enabled else 0,
            prize_kind=KIND_KEY if enabled else KIND_CONTACT,
        )
        yield event.plain_result(
            f"✅ 群 {group_id} 已开启私聊发奖：开奖后机器人会把密钥私聊发给中奖者。"
            if enabled
            else f"✅ 群 {group_id} 已关闭私聊发奖：本场改为只公布名单，中奖者联系群主领取。",
        )

    async def _h_describe(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """补充奖品说明：抽奖 说明 <文本>"""
        umo, group_id, _name, body = await self._target(event, tail)
        raffle = self._require_open_raffle(umo)
        text = body
        if not text:
            raise ValueError(
                "用法：抽奖 说明 <文本>"
                + (
                    f"，私聊里写作：抽奖 说明 {group_id} <文本>"
                    if event.is_private_chat()
                    else ""
                )
            )
        if len(text) > 300:
            raise ValueError("说明过长（300 字以内）")
        self.db.update_raffle(int(raffle["id"]), description=text)
        yield event.plain_result(f"✅ 奖品说明已更新：{text}")

    async def _h_cancel(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """取消当前抽奖。"""
        umo, group_id, _name, _body = await self._target(event, tail)
        raffle = self._require_open_raffle(umo)
        self.db.update_raffle(
            int(raffle["id"]),
            status=STATUS_CANCELLED,
            closed_note="管理员取消",
            drawn_at=int(time.time()),
        )
        yield event.plain_result(f"🚫 群 {group_id} 第 {raffle['id']} 期抽奖已取消。")

    async def _h_draw_now(
        self, event: AstrMessageEvent, tail: str
    ) -> AsyncGenerator[Any, None]:
        """立即开奖。"""
        umo, group_id, _name, _body = await self._target(event, tail)
        raffle = self._require_open_raffle(umo)
        yield event.plain_result(f"🎲 正在为群 {group_id} 开奖，请稍候…")
        error = await self.do_draw(int(raffle["id"]), trigger=TRIGGER_MANUAL)
        if error:
            yield event.plain_result(f"⚠️ 开奖失败：{error}")

    # ============================================================ 开奖核心

    async def do_draw(
        self, raffle_id: int, trigger: str = TRIGGER_MANUAL
    ) -> str | None:
        """执行一次开奖：抽取中奖者、发放密钥、发送群公告与私聊。

        Args:
            raffle_id: 场次 ID。
            trigger: 触发来源（manual / scheduled / full）。

        Returns:
            ``None`` 表示开奖流程已走完（公告已发出）；否则返回给用户看的错误文案。
        """
        raffle = self.db.get_raffle(raffle_id)
        if not raffle or raffle.get("status") != "open":
            return "这场抽奖已经结束或不存在了。"

        umo = str(raffle["umo"])
        group_id = str(raffle.get("group_id") or group_id_of(umo))
        # 只有「密钥奖品 + 开启私聊发奖」时才真正消耗密钥池，
        # 避免出现密钥被绑定却永远送不出去的情况。
        deliver_privately = raffle.get("prize_kind") == KIND_KEY and bool(
            raffle.get("private_notify")
        )
        prize_kind = KIND_KEY if deliver_privately else KIND_CONTACT

        participants = self.db.list_participants(raffle_id)
        if not participants:
            self.db.update_raffle(
                raffle_id,
                status=STATUS_CANCELLED,
                closed_note="无人参与",
                drawn_at=int(time.time()),
                draw_trigger=trigger,
            )
            message = f"😶 第 {raffle_id} 期「{raffle.get('title', '')}」无人报名，已自动取消。"
            if trigger != TRIGGER_MANUAL:
                await send_group_text(self.context, umo, message, allow_at=False)
                return None
            return "还没有人报名，无法开奖。"

        free_keys = self.db.list_free_keys(raffle_id) if deliver_privately else []
        seats = available_seats(
            {"winner_count": raffle.get("winner_count"), "prize_kind": prize_kind},
            len(free_keys),
        )
        if seats <= 0:
            return "密钥池为空，请先用「抽奖 密钥 <内容>」设置密钥后再开奖。"

        outcome = draw(
            participants=participants,
            winner_count=seats,
            keys=[k["content"] for k in free_keys],
            prize_kind=prize_kind,
        )
        if not outcome.winners:
            return "没有可开奖的参与者。"

        # 绑定密钥归属（仅密钥模式）
        if deliver_privately:
            assignments = [
                (int(free_keys[idx]["id"]), winner["user_id"])
                for idx, winner in enumerate(outcome.winners)
                if idx < len(free_keys) and winner.get("prize")
            ]
            self.db.assign_keys(assignments)

        drawn_at = int(time.time())
        platform_id = platform_of(umo)

        # 先私聊发奖，再发群公告 —— 这样公告里能准确提示谁需要自助补领
        failed_dm: list[str] = []
        winner_rows: list[dict[str, Any]] = []
        for winner in outcome.winners:
            delivered = False
            if deliver_privately and winner.get("prize"):
                text = texts.build_private(
                    raffle=raffle,
                    winner=winner,
                    template=self._private_template(),
                    contact=self._contact(),
                    group_id=group_id,
                    drawn_at=drawn_at,
                )
                delivered = await send_private_text(
                    self.context,
                    platform_id,
                    winner["user_id"],
                    text,
                )
                if not delivered:
                    failed_dm.append(str(winner.get("name") or winner["user_id"]))
                await asyncio.sleep(0.4)  # 轻微限速，降低被风控概率
            winner_rows.append(
                {
                    "umo": umo,
                    "group_id": group_id,
                    "user_id": winner["user_id"],
                    "name": winner["name"],
                    "title": raffle.get("title", ""),
                    "prize": winner.get("prize", ""),
                    "rank": winner["rank"],
                    "notified": delivered,
                    "created_at": drawn_at,
                },
            )

        self.db.add_winners(raffle_id, winner_rows)
        self.db.update_raffle(
            raffle_id,
            status=STATUS_DRAWN,
            drawn_at=drawn_at,
            draw_trigger=trigger,
        )

        # 组装公告
        notes: list[str] = []
        if outcome.seat_shortage:
            notes.append(f"报名人数不足，空缺 {outcome.seat_shortage} 个名额")
        if outcome.key_shortage:
            notes.append(f"密钥池不足，{outcome.key_shortage} 位中奖者暂未拿到密钥")
        if failed_dm:
            notes.append(
                "以下中奖者私聊发送失败，请主动私聊机器人发送「抽奖 领取」补领密钥："
                + "、".join(failed_dm),
            )

        use_real_at = self._at_enabled() and platform_supports_at(self.context, umo)
        display = texts.winners_text(outcome.winners, with_at=not use_real_at)
        announce = texts.build_announce(
            raffle=raffle,
            winners=outcome.winners,
            template=self._announce_template(),
            contact=self._contact(),
            group_id=group_id,
            winners_display=display,
            drawn_at=drawn_at,
            notes=notes,
        )
        await send_group_text(
            self.context,
            umo,
            announce,
            at_user_ids=[w["user_id"] for w in outcome.winners] if use_real_at else (),
            allow_at=use_real_at,
        )

        # 顺带按群裁剪历史，避免数据库无限增长
        try:
            self.db.prune_history(int(self._cfg("history_limit", 200) or 200))
        except Exception as exc:
            logger.warning(f"[群抽奖] 历史记录清理失败：{exc}")

        logger.info(
            f"[群抽奖] 第 {raffle_id} 期「{raffle.get('title', '')}」开奖完成："
            f"{outcome.winner_count} 名中奖者，触发方式 {trigger}，私聊失败 {len(failed_dm)} 人",
        )
        return None

    # ============================================================ 自动开奖

    def _start_loop(self) -> None:
        """启动（或重启）自动开奖调度循环。"""
        if self._terminating:
            return
        if self._loop_task and not self._loop_task.done():
            return
        try:
            self._loop_task = asyncio.create_task(self._auto_draw_loop())
            logger.info("[群抽奖] 自动开奖调度已启动")
        except RuntimeError:
            # 还没有运行中的事件循环，交给 on_astrbot_loaded
            self._loop_task = None

    async def _auto_draw_loop(self) -> None:
        """后台循环：定期检查到点的抽奖并自动开奖。"""
        interval = max(5, int(self._cfg("check_interval_seconds", 20) or 20))
        try:
            await asyncio.sleep(5)  # 等待平台适配器就绪
        except asyncio.CancelledError:
            return

        while not self._terminating:
            try:
                await self._draw_due_raffles()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[群抽奖] 自动开奖检查出错：{exc}", exc_info=True)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _draw_due_raffles(self) -> None:
        """把所有已到开奖时间的抽奖逐个开出。"""
        for raffle in self.db.list_due_raffles(int(time.time())):
            if self._terminating:
                return
            raffle_id = int(raffle["id"])
            umo = str(raffle["umo"])
            if not self._group_allowed(umo):
                self.db.update_raffle(
                    raffle_id,
                    status=STATUS_CANCELLED,
                    closed_note="群已停用",
                    drawn_at=int(time.time()),
                    draw_trigger=TRIGGER_SCHEDULED,
                )
                continue
            try:
                error = await self.do_draw(raffle_id, trigger=TRIGGER_SCHEDULED)
            except Exception as exc:
                error = f"内部错误：{exc}"
                logger.error(
                    f"[群抽奖] 第 {raffle_id} 期自动开奖异常：{exc}", exc_info=True
                )
            if error:
                # 开奖失败（例如密钥池为空）时清掉定时，否则每个周期都会重试并刷屏
                logger.warning(f"[群抽奖] 第 {raffle_id} 期自动开奖未完成：{error}")
                self.db.update_raffle(raffle_id, draw_at=None)
                await send_group_text(
                    self.context,
                    umo,
                    f"⚠️ 第 {raffle_id} 期「{raffle.get('title', '')}」定时开奖失败：{error}\n"
                    "已自动取消定时，请管理员处理后手动发送「抽奖 开奖」。",
                    allow_at=False,
                )
            await asyncio.sleep(1)

    # ============================================================ 供面板调用

    async def panel_draw(self, raffle_id: int) -> str | None:
        """管理面板触发的开奖（与群内「抽奖 开奖」等价）。"""
        return await self.do_draw(int(raffle_id), trigger=TRIGGER_MANUAL)
