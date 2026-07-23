"""Публичные эндпоинты для интеграции с платформой deflow.

Пользователь разворачивает этот сервер сам и подключает его к платформе,
указав только URL сервера. Платформа по закону не имеет права хранить или
читать токен Т-Инвестиций пользователя, поэтому эндпоинт статуса отдаёт
ТОЛЬКО булевы флаги:

  * token_configured — задана ли переменная окружения TINKOFF_TOKEN
    (само значение токена никогда и никуда не возвращается);
  * token_valid     — (опционально, ?verify=1) удалось ли выполнить пробный
    запрос GetAccounts к брокеру с этим токеном; в ответ идёт только
    true/false, без каких-либо данных счёта;
  * api_enabled     — включено ли API управления (задан API_AUTH_TOKEN);
  * auth_ok         — подошёл ли переданный платформой ключ X-API-Key
    (null, если ключ не передан).

Эндпоинт намеренно не требует аутентификации: он не раскрывает секретов и
нужен платформе для мгновенной проверки подключения по URL.
"""
import logging
import secrets
from typing import Optional

import httpx
from fastapi import APIRouter, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.services.grid_bot.config import ACCOUNTS_URL, CA_BUNDLE_PATH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/platform", tags=["platform"])

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer_scheme = HTTPBearer(auto_error=False)

APP_NAME = "t-invest-grid-bot"
APP_VERSION = "1.0.0"


async def _verify_broker_token() -> Optional[bool]:
    """Пробный запрос GetAccounts: работает ли токен из переменных окружения.

    Возвращает True/False либо None, если проверить не удалось (сеть/брокер
    недоступны). Ответ брокера никуда не пересылается — наружу уходит только
    булев флаг.
    """
    if not settings.tinkoff_token:
        return False
    try:
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
        logger.warning("Проверка токена: неожиданный ответ брокера %s", resp.status_code)
        return None
    except Exception as e:
        logger.warning("Проверка токена: брокер недоступен: %s", e)
        return None


@router.get("/status")
async def platform_status(
    verify: bool = False,
    api_key: Optional[str] = Security(_api_key_header),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> dict:
    api_enabled = bool(settings.api_auth_token)
    supplied = api_key or (bearer.credentials if bearer else None)

    auth_ok: Optional[bool] = None
    if supplied is not None:
        auth_ok = api_enabled and secrets.compare_digest(
            supplied, settings.api_auth_token
        )

    token_valid: Optional[bool] = None
    if verify:
        token_valid = await _verify_broker_token()

    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "token_configured": bool(settings.tinkoff_token),
        "token_valid": token_valid,
        "api_enabled": api_enabled,
        "auth_ok": auth_ok,
    }
