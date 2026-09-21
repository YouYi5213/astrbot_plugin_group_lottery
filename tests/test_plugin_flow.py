"""群抽奖系统 —— 端到端流程测试（用桩模块替代 AstrBot 运行时）。

覆盖：发布 / 报名 / 手动开奖 / 定时开奖 / 满员开奖 / 密钥私聊发放 /
权限校验 / 私聊补领 / 面板 Web API。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, StarTools
from astrbot_plugin_group_lottery.core.db import LotteryDB
from astrbot_plugin_group_lottery.main import GroupLotteryPlugin

GROUP_UMO = "aiocqhttp:GroupMessage:888888"
PLATFORM_ID = "aiocqhttp"
GROUP_ID = "888888"


# --------------------------------------------------------------- 事件替身


class _Bot:
    """最小 OneBot 客户端替身（群列表 + 撤回）。"""

    def __init__(self, groups=None) -> None:
        self.groups = (
            [{"group_id": int(GROUP_ID), "group_name": "测试群"}]
            if groups is None
            else groups
        )
        self.recalled: list = []

    async def get_group_list(self):
        return self.groups

    async def delete_msg(self, message_id):
        self.recalled.append(message_id)


class _Sender:
    def __init__(self, user_id: str, nickname: str, role: str = "member") -> None:
        self.user_id = user_id
        self.nickname = nickname
        self.role = role


class _MessageObj:
    def __init__(self, sender: _Sender, group_id: str, private: bool) -> None:
        self.sender = sender
        self.group_id = group_id
        self.type = "FriendMessage" if private else "GroupMessage"
        self.message_id = ""


class FakeEvent:
    """最小可用的 AstrMessageEvent 替身。

    ``unique_session=True`` 复刻 AstrBot 开启会话隔离时的行为：群消息的
    ``unified_msg_origin`` 会话段变成 ``{用户ID}_{群号}``（见
    ``pipeline/waking_check/stage.py``），而 ``get_group_id()`` 仍是真实群号。
    """

    def __init__(
        self,
        message_str: str,
        *,
        user_id: str = "1001",
        nickname: str = "测试用户",
        group_id: str = "888888",
        private: bool = False,
        role: str = "member",
        is_admin: bool = False,
        unique_session: bool = False,
    ) -> None:
        self.message_str = message_str
        self._sender = _Sender(user_id, nickname, role)
        self._group_id = group_id
        self._private = private
        self._is_admin = is_admin
        self._extras: dict = {}
        self._stopped = False
        self.bot = None
        self.message_obj = _MessageObj(self._sender, group_id, private)
        if private:
            session_id = user_id
        elif unique_session:
            session_id = f"{user_id}_{group_id}"
        else:
            session_id = group_id
        self.unified_msg_origin = (
            f"{PLATFORM_ID}:FriendMessage:{session_id}"
            if private
            else f"{PLATFORM_ID}:GroupMessage:{session_id}"
        )

    # --- AstrBot 事件接口子集 ---
    def get_message_str(self) -> str:
        return self.message_str

    def get_sender_id(self) -> str:
        return self._sender.user_id

    def get_sender_name(self) -> str:
        return self._sender.nickname

    def get_group_id(self) -> str:
        return "" if self._private else self._group_id

    def get_platform_id(self) -> str:
        return PLATFORM_ID

    def get_platform_name(self) -> str:
        return PLATFORM_ID

    def is_private_chat(self) -> bool:
        return self._private

    def is_admin(self) -> bool:
        return self._is_admin

    def set_extra(self, key, value) -> None:
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped

    def plain_result(self, text: str):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


# --------------------------------------------------------------- 测试夹具


@pytest.fixture()
def plugin() -> GroupLotteryPlugin:
    """构造插件实例（无运行中的事件循环，因此不会自动拉起调度任务）。"""
    context = Context()
    config = AstrBotConfig(
        {
            "contact": "群主 QQ 12345",
            "admins": ["9999"],
            "group_mode": "blacklist",
            "group_blacklist": [],
            "allow_no_prefix": True,
            "at_winners": True,
            "check_interval_seconds": 5,
            "history_limit": 50,
        },
    )

    # 每个用例换一个独立数据库文件，避免相互污染
    data_dir = StarTools.get_data_dir("astrbot_plugin_group_lottery") / "cases"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / f"case_{time.time_ns():x}.db"

    instance = GroupLotteryPlugin(context, config)
    instance.db.close()
    instance.db = LotteryDB(db_path)

    yield instance

    instance.db.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def run(coro):
    """在独立事件循环里执行协程。"""
    return asyncio.run(coro)


async def send(plugin: GroupLotteryPlugin, event: FakeEvent) -> list:
    """驱动一次命令派发，收集插件产出的回复。"""
    results = []
    async for item in plugin._dispatch(event):
        results.append(item)
    return results


def texts_of(results: list) -> str:
    """把回复列表拼成一段可断言的文本。"""
    return "\n".join(str(item[1]) for item in results if item and item[0] == "plain")


def sent_texts(plugin: GroupLotteryPlugin, umo: str | None = None) -> list[str]:
    """取出插件通过 context.send_message 主动发出的文本。"""
    out = []
    for session, chain in plugin.context.sent:
        if umo and session != umo:
            continue
        parts = []
        for comp in getattr(chain, "chain", []):
            parts.append(
                getattr(comp, "text", None) or f"<at:{getattr(comp, 'qq', '')}>"
            )
        out.append("".join(parts))
    return out


def admin_event(text: str, **kwargs) -> FakeEvent:
    """构造群里的管理员事件。"""
    kwargs.setdefault("is_admin", True)
    return FakeEvent(text, **kwargs)


def private_admin_event(text: str, *, groups=None, **kwargs) -> FakeEvent:
    """构造私聊里的全局管理员事件（带 OneBot 群列表能力）。"""
    kwargs.setdefault("is_admin", True)
    event = FakeEvent(text, private=True, **kwargs)
    event.bot = _Bot(groups=groups)
    return event


# --------------------------------------------------------------- 发布 / 报名


def test_publish_one_liner_with_options(plugin: GroupLotteryPlugin):
    """一行式发布：奖品名 + 名额 + 定时 + 满员 一次写完。"""
    out = texts_of(
        run(
            send(plugin, admin_event("抽奖 发布 支付宝口令红包5元 2 定时 20:00 满员 8"))
        ),
    )
    assert "抽奖已发布" in out
    assert "奖品：支付宝口令红包5元" in out
    assert "名额：2 名" in out
    assert "开奖时间：" in out
    assert "满员提前开奖：报名满 8 人" in out

    raffle = plugin.db.get_open_raffle(GROUP_UMO)
    assert raffle["title"] == "支付宝口令红包5元"
    assert raffle["winner_count"] == 2
    assert raffle["draw_at"] is not None
    assert raffle["min_players"] == 8


def test_publish_one_liner_from_private(plugin: GroupLotteryPlugin):
    """私聊一行式：群号 + 奖品 + 名额 + 定时 + 满员 + 说明。"""
    out = texts_of(
        run(
            send(
                plugin,
                private_admin_event(
                    f"抽奖 发布 {GROUP_ID} 月卡 3 定时 +2h 满员 10 说明 手慢无",
                ),
            )
        )
    )
    assert "抽奖已发布" in out
    assert "说明：手慢无" in out

    raffle = plugin.db.get_open_raffle(GROUP_UMO)
    assert raffle["title"] == "月卡"
    assert raffle["winner_count"] == 3
    assert raffle["min_players"] == 10
    assert raffle["description"] == "手慢无"
    # 群里要收到播报，且播报里带上说明与满员条件
    notice = sent_texts(plugin, GROUP_UMO)[-1]
    assert "手慢无" in notice
    assert "报名满 10 人" in notice


def test_publish_one_liner_reports_bad_option(plugin: GroupLotteryPlugin):
    out = texts_of(run(send(plugin, admin_event("抽奖 发布 月卡 满员 1"))))
    assert "满员人数至少为 2" in out
    # 参数错误时不该建出场次
    assert plugin.db.get_open_raffle(GROUP_UMO) is None


def test_publish_one_liner_does_not_create_on_bad_time(plugin: GroupLotteryPlugin):
    out = texts_of(run(send(plugin, admin_event("抽奖 发布 月卡 定时 不是时间"))))
    assert "参数错误" in out or "时间" in out
    assert plugin.db.get_open_raffle(GROUP_UMO) is None


def test_publish_and_join_flow(plugin: GroupLotteryPlugin):
    out = texts_of(run(send(plugin, admin_event("抽奖 发布 月卡 2"))))
    assert "抽奖已发布" in out
    assert "名额：2 名" in out
    assert "第 1 期" in out

    raffle = plugin.db.get_open_raffle(GROUP_UMO)
    assert raffle and raffle["title"] == "月卡" and raffle["winner_count"] == 2

    first = texts_of(
        run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    )
    assert "报名成功" in first and "第 1 位参与者" in first

    dup = texts_of(
        run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    )
    assert "已经报过名" in dup

    run(send(plugin, FakeEvent("抽奖 参与", user_id="1002", nickname="乙")))
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1003", nickname="丙")))

    status = texts_of(run(send(plugin, FakeEvent("抽奖 状态"))))
    assert "已报名：3 人" in status
    assert "未设置（由管理员手动开奖）" in status

    listing = texts_of(run(send(plugin, FakeEvent("抽奖 名单"))))
    assert "已报名 3 人" in listing and "甲" in listing


def test_publish_requires_admin(plugin: GroupLotteryPlugin):
    out = texts_of(run(send(plugin, FakeEvent("抽奖 发布 月卡"))))
    assert "仅群主 / 管理员" in out
    assert plugin.db.get_open_raffle(GROUP_UMO) is None


def test_publish_rejects_second_open_raffle(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    out = texts_of(run(send(plugin, admin_event("抽奖 发布 点卡"))))
    assert "已有一场进行中的抽奖" in out


def test_quit_and_cancel(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001")))
    assert "已取消报名" in texts_of(
        run(send(plugin, FakeEvent("抽奖 退出", user_id="1001")))
    )
    assert "没有报名记录" in texts_of(
        run(send(plugin, FakeEvent("抽奖 退出", user_id="1001")))
    )

    assert "已取消" in texts_of(run(send(plugin, admin_event("抽奖 取消"))))
    assert plugin.db.get_open_raffle(GROUP_UMO) is None


def test_unknown_subcommand_hint(plugin: GroupLotteryPlugin):
    assert "未知子命令" in texts_of(run(send(plugin, FakeEvent("抽奖 乱写"))))
    assert "命令帮助" in texts_of(run(send(plugin, FakeEvent("抽奖"))))
    assert "命令帮助" in texts_of(run(send(plugin, FakeEvent("抽奖 帮助"))))


# --------------------------------------------------------------- 手动开奖


def test_manual_draw_announces_winner_and_contacts_owner(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡 1")))
    for uid, name in (("1001", "甲"), ("1002", "乙")):
        run(send(plugin, FakeEvent("抽奖 参与", user_id=uid, nickname=name)))

    results = run(send(plugin, admin_event("抽奖 开奖")))
    assert "正在为群 888888 开奖" in texts_of(results)

    announcements = sent_texts(plugin, GROUP_UMO)
    assert announcements, "应当发送群公告"
    announce = announcements[-1]
    assert "开奖结果" in announce and "月卡" in announce
    assert "群主 QQ 12345" in announce
    assert "<at:" in announce, "应当真实 @ 中奖者"

    winners = plugin.db.list_winners(raffle_id=1)
    assert len(winners) == 1
    assert winners[0]["name"] in ("甲", "乙")
    assert plugin.db.get_raffle(1)["status"] == "drawn"
    assert plugin.db.get_raffle(1)["draw_trigger"] == "manual"


def test_draw_without_participants(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    out = texts_of(run(send(plugin, admin_event("抽奖 开奖"))))
    assert "还没有人报名" in out


def test_draw_shortage_note(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡 5")))
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))
    assert "空缺 4 个名额" in sent_texts(plugin, GROUP_UMO)[-1]


def test_at_winners_disabled_uses_text_mention(plugin: GroupLotteryPlugin):
    plugin.config["at_winners"] = False
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))
    announce = sent_texts(plugin, GROUP_UMO)[-1]
    assert "@甲" in announce
    assert "<at:" not in announce


# --------------------------------------------------------------- 密钥模式


def test_group_chat_refuses_keys(plugin: GroupLotteryPlugin):
    """群聊里发密钥必须被拒绝，否则全群都看得到明文。"""
    run(send(plugin, admin_event("抽奖 发布 月卡 2")))

    out = texts_of(run(send(plugin, admin_event("抽奖 密钥 LEAK-1111"))))
    assert "密钥不能在群里发送" in out
    assert f"抽奖 密钥 {GROUP_ID}" in out  # 指引里带上群号

    # 一条都不许入库
    assert plugin.db.count_keys(1) == 0
    assert plugin.db.get_raffle(1)["prize_kind"] == "contact"

    # 完全不带参数时只给指引，同样不报错
    out = texts_of(run(send(plugin, admin_event("抽奖 密钥"))))
    assert "请私聊机器人发送" in out


def test_group_chat_recalls_leaked_key_message(plugin: GroupLotteryPlugin):
    """OneBot 平台下，误发到群里的密钥消息应被尝试撤回。"""
    run(send(plugin, admin_event("抽奖 发布 月卡")))

    event = admin_event("抽奖 密钥 LEAK-2222")
    bot = _Bot()
    event.bot = bot
    event.message_obj.message_id = 4242

    out = texts_of(run(send(plugin, event)))
    assert bot.recalled == [4242]
    assert "原消息已尝试撤回" in out
    assert plugin.db.count_keys(1) == 0


def set_keys(
    plugin: GroupLotteryPlugin,
    content: str,
    group_id: str = GROUP_ID,
    *,
    user_id: str = "1001",
    is_admin: bool = True,
) -> str:
    """走私聊路径设置密钥（群聊路径已被禁止）。"""
    event = private_admin_event(
        f"抽奖 密钥 {group_id}\n{content}", user_id=user_id, is_admin=is_admin
    )
    return texts_of(run(send(plugin, event)))


def test_private_key_setup(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡 2")))

    out = set_keys(plugin, "KEY-AAA\nKEY-BBB\nKEY-CCC")
    assert "已设置 3 条密钥" in out
    assert "私聊发密钥" in out

    raffle = plugin.db.get_open_raffle(GROUP_UMO)
    assert raffle["prize_kind"] == "key"
    assert raffle["private_notify"] == 1


def test_private_key_setup_single_line(plugin: GroupLotteryPlugin):
    """也支持把密钥写在群号同一行。"""
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    assert "已设置 2 条密钥" in set_keys(plugin, "AAA,BBB")
    assert plugin.db.count_keys(1) == 2


def test_private_key_setup_requires_global_admin(plugin: GroupLotteryPlugin):
    """私聊里的跨群操作只认全局管理员 / 配置管理员。"""
    run(send(plugin, admin_event("抽奖 发布 月卡")))

    # 普通群友
    out = set_keys(plugin, "NOPE", user_id="1001", is_admin=False)
    assert "全局管理员" in out
    assert plugin.db.count_keys(1) == 0

    # 私聊消息里带了群主角色标记：仍然必须被拦住
    sneaky = FakeEvent(
        f"抽奖 密钥 {GROUP_ID}\nNOPE", user_id="1001", private=True, role="owner"
    )
    sneaky.bot = _Bot()
    out = texts_of(run(send(plugin, sneaky)))
    assert "全局管理员" in out
    assert plugin.db.count_keys(1) == 0

    # 配置里的管理员可以设置
    assert "已设置 1 条密钥" in set_keys(
        plugin, "OK-KEY", user_id="9999", is_admin=False
    )


def test_private_key_setup_validates_group_id(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))

    # 没写群号
    assert "私聊里请先写群号" in texts_of(
        run(send(plugin, private_admin_event("抽奖 密钥")))
    )
    # 群号不是数字
    assert "群号必须是纯数字" in texts_of(
        run(send(plugin, private_admin_event("抽奖 密钥 abc\nAAA")))
    )
    # 机器人不在该群
    assert "机器人不在群 999999 里" in texts_of(
        run(send(plugin, private_admin_event("抽奖 密钥 999999\nAAA")))
    )
    assert plugin.db.count_keys(1) == 0


def test_private_key_setup_falls_back_to_known_groups(plugin: GroupLotteryPlugin):
    """平台不支持枚举群列表时，退化为「该群此前有过抽奖记录」。"""
    run(send(plugin, admin_event("抽奖 发布 月卡")))

    event = FakeEvent(f"抽奖 密钥 {GROUP_ID}\nFALLBACK", private=True, is_admin=True)
    event.bot = None  # 非 OneBot / 无群列表能力
    assert "已设置 1 条密钥" in texts_of(run(send(plugin, event)))

    unknown = FakeEvent("抽奖 密钥 777777\nX", private=True, is_admin=True)
    unknown.bot = None
    assert "无法确认机器人是否在群 777777" in texts_of(run(send(plugin, unknown)))


def test_private_key_setup_rejects_closed_raffle(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1002", nickname="乙")))
    run(send(plugin, admin_event("抽奖 开奖")))
    assert "没有进行中的抽奖" in set_keys(plugin, "TOO-LATE")


def test_private_key_append_and_replace(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    set_keys(plugin, "AAA")
    set_keys(plugin, "+BBB")
    assert plugin.db.count_keys(1) == 2

    set_keys(plugin, "CCC")  # 默认覆盖
    assert plugin.db.count_keys(1) == 1


def test_private_key_view_and_clear(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    set_keys(plugin, "AAA,BBB")
    plugin.context.sent.clear()

    out = set_keys(plugin, "查看")
    assert "已私聊发送" in out
    assert "AAA" in "\n".join(sent_texts(plugin))

    out = set_keys(plugin, "清空")
    assert "已清空群 888888 第 1 期 2 条" in out
    assert plugin.db.count_keys(1) == 0


def test_group_key_view_and_clear_still_work(plugin: GroupLotteryPlugin):
    """群里仍可用「查看 / 清空」——它们本身不含密钥明文。"""
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    set_keys(plugin, "AAA,BBB")
    plugin.context.sent.clear()

    out = texts_of(run(send(plugin, admin_event("抽奖 密钥 查看"))))
    assert "已私聊发送" in out
    assert "AAA" in "\n".join(sent_texts(plugin))

    out = texts_of(run(send(plugin, admin_event("抽奖 密钥 清空"))))
    assert "已清空群 888888 第 1 期 2 条" in out
    assert plugin.db.get_raffle(1)["prize_kind"] == "contact"


def test_key_mode_private_delivery(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡 2")))
    out = set_keys(plugin, "KEY-AAA\nKEY-BBB\nKEY-CCC")
    assert "已设置 3 条密钥" in out
    assert "私聊发密钥" in out

    raffle = plugin.db.get_open_raffle(GROUP_UMO)
    assert raffle["prize_kind"] == "key"
    assert raffle["private_notify"] == 1

    for uid, name in (("1001", "甲"), ("1002", "乙"), ("1003", "丙")):
        run(send(plugin, FakeEvent("抽奖 参与", user_id=uid, nickname=name)))

    run(send(plugin, admin_event("抽奖 开奖")))

    winners = plugin.db.list_winners(raffle_id=1)
    assert len(winners) == 2
    keys = {w["prize"] for w in winners}
    assert len(keys) == 2 and "" not in keys
    assert keys <= {"KEY-AAA", "KEY-BBB", "KEY-CCC"}
    assert all(w["notified"] == 1 for w in winners)

    # 每位中奖者都会收到私聊
    sent_sessions = [s for s, _ in plugin.context.sent]
    for winner in winners:
        assert f"aiocqhttp:FriendMessage:{winner['user_id']}" in sent_sessions

    # 剩下的 1 条密钥私聊告知发布者（1001）
    leftover = [k["content"] for k in plugin.db.list_free_keys(1)]
    assert len(leftover) == 1
    admin_text = "\n".join(sent_texts(plugin, "aiocqhttp:FriendMessage:1001"))
    assert "还有 1 条密钥没有送出" in admin_text
    assert leftover[0] in admin_text

    # 中奖者的私聊里只出现自己那条密钥
    for winner in winners:
        own = "\n".join(
            sent_texts(plugin, f"aiocqhttp:FriendMessage:{winner['user_id']}")
        )
        assert winner["prize"] in own

    # 群公告绝不出现密钥明文
    announce = sent_texts(plugin, GROUP_UMO)[-1]
    for key in ("KEY-AAA", "KEY-BBB", "KEY-CCC"):
        assert key not in announce
    # 公告只提示条数，不泄露内容
    assert "另有 1 条密钥未送出" in announce


def test_key_shortage_is_reported(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡 3")))
    set_keys(plugin, "ONLY-ONE")
    for uid in ("1001", "1002", "1003"):
        run(send(plugin, FakeEvent("抽奖 参与", user_id=uid, nickname=f"用户{uid}")))

    run(send(plugin, admin_event("抽奖 开奖")))
    winners = plugin.db.list_winners(raffle_id=1)
    # 名额被密钥池限制为 1
    assert len(winners) == 1
    assert winners[0]["prize"] == "ONLY-ONE"


# ------------------------------------------------- 开奖后剩余密钥的归属


def test_leftover_keys_dm_the_admin(plugin: GroupLotteryPlugin):
    """3 个名额 3 条密钥，但只有 2 人报名 → 剩余密钥私聊告知发布者。"""
    run(send(plugin, admin_event("抽奖 发布 月卡 3")))
    set_keys(plugin, "KEY-AAA\nKEY-BBB\nKEY-CCC")
    for uid, name in (("1001", "甲"), ("1002", "乙")):
        run(send(plugin, FakeEvent("抽奖 参与", user_id=uid, nickname=name)))

    run(send(plugin, admin_event("抽奖 开奖")))

    winners = plugin.db.list_winners(raffle_id=1)
    assert len(winners) == 2

    leftover = [k["content"] for k in plugin.db.list_free_keys(1)]
    assert len(leftover) == 1, "应该正好剩 1 条密钥"

    # 发布者（1001）收到私聊提醒，且带明文与原因
    admin_text = "\n".join(sent_texts(plugin, "aiocqhttp:FriendMessage:1001"))
    assert "还有 1 条密钥没有送出" in admin_text
    assert leftover[0] in admin_text
    assert "报名人数不足" in admin_text
    assert "抽奖 密钥 888888 查看" in admin_text

    # 群里只出现条数，不出现明文
    announce = sent_texts(plugin, GROUP_UMO)[-1]
    assert "另有 1 条密钥未送出" in announce
    for key in ("KEY-AAA", "KEY-BBB", "KEY-CCC"):
        assert key not in announce


def test_leftover_keys_viewable_after_draw(plugin: GroupLotteryPlugin):
    """开奖后必须还能取回剩余密钥，否则这些密钥就丢了。"""
    run(send(plugin, admin_event("抽奖 发布 月卡 3")))
    set_keys(plugin, "KEY-AAA\nKEY-BBB\nKEY-CCC")
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))

    assert plugin.db.get_open_raffle(GROUP_UMO) is None, "开奖后没有进行中的抽奖"
    plugin.context.sent.clear()

    # 群里查看
    out = texts_of(run(send(plugin, admin_event("抽奖 密钥 查看"))))
    assert "已私聊发送" in out
    got = "\n".join(sent_texts(plugin))
    assert "已结束" in got
    assert "剩余密钥" in got

    # 私聊按群号查看同样可用
    plugin.context.sent.clear()
    out = set_keys(plugin, "查看")
    assert "已私聊发送" in out
    assert "剩余密钥" in "\n".join(sent_texts(plugin))


def test_leftover_keys_clearable_after_draw(plugin: GroupLotteryPlugin):
    """开奖后也能清空剩余密钥。"""
    run(send(plugin, admin_event("抽奖 发布 月卡 2")))
    set_keys(plugin, "KEY-AAA\nKEY-BBB")
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))

    out = texts_of(run(send(plugin, admin_event("抽奖 密钥 清空"))))
    assert "已清空" in out
    # 已发出去的那条保留，只清掉没送出的
    assert plugin.db.count_keys(1, only_free=True) == 0
    assert plugin.db.count_keys(1) == 1


def test_leftover_reported_when_private_notify_off(plugin: GroupLotteryPlugin):
    """设了密钥却关了私聊发奖：密钥没用上，也要告知发布者。"""
    run(send(plugin, admin_event("抽奖 发布 月卡 1")))
    set_keys(plugin, "UNUSED-KEY")
    run(send(plugin, admin_event("抽奖 私聊 关")))
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))

    admin_text = "\n".join(sent_texts(plugin, "aiocqhttp:FriendMessage:1001"))
    assert "还有 1 条密钥没有送出" in admin_text
    assert "未开启「私聊发密钥」" in admin_text
    assert plugin.db.list_free_keys(1), "关闭私聊发奖时不该消耗密钥"


def test_no_leftover_dm_when_all_keys_used(plugin: GroupLotteryPlugin):
    """密钥刚好发完时不应有多余私聊。"""
    run(send(plugin, admin_event("抽奖 发布 月卡 2")))
    set_keys(plugin, "KEY-AAA\nKEY-BBB")
    for uid, name in (("1001", "甲"), ("1002", "乙")):
        run(send(plugin, FakeEvent("抽奖 参与", user_id=uid, nickname=name)))
    run(send(plugin, admin_event("抽奖 开奖")))

    assert plugin.db.list_free_keys(1) == []
    assert "还有" not in "\n".join(sent_texts(plugin, "aiocqhttp:FriendMessage:1001"))
    announce = sent_texts(plugin, GROUP_UMO)[-1]
    assert "未送出" not in announce


def test_leftover_dm_falls_back_to_group_hint(plugin: GroupLotteryPlugin):
    """发布者私聊不通时，公告里要给出自助取回的办法。"""
    run(send(plugin, admin_event("抽奖 发布 月卡 3")))
    set_keys(plugin, "KEY-AAA\nKEY-BBB\nKEY-CCC")
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))

    plugin.context.fail_private = True
    run(send(plugin, admin_event("抽奖 开奖")))
    plugin.context.fail_private = False

    announce = sent_texts(plugin, GROUP_UMO)[-1]
    assert "另有 2 条密钥未送出" in announce
    assert "抽奖 密钥 888888 查看" in announce


def test_claim_resends_failed_key_in_private(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    set_keys(plugin, "SECRET-1")
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))

    # 模拟首轮私聊失败
    plugin.db._exec("UPDATE winners SET notified = 0")
    plugin.context.sent.clear()

    out = texts_of(
        run(send(plugin, FakeEvent("抽奖 领取", user_id="1001", nickname="甲")))
    )
    assert "已私聊发送你的密钥" in out
    assert "SECRET-1" in "\n".join(sent_texts(plugin))
    assert plugin.db.list_winners(user_id="1001")[0]["notified"] == 1


def test_claim_in_private_returns_key_directly(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    set_keys(plugin, "DM-KEY")
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))
    plugin.db._exec("UPDATE winners SET notified = 0")

    out = texts_of(
        run(send(plugin, FakeEvent("抽奖 领取", user_id="1001", private=True)))
    )
    assert "DM-KEY" in out


# --------------------------------------------------------------- 定时 / 满员


def test_scheduled_draw(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    out = texts_of(run(send(plugin, admin_event("抽奖 定时 +30m"))))
    assert "自动开奖时间已设为" in out
    assert plugin.db.get_open_raffle(GROUP_UMO)["draw_at"] is not None

    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))

    # 把开奖时间改成已过去，模拟到点
    plugin.db.update_raffle(1, draw_at=int(time.time()) - 5)
    assert len(plugin.db.list_due_raffles()) == 1

    run(plugin._draw_due_raffles())
    raffle = plugin.db.get_raffle(1)
    assert raffle["status"] == "drawn"
    assert raffle["draw_trigger"] == "scheduled"
    assert "开奖结果" in sent_texts(plugin, GROUP_UMO)[-1]


def test_scheduled_draw_cancels_when_nobody_joined(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    plugin.db.update_raffle(1, draw_at=int(time.time()) - 5)
    run(plugin._draw_due_raffles())
    assert plugin.db.get_raffle(1)["status"] == "cancelled"
    assert "无人报名" in sent_texts(plugin, GROUP_UMO)[-1]


def test_scheduled_draw_failure_clears_schedule(plugin: GroupLotteryPlugin):
    """定时到点但密钥池为空时，应取消定时并提示管理员，而不是每个周期重试刷屏。"""
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    # 直接构造「密钥模式但池子已空」的异常状态
    plugin.db.update_raffle(1, prize_kind="key", private_notify=1)
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    plugin.db.update_raffle(1, draw_at=int(time.time()) - 5)

    run(plugin._draw_due_raffles())

    raffle = plugin.db.get_raffle(1)
    assert raffle["status"] == "open"  # 未开奖
    assert raffle["draw_at"] is None  # 定时已清除，不会反复重试
    assert "定时开奖失败" in sent_texts(plugin, GROUP_UMO)[-1]


def test_key_mode_without_private_notify_keeps_key_pool(plugin: GroupLotteryPlugin):
    """prize_kind=key 但关闭了私聊发奖时，不应消耗密钥池。"""
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    set_keys(plugin, "NEVER-SENT")
    plugin.db.update_raffle(1, prize_kind="key", private_notify=0)
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))

    run(send(plugin, admin_event("抽奖 开奖")))

    winners = plugin.db.list_winners(raffle_id=1)
    assert winners[0]["prize"] == ""
    assert plugin.db.count_keys(1, only_free=True) == 1
    assert "群主 QQ 12345" in sent_texts(plugin, GROUP_UMO)[-1]


def test_schedule_off(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    run(send(plugin, admin_event("抽奖 定时 +1h")))
    assert "已取消群 888888 的定时自动开奖" in texts_of(
        run(send(plugin, admin_event("抽奖 定时 关")))
    )
    assert plugin.db.get_open_raffle(GROUP_UMO)["draw_at"] is None


def test_schedule_rejects_bad_input(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    assert "参数错误" in texts_of(run(send(plugin, admin_event("抽奖 定时 明天"))))
    assert "用法" in texts_of(run(send(plugin, admin_event("抽奖 定时"))))


def test_full_house_auto_draw(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡 2")))
    assert "报名满 2 人立即开奖" in texts_of(
        run(send(plugin, admin_event("抽奖 满员 2")))
    )

    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    assert plugin.db.get_raffle(1)["status"] == "open"

    results = run(send(plugin, FakeEvent("抽奖 参与", user_id="1002", nickname="乙")))
    assert "马上开奖" in texts_of(results)
    assert plugin.db.get_raffle(1)["status"] == "drawn"
    assert plugin.db.get_raffle(1)["draw_trigger"] == "full"
    assert len(plugin.db.list_winners(raffle_id=1)) == 2


# --------------------------------------------------------------- 其它命令


def test_seats_and_description(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    assert "名额已设为 4 名" in texts_of(run(send(plugin, admin_event("抽奖 名额 4"))))
    assert "说明已更新" in texts_of(
        run(send(plugin, admin_event("抽奖 说明 仅限本群成员")))
    )
    assert "仅限本群成员" in texts_of(run(send(plugin, FakeEvent("抽奖 状态"))))

    assert "参数错误" in texts_of(run(send(plugin, admin_event("抽奖 名额 0"))))
    assert "参数错误" in texts_of(run(send(plugin, admin_event("抽奖 名额 abc"))))


def test_private_toggle_requires_keys(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    assert "密钥池为空" in texts_of(run(send(plugin, admin_event("抽奖 私聊 开"))))
    set_keys(plugin, "AAA")
    assert "已开启私聊发奖" in texts_of(run(send(plugin, admin_event("抽奖 私聊 开"))))
    assert "已关闭私聊发奖" in texts_of(run(send(plugin, admin_event("抽奖 私聊 关"))))


def test_private_chat_rejects_participant_commands(plugin: GroupLotteryPlugin):
    """报名 / 退出这类需要群上下文的命令在私聊里没有意义。"""
    out = texts_of(run(send(plugin, FakeEvent("抽奖 参与", private=True))))
    assert "需要在群里使用" in out


# --------------------------------------------- 私聊按群号发布（跨群管理）


def test_publish_from_private_announces_in_group(plugin: GroupLotteryPlugin):
    """私聊发布：在目标群建抽奖，并在该群播报一条抽奖信息。"""
    out = texts_of(
        run(send(plugin, private_admin_event(f"抽奖 发布 {GROUP_ID} 月卡 3")))
    )
    assert "抽奖已发布" in out
    assert "群 888888（测试群）" in out
    assert "已在该群播报抽奖信息" in out

    raffle = plugin.db.get_open_raffle(GROUP_UMO)
    assert raffle is not None
    assert raffle["title"] == "月卡"
    assert raffle["winner_count"] == 3
    assert raffle["group_id"] == GROUP_ID

    # 群里应当收到一条播报（私聊发布时群里看不到命令痕迹）
    announcements = sent_texts(plugin, GROUP_UMO)
    assert announcements, "应当在群里播报新抽奖"
    assert "新抽奖开始啦" in announcements[-1]
    assert "月卡" in announcements[-1]
    assert "抽奖 参与" in announcements[-1]


def test_publish_from_group_does_not_double_announce(plugin: GroupLotteryPlugin):
    """群里发布时，回复本身就在群里，不需要再额外播报。"""
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    assert sent_texts(plugin, GROUP_UMO) == []


def test_publish_from_private_requires_bot_in_group(plugin: GroupLotteryPlugin):
    out = texts_of(run(send(plugin, private_admin_event("抽奖 发布 999999 月卡"))))
    assert "机器人不在群 999999 里" in out
    assert plugin.db.list_raffles(limit=10) == []


def test_publish_from_private_rejects_bad_group_id(plugin: GroupLotteryPlugin):
    out = texts_of(run(send(plugin, private_admin_event("抽奖 发布 月卡"))))
    assert "群号必须是纯数字" in out


def test_full_private_workflow(plugin: GroupLotteryPlugin):
    """完整私聊流程：发布 → 密钥 → 定时 → 开奖。"""
    run(send(plugin, private_admin_event(f"抽奖 发布 {GROUP_ID} 月卡 1")))
    assert "已设置 1 条密钥" in set_keys(plugin, "PRIVATE-KEY")
    assert "自动开奖时间已设为" in texts_of(
        run(send(plugin, private_admin_event(f"抽奖 定时 {GROUP_ID} +1h")))
    )
    assert "报名成功" in texts_of(
        run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    )

    plugin.db.update_raffle(1, draw_at=int(time.time()) - 5)
    run(plugin._draw_due_raffles())

    winners = plugin.db.list_winners(raffle_id=1)
    assert len(winners) == 1
    assert winners[0]["prize"] == "PRIVATE-KEY"
    assert "PRIVATE-KEY" in "\n".join(sent_texts(plugin))
    assert "PRIVATE-KEY" not in "\n".join(sent_texts(plugin, GROUP_UMO))


def test_private_status_and_cancel_by_group_id(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡 2")))

    out = texts_of(run(send(plugin, private_admin_event(f"抽奖 状态 {GROUP_ID}"))))
    assert "群 888888（测试群）" in out
    assert "月卡" in out

    out = texts_of(run(send(plugin, private_admin_event(f"抽奖 名单 {GROUP_ID}"))))
    assert "还没有人报名" in out

    out = texts_of(run(send(plugin, private_admin_event(f"抽奖 取消 {GROUP_ID}"))))
    assert "已取消" in out
    assert plugin.db.get_open_raffle(GROUP_UMO) is None


def test_private_commands_need_group_id(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    for text in ("抽奖 状态", "抽奖 开奖", "抽奖 取消", "抽奖 密钥"):
        out = texts_of(run(send(plugin, private_admin_event(text))))
        assert "私聊里请先写群号" in out, text

    # 把时间当群号写会被明确拒绝
    out = texts_of(run(send(plugin, private_admin_event("抽奖 定时 20:00"))))
    assert "群号必须是纯数字" in out


# ------------------------------------------------------- 每个群期数独立


def test_raffle_numbering_is_per_group(plugin: GroupLotteryPlugin):
    """期号按群独立编号，而不是全局累加。"""
    other_umo = f"{PLATFORM_ID}:GroupMessage:999999"

    # 本群第 1 期
    out = texts_of(run(send(plugin, admin_event("抽奖 发布 月卡"))))
    assert "第 1 期" in out
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))

    # 另一个群发布，也应该是第 1 期
    out = texts_of(
        run(send(plugin, admin_event("抽奖 发布 点卡", group_id="999999"))),
    )
    assert "第 1 期" in out
    assert plugin.db.get_open_raffle(other_umo)["seq"] == 1
    # 内部主键是全局的，两场不能撞
    assert plugin.db.get_open_raffle(other_umo)["id"] != 1

    # 本群第 2 期
    out = texts_of(run(send(plugin, admin_event("抽奖 发布 季卡"))))
    assert "第 2 期" in out
    assert plugin.db.get_open_raffle(GROUP_UMO)["seq"] == 2


def test_records_show_per_group_number(plugin: GroupLotteryPlugin):
    """中奖记录里的期号也要用群内期号，而不是全局 id。"""
    # 先让另一个群开一场，把全局 id 顶到 1
    run(send(plugin, admin_event("抽奖 发布 点卡", group_id="999999")))
    run(
        send(
            plugin,
            FakeEvent("抽奖 参与", user_id="2002", nickname="乙", group_id="999999"),
        )
    )
    run(send(plugin, admin_event("抽奖 开奖", group_id="999999")))

    # 本群这场全局 id 会是 2，但群内期号仍是第 1 期
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    local = plugin.db.get_open_raffle(GROUP_UMO)
    assert local["id"] == 2 and local["seq"] == 1

    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))

    out = texts_of(run(send(plugin, admin_event("抽奖 记录"))))
    assert "第 1 期" in out
    assert "第 2 期" not in out


# ------------------------------------------- unique_session（会话隔离）回归


def test_unique_session_group_raffle_is_shared(plugin: GroupLotteryPlugin):
    """开启 unique_session 时，同群不同用户必须看到同一场抽奖。

    回归：早期版本拿 ``event.unified_msg_origin`` 当群标识，而它在会话隔离下是
    ``{用户ID}_{群号}``，导致抽奖被存成「每人一份」，别人无法参与。
    """
    # 管理员在群里发布
    run(send(plugin, admin_event("抽奖 发布 月卡 2", unique_session=True)))

    raffle = plugin.db.get_open_raffle(GROUP_UMO)
    assert raffle is not None, "抽奖必须落在群级会话下"
    assert raffle["group_id"] == GROUP_ID, "群号不应带用户前缀"

    # 另一个用户参与
    out = texts_of(
        run(
            send(
                plugin,
                FakeEvent(
                    "抽奖 参与",
                    user_id="2002",
                    nickname="乙",
                    unique_session=True,
                ),
            )
        )
    )
    assert "报名成功" in out
    assert plugin.db.count_participants(1) == 1

    # 第三个用户能看到状态
    out = texts_of(
        run(
            send(
                plugin,
                FakeEvent("抽奖 状态", user_id="3003", unique_session=True),
            )
        )
    )
    assert "月卡" in out
    assert "1 人" in out or "1/" in out


def test_unique_session_private_publish_then_group_join(
    plugin: GroupLotteryPlugin,
):
    """路线 A + 会话隔离：私聊发布后，群里任何人都能参与。"""
    run(send(plugin, private_admin_event(f"抽奖 发布 {GROUP_ID} 月卡 1")))

    out = texts_of(
        run(
            send(
                plugin,
                FakeEvent(
                    "抽奖 参与", user_id="2002", nickname="乙", unique_session=True
                ),
            )
        )
    )
    assert "报名成功" in out

    # 私聊按群号设置密钥也应命中同一场
    assert "已设置 1 条密钥" in set_keys(plugin, "UNIQUE-KEY")
    assert plugin.db.count_keys(1) == 1


def test_unique_session_draw_delivers_to_real_group(plugin: GroupLotteryPlugin):
    """会话隔离下开奖：公告要发到群级会话，而不是某个用户的隔离会话。"""
    run(send(plugin, admin_event("抽奖 发布 月卡 1", unique_session=True)))
    set_keys(plugin, "UNIQUE-KEY")
    run(
        send(
            plugin,
            FakeEvent("抽奖 参与", user_id="2002", nickname="乙", unique_session=True),
        )
    )
    run(send(plugin, admin_event("抽奖 开奖", unique_session=True)))

    sessions = [s for s, _ in plugin.context.sent]
    assert GROUP_UMO in sessions, f"公告应发往 {GROUP_UMO}，实际发往 {sessions}"
    assert not any("_888888" in s for s in sessions), "不应发往按用户隔离的会话"
    assert "UNIQUE-KEY" in "\n".join(sent_texts(plugin))


def test_group_id_of_strips_user_prefix():
    """group_id_of 对隔离会话也要还原出真实群号（历史数据兼容）。"""
    from astrbot_plugin_group_lottery.core.notifier import group_id_of, group_umo

    assert group_id_of("aiocqhttp:GroupMessage:1626810822_762429641") == "762429641"
    assert group_id_of("aiocqhttp:GroupMessage:762429641") == "762429641"
    assert group_id_of("") == ""

    class _E:
        def get_group_id(self):
            return "762429641"

        def get_platform_id(self):
            return "aiocqhttp"

        unified_msg_origin = "aiocqhttp:GroupMessage:1626810822_762429641"

    assert group_umo(_E()) == "aiocqhttp:GroupMessage:762429641"


def test_db_migrates_isolated_rows(plugin: GroupLotteryPlugin, tmp_path=None):
    """升级后历史数据要能自动归一化，而不是作废。"""
    # 手工插入一条被 unique_session 污染的记录
    plugin.db._conn.execute(
        "INSERT INTO raffles (umo, group_id, title, created_at) VALUES (?, ?, ?, ?)",
        (
            "aiocqhttp:GroupMessage:1626810822_762429641",
            "1626810822_762429641",
            "旧抽奖",
            1,
        ),
    )
    plugin.db._conn.commit()

    # 重新打开数据库会触发迁移
    from astrbot_plugin_group_lottery.core.db import LotteryDB

    db_path = plugin.db.path
    plugin.db.close()
    migrated = LotteryDB(db_path)
    try:
        row = migrated.get_raffle(1)
        assert row["group_id"] == "762429641"
        assert row["umo"] == "aiocqhttp:GroupMessage:762429641"
        assert migrated.get_open_raffle("aiocqhttp:GroupMessage:762429641") is not None
    finally:
        migrated.close()
    plugin.db = LotteryDB(db_path)


def test_group_blacklist_blocks_commands(plugin: GroupLotteryPlugin):
    plugin.config["group_blacklist"] = ["888888"]
    assert texts_of(run(send(plugin, FakeEvent("抽奖 状态")))) == ""
    assert texts_of(run(send(plugin, admin_event("抽奖 发布 月卡")))) == ""


def test_extra_admin_from_config(plugin: GroupLotteryPlugin):
    out = texts_of(run(send(plugin, FakeEvent("抽奖 发布 月卡", user_id="9999"))))
    assert "抽奖已发布" in out


def test_group_owner_is_admin(plugin: GroupLotteryPlugin):
    out = texts_of(
        run(send(plugin, FakeEvent("抽奖 发布 月卡", user_id="7777", role="owner")))
    )
    assert "抽奖已发布" in out


def test_help_and_records(plugin: GroupLotteryPlugin):
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))

    records = texts_of(run(send(plugin, FakeEvent("抽奖 记录"))))
    assert "最近 1 次开奖" in records and "月卡" in records

    assert "你还没有中奖记录" in texts_of(
        run(send(plugin, FakeEvent("抽奖 记录", user_id="999", private=True)))
    )
    assert "月卡" in texts_of(
        run(send(plugin, FakeEvent("抽奖 记录", user_id="1001", private=True)))
    )


def test_no_prefix_regex_entry(plugin: GroupLotteryPlugin):
    """正则兜底入口在未被指令 handler 接管时应当响应。"""
    event = FakeEvent("抽奖 状态")
    results = run(_drive_no_prefix(plugin, event))
    assert "当前没有进行中的抽奖" in texts_of(results)

    handled = FakeEvent("抽奖 状态")
    handled.set_extra("lottery_handled", True)
    assert run(_drive_no_prefix(plugin, handled)) == []


async def _drive_no_prefix(plugin: GroupLotteryPlugin, event: FakeEvent) -> list:
    results = []
    async for item in plugin.cmd_lottery_no_prefix(event):
        results.append(item)
    return results


def test_no_prefix_can_be_disabled(plugin: GroupLotteryPlugin):
    plugin.config["allow_no_prefix"] = False
    event = FakeEvent("抽奖 状态")
    assert run(_drive_no_prefix(plugin, event)) == []


def test_dispatch_always_stops_event(plugin: GroupLotteryPlugin):
    event = FakeEvent("抽奖 帮助")
    run(send(plugin, event))
    assert event.is_stopped() is True


# --------------------------------------------------------------- Web API


def test_web_api_overview_and_detail(plugin: GroupLotteryPlugin):
    from astrbot_plugin_group_lottery.web_api import LotteryWebApi

    run(send(plugin, admin_event("抽奖 发布 月卡 2")))
    set_keys(plugin, "WEB-KEY")
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))
    run(send(plugin, admin_event("抽奖 开奖")))

    api = LotteryWebApi(plugin)

    overview = run(api.overview())
    assert overview["status"] == "ok"
    assert overview["data"]["stats"]["raffle_total"] == 1
    assert overview["data"]["stats"]["winner_total"] == 1

    detail = run(api.raffle_detail(raffle_id="1"))
    assert detail["status"] == "ok"
    assert detail["data"]["raffle"]["title"] == "月卡"
    assert len(detail["data"]["participants"]) == 1
    assert detail["data"]["winners"][0]["prize"] == "WEB-KEY"
    assert detail["data"]["keys"][0]["assigned_to"] == "1001"

    winners = run(api.list_winners())
    assert winners["data"]["total"] == 1

    groups = run(api.list_groups())
    assert groups["data"]["groups"][0]["group_id"] == "888888"


def test_web_api_draw_and_cancel(plugin: GroupLotteryPlugin):
    from astrbot.api.web import request as request_stub
    from astrbot_plugin_group_lottery.web_api import LotteryWebApi

    api = LotteryWebApi(plugin)
    run(send(plugin, admin_event("抽奖 发布 月卡")))
    run(send(plugin, FakeEvent("抽奖 参与", user_id="1001", nickname="甲")))

    request_stub._json = {}
    assert run(api.draw_now())["status"] == "error"  # 缺 raffle_id

    request_stub._json = {"raffle_id": 1}
    assert run(api.draw_now())["status"] == "ok"
    assert plugin.db.get_raffle(1)["status"] == "drawn"

    # 已结束的场次不能重复开奖 / 取消
    assert run(api.draw_now())["status"] == "error"
    assert run(api.cancel())["status"] == "error"


def test_web_api_cancel_open_raffle(plugin: GroupLotteryPlugin):
    from astrbot.api.web import request as request_stub
    from astrbot_plugin_group_lottery.web_api import LotteryWebApi

    api = LotteryWebApi(plugin)
    run(send(plugin, admin_event("抽奖 发布 月卡")))

    request_stub._json = {"raffle_id": 1}
    assert run(api.cancel())["status"] == "ok"
    assert plugin.db.get_raffle(1)["status"] == "cancelled"


def test_web_api_save_keys(plugin: GroupLotteryPlugin):
    from astrbot.api.web import request as request_stub
    from astrbot_plugin_group_lottery.web_api import LotteryWebApi

    run(send(plugin, admin_event("抽奖 发布 月卡")))

    request_stub._json = {"raffle_id": 1, "keys": "A1\nA2,A3", "mode": "replace"}
    api = LotteryWebApi(plugin)
    saved = run(api.save_keys())
    assert saved["status"] == "ok"
    assert saved["data"]["free_keys"] == 3
    assert plugin.db.get_raffle(1)["prize_kind"] == "key"

    request_stub._json = {"raffle_id": 1, "keys": "", "mode": "append"}
    assert run(api.save_keys())["status"] == "error"


def test_web_api_registers_routes(plugin: GroupLotteryPlugin):
    routes = {route for route, *_ in plugin.context.web_apis}
    assert "/astrbot_plugin_group_lottery/overview" in routes
    assert "/astrbot_plugin_group_lottery/raffle/<raffle_id>" in routes
    assert "/astrbot_plugin_group_lottery/raffle/draw" in routes


# --------------------------------------------------------------- 生命周期


def test_terminate_is_safe(plugin: GroupLotteryPlugin):
    run(plugin.terminate())
    assert plugin._terminating is True
