"""群抽奖系统 —— 纯逻辑层单元测试（不依赖 AstrBot 运行时）。

运行方式（在插件目录下）：

    python -m pytest tests -q
"""

from __future__ import annotations

import io
import random
import shutil
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# sys.path 由同目录的 conftest.py 统一注入，这里直接按包名导入即可
from core import poster, texts
from core.args import PublishParseError, parse_publish
from core.db import LotteryDB
from core.engine import available_seats, draw, parse_keys
from core.models import KIND_CONTACT, KIND_KEY, STATUS_DRAWN, STATUS_OPEN
from core.timeparse import (
    MAX_REMIND_INTERVAL,
    MIN_REMIND_INTERVAL,
    REMIND_BEFORE_SECONDS,
    format_ts,
    humanize_interval,
    humanize_remaining,
    parse_draw_time,
    parse_interval,
)

BASE = datetime(2026, 3, 10, 9, 30, 0)


# ------------------------------------------------------------------ 时间解析


def test_parse_relative_minutes():
    ts, readable = parse_draw_time("+30m", now=BASE)
    assert ts == int((BASE + timedelta(minutes=30)).timestamp())
    assert readable == "2026-03-10 10:00"


def test_parse_relative_chinese_units():
    ts, _ = parse_draw_time("+2小时", now=BASE)
    assert ts == int((BASE + timedelta(hours=2)).timestamp())


def test_parse_relative_rejects_zero():
    with pytest.raises(ValueError):
        parse_draw_time("+0m", now=BASE)


def test_parse_clock_today_when_future():
    ts, readable = parse_draw_time("20:00", now=BASE)
    assert readable == "2026-03-10 20:00"
    assert ts == int(datetime(2026, 3, 10, 20, 0).timestamp())


def test_parse_clock_rolls_to_tomorrow_when_passed():
    _, readable = parse_draw_time("08:00", now=BASE)
    assert readable == "2026-03-11 08:00"


def test_parse_month_day_rolls_to_next_year():
    _, readable = parse_draw_time("01-01 20:00", now=BASE)
    assert readable == "2027-01-01 20:00"


def test_parse_full_date():
    _, readable = parse_draw_time("2026/12/31 21:05", now=BASE)
    assert readable == "2026-12-31 21:05"


def test_parse_date_without_clock_defaults_to_2000():
    _, readable = parse_draw_time("12-31", now=BASE)
    assert readable == "2026-12-31 20:00"


def test_parse_rejects_past_absolute_time():
    with pytest.raises(ValueError, match="必须晚于当前时间"):
        parse_draw_time("2020-01-01 10:00", now=BASE)


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_draw_time("明天下午", now=BASE)


def test_parse_rejects_too_far():
    with pytest.raises(ValueError, match="一年以内"):
        parse_draw_time("+400d", now=BASE)


# ---------------------------------------------------------------- 提醒间隔


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2h", 7200),
        ("2H", 7200),
        ("+2h", 7200),
        ("30m", 1800),
        ("30分钟", 1800),
        ("1天", 86400),
        ("1d", 86400),
        ("90s", 90),
        ("120", 7200),  # 裸数字按分钟
        (" 2小时 ", 7200),
    ],
)
def test_parse_interval(text, expected):
    assert parse_interval(text) == expected


def test_parse_interval_rejects_garbage_and_out_of_range():
    with pytest.raises(ValueError):
        parse_interval("")
    with pytest.raises(ValueError):
        parse_interval("每天")
    with pytest.raises(ValueError, match="不能短于"):
        parse_interval("10s")
    with pytest.raises(ValueError, match="不能短于"):
        parse_interval("0")
    with pytest.raises(ValueError, match="不能长于"):
        parse_interval("8d")
    assert MIN_REMIND_INTERVAL == 60
    assert MAX_REMIND_INTERVAL == 7 * 86400


def test_humanize_interval():
    assert humanize_interval(0) == "不通知"
    assert humanize_interval(7200) == "每 2 小时"
    assert humanize_interval(86400) == "每 1 天"
    assert humanize_interval(1800) == "每 30 分钟"
    assert humanize_interval(90) == "每 90 秒"


def test_remind_before_window_is_30_minutes():
    assert REMIND_BEFORE_SECONDS == 1800


def test_format_and_humanize():
    assert format_ts(None) == ""
    assert format_ts(0) == ""
    assert format_ts(int(BASE.timestamp())) == "2026-03-10 09:30"
    assert (
        humanize_remaining(
            int((BASE + timedelta(hours=3, minutes=12)).timestamp()), now=BASE
        )
        == "3 小时 12 分后"
    )
    assert (
        humanize_remaining(int((BASE - timedelta(minutes=1)).timestamp()), now=BASE)
        == "即将开奖"
    )


# ------------------------------------------------------------------ 密钥解析


def test_parse_keys_multiple_separators():
    raw = "AAA-111\nBBB-222,CCC-333；DDD-444、EEE-555|FFF-666"
    assert parse_keys(raw) == [
        "AAA-111",
        "BBB-222",
        "CCC-333",
        "DDD-444",
        "EEE-555",
        "FFF-666",
    ]


def test_parse_keys_keeps_inner_spaces_and_dedupes():
    raw = "KEY WITH SPACE\nKEY WITH SPACE\n  \nOTHER"
    assert parse_keys(raw) == ["KEY WITH SPACE", "OTHER"]


def test_parse_keys_empty():
    assert parse_keys("") == []
    assert parse_keys("\n\n , ; ") == []


# ------------------------------------------------------------------ 开奖引擎


