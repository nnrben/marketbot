"""Конфигурация приложения.

Все настройки читаются из переменных окружения (или из файла .env при
локальном запуске). На Timeweb Cloud Apps переменные задаются в настройках
приложения при его создании — токен Т-Инвестиций НИКОГДА не хранится в коде
или репозитории.
"""
import logging
from typing import Dict, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Доступ к брокеру ---
    # Токен Т-Инвестиций с правами на торговые операции.
    tinkoff_token: str = Field(
        "",
        validation_alias=AliasChoices("TINKOFF_TOKEN", "T_INVEST_TOKEN", "TBANK_TOKEN"),
    )
    # Необязательно: конкретный брокерский счёт. Если не задан, берётся
    # первый открытый счёт (обычный или ИИС).
    account_id: str = Field("", validation_alias=AliasChoices("ACCOUNT_ID"))

    # --- Параметры сеточного бота ---
    ticker: str = Field("", validation_alias=AliasChoices("TICKER"))
    class_code: str = Field("TQBR", validation_alias=AliasChoices("CLASS_CODE"))
    figi: str = Field("", validation_alias=AliasChoices("FIGI"))
    p_low: float = Field(0.0, validation_alias=AliasChoices("P_LOW"))
    p_high: float = Field(0.0, validation_alias=AliasChoices("P_HIGH"))
    capital: float = Field(0.0, validation_alias=AliasChoices("CAPITAL"))
    grid_levels: int = Field(0, validation_alias=AliasChoices("GRID_LEVELS", "N_LEVELS"))

    # --- Поведение при запуске ---
    auto_start: bool = Field(True, validation_alias=AliasChoices("AUTO_START"))
    # Разрешить старт, если на счёте уже есть позиция по инструменту:
    # бот примет её как свою начальную позицию (адаптация после рестарта).
    allow_existing_position: bool = Field(
        False, validation_alias=AliasChoices("ALLOW_EXISTING_POSITION")
    )
    # Отменить при старте все активные стоп-заявки по инструменту бота
    # (например, оставшиеся от предыдущего деплоя).
    reset_orders_on_start: bool = Field(
        False, validation_alias=AliasChoices("RESET_ORDERS_ON_START")
    )

    # --- HTTP API ---
    # Ключ доступа к API управления ботом. Если не задан — API отключено
    # (бот работает только по конфигурации из переменных окружения).
    api_auth_token: str = Field("", validation_alias=AliasChoices("API_AUTH_TOKEN"))
    host: str = Field("0.0.0.0", validation_alias=AliasChoices("HOST"))
    port: int = Field(8000, validation_alias=AliasChoices("PORT"))

    # --- Лицензия / интеграция с платформой deflow ---
    # Сервер бота сам обращается к платформе (исходящий запрос) за подписанным
    # lease и отдаёт статистику. Платформа на этот сервер не звонит.
    # Идентификатор и секрет лицензии выдаются в личном кабинете deflow.
    license_id: str = Field("", validation_alias=AliasChoices("LICENSE_ID"))
    license_secret: str = Field("", validation_alias=AliasChoices("LICENSE_SECRET"))
    # Базовый URL платформы deflow (например, https://deflow.ru).
    deflow_api_url: str = Field("", validation_alias=AliasChoices("DEFLOW_API_URL"))
    # Как часто спрашивать статус лицензии и слать статистику (секунды).
    license_poll_seconds: int = Field(
        900, validation_alias=AliasChoices("LICENSE_POLL_SECONDS")
    )
    # Включена ли проверка лицензии. По умолчанию ВКЛючена (fail-closed):
    # без активного lease бот не торгует. Для локального теста без платформы
    # можно выставить LICENSE_ENFORCE=false.
    license_enforce: bool = Field(
        True, validation_alias=AliasChoices("LICENSE_ENFORCE")
    )
    # Необязательное переопределение публичного ключа платформы (base64 DER
    # SPKI Ed25519). Обычно ключ вшит в код (app/license.py); эта переменная
    # нужна лишь для ротации без пересборки.
    license_public_key: str = Field(
        "", validation_alias=AliasChoices("LICENSE_PUBLIC_KEY")
    )

    # --- Служебные ---
    data_dir: str = Field("/app/data", validation_alias=AliasChoices("DATA_DIR"))
    ca_bundle_path: str = Field(
        "/app/certs/ca-bundle.crt", validation_alias=AliasChoices("CA_BUNDLE_PATH")
    )
    log_level: str = Field("INFO", validation_alias=AliasChoices("LOG_LEVEL"))

    @property
    def bot_configured(self) -> bool:
        """Заданы ли параметры сетки через переменные окружения."""
        return bool(self.ticker)

    def validate_runtime(self) -> None:
        """Проверяет настройки на старте и логирует предупреждения.

        ВАЖНО: этот метод НИКОГДА не бросает исключение и не завершает
        приложение. Задача контейнера — просто подняться (HTTP-сервер и
        /health должны отвечать на хостинге, например Timeweb Cloud Apps)
        как с токеном, так и без него. Торговые параметры бота приходят
        удалённо через HTTP API, а не из переменных окружения, поэтому их
        отсутствие/некорректность не должны ронять старт контейнера."""
        if not self.tinkoff_token:
            logger.warning(
                "Не задан TINKOFF_TOKEN. Контейнер запустится, но торговые "
                "операции будут недоступны, пока токен не задан. Добавьте "
                "переменную окружения TINKOFF_TOKEN в настройках приложения "
                "(Timeweb Cloud Apps -> Переменные окружения)."
            )
        if self.bot_configured:
            errors = []
            if self.p_high <= self.p_low or self.p_low <= 0:
                errors.append(
                    "P_LOW/P_HIGH: нижняя граница должна быть > 0 и меньше верхней"
                )
            if self.capital <= 0:
                errors.append("CAPITAL: капитал должен быть положительным числом")
            if self.grid_levels < 1:
                errors.append("GRID_LEVELS: количество уровней должно быть не менее 1")
            if errors:
                logger.warning(
                    "Некорректные параметры бота в переменных окружения (бот "
                    "не будет создан автоматически, задайте параметры через "
                    "API): %s",
                    "; ".join(errors),
                )
        else:
            logger.info(
                "Переменная TICKER не задана — бот не создаётся из окружения. "
                "Параметры бота задаются удалённо через HTTP API "
                "(заголовок X-API-Key, переменная API_AUTH_TOKEN)."
            )


settings = Settings()

# Ручное сопоставление тикер -> FIGI. Обычно не требуется: FIGI определяется
# автоматически через API брокера по тикеру и классу инструмента.
TICKER1_TO_FIGIA: Dict[str, str] = {}
if settings.figi and settings.ticker:
    TICKER1_TO_FIGIA[settings.ticker] = settings.figi
