import asyncio
import logging
import os
import re
import secrets
from collections import deque
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import asyncpg
import httpx

from app.config import settings
from app.license import license_manager

logger = logging.getLogger(__name__)

APP_NAME = "t-invest-grid-bot"
APP_VERSION = "1.0.0"

_log_queue: "deque[tuple]" = deque(maxlen=2000)


class MarketLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("asyncpg"):
                return
            _log_queue.append(
                (record.levelname, self.format(record), datetime.now(timezone.utc))
            )
        except Exception:
            pass


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    port = parts.port
    default = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if (port is None or default) else f"{host}:{port}"
    return f"{scheme}://{netloc}"


def _load_or_create_server_key() -> str:
    path = os.path.join(settings.data_dir, "server_key")
    try:
        os.makedirs(settings.data_dir, exist_ok=True)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        v = "srv_" + secrets.token_hex(16)
        with open(path, "w", encoding="utf-8") as f:
            f.write(v)
        return v
    except Exception:
        return "srv_" + secrets.token_hex(16)


async def _verify_broker_token() -> Optional[bool]:
    if not settings.tinkoff_token:
        return False
    try:
        from app.services.grid_bot.config import ACCOUNTS_URL, CA_BUNDLE_PATH

        async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=10.0) as client:
            resp = await client.post(
                ACCOUNTS_URL,
                headers={
                    "Authorization": f"Bearer {settings.tinkoff_token}",
                    "Content-Type": "application/json",
                },
                json={},
            )
        if resp.status_code == 200:
            return True
        if resp.status_code in (401, 403):
            return False
        return None
    except Exception:
        return None