def _people(n: int) -> list[dict]:
    return [{"user_id": str(1000 + i), "name": f"用户{i}"} for i in range(n)]


def test_draw_returns_requested_number_of_distinct_winners():
    outcome = draw(_people(10), 3, rng=random.Random(42))
    ids = [w["user_id"] for w in outcome.winners]
    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert outcome.participant_total == 10
    assert outcome.seat_shortage == 0
    assert [w["rank"] for w in outcome.winners] == [1, 2, 3]


def test_draw_clamps_to_participant_count():
    outcome = draw(_people(2), 5, rng=random.Random(1))
    assert outcome.winner_count == 2
    assert outcome.seat_shortage == 3


def test_draw_with_no_participants():
    outcome = draw([], 3)
    assert outcome.winner_count == 0
    assert outcome.participant_total == 0


def test_draw_assigns_keys_in_order():
    outcome = draw(
        _people(3),
        2,
        keys=["K1", "K2", "K3"],
        prize_kind=KIND_KEY,
        rng=random.Random(7),
    )
    assert [w["prize"] for w in outcome.winners] == ["K1", "K2"]
    assert outcome.key_shortage == 0


def test_draw_reports_key_shortage():
    outcome = draw(
        _people(3), 3, keys=["K1"], prize_kind=KIND_KEY, rng=random.Random(7)
    )
    assert [w["prize"] for w in outcome.winners] == ["K1", "", ""]
    assert outcome.key_shortage == 2
    assert len([w for w in outcome.winners if not w["prize"]]) == 2


def test_draw_ignores_keys_in_contact_mode():
    outcome = draw(
        _people(2), 2, keys=["K1", "K2"], prize_kind=KIND_CONTACT, rng=random.Random(7)
    )
    assert [w["prize"] for w in outcome.winners] == ["", ""]


def test_available_seats_limited_by_key_pool():
    raffle = {"winner_count": 5, "prize_kind": KIND_KEY}
    assert available_seats(raffle, 3) == 3
    assert available_seats(raffle, 0) == 0
    assert available_seats({"winner_count": 5, "prize_kind": KIND_CONTACT}, 0) == 5


# ------------------------------------------------------------------ 存储层


@pytest.fixture()
def db() -> LotteryDB:
    # 不用 pytest 的 tmp_path：部分受限环境下系统临时目录不可写，
    # 这里统一落在插件目录下的 tests/.tmp。
    workdir = Path(__file__).resolve().parent / ".tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / f"test_{random.getrandbits(32):08x}.db"
    database = LotteryDB(db_path)
    yield database
    database.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def test_raffle_crud(db: LotteryDB):
    rid = db.create_raffle(
        umo="aiocqhttp:GroupMessage:123", group_id="123", title="月卡", winner_count=2
    )
    raffle = db.get_raffle(rid)
    assert raffle is not None
    assert raffle["status"] == STATUS_OPEN
    assert raffle["prize_kind"] == KIND_CONTACT
    assert db.get_open_raffle("aiocqhttp:GroupMessage:123")["id"] == rid

    db.update_raffle(
        rid, winner_count=5, draw_at=1234567890, status=STATUS_DRAWN, hacked="x"
    )
    updated = db.get_raffle(rid)
    assert updated["winner_count"] == 5
    assert updated["draw_at"] == 1234567890
    assert "hacked" not in updated  # 非白名单字段被忽略

    assert db.get_open_raffle("aiocqhttp:GroupMessage:123") is None
    assert db.list_due_raffles(now=1234567891) == []


def test_due_raffles(db: LotteryDB):
    rid = db.create_raffle(umo="u1", group_id="1", title="A", draw_at=1000)
    db.create_raffle(umo="u2", group_id="2", title="B", draw_at=5000)
    db.create_raffle(umo="u3", group_id="3", title="C")
    due = db.list_due_raffles(now=2000)
    assert [r["id"] for r in due] == [rid]


def test_participants(db: LotteryDB):
    rid = db.create_raffle(umo="u", group_id="1", title="A")
    assert db.add_participant(rid, "1", "甲") is True
    assert db.add_participant(rid, "2", "乙") is True
    assert db.add_participant(rid, "1", "甲改名") is False  # 重复报名
    assert db.count_participants(rid) == 2
    names = [p["name"] for p in db.list_participants(rid)]
    assert names == ["甲改名", "乙"]
    assert db.remove_participant(rid, "1") is True
    assert db.remove_participant(rid, "1") is False
    assert db.count_participants(rid) == 1


def test_key_pool_lifecycle(db: LotteryDB):
    rid = db.create_raffle(umo="u", group_id="1", title="A")
    assert db.add_keys(rid, ["K1", "K2", "K1", " "], append=True) == 2
    assert db.add_keys(rid, ["K3"], append=True) == 1
    assert db.count_keys(rid) == 3
    assert db.count_keys(rid, only_free=True) == 3

    free = db.list_free_keys(rid)
    assert [k["content"] for k in free] == ["K1", "K2", "K3"]
    db.assign_keys([(free[0]["id"], "1001")])
    assert db.count_keys(rid, only_free=True) == 2
    assert db.list_keys(rid)[0]["assigned_to"] == "1001"

    assert db.add_keys(rid, ["K9"], append=False) == 1
    assert [k["content"] for k in db.list_keys(rid)] == ["K9"]
    assert db.clear_keys(rid) == 1
    assert db.count_keys(rid) == 0


