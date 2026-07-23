"""Точка входа приложения.

Порядок работы при старте контейнера:
  1. Проверка настроек (токен, параметры сетки): при отсутствии/некорректности
     пишется предупреждение в лог, но контейнер ВСЕГДА поднимается — задача
     контейнера просто запуститься (с TINKOFF_TOKEN/API_AUTH_TOKEN или без них),
     торговые параметры приходят удалённо по HTTP API.
  2. Инициализация локальной базы данных (SQLite).
  3. Создание бота по параметрам из переменных окружения (если он ещё
     не создан) и, при AUTO_START=true, его запуск.
  4. HTTP-сервер: /health для проверок хостинга и защищённое API
     управления ботами (см. app/security.py).

ВАЖНО: приложение рассчитано ровно на ОДИН экземпляр (один контейнер).
Запуск нескольких реплик приведёт к дублированию сделок.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI

from app.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Импорт после настройки логирования: модули грид-бота при загрузке
# собирают CA bundle и пишут об этом в лог.
from app import security  # noqa: E402
from app.database import db, init_db  # noqa: E402
from app.license import license_manager, license_loop  # noqa: E402
from app.platform import router as platform_router  # noqa: E402
from app.services.grid_bot.router import router as grid_bot_router  # noqa: E402


async def _ensure_default_bot() -> Optional[int]:
    """Создаёт (или находит уже созданного) бота по параметрам из переменных
    окружения. Возвращает id бота либо None, если параметры не заданы."""
    if not settings.bot_configured:
        return None
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, status FROM grid_bots "
            "WHERE user_id=$1 AND ticker=$2 AND class_code=$3 AND p_low=$4 "
            "AND p_high=$5 AND capital=$6 AND n=$7 AND status != 'archived' "
            "ORDER BY id DESC LIMIT 1",
            "default", settings.ticker, settings.class_code, settings.p_low,
            settings.p_high, settings.capital, settings.grid_levels,
        )
        if row:
            logger.info(
                "Найден существующий бот #%s (%s), статус: %s",
                row["id"], settings.ticker, row["status"],
            )
            return row["id"]
        row = await conn.fetchrow(
            "INSERT INTO grid_bots (user_id, ticker, class_code, p_low, p_high, capital, n, status, type) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, 'created', 'simple') RETURNING id",
            "default", settings.ticker, settings.class_code, settings.p_low,
            settings.p_high, settings.capital, settings.grid_levels,
        )
    logger.info(
        "Создан бот #%s: %s (%s), диапазон [%.2f; %.2f], капитал %.2f руб., уровней: %d",
        row["id"], settings.ticker, settings.class_code, settings.p_low,
        settings.p_high, settings.capital, settings.grid_levels,
    )
    return row["id"]


async def _auto_start_bot(bot_id: int) -> None:
    """Фоновый автозапуск бота с повторами при транзиентных ошибках."""
    from app.services.grid_bot.service import GridBotService

    attempts = 5
    for attempt in range(1, attempts + 1):
        try:
            bot = await GridBotService.get_bot(bot_id)
            if bot is None:
                logger.error("Автозапуск: бот #%s не найден", bot_id)
                return
            if bot["status"] == "active" and bot_id in GridBotService.get_waiting_bots():
                return
            result = await GridBotService.start_bot(bot_id)
            logger.info("Автозапуск бота #%s: %s", bot_id, result.get("message", result))
            return
        except Exception as e:
            logger.error(
                "Автозапуск бота #%s не удался (попытка %d/%d): %s",
                bot_id, attempt, attempts, e,
            )
            if attempt < attempts:
                await asyncio.sleep(15 * attempt)
    logger.critical(
        "Автозапуск бота #%s не удался после %d попыток. Проверьте токен и "
        "параметры в переменных окружения (см. сообщения об ошибках выше). "
        "После исправления перезапустите приложение либо запустите бота через "
        "API: POST /api/grid-bot/start/%s",
        bot_id, attempts, bot_id,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.grid_bot.service import GridBotService

    settings.validate_runtime()
    await init_db()

    # Первичная проверка лицензии ДО автозапуска: сервер бота сам обращается к
    # платформе deflow за подписанным lease. Без активной премиум-подписки бот
    # поднимется, но торговать не будет (встанет на паузу) — см. app/license.py.
    if license_manager.enforced:
        logger.info(
            "Лицензирование включено (LICENSE_ENFORCE=true): бот торгует только "
            "при активной премиум-подписке. Статус запрашивается у платформы "
            "каждые %d сек.", settings.license_poll_seconds,
        )
        try:
            await license_manager.fetch(await GridBotService.collect_license_stats())
        except Exception as e:
            logger.warning("Лицензия: первичная проверка не удалась: %s", e)
    else:
        logger.warning(
            "Лицензирование ВЫКЛючено (LICENSE_ENFORCE=false): проверка премиум-"
            "подписки не выполняется. Используйте только для локального теста."
        )

    bot_id = await _ensure_default_bot()
    await GridBotService.restore_active_bots()

    # Фоновый цикл лицензии: раз в 15 минут отдаёт статистику платформе и
    # обновляет lease. Боты сами читают license_manager.is_active() в своём
    # цикле мониторинга и встают на паузу/возобновляются.
    license_task: asyncio.Task = asyncio.create_task(
        license_loop(GridBotService.collect_license_stats)
    )

    auto_start_task: Optional[asyncio.Task] = None
    if settings.auto_start and bot_id is not None:
        auto_start_task = asyncio.create_task(_auto_start_bot(bot_id))
    elif bot_id is not None:
        logger.info("AUTO_START=false — бот #%s не запущен автоматически", bot_id)

    yield

    if auto_start_task is not None and not auto_start_task.done():
        auto_start_task.cancel()
    if not license_task.done():
        license_task.cancel()
        try:
            await license_task
        except asyncio.CancelledError:
            pass
    # Останавливаем фоновые задачи, НЕ отменяя заявки у брокера: они должны
    # пережить перезапуск контейнера.
    await GridBotService.shutdown_monitoring()


app = FastAPI(
    title="T-Invest Grid Bot",
    description=(
        "Сеточный торговый бот для API Т-Инвестиций. Эндпоинты управления "
        "требуют заголовок X-API-Key (переменная окружения API_AUTH_TOKEN)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(grid_bot_router, dependencies=[Depends(security.require_api_key)])

# Публичный статус для интеграции с платформой deflow: только булевы флаги
# (токен задан/валиден, API включено), никаких секретов — см. app/platform.py.
app.include_router(platform_router)


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    """Проверка живости для хостинга (не требует аутентификации)."""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "app": "t-invest-grid-bot",
        "status": "ok",
        "docs": "/docs",
        "api": "/api/grid-bot (требуется заголовок X-API-Key)",
    }


def main() -> None:
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # Один воркер обязателен: состояние ботов хранится в памяти процесса.
        workers=1,
    )


if __name__ == "__main__":
    main()
