
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(settings.data_dir, "grid_bot.sqlite3")

_PLACEHOLDER_RE = re.compile(r"\$(\d+)")
_NOW_RE = re.compile(r"\bNOW\(\)", re.IGNORECASE)


def _adapt_datetime(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(sep=" ", timespec="microseconds")


def _convert_timestamp(raw: bytes) -> Optional[datetime]:
    text = raw.decode()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("TIMESTAMP", _convert_timestamp)


def _translate_sql(sql: str) -> str:
    """Переводит asyncpg-синтаксис в SQLite: $N -> ?N, NOW() -> CURRENT_TIMESTAMP."""
    sql = _NOW_RE.sub("CURRENT_TIMESTAMP", sql)
    return _PLACEHOLDER_RE.sub(r"?\1", sql)


class _Connection:
    """Обёртка над aiosqlite-соединением с интерфейсом asyncpg."""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def execute(self, query: str, *args: Any) -> None:
        await self._conn.execute(_translate_sql(query), args)
        await self._conn.commit()

    async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
        cursor = await self._conn.execute(_translate_sql(query), args)
        rows = await cursor.fetchall()
        await self._conn.commit()
        return [dict(row) for row in rows]

    async def fetchrow(self, query: str, *args: Any) -> Optional[Dict[str, Any]]:
        cursor = await self._conn.execute(_translate_sql(query), args)
        row = await cursor.fetchone()
        await self._conn.commit()
        return dict(row) if row is not None else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        cursor = await self._conn.execute(_translate_sql(query), args)
        row = await cursor.fetchone()
        await self._conn.commit()
        return row[0] if row is not None else None


class _AcquireContext:
    def __init__(self, path: str):
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> _Connection:
        self._conn = await aiosqlite.connect(
            self._path, detect_types=sqlite3.PARSE_DECLTYPES, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        return _Connection(self._conn)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            await self._conn.close()


class Database:
    def __init__(self, path: str):
        self.path = path

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self.path)


db = Database(DB_PATH)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_bots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    class_code      TEXT    NOT NULL,
    p_low           REAL    NOT NULL,
    p_high          REAL    NOT NULL,
    capital         REAL    NOT NULL,
    n               INTEGER NOT NULL,
    initial_lots    INTEGER NOT NULL DEFAULT 0,
    cash_remaining  REAL    NOT NULL DEFAULT 0,
    current_price   REAL,
    status          TEXT    NOT NULL DEFAULT 'created',
    type            TEXT    DEFAULT 'simple',
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS grid_orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id            INTEGER NOT NULL REFERENCES grid_bots(id),
    order_id          TEXT    NOT NULL,
    client_order_id   TEXT,
    exchange_order_id TEXT,
    side              TEXT    NOT NULL,
    level_idx         INTEGER NOT NULL,
    lots              INTEGER NOT NULL,
    pair_level        INTEGER,
    status            TEXT    NOT NULL DEFAULT 'active',
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_grid_orders_bot_status ON grid_orders(bot_id, status);

CREATE TABLE IF NOT EXISTS trade_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id           INTEGER NOT NULL,
    bot_type         TEXT,
    order_id         TEXT,
    figi             TEXT,
    ticker           TEXT,
    direction        TEXT    NOT NULL,
    lots             INTEGER NOT NULL,
    lot_size         INTEGER NOT NULL DEFAULT 1,
    price_per_share  REAL    NOT NULL DEFAULT 0,
    quantity_shares  INTEGER NOT NULL DEFAULT 0,
    total_amount     REAL    NOT NULL DEFAULT 0,
    commission       REAL    NOT NULL DEFAULT 0,
    executed_at      TIMESTAMP,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trade_history_bot ON trade_history(bot_id, executed_at);
"""


async def init_db() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH, timeout=30.0)
    try:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()
    logger.info("База данных инициализирована: %s", DB_PATH)