def test_winners_and_claim(db: LotteryDB):
    rid = db.create_raffle(umo="u", group_id="1", title="A")
    db.add_winners(
        rid,
        [
            {
                "umo": "u",
                "group_id": "1",
                "user_id": "1",
                "name": "甲",
                "title": "A",
                "prize": "K1",
                "rank": 1,
                "notified": False,
            },
            {
                "umo": "u",
                "group_id": "1",
                "user_id": "2",
                "name": "乙",
                "title": "A",
                "prize": "K2",
                "rank": 2,
                "notified": True,
            },
        ],
    )
    rows = db.list_winners(umo="u")
    assert len(rows) == 2
    claimable = db.list_claimable("1")
    assert [r["prize"] for r in claimable] == ["K1"]
    assert db.list_claimable("2") == []  # 已成功私聊的不需要补领

    db.mark_claimed([claimable[0]["id"]])
    assert db.list_claimable("1") == []
    assert db.list_winners(user_id="1")[0]["claimed"] == 1


def test_overview_and_groups(db: LotteryDB):
    rid = db.create_raffle(umo="aiocqhttp:GroupMessage:1", group_id="1", title="A")
    db.add_keys(rid, ["K1", "K2"])
    db.add_participant(rid, "1", "甲")
    db.update_raffle(rid, status=STATUS_DRAWN)
    db.add_winners(
        rid,
        [
            {
                "umo": "aiocqhttp:GroupMessage:1",
                "group_id": "1",
                "user_id": "1",
                "name": "甲",
                "prize": "K1",
            }
        ],
    )

    stats = db.overview()
    assert stats["raffle_total"] == 1
    assert stats["raffle_open"] == 0
    assert stats["winner_total"] == 1
    assert stats["key_total"] == 2
    assert stats["key_free"] == 2
    assert stats["group_total"] == 1

    groups = db.known_groups()
    assert groups[0]["group_id"] == "1"
    assert groups[0]["raffle_total"] == 1


def test_prune_history_keeps_recent(db: LotteryDB):
    for i in range(15):
        rid = db.create_raffle(umo="u", group_id="1", title=f"第{i}期")
        db.update_raffle(rid, status=STATUS_DRAWN)
    # 下限保护：至少保留 10 场
    assert db.prune_history(per_group_limit=1) == 5
    assert len(db.list_raffles(umo="u", limit=100)) == 10


def test_delete_raffle_cascades(db: LotteryDB):
    rid = db.create_raffle(umo="u", group_id="1", title="A")
    db.add_participant(rid, "1", "甲")
    db.add_keys(rid, ["K1"])
    db.add_winners(rid, [{"umo": "u", "group_id": "1", "user_id": "1", "prize": "K1"}])
    db.delete_raffle(rid)
    assert db.get_raffle(rid) is None
    assert db.list_participants(rid) == []
    assert db.list_keys(rid) == []
    assert db.list_winners(raffle_id=rid) == []


# ------------------------------------------------------------------ 文案渲染


def test_fill_replaces_placeholders():
    assert (
        texts.fill("你好 <name>，共 <n> 个", {"name": "甲", "n": 3})
        == "你好 甲，共 3 个"
    )
    assert texts.fill("未知 <other>", {"name": "甲"}) == "未知 <other>"


def test_winners_text():
    winners = [{"name": "甲"}, {"name": "乙"}]
    assert texts.winners_text(winners) == "甲、乙"
    assert texts.winners_text(winners, with_at=True) == "@甲、@乙"
    assert texts.winners_text([]) == "（无人中奖）"


def test_build_announce_contains_placeholders_content():
    raffle = {"id": 7, "title": "月卡", "group_id": "123"}
    winners = [{"name": "甲", "user_id": "1"}, {"name": "乙", "user_id": "2"}]
    text = texts.build_announce(
        raffle=raffle,
        winners=winners,
        template="恭喜 <winners> 中奖！奖品 <prize>，共 <count> 名，请联系 <contact>。",
        contact="群主",
        group_id="123",
        drawn_at=int(BASE.timestamp()),
        notes=["报名人数不足，空缺 1 个名额"],
    )
    assert "恭喜 甲、乙 中奖" in text
    assert "奖品 月卡" in text
    assert "共 2 名" in text
    assert "请联系 群主" in text
    assert "空缺 1 个名额" in text
    assert text.startswith("🎊 开奖结果 ·「月卡」")


def test_build_announce_supports_time_and_group_placeholders():
    raffle = {"id": 7, "title": "月卡", "group_id": "123"}
    text = texts.build_announce(
        raffle=raffle,
        winners=[{"name": "甲", "user_id": "1"}],
        template="群 <group> 于 <time> 开出 <winners>（第 <raffle_id> 期）",
        contact="群主",
        group_id="123",
        drawn_at=int(BASE.timestamp()),
    )
    assert "群 123 于 2026-03-10 09:30 开出 甲（第 7 期）" in text


def test_announce_private_mode_replaces_legacy_cta():
    """密钥模式：老模板里写死的「联系群主领取」要换成「查看私聊」。"""
    text = texts.build_announce(
        raffle={"id": 7, "title": "月卡"},
        winners=[{"name": "甲", "user_id": "1"}],
        template=(
            "🎉 恭喜 <winners> 中奖！\n"
            "奖品：<prize>（共 <count> 名）\n"
            "请尽快联系 <contact> 领取奖励。"
        ),
        contact="群主",
        claim_private=True,
    )
    assert "查看机器人私聊领取奖励" in text
    assert "抽奖 领取" in text
    assert "请尽快联系" not in text


