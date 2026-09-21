"""群抽奖系统 —— SQLite 存储层。

设计要点：

* 单文件数据库 ``lottery.db``，位于 ``data/plugin_data/astrbot_plugin_group_lottery/``。
* 所有写操作走同一把可重入锁；单条 SQL 都在微秒级，不会阻塞事件循环。
* 密钥池单独成表（``raffle_keys``），既能统计剩余量，也能追溯每条密钥发给了谁。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import KIND_CONTACT, STATUS_OPEN

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raffles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    umo            TEXT    NOT NULL,
    group_id       TEXT    NOT NULL DEFAULT '',
    seq            INTEGER NOT NULL DEFAULT 0,
    title          TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    prize_kind     TEXT    NOT NULL DEFAULT 'contact',
    winner_count   INTEGER NOT NULL DEFAULT 1,
    draw_at        INTEGER,
    min_players    INTEGER,
    private_notify INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'open',
    created_at     INTEGER NOT NULL,
    created_by     TEXT    NOT NULL DEFAULT '',
    drawn_at       INTEGER,
    draw_trigger   TEXT    NOT NULL DEFAULT '',
    closed_note    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_raffles_umo    ON raffles(umo, status);
CREATE INDEX IF NOT EXISTS idx_raffles_status ON raffles(status, draw_at);

CREATE TABLE IF NOT EXISTS participants (
    raffle_id INTEGER NOT NULL,
    user_id   TEXT    NOT NULL,
    name      TEXT    NOT NULL DEFAULT '',
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (raffle_id, user_id)
);

CREATE TABLE IF NOT EXISTS raffle_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    raffle_id   INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    assigned_to TEXT,
    assigned_at INTEGER,
    UNIQUE (raffle_id, content)
);
CREATE INDEX IF NOT EXISTS idx_keys_raffle ON raffle_keys(raffle_id, assigned_to);

CREATE TABLE IF NOT EXISTS winners (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raffle_id  INTEGER NOT NULL,
    umo        TEXT    NOT NULL DEFAULT '',
    group_id   TEXT    NOT NULL DEFAULT '',
    user_id    TEXT    NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    title      TEXT    NOT NULL DEFAULT '',
    prize      TEXT    NOT NULL DEFAULT '',
    rank       INTEGER NOT NULL DEFAULT 1,
    notified   INTEGER NOT NULL DEFAULT 0,
    claimed    INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_winners_umo ON winners(umo, created_at);
CREATE INDEX IF NOT EXISTS idx_winners_uid ON winners(user_id, created_at);
"""

# 允许通过 update_raffle 修改的字段白名单
_UPDATABLE_RAFFLE_FIELDS = {
    "title",
    "description",
    "prize_kind",
    "winner_count",
    "draw_at",
    "min_players",
    "private_notify",
    "status",
    "drawn_at",
    "draw_trigger",
    "closed_note",
}


def _now() -> int:
    return int(time.time())


