import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import aiosqlite
from app.config import settings
logger = logging.getLogger(__name__)
DB_PATH = os.path.join(settings.data_dir, 'grid_bot.sqlite3')
_PLACEHOLDER_RE = re.compile('\\$(\\d+)')
_NOW_RE = re.compile('\\bNOW\\(\\)', re.IGNORECASE)

def _adapt_datetime(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(sep=' ', timespec='microseconds')

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
sqlite3.register_converter('TIMESTAMP', _convert_timestamp)

def _translate_sql(sql: str) -> str:
    sql = _NOW_RE.sub('CURRENT_TIMESTAMP', sql)
    return _PLACEHOLDER_RE.sub('?\\1', sql)

class _Connection:

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
        self._conn = await aiosqlite.connect(self._path, detect_types=sqlite3.PARSE_DECLTYPES, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute('PRAGMA journal_mode=WAL')
        await self._conn.execute('PRAGMA busy_timeout=5000')
        await self._conn.execute('PRAGMA foreign_keys=ON')
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
_SCHEMA = "\nCREATE TABLE IF NOT EXISTS grid_bots (\n    id              INTEGER PRIMARY KEY AUTOINCREMENT,\n    user_id         TEXT    NOT NULL,\n    ticker          TEXT    NOT NULL,\n    class_code      TEXT    NOT NULL,\n    p_low           REAL    NOT NULL,\n    p_high          REAL    NOT NULL,\n    capital         REAL    NOT NULL,\n    n               INTEGER NOT NULL,\n    initial_lots    INTEGER NOT NULL DEFAULT 0,\n    cash_remaining  REAL    NOT NULL DEFAULT 0,\n    current_price   REAL,\n    status          TEXT    NOT NULL DEFAULT 'created',\n    type            TEXT    DEFAULT 'simple',\n    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\n\nCREATE TABLE IF NOT EXISTS grid_orders (\n    id                INTEGER PRIMARY KEY AUTOINCREMENT,\n    bot_id            INTEGER NOT NULL REFERENCES grid_bots(id),\n    order_id          TEXT    NOT NULL,\n    client_order_id   TEXT,\n    exchange_order_id TEXT,\n    side              TEXT    NOT NULL,\n    level_idx         INTEGER NOT NULL,\n    lots              INTEGER NOT NULL,\n    pair_level        INTEGER,\n    status            TEXT    NOT NULL DEFAULT 'active',\n    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\nCREATE INDEX IF NOT EXISTS idx_grid_orders_bot_status ON grid_orders(bot_id, status);\n\nCREATE TABLE IF NOT EXISTS trade_history (\n    id               INTEGER PRIMARY KEY AUTOINCREMENT,\n    bot_id           INTEGER NOT NULL,\n    bot_type         TEXT,\n    order_id         TEXT,\n    figi             TEXT,\n    ticker           TEXT,\n    direction        TEXT    NOT NULL,\n    lots             INTEGER NOT NULL,\n    lot_size         INTEGER NOT NULL DEFAULT 1,\n    price_per_share  REAL    NOT NULL DEFAULT 0,\n    quantity_shares  INTEGER NOT NULL DEFAULT 0,\n    total_amount     REAL    NOT NULL DEFAULT 0,\n    commission       REAL    NOT NULL DEFAULT 0,\n    executed_at      TIMESTAMP,\n    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\nCREATE INDEX IF NOT EXISTS idx_trade_history_bot ON trade_history(bot_id, executed_at);\n"

async def init_db() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH, timeout=30.0)
    try:
        await conn.execute('PRAGMA journal_mode=WAL')
        await conn.executescript(_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()
    logger.info('База данных инициализирована: %s', DB_PATH)