def test_announce_how_placeholder_follows_prize_mode():
    """新模板里的 <how> 按奖品形态自动切换领奖指引。"""
    raffle = {"id": 7, "title": "月卡"}
    template = "恭喜 <winners> 中奖！\n<how>"
    public = texts.build_announce(
        raffle=raffle,
        winners=[{"name": "甲"}],
        template=template,
        contact="群主",
        claim_private=False,
    )
    private = texts.build_announce(
        raffle=raffle,
        winners=[{"name": "甲"}],
        template=template,
        contact="群主",
        claim_private=True,
    )
    assert "请尽快联系 群主 领取奖励。" in public
    assert "查看机器人私聊领取奖励" in private
    assert "<how>" not in public
    assert "<how>" not in private


def test_announce_private_mode_appends_cta_to_custom_template():
    """模板被完全改写、既没有 <how> 也没有旧默认那句时，补一条私聊指引。"""
    raffle = {"id": 7, "title": "月卡"}
    template = "中奖啦：<winners>（<prize>）"
    private = texts.build_announce(
        raffle=raffle,
        winners=[{"name": "甲"}],
        template=template,
        contact="群主",
        claim_private=True,
    )
    plain = texts.build_announce(
        raffle=raffle,
        winners=[{"name": "甲"}],
        template=template,
        contact="群主",
        claim_private=False,
    )
    assert "查看机器人私聊领取奖励" in private
    assert "查看机器人私聊" not in plain


def test_build_private_uses_key():
    raffle = {"id": 7, "title": "月卡", "group_id": "123"}
    text = texts.build_private(
        raffle=raffle,
        winner={"prize": "SECRET-KEY", "name": "甲"},
        template="<name> 你好，你的密钥是 <key>（来自 <prize>）",
        contact="群主",
        group_id="123",
        drawn_at=int(BASE.timestamp()),
    )
    assert text == "甲 你好，你的密钥是 SECRET-KEY（来自 月卡）"


def test_build_status_without_raffle():
    assert "没有进行中的抽奖" in texts.build_status(raffle=None)


def test_build_status_warns_when_keys_insufficient():
    raffle = {
        "id": 3,
        "title": "月卡",
        "winner_count": 5,
        "prize_kind": KIND_KEY,
        "draw_at": int((BASE + timedelta(hours=1)).timestamp()),
        "min_players": 10,
    }
    text = texts.build_status(raffle=raffle, participant_count=2, free_keys=3)
    assert "已报名：2 人" in text
    assert "密钥池剩余 3 条" in text
    assert "还需要 2 条密钥" in text
    assert "满员开奖：报名满 10 人立即开奖" in text


def test_build_participants_truncates():
    names = [f"用户{i}" for i in range(60)]
    text = texts.build_participants(names, group_id="1", limit=50)
    assert "已报名 60 人" in text
    assert "仅显示前 50 位" in text


def test_build_help_lists_core_commands():
    text = texts.build_help()
    assert "抽奖 参与" in text
    assert "抽奖 开奖" in text
    assert "抽奖 密钥" in text


# ------------------------------------------------------------- 群内期号


def test_raffle_no_prefers_seq():
    """展示一律用群内期号 seq，而不是跨群全局自增的 id。"""
    assert texts.raffle_no({"id": 99, "seq": 3}) == 3
    assert texts.raffle_no({"raffle_id": 99, "raffle_seq": 4}) == 4
    # 老数据缺 seq 时回退到 id，避免显示成 0
    assert texts.raffle_no({"id": 7}) == 7
    assert texts.raffle_no(None) == 0
    assert texts.raffle_label({"seq": 3}) == "第 3 期"


def test_seq_is_independent_per_group(db: LotteryDB):
    """每个群的期数独立从 1 开始，而不是全局累加。"""
    a1 = db.create_raffle(umo="aiocqhttp:GroupMessage:111", group_id="111", title="A1")
    a2 = db.create_raffle(umo="aiocqhttp:GroupMessage:111", group_id="111", title="A2")
    b1 = db.create_raffle(umo="aiocqhttp:GroupMessage:222", group_id="222", title="B1")
    c1 = db.create_raffle(umo="aiocqhttp:GroupMessage:333", group_id="333", title="C1")
    b2 = db.create_raffle(umo="aiocqhttp:GroupMessage:222", group_id="222", title="B2")

    assert [db.get_raffle(i)["seq"] for i in (a1, a2)] == [1, 2]
    assert [db.get_raffle(i)["seq"] for i in (b1, b2)] == [1, 2]
    assert db.get_raffle(c1)["seq"] == 1
    # 内部主键仍是全局唯一，供参与者 / 密钥 / 中奖记录关联
    assert len({a1, a2, b1, b2, c1}) == 5


def test_seq_backfilled_for_existing_rows(db: LotteryDB):
    """老库没有 seq 列时，启动迁移要补列并按群回填。"""
    for group_id, title, created in (
        ("111", "旧A", 1),
        ("222", "旧B", 2),
        ("111", "旧C", 3),
    ):
        db._conn.execute(
            "INSERT INTO raffles (umo, group_id, title, created_at) VALUES (?, ?, ?, ?)",
            (f"aiocqhttp:GroupMessage:{group_id}", group_id, title, created),
        )
    db._conn.execute("UPDATE raffles SET seq = 0")
    db._conn.commit()

    db.close()
    reopened = LotteryDB(db.path)
    try:
        rows = {r["title"]: r["seq"] for r in reopened.list_raffles(limit=10)}
        assert rows == {"旧A": 1, "旧C": 2, "旧B": 1}
        # 新场次接在回填结果之后
        new_id = reopened.create_raffle(
            umo="aiocqhttp:GroupMessage:111", group_id="111", title="新D"
        )
        assert reopened.get_raffle(new_id)["seq"] == 3
    finally:
        reopened.close()


