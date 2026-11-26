"""State repository using SQLite for persistence."""
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional
from uuid import uuid4

import aiosqlite

from core.events import Event, TIMELINE_TYPES

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "weibo_monitor.db"
DB_PATH = Path(os.environ.get("STATE_DIR", str(_DEFAULT_DB_PATH))) / "weibo_monitor.db" \
    if os.environ.get("STATE_DIR") else _DEFAULT_DB_PATH
MAX_DELIVERY_ATTEMPTS = 5


class StateRepository:
    """Repository for managing weibo state, history, and push logs with SQLite backend."""

    def __init__(self):
        self._cache: Dict[str, Dict] = {}
        self._db_path = DB_PATH
        self._initialized = False

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Get database connection with foreign keys enabled."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    async def initialize(self) -> None:
        """Initialize database schema and load cache into memory."""
        await self._ensure_database()
        await self._load_cache()
        self._initialized = True

    async def _ensure_database(self) -> None:
        """Create tables and indexes if they don't exist."""
        schema_path = Path(__file__).resolve().parent / "schema.sql"
        async with self._connect() as db:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema_sql = f.read()
            await db.executescript(schema_sql)
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("PRAGMA table_info(events)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if "delivery_attempts" not in columns:
                await db.execute("ALTER TABLE events ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0")
            if "failed_at" not in columns:
                await db.execute("ALTER TABLE events ADD COLUMN failed_at TEXT")
            await db.commit()

    async def _load_cache(self) -> None:
        """Load all account states from database into memory cache."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT weiboid, latest_id, screen_name FROM account_state"
            ) as cursor:
                async for row in cursor:
                    weiboid, latest_id, screen_name = row
                    self._cache[weiboid] = {
                        "latest_id": latest_id,
                        "screen_name": screen_name or "",
                    }

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def get_latest_id(self, weiboid: str) -> str:
        """Get latest_id from memory cache (zero database queries)."""
        return self._cache.get(weiboid, {}).get("latest_id", "")

    def get_screen_name(self, weiboid: str) -> str:
        """Get screen_name from memory cache."""
        return self._cache.get(weiboid, {}).get("screen_name", "")

    async def set_latest_id(
        self, weiboid: str, latest_id: str, screen_name: Optional[str] = None
    ) -> None:
        """Update latest_id in database first, then update memory cache."""
        async with self._connect() as db:
            await self._write_latest_id(db, weiboid, latest_id, screen_name)
            await db.commit()
        self._cache_latest_id(weiboid, latest_id, screen_name)

    @staticmethod
    async def _write_latest_id(db, weiboid, latest_id, screen_name):
        await db.execute(
            """
            INSERT INTO account_state (weiboid, latest_id, screen_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(weiboid) DO UPDATE SET
                latest_id = excluded.latest_id,
                screen_name = COALESCE(excluded.screen_name, account_state.screen_name),
                updated_at = excluded.updated_at
            """,
            (weiboid, latest_id, screen_name, datetime.now().isoformat()),
        )

    def _cache_latest_id(self, weiboid, latest_id, screen_name):
        if weiboid not in self._cache:
            self._cache[weiboid] = {}
        self._cache[weiboid]["latest_id"] = latest_id
        if screen_name:
            self._cache[weiboid]["screen_name"] = screen_name

    async def save_weibo_history(self, weibo_info: Dict) -> None:
        """Save weibo to history table with duplicate prevention."""
        async with self._connect() as db:
            await self._write_weibo_history(db, weibo_info)
            await db.commit()

    @staticmethod
    async def _write_weibo_history(db, weibo_info):
        await db.execute(
            """
            INSERT INTO weibo_history
            (weiboid, weibo_id, text, screen_name, source, region_name, created_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(weiboid, weibo_id) DO NOTHING
            """,
            (
                weibo_info["weiboid"],
                weibo_info["id"],
                weibo_info["text"],
                weibo_info["screen_name"],
                weibo_info.get("source"),
                weibo_info.get("region_name"),
                weibo_info.get("time"),
            ),
        )

    async def log_push(
        self,
        weiboid: str,
        weibo_id: str,
        channel: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """Log push result for a specific channel."""
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO push_log (weiboid, weibo_id, channel, status, error_message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (weiboid, weibo_id, channel, status, error_message),
            )
            await db.commit()

    async def get_weibo_history(
        self, weiboid: str, limit: int = 100
    ) -> List[Dict]:
        """Query weibo history for a specific account."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM weibo_history
                WHERE weiboid = ?
                ORDER BY fetched_at DESC
                LIMIT ?
                """,
                (weiboid, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_push_stats(self, days: int = 7) -> Dict[str, int]:
        """Get push statistics for the last N days."""
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT channel, status, COUNT(*) as count
                FROM push_log
                WHERE pushed_at >= datetime('now', '-' || ? || ' days')
                GROUP BY channel, status
                """,
                (days,),
            ) as cursor:
                rows = await cursor.fetchall()
                return {f"{row[0]}_{row[1]}": row[2] for row in rows}

    async def get_event_snapshot(self, account_id: str, stream: str) -> tuple[int, dict]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT revision, snapshot FROM event_snapshots WHERE account_id = ? AND stream = ?",
                (account_id, stream),
            ) as cursor:
                row = await cursor.fetchone()
        return (row[0], json.loads(row[1])) if row else (0, {})

    async def save_event_scan(
        self, account_id: str, stream: str, revision: int, snapshot: dict, events: list[Event],
        *, legacy_baseline: tuple[str, str] | None = None,
    ) -> None:
        """Commit a snapshot and its new events together if the input revision is current."""
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT revision FROM event_snapshots WHERE account_id = ? AND stream = ?",
                (account_id, stream),
            ) as cursor:
                row = await cursor.fetchone()
            if (row[0] if row else 0) != revision:
                return
            await db.execute(
                """
                INSERT INTO event_snapshots (account_id, stream, revision, snapshot)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id, stream) DO UPDATE SET
                    revision = excluded.revision, snapshot = excluded.snapshot
                """,
                (account_id, stream, revision + 1, json.dumps(snapshot, ensure_ascii=False)),
            )
            for event in events:
                if event.type in TIMELINE_TYPES:
                    async with db.execute(
                        "SELECT 1 FROM weibo_history WHERE weiboid = ? AND weibo_id = ?",
                        (account_id, event.subject["id"]),
                    ) as cursor:
                        if await cursor.fetchone():
                            continue
                await db.execute(
                    """
                    INSERT INTO events (event_id, account_id, type, observed_at, payload)
                    VALUES (?, ?, ?, ?, ?) ON CONFLICT(event_id) DO NOTHING
                    """,
                    (event.event_id, event.account_id, event.type, event.observed_at,
                     json.dumps(event.to_dict(), ensure_ascii=False)),
                )
            if legacy_baseline:
                await self._write_latest_id(db, account_id, *legacy_baseline)
            await db.commit()
        if legacy_baseline:
            self._cache_latest_id(account_id, *legacy_baseline)

    async def claim_pending_event(self, account_id: str) -> tuple[Event, str] | None:
        """Lease the oldest delivery so concurrent workers cannot send it together."""
        token = str(uuid4())
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """
                SELECT event_id, payload, lease_until IS NULL OR lease_until <= CURRENT_TIMESTAMP
                FROM events WHERE account_id = ? AND delivered_at IS NULL AND failed_at IS NULL
                ORDER BY rowid LIMIT 1
                """,
                (account_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row or not row[2]:
                return None
            await db.execute(
                "UPDATE events SET delivery_token = ?, lease_until = datetime('now', '+2 minutes') WHERE event_id = ?",
                (token, row[0]),
            )
            await db.commit()
        return Event.from_dict(json.loads(row[1])), token

    async def finish_event_delivery(self, event: Event, token: str, results: dict) -> bool:
        """Record an attempt and return whether the worker can advance the queue."""
        success = any(sent for sent, _ in results.values())
        timeline = event.type in TIMELINE_TYPES
        post = event.data.get("post", {})
        account_id = event.account_id
        post_id = event.subject["id"]
        async with self._connect() as db:
            async with db.execute(
                """
                UPDATE events SET delivered_at = CASE WHEN ? THEN CURRENT_TIMESTAMP END,
                    failed_at = CASE WHEN NOT ? AND delivery_attempts + 1 >= ? THEN CURRENT_TIMESTAMP END,
                    delivery_attempts = delivery_attempts + 1,
                    delivery_token = NULL, lease_until = NULL
                WHERE event_id = ? AND delivery_token = ? AND delivered_at IS NULL AND failed_at IS NULL
                """,
                (success, success, MAX_DELIVERY_ATTEMPTS, event.event_id, token),
            ) as cursor:
                if not cursor.rowcount:
                    return False
            async with db.execute("SELECT failed_at FROM events WHERE event_id = ?", (event.event_id,)) as cursor:
                failed_at, = await cursor.fetchone()
            logs = [
                (channel, "success" if sent else "failed", error)
                for channel, (sent, error) in results.items()
            ]
            await db.executemany(
                "INSERT INTO event_push_log (event_id, channel, status, error_message) VALUES (?, ?, ?, ?)",
                [(event.event_id, *entry) for entry in logs],
            )
            if timeline:
                if success:
                    await self._write_latest_id(db, account_id, post_id, post["screen_name"])
                    await self._write_weibo_history(db, {**post, "weiboid": account_id})
                await db.executemany(
                    """
                    INSERT INTO push_log (weiboid, weibo_id, channel, status, error_message)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [(account_id, post_id, *entry) for entry in logs],
                )
            await db.commit()
        if timeline and success:
            self._cache_latest_id(account_id, post_id, post["screen_name"])
        return success or failed_at is not None