class MarketSync:
    def __init__(self) -> None:
        self.pool: Optional[asyncpg.Pool] = None
        self.server_key = _load_or_create_server_key()
        self.url = normalize_url(settings.app_url)
        self.server_id: Optional[int] = None
        self._last_pushed: dict = {}
        self._create_failed: set = set()
        self._token_valid: Optional[bool] = None
        self._cycle = 0

    @property
    def enabled(self) -> bool:
        return bool(settings.bot_market_dsn and self.url)

    async def connect(self) -> None:
        dsn = settings.bot_market_dsn
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, timeout=15)

    async def register(self) -> None:
        assert self.pool is not None
        token_configured = bool(settings.tinkoff_token)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO bot_servers (server_key, url, token_configured, status, version, last_seen_at, updated_at)
                VALUES ($1, $2, $3, 'online', $4, now(), now())
                ON CONFLICT (url) DO UPDATE SET
                    server_key = EXCLUDED.server_key,
                    token_configured = EXCLUDED.token_configured,
                    status = 'online',
                    version = EXCLUDED.version,
                    last_seen_at = now(),
                    updated_at = now()
                RETURNING id
                """,
                self.server_key,
                self.url,
                token_configured,
                APP_VERSION,
            )
            self.server_id = int(row["id"])
        logger.info(
            "bot_market: сервер зарегистрирован (url=%s, id=%s)", self.url, self.server_id
        )

    async def _self_row(self, conn: asyncpg.Connection):
        return await conn.fetchrow(
            "SELECT id, user_id, connected, lease, lease_sig FROM bot_servers WHERE server_key=$1",
            self.server_key,
        )

    async def _heartbeat(self, conn: asyncpg.Connection) -> None:
        if self._cycle % 20 == 0:
            self._token_valid = await _verify_broker_token()
        await conn.execute(
            """
            UPDATE bot_servers
               SET status='online', last_seen_at=now(), updated_at=now(),
                   token_configured=$2, token_valid=$3, version=$4
             WHERE server_key=$1
            """,
            self.server_key,
            bool(settings.tinkoff_token),
            self._token_valid,
            APP_VERSION,
        )

    async def _reconcile_bots(self, conn: asyncpg.Connection, user_id: Optional[str]) -> None:
        from app.services.grid_bot.models import GridBotCreate
        from app.services.grid_bot.service import GridBotService

        rows = await conn.fetch(
            "SELECT id, ticker, class_code, p_low, p_high, capital, n, desired_state, "
            "sell_on_delete, status, remote_bot_id FROM bots WHERE server_id=$1",
            self.server_id,
        )

        for cfg in rows:
            cfg_id = int(cfg["id"])
            try:
                if cfg["desired_state"] == "delete":
                    if cfg["remote_bot_id"] is not None:
                        await GridBotService.delete_bot(
                            int(cfg["remote_bot_id"]), bool(cfg["sell_on_delete"])
                        )
                    await conn.execute("DELETE FROM bots WHERE id=$1", cfg_id)
                    self._last_pushed.pop(cfg_id, None)
                    continue

                if cfg_id in self._create_failed:
                    continue

                remote_id = cfg["remote_bot_id"]
                if remote_id is None and not settings.tinkoff_token:
                    continue
                if remote_id is None:
                    try:
                        remote_id = await GridBotService.create_bot(
                            GridBotCreate(
                                ticker=cfg["ticker"],
                                class_code=cfg["class_code"],
                                P_low=float(cfg["p_low"]),
                                P_high=float(cfg["p_high"]),
                                capital=float(cfg["capital"]),
                                N=int(cfg["n"]),
                            )
                        )
                    except ValueError as e:
                        self._create_failed.add(cfg_id)
                        await conn.execute(
                            "UPDATE bots SET status='error', error=$2, updated_at=now() WHERE id=$1",
                            cfg_id,
                            str(e),
                        )
                        continue
                    await conn.execute(
                        "UPDATE bots SET remote_bot_id=$2, status='created', updated_at=now() WHERE id=$1",
                        cfg_id,
                        int(remote_id),
                    )

                remote_id = int(remote_id)
                local = await GridBotService.get_bot(remote_id)
                if local is None:
                    await conn.execute(
                        "UPDATE bots SET remote_bot_id=NULL, updated_at=now() WHERE id=$1",
                        cfg_id,
                    )
                    continue

                allowed = license_manager.is_active() and bool(settings.tinkoff_token)
                if cfg["desired_state"] == "run" and allowed:
                    if local.get("status") != "active":
                        await GridBotService.start_bot(remote_id)
                elif cfg["desired_state"] == "stop":
                    if local.get("status") == "active":
                        await GridBotService.stop_bot(remote_id)

                local = await GridBotService.get_bot(remote_id) or local
                status = local.get("status")
                if status != "active" and remote_id in GridBotService.get_waiting_bots():
                    status = "waiting"

                await conn.execute(
                    """
                    UPDATE bots SET status=$2, current_price=$3, initial_lots=$4,
                        cash_remaining=$5, error=NULL, updated_at=now()
                     WHERE id=$1
                    """,
                    cfg_id,
                    status,
                    _f(local.get("current_price")),
                    _i(local.get("initial_lots")),
                    _f(local.get("cash_remaining")),
                )

                if user_id:
                    await self._push_trades(conn, cfg_id, remote_id, user_id, cfg["ticker"])
            except Exception as e:
                logger.error("bot_market: ошибка синхронизации бота #%s: %s", cfg_id, e)
                try:
                    await conn.execute(
                        "UPDATE bots SET status='error', error=$2, updated_at=now() WHERE id=$1",
                        cfg_id,
                        str(e),
                    )
                except Exception:
                    pass

    async def _push_trades(
        self,
        conn: asyncpg.Connection,
        cfg_id: int,
        remote_id: int,
        user_id: str,
        ticker: Optional[str],
    ) -> None:
        from app.database import db

        last = self._last_pushed.get(cfg_id, 0)
        async with db.acquire() as local_conn:
            trades = await local_conn.fetch(
                "SELECT id, direction, lots, lot_size, price_per_share, quantity_shares, "
                "total_amount, commission, executed_at, ticker FROM trade_history "
                "WHERE bot_id=$1 AND id > $2 ORDER BY id ASC",
                remote_id,
                last,
            )
        max_id = last
        for t in trades:
            executed = t["executed_at"]
            if isinstance(executed, str):
                try:
                    executed = datetime.fromisoformat(executed)
                except ValueError:
                    executed = datetime.now(timezone.utc)
            if executed is None:
                executed = datetime.now(timezone.utc)
            if executed.tzinfo is None:
                executed = executed.replace(tzinfo=timezone.utc)
            await conn.execute(
                """
                INSERT INTO bot_trades (bot_config_id, server_id, user_id, remote_trade_id,
                    remote_bot_id, ticker, direction, lots, lot_size, price_per_share,
                    quantity_shares, total_amount, commission, executed_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                ON CONFLICT (server_id, remote_trade_id) DO NOTHING
                """,
                cfg_id,
                self.server_id,
                user_id,
                int(t["id"]),
                remote_id,
                t["ticker"] or ticker,
                t["direction"],
                int(t["lots"] or 0),
                int(t["lot_size"] or 1),
                _f(t["price_per_share"]) or 0.0,
                int(t["quantity_shares"] or 0),
                _f(t["total_amount"]) or 0.0,
                _f(t["commission"]) or 0.0,
                executed,
            )
            max_id = max(max_id, int(t["id"]))
        self._last_pushed[cfg_id] = max_id

    async def _flush_logs(self, conn: asyncpg.Connection, user_id: Optional[str]) -> None:
        if not _log_queue or self.server_id is None:
            return
        batch = []
        while _log_queue:
            level, message, created = _log_queue.popleft()
            batch.append((self.server_id, user_id, level, message[:4000], created))
        if batch:
            try:
                await conn.executemany(
                    "INSERT INTO bot_logs (server_id, user_id, level, message, created_at) "
                    "VALUES ($1,$2,$3,$4,$5)",
                    batch,
                )
            except Exception:
                pass

    async def cycle(self) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await self._self_row(conn)
            if row is None:
                await self.register()
                return

            self.server_id = int(row["id"])
            if row["lease"] and row["lease_sig"]:
                license_manager.apply_db_lease(row["lease"], row["lease_sig"], self.server_key)
            else:
                license_manager.mark_suspended("not_connected")

            await self._heartbeat(conn)

            user_id = row["user_id"]
            if row["connected"] and user_id:
                await self._reconcile_bots(conn, user_id)

            await self._flush_logs(conn, user_id)

    async def run(self) -> None:
        interval = max(5, int(settings.market_poll_seconds))
        try:
            await self.connect()
            await self.register()
        except Exception as e:
            logger.error("bot_market: не удалось подключиться к базе: %s", e)
        while True:
            try:
                if self.pool is None:
                    await self.connect()
                    await self.register()
                self._cycle += 1
                await self.cycle()
            except Exception as e:
                logger.warning("bot_market: сбой цикла синхронизации: %s", e)
            await asyncio.sleep(interval)


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


market_sync = MarketSync()


async def market_loop() -> None:
    if not market_sync.enabled:
        logger.warning(
            "bot_market: интеграция отключена (нужны APP_URL и BOT_MARKET_DATABASE_URL "
            "или POSTGRESQL_*). Контейнер работает, но не связан с платформой."
        )
        return
    await market_sync.run()