def test_winners_carry_raffle_seq(db: LotteryDB):
    """中奖记录要带出群内期号，供「抽奖 记录」展示。"""
    umo = "aiocqhttp:GroupMessage:111"
    rid = db.create_raffle(umo=umo, group_id="111", title="月卡")
    db.add_winners(
        rid,
        [
            {
                "umo": umo,
                "group_id": "111",
                "user_id": "1001",
                "name": "甲",
                "title": "月卡",
                "prize": "KEY-1",
            }
        ],
    )
    rows = db.list_winners(raffle_id=rid)
    assert rows[0]["raffle_seq"] == 1
    assert texts.raffle_no(rows[0]) == 1
    assert "第 1 期" in texts.build_records(rows, group_id="111")


# --------------------------------------------------------- 一行式发布解析


def test_parse_publish_one_liner():
    """用户给的例子：抽奖 发布 754797467 支付宝口令红包5元 2 定时 20:00 满员 8。"""
    spec = parse_publish("支付宝口令红包5元 2 定时 20:00 满员 8")
    assert spec.title == "支付宝口令红包5元"
    assert spec.winner_count == 2
    assert spec.draw_at_text == "20:00"
    assert spec.min_players == 8
    assert spec.description == ""
    assert spec.private_notify is None


def test_parse_publish_minimal():
    spec = parse_publish("月卡")
    assert spec.title == "月卡"
    assert spec.winner_count is None
    assert spec.draw_at_text == ""
    assert spec.min_players is None


def test_parse_publish_positional_seats():
    spec = parse_publish("月卡 3")
    assert spec.title == "月卡"
    assert spec.winner_count == 3


def test_parse_publish_title_with_digits():
    """奖品名里带数字不能被误当成名额。"""
    spec = parse_publish("5元红包")
    assert spec.title == "5元红包"
    assert spec.winner_count is None

    spec = parse_publish("5元红包 2")
    assert spec.title == "5元红包"
    assert spec.winner_count == 2


def test_parse_publish_all_options():
    spec = parse_publish("月卡 3 名额 5 定时 12-31 20:00 满员 10 私聊 开 说明 手慢无")
    assert spec.title == "月卡"
    assert spec.winner_count == 5  # 「名额」覆盖位置参数
    assert spec.draw_at_text == "12-31 20:00"  # 带空格的时间要整体取到
    assert spec.min_players == 10
    assert spec.private_notify is True
    assert spec.description == "手慢无"


def test_parse_publish_relative_time_and_off_switches():
    spec = parse_publish("月卡 定时 +2h 满员 关 私聊 关")
    assert spec.draw_at_text == "+2h"
    assert spec.min_players is None
    assert spec.private_notify is False


def test_parse_publish_poster_option():
    """长命令可以顺手带上海报链接（用户给的例子那种写法）。"""
    spec = parse_publish(
        "Steam游戏转生魔塔CDK 1 定时 18:18 满员 3 说明 What can i say "
        "海报 https://example.com/prize.png",
    )
    assert spec.title == "Steam游戏转生魔塔CDK"
    assert spec.winner_count == 1
    assert spec.draw_at_text == "18:18"
    assert spec.min_players == 3
    assert spec.description == "What can i say"
    assert spec.poster_url == "https://example.com/prize.png"


def test_parse_publish_poster_terminates_description():
    """「海报」是值的终止符：说明不能把链接一起吞掉，也不能反过来。"""
    spec = parse_publish("月卡 说明 手慢无 海报 https://x/y.png")
    assert spec.description == "手慢无"
    assert spec.poster_url == "https://x/y.png"

    spec = parse_publish("月卡 海报 https://x/y.png 说明 手慢无")
    assert spec.poster_url == "https://x/y.png"
    assert spec.description == "手慢无"


def test_parse_publish_poster_requires_a_link():
    with pytest.raises(PublishParseError, match="海报"):
        parse_publish("月卡 海报")
    with pytest.raises(PublishParseError, match="海报"):
        parse_publish("月卡 海报 名额 2")


def test_parse_publish_rejects_bad_input():
    with pytest.raises(PublishParseError):
        parse_publish("")
    with pytest.raises(PublishParseError):
        parse_publish("月卡 0")
    with pytest.raises(PublishParseError):
        parse_publish("月卡 满员 1")
    with pytest.raises(PublishParseError):
        parse_publish("月卡 满员 abc")
    with pytest.raises(PublishParseError):
        parse_publish("月卡 定时")
    with pytest.raises(PublishParseError):
        parse_publish("月卡 私聊 也许")
    with pytest.raises(PublishParseError):
        parse_publish("月卡 说明")
    with pytest.raises(PublishParseError):
        parse_publish("x" * 61)


# ---------------------------------------------------------------- 定时提醒


UMO = "aiocqhttp:GroupMessage:123"


def test_create_raffle_sets_reminder_plan(db: LotteryDB):
    rid = db.create_raffle(
        umo=UMO,
        group_id="123",
        title="月卡",
        draw_at=int(time.time()) + 6 * 3600,
        remind_interval=7200,
    )
    raffle = db.get_raffle(rid)
    assert raffle["remind_interval"] == 7200
    # 第一次提醒从「现在 + 间隔」起算，而不是立刻
    assert raffle["next_remind_at"] == raffle["created_at"] + 7200
    # 开奖还在 6 小时后，开奖前那次提醒尚未消费
    assert raffle["remind_before_sent"] == 0


def test_create_raffle_without_interval_has_no_next_remind(db: LotteryDB):
    rid = db.create_raffle(umo=UMO, group_id="123", title="月卡")
    raffle = db.get_raffle(rid)
    assert raffle["remind_interval"] == 0
    assert raffle["next_remind_at"] is None
    assert db.list_due_reminders(now=raffle["created_at"] + 10**6) == []


