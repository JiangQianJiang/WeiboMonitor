"""State repository using SQLite for persistence."""
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

import aiosqlite

DB_PATH = Path(__file__).resolve().parent.parent / "weibo_monitor.db"


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
        now = datetime.now().isoformat()
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO account_state (weiboid, latest_id, screen_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(weiboid) DO UPDATE SET
                    latest_id = excluded.latest_id,
                    screen_name = COALESCE(excluded.screen_name, account_state.screen_name),
                    updated_at = excluded.updated_at
                """,
                (weiboid, latest_id, screen_name, now),
            )
            await db.commit()

        if weiboid not in self._cache:
            self._cache[weiboid] = {}
        self._cache[weiboid]["latest_id"] = latest_id
        if screen_name:
            self._cache[weiboid]["screen_name"] = screen_name

    async def save_weibo_history(self, weibo_info: Dict) -> None:
        """Save weibo to history table with duplicate prevention."""
        async with self._connect() as db:
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
            await db.commit()

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