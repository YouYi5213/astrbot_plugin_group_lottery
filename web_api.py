"""群抽奖系统 —— 管理面板 Web API。

路由以插件名为前缀注册（``/{plugin}/...``），页面里通过
``window.AstrBotPluginPage.apiGet/apiPost`` 调用，未嵌入时回退到
``/api/plug/{plugin}/...`` 同源直连。

所有响应统一为 ``{status, message, data}`` 信封。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.web import json_response, request

from .core import texts
from .core.engine import parse_keys
from .core.models import KIND_KEY, STATUS_CANCELLED, STATUS_LABELS, TRIGGER_LABELS
from .core.timeparse import format_ts


def _ok(data: Any = None, message: str = "") -> Any:
    """构造成功响应。"""
    return json_response({"status": "ok", "message": message, "data": data})


def _err(message: str) -> Any:
    """构造失败响应。"""
    return json_response({"status": "error", "message": message, "data": None})


def _decorate(raffle: dict[str, Any]) -> dict[str, Any]:
    """给抽奖记录补充面板展示用的派生字段。"""
    item = dict(raffle)
    item["status_label"] = STATUS_LABELS.get(
        str(item.get("status")), str(item.get("status"))
    )
    item["trigger_label"] = TRIGGER_LABELS.get(str(item.get("draw_trigger")), "")
    item["draw_at_str"] = format_ts(item.get("draw_at"))
    item["created_at_str"] = format_ts(item.get("created_at"))
    item["drawn_at_str"] = format_ts(item.get("drawn_at"))
    # 面板展示用群内期号；raffle.id 仍是内部主键（接口按它取详情）
    item["seq"] = texts.raffle_no(item)
    return item


def _decorate_winner(row: dict[str, Any]) -> dict[str, Any]:
    """给中奖记录补充展示字段。"""
    item = dict(row)
    item["time_str"] = format_ts(item.get("created_at"))
    item["raffle_seq"] = texts.raffle_no(item)
    return item


class LotteryWebApi:
    """持有插件实例，方法即路由处理函数。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ------------------------------------------------------------ GET

    async def overview(self, **_: Any) -> Any:
        """GET 总览统计 + 最近中奖。"""
        try:
            db = self.plugin.db
            stats = db.overview()
            recent = [_decorate_winner(r) for r in db.list_winners(limit=10)]
            open_raffles = [
                _decorate(r) for r in db.list_raffles(status="open", limit=20)
            ]
            return _ok({"stats": stats, "recent": recent, "open_raffles": open_raffles})
        except Exception as exc:
            logger.error(f"[群抽奖] 面板总览失败：{exc}", exc_info=True)
            return _err(str(exc))

    async def list_raffles(self, **_: Any) -> Any:
        """GET 抽奖场次列表。query: status / group_id / limit"""
        try:
            query = request.query
            status = (query.get("status") or "").strip() or None
            group_id = (query.get("group_id") or "").strip()
            limit = query.get("limit", 100, type=int) or 100
            rows = self.plugin.db.list_raffles(
                status=status, limit=min(max(limit, 1), 500)
            )
            if group_id:
                rows = [r for r in rows if str(r.get("group_id")) == group_id]
            items = []
            for row in rows:
                item = _decorate(row)
                item["participant_count"] = self.plugin.db.count_participants(
                    int(row["id"])
                )
                item["free_keys"] = self.plugin.db.count_keys(
                    int(row["id"]), only_free=True
                )
                items.append(item)
            return _ok({"raffles": items, "total": len(items)})
        except Exception as exc:
            logger.error(f"[群抽奖] 场次列表失败：{exc}", exc_info=True)
            return _err(str(exc))

    async def raffle_detail(self, raffle_id: str = "", **_: Any) -> Any:
        """GET 单场抽奖详情：报名名单 / 中奖名单 / 密钥池。"""
        try:
            raffle = self.plugin.db.get_raffle(int(raffle_id))
            if not raffle:
                return _err(f"未找到第 {raffle_id} 期抽奖")
            keys = self.plugin.db.list_keys(int(raffle_id))
            return _ok(
                {
                    "raffle": _decorate(raffle),
                    "participants": self.plugin.db.list_participants(int(raffle_id)),
                    "winners": [
                        _decorate_winner(w)
                        for w in self.plugin.db.list_winners(
                            raffle_id=int(raffle_id), limit=500
                        )
                    ],
                    "keys": [
                        {
                            "id": k["id"],
                            "content": k["content"],
                            "assigned_to": k.get("assigned_to") or "",
                            "assigned_at_str": format_ts(k.get("assigned_at")),
                        }
                        for k in keys
                    ],
                },
            )
        except ValueError:
            return _err("场次 ID 必须是数字")
        except Exception as exc:
            logger.error(f"[群抽奖] 场次详情失败：{exc}", exc_info=True)
            return _err(str(exc))

    async def list_winners(self, **_: Any) -> Any:
        """GET 中奖记录。query: group_id / limit"""
        try:
            query = request.query
            group_id = (query.get("group_id") or "").strip()
            limit = query.get("limit", 200, type=int) or 200
            rows = self.plugin.db.list_winners(limit=min(max(limit, 1), 1000))
            if group_id:
                rows = [r for r in rows if str(r.get("group_id")) == group_id]
            items = [_decorate_winner(r) for r in rows]
            return _ok({"winners": items, "total": len(items)})
        except Exception as exc:
            logger.error(f"[群抽奖] 中奖记录失败：{exc}", exc_info=True)
            return _err(str(exc))

    async def list_groups(self, **_: Any) -> Any:
        """GET 出现过抽奖的群列表。"""
        try:
            groups = self.plugin.db.known_groups()
            for group in groups:
                group["last_at_str"] = format_ts(group.get("last_at"))
            return _ok({"groups": groups})
        except Exception as exc:
            logger.error(f"[群抽奖] 群列表失败：{exc}", exc_info=True)
            return _err(str(exc))

    # ------------------------------------------------------------ POST

    async def save_keys(self, **_: Any) -> Any:
        """POST 保存密钥池。body: {raffle_id, keys | text, mode: replace|append}"""
        try:
            body = await request.json(default={}) or {}
            raffle_id = int(body.get("raffle_id") or 0)
            raffle = self.plugin.db.get_raffle(raffle_id)
            if not raffle:
                return _err("抽奖不存在")
            if raffle.get("status") != "open":
                return _err("该场抽奖已结束，无法修改密钥池")

            raw = body.get("keys")
            if isinstance(raw, list):
                keys = parse_keys("\n".join(str(x) for x in raw))
            else:
                keys = parse_keys(str(raw or ""))
            mode = str(body.get("mode") or "append").lower()
            append = mode != "replace"

            if not keys:
                return _err("没有解析到有效密钥")

            inserted = self.plugin.db.add_keys(raffle_id, keys, append=append)
            free = self.plugin.db.count_keys(raffle_id, only_free=True)
            if free > 0:
                self.plugin.db.update_raffle(
                    raffle_id,
                    prize_kind=KIND_KEY,
                    private_notify=1,
                )
            return _ok(
                {"inserted": inserted, "free_keys": free},
                message=f"已{'追加' if append else '覆盖'} {inserted} 条密钥，剩余 {free} 条",
            )
        except Exception as exc:
            logger.error(f"[群抽奖] 保存密钥失败：{exc}", exc_info=True)
            return _err(str(exc))

    async def draw_now(self, **_: Any) -> Any:
        """POST 立即开奖。body: {raffle_id}"""
        try:
            body = await request.json(default={}) or {}
            raffle_id = int(body.get("raffle_id") or 0)
            error = await self.plugin.panel_draw(raffle_id)
            if error:
                return _err(error)
            return _ok(None, message="开奖指令已执行，请在群里查看结果")
        except Exception as exc:
            logger.error(f"[群抽奖] 面板开奖失败：{exc}", exc_info=True)
            return _err(str(exc))

    async def cancel(self, **_: Any) -> Any:
        """POST 取消抽奖。body: {raffle_id}"""
        try:
            body = await request.json(default={}) or {}
            raffle_id = int(body.get("raffle_id") or 0)
            raffle = self.plugin.db.get_raffle(raffle_id)
            if not raffle:
                return _err("抽奖不存在")
            if raffle.get("status") != "open":
                return _err("该场抽奖已经结束")
            self.plugin.db.update_raffle(
                raffle_id,
                status=STATUS_CANCELLED,
                closed_note="面板取消",
                drawn_at=int(time.time()),
            )
            return _ok(None, message=f"第 {raffle_id} 期已取消")
        except Exception as exc:
            logger.error(f"[群抽奖] 面板取消失败：{exc}", exc_info=True)
            return _err(str(exc))
