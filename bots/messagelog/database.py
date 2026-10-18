"""SQLite persistence for per-guild settings and message-event records."""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite


DEFAULT_EVENTS = {
    "message_delete": True,
    "message_edit": True,
    "bulk_delete": True,
    "member_join": False,
    "member_leave": False,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("DATABASE_PATH", "messagelog.sqlite3")

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    event_types TEXT NOT NULL,
                    retention_days INTEGER NOT NULL DEFAULT 30,
                    updated_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS message_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    message_id INTEGER,
                    event_type TEXT NOT NULL,
                    author_id INTEGER,
                    author_name TEXT,
                    channel_id INTEGER,
                    channel_name TEXT,
                    content TEXT,
                    previous_content TEXT,
                    attachments TEXT NOT NULL DEFAULT '[]',
                    event_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS feedback_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    source_guild_id INTEGER,
                    source_guild_name TEXT,
                    content TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    delivery_message_id INTEGER,
                    reply_text TEXT,
                    replied_at TEXT,
                    replied_by INTEGER
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_user_time "
                "ON feedback_submissions(user_id, submitted_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_delivery "
                "ON feedback_submissions(delivery_message_id, replied_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_guild_time "
                "ON message_logs(guild_id, event_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_message "
                "ON message_logs(guild_id, message_id, event_type)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_member "
                "ON message_logs(guild_id, author_id, event_at DESC)"
            )
            await db.commit()

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
            )
            row = await cur.fetchone()
        if not row:
            return {
                "guild_id": guild_id,
                "channel_id": None,
                "enabled": False,
                "event_types": dict(DEFAULT_EVENTS),
                "retention_days": 30,
                "updated_at": None,
            }
        result = dict(row)
        try:
            result["event_types"] = json.loads(result["event_types"])
        except (TypeError, json.JSONDecodeError):
            result["event_types"] = dict(DEFAULT_EVENTS)
        result["enabled"] = bool(result["enabled"])
        return result

    async def update_config(self, guild_id: int, **changes: Any) -> dict[str, Any]:
        config = await self.get_config(guild_id)
        config.update(changes)
        events = config.get("event_types") or dict(DEFAULT_EVENTS)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO guild_config
                   (guild_id, channel_id, enabled, event_types, retention_days, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                     channel_id=excluded.channel_id,
                     enabled=excluded.enabled,
                     event_types=excluded.event_types,
                     retention_days=excluded.retention_days,
                     updated_at=excluded.updated_at""",
                (
                    guild_id,
                    config.get("channel_id"),
                    int(bool(config.get("enabled"))),
                    json.dumps(events),
                    int(config.get("retention_days", 30)),
                    utcnow(),
                ),
            )
            await db.commit()
        return await self.get_config(guild_id)

    async def add_log(
        self,
        *,
        guild_id: int,
        event_type: str,
        message_id: int | None = None,
        author_id: int | None = None,
        author_name: str | None = None,
        channel_id: int | None = None,
        channel_name: str | None = None,
        content: str | None = None,
        previous_content: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        event_at: str | None = None,
    ) -> int:
        config = await self.get_config(guild_id)
        now = event_at or utcnow()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """INSERT INTO message_logs
                   (guild_id, message_id, event_type, author_id, author_name,
                    channel_id, channel_name, content, previous_content,
                    attachments, event_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    message_id,
                    event_type,
                    author_id,
                    author_name,
                    channel_id,
                    channel_name,
                    content,
                    previous_content,
                    json.dumps(attachments or []),
                    now,
                    json.dumps(metadata or {}),
                ),
            )
            retention = int(config.get("retention_days", 30))
            if retention > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=retention)).isoformat()
                await db.execute(
                    "DELETE FROM message_logs WHERE guild_id = ? AND event_at < ?",
                    (guild_id, cutoff),
                )
            await db.commit()
            return int(cur.lastrowid)

    async def get_log(self, guild_id: int, log_id: int) -> dict[str, Any] | None:
        rows = await self.query(
            "guild_id = ? AND id = ?", (guild_id, log_id), limit=1
        )
        return rows[0] if rows else None

    async def query(
        self,
        where: str,
        params: tuple[Any, ...] = (),
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        # `where` is assembled only from fixed internal clauses; values are bound.
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT * FROM message_logs WHERE {where} "
                "ORDER BY event_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 25)), max(0, offset)),
            )
            rows = [dict(row) for row in await cur.fetchall()]
        for row in rows:
            for key in ("attachments", "metadata"):
                try:
                    row[key] = json.loads(row[key] or ("[]" if key == "attachments" else "{}"))
                except (TypeError, json.JSONDecodeError):
                    row[key] = [] if key == "attachments" else {}
        return rows

    async def search_logs(
        self,
        guild_id: int,
        *,
        query: str | None = None,
        author_id: int | None = None,
        channel_id: int | None = None,
        event_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        clauses = ["guild_id = ?"]
        params: list[Any] = [guild_id]
        if query:
            clauses.append("(content LIKE ? OR previous_content LIKE ?)")
            pattern = f"%{query[:100]}%"
            params.extend([pattern, pattern])
        if author_id:
            clauses.append("author_id = ?")
            params.append(author_id)
        if channel_id:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        return await self.query(
            " AND ".join(clauses), tuple(params), limit=limit
        )

    async def create_feedback(
        self,
        *,
        user_id: int,
        user_name: str,
        source_guild_id: int | None,
        source_guild_name: str | None,
        content: str,
        cooldown_seconds: int = 24 * 60 * 60,
    ) -> tuple[int | None, int]:
        """Save feedback if this user is outside the rolling cooldown.

        Returns (feedback_id, 0) when accepted, or (None, seconds_remaining)
        when the user's most recent submission is still inside the cooldown.
        """
        submitted_at = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """SELECT submitted_at FROM feedback_submissions
                   WHERE user_id = ? ORDER BY submitted_at DESC, id DESC LIMIT 1""",
                (user_id,),
            )
            previous = await cur.fetchone()
            if previous:
                previous_at = datetime.fromisoformat(previous["submitted_at"])
                if previous_at.tzinfo is None:
                    previous_at = previous_at.replace(tzinfo=timezone.utc)
                elapsed = (submitted_at - previous_at).total_seconds()
                remaining = cooldown_seconds - elapsed
                if remaining > 0:
                    await db.rollback()
                    return None, int(remaining) + 1

            cur = await db.execute(
                """INSERT INTO feedback_submissions
                   (user_id, user_name, source_guild_id, source_guild_name,
                    content, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    user_name,
                    source_guild_id,
                    source_guild_name,
                    content,
                    submitted_at.isoformat(),
                ),
            )
            await db.commit()
            return int(cur.lastrowid), 0

    async def get_feedback(self, feedback_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM feedback_submissions WHERE id = ?",
                (feedback_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_undelivered_feedback(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM feedback_submissions
                   WHERE delivery_message_id IS NULL ORDER BY id"""
            )
            return [dict(row) for row in await cur.fetchall()]

    async def get_pending_feedback_replies(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM feedback_submissions
                   WHERE delivery_message_id IS NOT NULL AND replied_at IS NULL
                   ORDER BY id"""
            )
            return [dict(row) for row in await cur.fetchall()]

    async def mark_feedback_delivered(
        self, feedback_id: int, delivery_message_id: int
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE feedback_submissions SET delivery_message_id = ?
                   WHERE id = ? AND delivery_message_id IS NULL""",
                (delivery_message_id, feedback_id),
            )
            await db.commit()

    async def mark_feedback_replied(
        self, feedback_id: int, reply_text: str, replied_by: int
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE feedback_submissions
                   SET reply_text = ?, replied_at = ?, replied_by = ?
                   WHERE id = ? AND replied_at IS NULL""",
                (reply_text, utcnow(), replied_by, feedback_id),
            )
            await db.commit()
