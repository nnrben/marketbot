
import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from app.database import init_db  # noqa: E402
from app.market import MarketLogHandler, market_loop  # noqa: E402

_market_handler = MarketLogHandler()
_market_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
_market_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_market_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.grid_bot.service import GridBotService

    settings.validate_runtime()
    await init_db()
    await GridBotService.restore_active_bots()

    market_task: asyncio.Task = asyncio.create_task(market_loop())

    yield

    if not market_task.done():
        market_task.cancel()
        try:
            await market_task
        except asyncio.CancelledError:
            pass
    await GridBotService.shutdown_monitoring()


app = FastAPI(
    title="T-Invest Grid Bot",
    description=(
        "Сеточный торговый бот для API Т-Инвестиций. Обмен данными с платформой "
        "deflow идёт только через базу данных bot_market."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {"app": "t-invest-grid-bot", "status": "ok"}


def main() -> None:
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        workers=1,
    )


if __name__ == "__main__":
    main()