def test_create_raffle_skips_pre_draw_reminder_when_draw_is_close(db: LotteryDB):
    """刚发布就开奖（不足 30 分钟）时不要再补一条「马上开奖」。"""
    now = int(time.time())
    rid = db.create_raffle(umo=UMO, group_id="123", title="月卡", draw_at=now + 600)
    assert db.get_raffle(rid)["remind_before_sent"] == 1
    assert db.list_due_reminders(now=now + 1) == []


def test_list_due_reminders_interval(db: LotteryDB):
    now = int(time.time())
    rid = db.create_raffle(umo=UMO, group_id="123", title="月卡", remind_interval=3600)
    assert db.list_due_reminders(now=now + 3599) == []
    due = db.list_due_reminders(now=now + 3600)
    assert [int(r["id"]) for r in due] == [rid]


def test_list_due_reminders_before_draw(db: LotteryDB):
    now = int(time.time())
    rid = db.create_raffle(
        umo=UMO, group_id="123", title="月卡", draw_at=now + 6 * 3600
    )
    # 距开奖还有 6 小时：不该提醒
    assert db.list_due_reminders(now=now + 1) == []
    # 距开奖还有 29 分钟：到了开奖前窗口
    assert [int(r["id"]) for r in db.list_due_reminders(now=now + 6 * 3600 - 1740)] == [
        rid
    ]
    # 已经提醒过就不再重复
    db.update_raffle(rid, remind_before_sent=1)
    assert db.list_due_reminders(now=now + 6 * 3600 - 1740) == []


def test_list_due_reminders_ignores_closed_and_far_raffles(db: LotteryDB):
    now = int(time.time())
    rid = db.create_raffle(
        umo=UMO,
        group_id="123",
        title="月卡",
        draw_at=now + 100,
        remind_interval=3600,
    )
    # 距开奖只有 100 秒 → 建场时就标记了「开奖前提醒已消费」，且间隔还没到
    assert db.list_due_reminders(now=now + 1) == []
    db.update_raffle(rid, status=STATUS_DRAWN)
    db.update_raffle(rid, next_remind_at=now - 1)
    assert db.list_due_reminders(now=now) == []


def test_reminder_columns_survive_legacy_database():
    """老库（没有提醒列）升级后应该能补列并正常查询。"""
    workdir = Path(__file__).resolve().parent / ".tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / f"legacy_{random.getrandbits(32):08x}.db"
    database = LotteryDB(db_path)
    # 模拟老库：先删掉依赖这些列的索引，再把列删掉（SQLite 支持 DROP COLUMN）
    database._conn.execute("DROP INDEX IF EXISTS idx_raffles_remind")
    for column in ("remind_interval", "next_remind_at", "remind_before_sent"):
        database._conn.execute(f"ALTER TABLE raffles DROP COLUMN {column}")
    database._conn.commit()
    database.close()

    reopened = LotteryDB(db_path)
    columns = reopened._columns("raffles")
    assert {"remind_interval", "next_remind_at", "remind_before_sent"} <= set(columns)
    assert reopened.list_due_reminders(now=int(time.time())) == []
    reopened.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def test_poster_column_survives_legacy_database():
    """老库（没有 poster 列）升级后应该能补列并正常读写。"""
    workdir = Path(__file__).resolve().parent / ".tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / f"legacy_poster_{random.getrandbits(32):08x}.db"
    database = LotteryDB(db_path)
    database._conn.execute("ALTER TABLE raffles DROP COLUMN poster")
    database._conn.commit()
    database.close()

    reopened = LotteryDB(db_path)
    assert "poster" in reopened._columns("raffles")
    raffle_id = reopened.create_raffle(umo="u", group_id="1", title="月卡")
    reopened.update_raffle(raffle_id, poster="covers/1-1.jpg")
    assert reopened.get_raffle(raffle_id)["poster"] == "covers/1-1.jpg"
    assert reopened.list_poster_keys() == {"1-1"}
    reopened.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def test_list_poster_keys_only_returns_configured_raffles(db):
    plain = db.create_raffle(umo="u", group_id="888888", title="没有配图")
    with_poster = db.create_raffle(umo="u", group_id="888888", title="有配图")
    db.update_raffle(with_poster, poster="covers/888888-2.jpg")

    assert db.create_raffle(umo="u", group_id="999999", title="别的群") > 0
    assert db.list_poster_keys() == {"888888-2"}

    # 清空之后就不再被引用
    db.update_raffle(with_poster, poster="")
    assert db.list_poster_keys() == set()
    assert db.get_raffle(plain)["poster"] == ""


def test_pending_poster_record_round_trip(db):
    """待用配图一个群最多一张，覆盖式写入。"""
    assert db.get_pending_poster("888888") == ""

    db.set_pending_poster("888888", "covers/pending-888888.jpg")
    db.set_pending_poster("888888", "covers/pending-888888.png")
    db.set_pending_poster("999999", "covers/pending-999999.jpg")

    assert db.get_pending_poster("888888") == "covers/pending-888888.png"
    assert db.get_pending_poster("999999") == "covers/pending-999999.jpg"
    # 待用配图也要算「还在用」，否则会被清理当孤儿删掉
    assert db.list_poster_keys() == {"pending-888888", "pending-999999"}

    db.clear_pending_poster("888888")
    assert db.get_pending_poster("888888") == ""
    assert db.get_pending_poster("") == ""
    assert db.list_poster_keys() == {"pending-999999"}