class LotteryDB:
    """抽奖数据访问对象。"""

    def __init__(self, path: str | Path) -> None:
        """打开（必要时创建）数据库。

        Args:
            path: 数据库文件路径。
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate_session_isolation()
            self._migrate_add_seq()
            self._conn.commit()

    def _columns(self, table: str) -> set[str]:
        """读取表的列名集合。"""
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _migrate_add_seq(self) -> None:
        """补上群内期号 ``seq`` 列，并为历史数据回填。

        ``CREATE TABLE IF NOT EXISTS`` 不会给老库加列，所以这里显式补。回填按
        ``(group_id, id)`` 顺序编号，保证老记录也有正确的群内期号。
        """
        try:
            if "seq" not in self._columns("raffles"):
                self._conn.execute(
                    "ALTER TABLE raffles ADD COLUMN seq INTEGER NOT NULL DEFAULT 0",
                )
            pending = self._conn.execute(
                "SELECT id, group_id FROM raffles WHERE seq IS NULL OR seq = 0 ORDER BY group_id, id",
            ).fetchall()
            if not pending:
                return
            counters: dict[str, int] = {}
            for row in self._conn.execute(
                "SELECT group_id, COALESCE(MAX(seq), 0) AS top FROM raffles GROUP BY group_id",
            ).fetchall():
                counters[str(row["group_id"])] = int(row["top"] or 0)
            for row in pending:
                group_id = str(row["group_id"])
                counters[group_id] = counters.get(group_id, 0) + 1
                self._conn.execute(
                    "UPDATE raffles SET seq = ? WHERE id = ?",
                    (counters[group_id], row["id"]),
                )
        except Exception as exc:  # pragma: no cover - 迁移失败不应阻断启动
            logger.warning(f"[群抽奖] 群内期号迁移失败（可忽略）：{exc}")

    def _migrate_session_isolation(self) -> None:
        """把受 ``unique_session`` 污染的群标识修正回真实群号。

        v1.0.0 早期版本直接拿 ``event.unified_msg_origin`` 当群标识。AstrBot 开启
        ``unique_session`` 后，群消息的会话段是 ``{用户ID}_{群号}``，于是同一群里
        每个人各存了一份互不可见的抽奖。这里把历史行归一化，避免用户升级后数据作废。

        群号本身不含下划线（QQ 群号是纯数字，各平台适配器也按 ``split("_")[-1]``
        还原群号），因此只要会话段含下划线就一定是被隔离过的。
        """
        try:
            rows = self._conn.execute(
                "SELECT id, umo, group_id FROM raffles WHERE group_id LIKE '%\\_%' ESCAPE '\\'",
            ).fetchall()
            for row in rows:
                group_id = str(row["group_id"]).split("_")[-1]
                platform = str(row["umo"]).split(":", 1)[0] or "aiocqhttp"
                self._conn.execute(
                    "UPDATE raffles SET umo = ?, group_id = ? WHERE id = ?",
                    (f"{platform}:GroupMessage:{group_id}", group_id, row["id"]),
                )
                self._conn.execute(
                    "UPDATE winners SET umo = ?, group_id = ? WHERE raffle_id = ?",
                    (f"{platform}:GroupMessage:{group_id}", group_id, row["id"]),
                )
        except Exception as exc:  # pragma: no cover - 迁移失败不应阻断启动
            logger.warning(f"[群抽奖] 历史会话标识迁移失败（可忽略）：{exc}")

    # ------------------------------------------------------------------ 基础

    def close(self) -> None:
        """关闭数据库连接（插件卸载时调用）。"""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------- 抽奖场次

    def create_raffle(
        self,
        *,
        umo: str,
        group_id: str,
        title: str,
        description: str = "",
        winner_count: int = 1,
        draw_at: int | None = None,
        min_players: int | None = None,
        created_by: str = "",
    ) -> int:
        """新建一场抽奖，返回场次 ID。

        ``id`` 是跨群全局自增的内部主键（用于关联参与者 / 密钥 / 中奖记录）；
        对用户展示的期号用 ``seq``，它按群独立从 1 开始。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM raffles WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            seq = int(row["next"] if row else 1)
        cur = self._exec(
            """
            INSERT INTO raffles
                (umo, group_id, seq, title, description, prize_kind, winner_count,
                 draw_at, min_players, private_notify, status, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                umo,
                group_id,
                seq,
                title,
                description,
                KIND_CONTACT,
                max(1, int(winner_count)),
                draw_at,
                min_players,
                STATUS_OPEN,
                _now(),
                created_by,
            ),
        )
        return int(cur.lastrowid or 0)

    def get_raffle(self, raffle_id: int) -> dict[str, Any] | None:
        """按 ID 读取抽奖。"""
        return self._query_one("SELECT * FROM raffles WHERE id = ?", (int(raffle_id),))

    def get_open_raffle(self, umo: str) -> dict[str, Any] | None:
        """读取某会话当前进行中的抽奖（同一会话同时只允许一场）。"""
        return self._query_one(
            "SELECT * FROM raffles WHERE umo = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (umo, STATUS_OPEN),
        )

    def latest_raffle_by(self, created_by: str) -> dict[str, Any] | None:
        """取某位管理员最近发布的一场抽奖。

        私聊里省略群号时用它兜底：管理员刚操作过的那场通常就是他想继续操作的。
        """
        if not created_by:
            return None
        return self._query_one(
            "SELECT * FROM raffles WHERE created_by = ? ORDER BY id DESC LIMIT 1",
            (str(created_by),),
        )

    def recent_groups_by(self, created_by: str, limit: int = 5) -> list[dict[str, Any]]:
        """列出某位管理员最近发布过抽奖的群（按最近一次操作排序）。

        用于在私聊里给出「你最近操作过这些群」的提示。
        """
        if not created_by:
            return []
        return self._query(
            """
            SELECT group_id, MAX(id) AS last_id, COUNT(*) AS total
            FROM raffles WHERE created_by = ?
            GROUP BY group_id ORDER BY last_id DESC LIMIT ?
            """,
            (str(created_by), max(1, int(limit))),
        )

    def update_raffle(self, raffle_id: int, **fields: Any) -> None:
        """更新抽奖字段（仅白名单字段生效）。"""
        clean = {k: v for k, v in fields.items() if k in _UPDATABLE_RAFFLE_FIELDS}
        if not clean:
            return
        assignments = ", ".join(f"{key} = ?" for key in clean)
        self._exec(
            f"UPDATE raffles SET {assignments} WHERE id = ?",
            (*clean.values(), int(raffle_id)),
        )

    def list_due_raffles(self, now: int | None = None) -> list[dict[str, Any]]:
        """列出所有到点待开奖的抽奖。"""
        return self._query(
            "SELECT * FROM raffles WHERE status = ? AND draw_at IS NOT NULL AND draw_at <= ?"
            " ORDER BY draw_at ASC",
            (STATUS_OPEN, int(now if now is not None else _now())),
        )

    def list_raffles(
        self,
        umo: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """列出抽奖，可按会话与状态过滤。"""
        sql = "SELECT * FROM raffles WHERE 1 = 1"
        params: list[Any] = []
        if umo:
            sql += " AND umo = ?"
            params.append(umo)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return self._query(sql, tuple(params))

    def delete_raffle(self, raffle_id: int) -> None:
        """彻底删除一场抽奖及其报名、密钥、中奖记录。"""
        rid = int(raffle_id)
        with self._lock:
            self._conn.execute("DELETE FROM participants WHERE raffle_id = ?", (rid,))
            self._conn.execute("DELETE FROM raffle_keys WHERE raffle_id = ?", (rid,))
            self._conn.execute("DELETE FROM winners WHERE raffle_id = ?", (rid,))
            self._conn.execute("DELETE FROM raffles WHERE id = ?", (rid,))
            self._conn.commit()

    # --------------------------------------------------------------- 报名

    def add_participant(self, raffle_id: int, user_id: str, name: str) -> bool:
        """加入报名，返回 True 表示新增（False 表示此前已报名）。"""
        try:
            self._exec(
                "INSERT INTO participants (raffle_id, user_id, name, joined_at)"
                " VALUES (?, ?, ?, ?)",
                (int(raffle_id), str(user_id), name or "", _now()),
            )
            return True
        except sqlite3.IntegrityError:
            # 已报名：顺带刷新昵称
            self._exec(
                "UPDATE participants SET name = ? WHERE raffle_id = ? AND user_id = ?",
                (name or "", int(raffle_id), str(user_id)),
            )
            return False

    def remove_participant(self, raffle_id: int, user_id: str) -> bool:
        """退出报名，返回是否确实删掉了一条记录。"""
        cur = self._exec(
            "DELETE FROM participants WHERE raffle_id = ? AND user_id = ?",
            (int(raffle_id), str(user_id)),
        )
        return cur.rowcount > 0

    def list_participants(self, raffle_id: int) -> list[dict[str, Any]]:
        """按报名先后列出参与者。"""
        return self._query(
            "SELECT user_id, name, joined_at FROM participants"
            " WHERE raffle_id = ? ORDER BY joined_at ASC, rowid ASC",
            (int(raffle_id),),
        )

    def count_participants(self, raffle_id: int) -> int:
        """统计报名人数。"""
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM participants WHERE raffle_id = ?",
            (int(raffle_id),),
        )
        return int(row["n"]) if row else 0

    # --------------------------------------------------------------- 密钥池

    def add_keys(self, raffle_id: int, keys: list[str], append: bool = True) -> int:
        """写入密钥池。

        Args:
            raffle_id: 场次 ID。
            keys: 密钥列表，会做去空白、去重、去空行处理。
            append: True 追加；False 先清空已有密钥再写入。

        Returns:
            实际写入的新密钥条数。
        """
        rid = int(raffle_id)
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in keys:
            item = (raw or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            cleaned.append(item)

        with self._lock:
            if not append:
                self._conn.execute(
                    "DELETE FROM raffle_keys WHERE raffle_id = ?", (rid,)
                )
            inserted = 0
            for item in cleaned:
                try:
                    self._conn.execute(
                        "INSERT INTO raffle_keys (raffle_id, content) VALUES (?, ?)",
                        (rid, item),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    continue
            self._conn.commit()
        return inserted

    def list_keys(self, raffle_id: int) -> list[dict[str, Any]]:
        """列出密钥池（含已发放记录）。"""
        return self._query(
            "SELECT id, content, assigned_to, assigned_at FROM raffle_keys"
            " WHERE raffle_id = ? ORDER BY id ASC",
            (int(raffle_id),),
        )

    def count_keys(self, raffle_id: int, only_free: bool = False) -> int:
        """统计密钥数量。"""
        sql = "SELECT COUNT(*) AS n FROM raffle_keys WHERE raffle_id = ?"
        if only_free:
            sql += " AND assigned_to IS NULL"
        row = self._query_one(sql, (int(raffle_id),))
        return int(row["n"]) if row else 0

    def list_free_keys(self, raffle_id: int, limit: int = 1000) -> list[dict[str, Any]]:
        """按入池顺序列出尚未发放的密钥（只读，不改变归属）。"""
        return self._query(
            "SELECT id, content FROM raffle_keys"
            " WHERE raffle_id = ? AND assigned_to IS NULL ORDER BY id ASC LIMIT ?",
            (int(raffle_id), max(1, int(limit))),
        )

    def assign_keys(self, assignments: list[tuple[int, str]]) -> None:
        """把若干条密钥绑定到中奖者。

        Args:
            assignments: ``(密钥行 ID, 中奖者用户 ID)`` 列表。
        """
        if not assignments:
            return
        ts = _now()
        with self._lock:
            for key_id, user_id in assignments:
                self._conn.execute(
                    "UPDATE raffle_keys SET assigned_to = ?, assigned_at = ? WHERE id = ?",
                    (str(user_id), ts, int(key_id)),
                )
            self._conn.commit()

    def clear_keys(self, raffle_id: int) -> int:
        """清空某场次尚未发放的密钥，返回清理条数。"""
        cur = self._exec(
            "DELETE FROM raffle_keys WHERE raffle_id = ? AND assigned_to IS NULL",
            (int(raffle_id),),
        )
        return cur.rowcount

    # --------------------------------------------------------------- 中奖记录

    def add_winners(self, raffle_id: int, rows: list[dict[str, Any]]) -> None:
        """批量写入中奖记录。

        Args:
            raffle_id: 场次 ID。
            rows: 每项包含 user_id / name / prize / rank / notified 等字段。
        """
        if not rows:
            return
        ts = _now()
        with self._lock:
            for row in rows:
                self._conn.execute(
                    """
                    INSERT INTO winners
                        (raffle_id, umo, group_id, user_id, name, title, prize,
                         rank, notified, claimed, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        int(raffle_id),
                        row.get("umo", ""),
                        row.get("group_id", ""),
                        str(row.get("user_id", "")),
                        row.get("name", ""),
                        row.get("title", ""),
                        row.get("prize", ""),
                        int(row.get("rank", 1)),
                        1 if row.get("notified") else 0,
                        int(row.get("created_at") or ts),
                    ),
                )
            self._conn.commit()

    def list_winners(
        self,
        umo: str | None = None,
        user_id: str | None = None,
        raffle_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询中奖记录。

        额外带出该场在群内的期号 ``raffle_seq``，供文案展示使用。
        """
        sql = (
            "SELECT w.*, COALESCE(r.seq, 0) AS raffle_seq FROM winners w "
            "LEFT JOIN raffles r ON r.id = w.raffle_id WHERE 1 = 1"
        )
        params: list[Any] = []
        if umo:
            sql += " AND w.umo = ?"
            params.append(umo)
        if user_id:
            sql += " AND w.user_id = ?"
            params.append(str(user_id))
        if raffle_id is not None:
            sql += " AND w.raffle_id = ?"
            params.append(int(raffle_id))
        sql += " ORDER BY w.id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return self._query(sql, tuple(params))

    def list_claimable(
        self, user_id: str, umo: str | None = None
    ) -> list[dict[str, Any]]:
        """列出某用户首轮私聊未送达、需要自助补领的密钥奖品。"""
        sql = "SELECT * FROM winners WHERE user_id = ? AND prize <> '' AND notified = 0"
        params: list[Any] = [str(user_id)]
        if umo:
            sql += " AND umo = ?"
            params.append(umo)
        sql += " ORDER BY id ASC LIMIT 20"
        return self._query(sql, tuple(params))

    def mark_claimed(self, winner_ids: list[int]) -> None:
        """把若干中奖记录标记为已领取。"""
        if not winner_ids:
            return
        placeholders = ",".join("?" for _ in winner_ids)
        self._exec(
            f"UPDATE winners SET claimed = 1, notified = 1 WHERE id IN ({placeholders})",
            tuple(int(i) for i in winner_ids),
        )

    def mark_notified(self, winner_id: int, ok: bool) -> None:
        """更新某条中奖记录的私聊投递状态。"""
        self._exec(
            "UPDATE winners SET notified = ? WHERE id = ?",
            (1 if ok else 0, int(winner_id)),
        )

    # ----------------------------------------------------------------- 统计

    def overview(self) -> dict[str, Any]:
        """全局统计，供管理面板总览使用。"""
        row = self._query_one(
            """
            SELECT
                (SELECT COUNT(*) FROM raffles)                       AS raffle_total,
                (SELECT COUNT(*) FROM raffles WHERE status = 'open') AS raffle_open,
                (SELECT COUNT(*) FROM raffles WHERE status = 'drawn') AS raffle_drawn,
                (SELECT COUNT(*) FROM winners)                       AS winner_total,
                (SELECT COUNT(*) FROM winners WHERE notified = 1)    AS winner_notified,
                (SELECT COUNT(*) FROM raffle_keys WHERE assigned_to IS NULL) AS key_free,
                (SELECT COUNT(*) FROM raffle_keys)                   AS key_total,
                (SELECT COUNT(DISTINCT umo) FROM raffles)            AS group_total
            """,
        )
        return dict(row) if row else {}

    def known_groups(self) -> list[dict[str, Any]]:
        """列出出现过的群（含进行中抽奖数与累计中奖数）。"""
        return self._query(
            """
            SELECT
                r.umo                                   AS umo,
                r.group_id                              AS group_id,
                COUNT(*)                                AS raffle_total,
                SUM(CASE WHEN r.status = 'open' THEN 1 ELSE 0 END) AS raffle_open,
                MAX(r.created_at)                       AS last_at
            FROM raffles r
            GROUP BY r.umo
            ORDER BY last_at DESC
            LIMIT 200
            """,
        )

    def prune_history(self, per_group_limit: int = 200) -> int:
        """按群裁剪历史开奖记录，返回删除的场次数。

        Args:
            per_group_limit: 每个群保留的最近场次数。

        Returns:
            被删除的场次数量。
        """
        keep = max(10, int(per_group_limit))
        rows = self._query(
            """
            SELECT id FROM (
                SELECT id, umo,
                       ROW_NUMBER() OVER (PARTITION BY umo ORDER BY id DESC) AS rn
                FROM raffles
                WHERE status <> 'open'
            ) WHERE rn > ?
            """,
            (keep,),
        )
        for row in rows:
            self.delete_raffle(int(row["id"]))
        return len(rows)
