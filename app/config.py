
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


    tinkoff_token: str = Field(
        "",
        validation_alias=AliasChoices("TINKOFF_TOKEN", "T_INVEST_TOKEN", "TBANK_TOKEN"),
    )

    account_id: str = Field("", validation_alias=AliasChoices("ACCOUNT_ID"))

    ticker: str = Field("", validation_alias=AliasChoices("TICKER"))
    class_code: str = Field("TQBR", validation_alias=AliasChoices("CLASS_CODE"))
    figi: str = Field("", validation_alias=AliasChoices("FIGI"))
    p_low: float = Field(0.0, validation_alias=AliasChoices("P_LOW"))
    p_high: float = Field(0.0, validation_alias=AliasChoices("P_HIGH"))
    capital: float = Field(0.0, validation_alias=AliasChoices("CAPITAL"))
    grid_levels: int = Field(0, validation_alias=AliasChoices("GRID_LEVELS", "N_LEVELS"))

    auto_start: bool = Field(True, validation_alias=AliasChoices("AUTO_START"))

    allow_existing_position: bool = Field(
        False, validation_alias=AliasChoices("ALLOW_EXISTING_POSITION")
    )

    reset_orders_on_start: bool = Field(
        False, validation_alias=AliasChoices("RESET_ORDERS_ON_START")
    )


    api_auth_token: str = Field("", validation_alias=AliasChoices("API_AUTH_TOKEN"))
    host: str = Field("0.0.0.0", validation_alias=AliasChoices("HOST"))
    port: int = Field(8000, validation_alias=AliasChoices("PORT"))


    license_id: str = Field("", validation_alias=AliasChoices("LICENSE_ID"))
    license_secret: str = Field("", validation_alias=AliasChoices("LICENSE_SECRET"))
    # Базовый URL платформы deflow (например, https://deflow.ru).
    deflow_api_url: str = Field("", validation_alias=AliasChoices("DEFLOW_API_URL"))
    license_poll_seconds: int = Field(
        900, validation_alias=AliasChoices("LICENSE_POLL_SECONDS")
    )

    license_enforce: bool = Field(
        True, validation_alias=AliasChoices("LICENSE_ENFORCE")
    )

    license_public_key: str = Field(
        "", validation_alias=AliasChoices("LICENSE_PUBLIC_KEY")
    )

    app_url: str = Field("", validation_alias=AliasChoices("APP_URL", "SERVER_URL", "PUBLIC_URL"))
    bot_market_url: str = Field(
        "", validation_alias=AliasChoices("BOT_MARKET_DATABASE_URL", "DATABASE_URL2")
    )
    pg_host: str = Field("", validation_alias=AliasChoices("POSTGRESQL_HOST"))
    pg_port: int = Field(5432, validation_alias=AliasChoices("POSTGRESQL_PORT"))
    pg_user: str = Field("", validation_alias=AliasChoices("POSTGRESQL_USER"))
    pg_password: str = Field("", validation_alias=AliasChoices("POSTGRESQL_PASSWORD"))
    pg_dbname: str = Field("", validation_alias=AliasChoices("POSTGRESQL_DBNAME"))
    market_poll_seconds: int = Field(
        15, validation_alias=AliasChoices("MARKET_POLL_SECONDS")
    )

    data_dir: str = Field("/app/data", validation_alias=AliasChoices("DATA_DIR"))
    ca_bundle_path: str = Field(
        "/app/certs/ca-bundle.crt", validation_alias=AliasChoices("CA_BUNDLE_PATH")
    )
    log_level: str = Field("INFO", validation_alias=AliasChoices("LOG_LEVEL"))

    @property
    def bot_configured(self) -> bool:
        return bool(self.ticker)

    @property
    def bot_market_dsn(self) -> str:
        if self.bot_market_url:
            return self.bot_market_url
        if self.pg_host and self.pg_user and self.pg_dbname:
            from urllib.parse import quote
            user = quote(self.pg_user, safe="")
            pwd = quote(self.pg_password, safe="")
            return (
                f"postgresql://{user}:{pwd}@{self.pg_host}:{self.pg_port}/{self.pg_dbname}"
            )
        return ""

    def validate_runtime(self) -> None:

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
                "Параметры ботов приходят с платформы через базу данных bot_market."
            )


settings = Settings()


TICKER1_TO_FIGIA: Dict[str, str] = {}
if settings.figi and settings.ticker:
    TICKER1_TO_FIGIA[settings.ticker] = settings.figi