def test_pending_poster_table_created_for_legacy_database():
    """老库升级后应该自动有 pending_posters 表。"""
    workdir = Path(__file__).resolve().parent / ".tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / f"legacy_pending_{random.getrandbits(32):08x}.db"
    database = LotteryDB(db_path)
    database._conn.execute("DROP TABLE pending_posters")
    database._conn.commit()
    database.close()

    reopened = LotteryDB(db_path)
    reopened.set_pending_poster("888888", "covers/pending-888888.jpg")
    assert reopened.get_pending_poster("888888") == "covers/pending-888888.jpg"
    reopened.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def _raffle_stub(**overrides):
    base = {
        "id": 1,
        "seq": 3,
        "title": "月卡",
        "winner_count": 2,
        "draw_at": None,
        "description": "",
    }
    base.update(overrides)
    return base


def test_build_reminder_interval():
    now = 1_800_000_000
    text = texts.build_reminder(
        raffle=_raffle_stub(draw_at=now + 7200),
        participant_count=5,
        kind="interval",
        now=now,
    )
    assert "第 3 期" in text
    assert "月卡" in text
    assert "已报名：5 人 · 名额 2 名" in text
    assert "抽奖 参与" in text
    assert "2 小时 0 分后" in text


def test_build_reminder_before_draw():
    now = 1_800_000_000
    text = texts.build_reminder(
        raffle=_raffle_stub(draw_at=now + 1500),
        participant_count=5,
        kind="before_draw",
        now=now,
    )
    assert "就开奖了" in text
    assert "还有 25 分钟就开奖了" in text
    assert "抓紧" in text
    # 开奖前那条不必再重复「还有多久开奖」的括号
    assert "（25 分钟后）" not in text


def test_build_reminder_before_draw_with_past_deadline():
    """兜底：时间已经过了也不该拼出「还有 即将开奖就开奖了」这种病句。"""
    now = 1_800_000_000
    text = texts.build_reminder(
        raffle=_raffle_stub(draw_at=now - 5),
        participant_count=1,
        kind="before_draw",
        now=now,
    )
    assert "马上就要开奖了" in text
    assert "即将开奖就" not in text


def test_build_reminder_without_draw_time():
    text = texts.build_reminder(raffle=_raffle_stub(), participant_count=0, now=0)
    assert "由管理员手动开奖" in text
    assert "已报名：0 人" in text


def test_build_reminder_never_mentions_at_all_marker():
    """提醒文案本身不含 @ 段（发送时也不带 At），这里只做文案侧的兜底。"""
    now = 1_800_000_000
    for kind in ("interval", "before_draw"):
        text = texts.build_reminder(
            raffle=_raffle_stub(draw_at=now + 600),
            participant_count=1,
            kind=kind,
            now=now,
        )
        assert "@全体成员" not in text


# ------------------------------------------------------------------ 奖品配图


