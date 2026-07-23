"""Защита HTTP API управления ботом.

Приложение на Timeweb Cloud Apps доступно из интернета по публичному URL,
поэтому эндпоинты управления (создание/запуск/остановка/удаление ботов)
обязаны требовать аутентификацию:

  * ключ задаётся переменной окружения API_AUTH_TOKEN;
  * клиент передаёт его в заголовке `X-API-Key: <ключ>` либо
    `Authorization: Bearer <ключ>`;
  * если API_AUTH_TOKEN не задан — API управления полностью отключено
    (бот при этом продолжает работать по конфигурации из переменных
    окружения). Это безопасное поведение по умолчанию.
"""
import secrets
from typing import Optional

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer_scheme = HTTPBearer(auto_error=False)


async def require_api_key(
    api_key: Optional[str] = Security(_api_key_header),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    expected = settings.api_auth_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "API управления отключено. Чтобы включить его, задайте переменную "
                "окружения API_AUTH_TOKEN (длинную случайную строку) и передавайте "
                "её в заголовке X-API-Key."
            ),
        )
    supplied = api_key or (bearer.credentials if bearer else None)
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или отсутствующий ключ API (заголовок X-API-Key).",
        )