def _png(width: int = 64, height: int = 48, mode: str = "RGB") -> bytes:
    """造一张真 PNG（插件依赖 Pillow，测试直接用没问题）。"""
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new(mode, (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _animated_gif() -> bytes:
    """造一张两帧 GIF。

    帧内容必须真的不同：两帧像素一致时 Pillow 会把它写成单帧，测不到动图分支。
    """
    from PIL import Image as PILImage

    first = PILImage.new("RGB", (16, 16), (255, 0, 0)).convert("P")
    second = PILImage.new("RGB", (16, 16), (0, 0, 255)).convert("P")
    buffer = io.BytesIO()
    first.save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )
    return buffer.getvalue()


@pytest.fixture()
def poster_dir() -> Path:
    """配图目录：本机 ``tmp_path`` 不可用，统一放到 ``tests/.tmp`` 下。"""
    target = Path(__file__).resolve().parent / ".tmp" / "posters"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    yield target
    shutil.rmtree(target, ignore_errors=True)


def test_sniff_ext_recognizes_common_formats():
    assert poster.sniff_ext(_png()) == "png"
    assert poster.sniff_ext(b"\xff\xd8\xff\xe0abc") == "jpg"
    assert poster.sniff_ext(b"GIF89a....") == "gif"
    assert poster.sniff_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert poster.sniff_ext(b"BM....") == "bmp"
    assert poster.sniff_ext(b"definitely not an image") is None


def test_parse_url_only_accepts_http():
    assert poster.parse_url(" https://a/b.png ") == "https://a/b.png"
    assert poster.parse_url("http://a/b.png") == "http://a/b.png"
    assert poster.parse_url("查看") is None
    assert poster.parse_url("ftp://a/b.png") is None
    assert poster.parse_url("") is None


def test_compress_image_shrinks_and_resizes():
    """大图压成 JPEG，并且最长边不超过上限。"""
    from PIL import Image as PILImage

    big = _png(2400, 1800)
    packed, ext = poster.compress_image(big)
    assert ext == "jpg"
    assert len(packed) < len(big)
    with PILImage.open(io.BytesIO(packed)) as img:
        assert max(img.size) <= poster.MAX_EDGE


def test_compress_image_keeps_transparency_as_png():
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new("RGBA", (80, 80), (0, 0, 0, 0)).save(buffer, format="PNG")
    _packed, ext = poster.compress_image(buffer.getvalue())
    assert ext == "png"


def test_compress_image_keeps_animated_gif_intact():
    """动图重编码会掉帧，必须原样保留。"""
    gif = _animated_gif()
    packed, ext = poster.compress_image(gif)
    assert ext == "gif"
    assert packed == gif


def test_compress_image_rejects_garbage():
    with pytest.raises(ValueError, match="无法识别或已损坏"):
        poster.compress_image(b"definitely not an image")


def test_save_and_delete_poster_round_trip(poster_dir):
    rel = poster.save_poster(poster_dir, "888888", 3, _png())
    assert rel == "covers/888888-3.jpg"

    saved = poster.resolve_path(poster_dir, rel)
    assert saved is not None and saved.is_file()

    assert poster.delete_poster(poster_dir, "888888", 3) == 1
    assert not saved.exists()
    assert poster.resolve_path(poster_dir, rel) is None
    # 再删一次不会报错
    assert poster.delete_poster(poster_dir, "888888", 3) == 0


def test_save_poster_replaces_previous_extension(poster_dir):
    """换图时旧后缀的残留要清掉，否则目录会越堆越多。"""
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new("RGBA", (40, 40), (0, 0, 0, 0)).save(buffer, format="PNG")
    first = poster.save_poster(poster_dir, "888888", 1, buffer.getvalue())
    assert first.endswith(".png")

    second = poster.save_poster(poster_dir, "888888", 1, _png())
    assert second.endswith(".jpg")
    assert [p.name for p in (poster_dir / "covers").iterdir()] == ["888888-1.jpg"]


def test_save_poster_rejects_empty_and_oversize(poster_dir):
    with pytest.raises(ValueError, match="没有读到图片内容"):
        poster.save_poster(poster_dir, "1", 1, b"")
    with pytest.raises(ValueError, match="图片太大"):
        poster.save_poster(poster_dir, "1", 1, b"x" * (poster.MAX_SOURCE_BYTES + 1))


def test_resolve_path_rejects_traversal_and_missing(poster_dir):
    assert poster.resolve_path(poster_dir, "") is None
    assert poster.resolve_path(poster_dir, "../secrets.txt") is None
    assert poster.resolve_path(poster_dir, "covers/../secrets.txt") is None


def test_sweep_removes_orphans(poster_dir):
    poster.save_poster(poster_dir, "888888", 1, _png())
    poster.save_poster(poster_dir, "888888", 2, _png())

    assert poster.sweep(poster_dir, {"888888-2"}) == 1
    assert [p.name for p in (poster_dir / "covers").iterdir()] == ["888888-2.jpg"]


def test_save_and_delete_pending_poster(poster_dir):
    rel = poster.save_pending(poster_dir, "888888", _png())

    assert rel == "covers/pending-888888.jpg"
    assert poster.resolve_path(poster_dir, rel) is not None
    assert poster.pending_stem("888888") == "pending-888888"

    assert poster.delete_pending(poster_dir, "888888") == 1
    assert (poster_dir / "covers").exists() is False or not list(
        (poster_dir / "covers").iterdir()
    )


def test_adopt_moves_pending_poster_under_a_raffle(poster_dir):
    """发布时把待用配图改名到该场名下，不重新编码。"""
    pending = poster.save_pending(poster_dir, "888888", _png())
    before = poster.resolve_path(poster_dir, pending)
    assert before is not None

    rel = poster.adopt(poster_dir, pending, "888888", 3)

    assert rel == "covers/888888-3.jpg"
    assert not before.exists()
    assert [p.name for p in (poster_dir / "covers").iterdir()] == ["888888-3.jpg"]


def test_adopt_replaces_existing_and_survives_missing(poster_dir):
    """认领时清掉本场旧图；源文件丢了就返回 None。"""
    poster.save_poster(poster_dir, "888888", 3, _png())
    pending = poster.save_pending(poster_dir, "888888", _png())

    assert poster.adopt(poster_dir, pending, "888888", 3) == "covers/888888-3.jpg"
    assert [p.name for p in (poster_dir / "covers").iterdir()] == ["888888-3.jpg"]

    assert poster.adopt(poster_dir, "covers/pending-888888.jpg", "888888", 4) is None


def test_sweep_keeps_pending_stem(poster_dir):
    """清理时要把待用配图当作「还在用」。"""
    poster.save_poster(poster_dir, "888888", 1, _png())
    poster.save_pending(poster_dir, "888888", _png())

    assert poster.sweep(poster_dir, {"pending-888888"}) == 1
    assert [p.name for p in (poster_dir / "covers").iterdir()] == ["pending-888888.jpg"]


class _FakeImageEvent:
    """只带消息组件的极简事件。"""

    def __init__(self, components) -> None:
        self._components = list(components)
        self.message_obj = types.SimpleNamespace(message=[])

    def get_messages(self) -> list:
        return list(self._components)


def _comp(kind: str, **kwargs):
    return types.SimpleNamespace(type=kind, **kwargs)


def test_extract_image_prefers_own_attachment_over_reply():
    attached = _comp("image")
    replied = _comp("image")
    event = _FakeImageEvent([_comp("reply", chain=[replied]), attached])
    assert poster.extract_image(event) is attached


def test_extract_image_falls_back_to_replied_message():
    replied = _comp("image")
    event = _FakeImageEvent([_comp("reply", chain=[_comp("plain"), replied])])
    assert poster.extract_image(event) is replied


def test_extract_image_returns_none_without_image():
    event = _FakeImageEvent(
        [_comp("plain"), _comp("reply", chain=[_comp("plain")])],
    )
    assert poster.extract_image(event) is None


def test_extract_image_accepts_enum_style_type():
    """真实组件里 ``type`` 是 ``ComponentType`` 枚举，要取 ``.value`` 比较。"""

    class _ComponentType:
        value = "image"

    event = _FakeImageEvent([_comp("plain"), _comp(_ComponentType())])
    assert poster.extract_image(event) is not None
