
import asyncio
import base64
import json
import logging
import os
import secrets
import socket
import time
from typing import Awaitable, Callable, Optional

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key

from app.config import settings



logger = logging.getLogger(__name__)


EMBEDDED_PUBLIC_KEY_B64 = "MCowBQYDK2VwAyEAFAEHY72KUJHLZq3mrBV9mUPToRmZQre3Ja8BGsi66Xc="

LEASE_VERSION = 1
_CLOCK_SKEW = 60


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class LicenseManager:
    """Хранит текущее состояние лицензии и умеет его обновлять с платформы."""

    def __init__(self) -> None:
        self._status = "unknown"  # active | suspended | unknown
        self._exp = 0.0
        self._tariff: Optional[str] = None
        self._reason: Optional[str] = None
        self._pubkey: Ed25519PublicKey = self._load_pubkey()
        self._lease_path = os.path.join(settings.data_dir, "license_lease.json")
        self._fingerprint = self._load_or_create_fingerprint()
        self._warned_unconfigured = False
        self._load_cached()

    # --- ключ и отпечаток ---

    def _load_pubkey(self) -> Ed25519PublicKey:
        b64 = settings.license_public_key or EMBEDDED_PUBLIC_KEY_B64
        key = load_der_public_key(base64.b64decode(b64))
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("LICENSE public key: ожидался Ed25519 SPKI")
        return key

    def _load_or_create_fingerprint(self) -> str:
        """Стабильный идентификатор этой установки (для детекта шаринга
        лицензии на стороне платформы). Хранится в data_dir, генерируется один
        раз. Секрета не содержит."""
        path = os.path.join(settings.data_dir, "instance_id")
        try:
            os.makedirs(settings.data_dir, exist_ok=True)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    v = f.read().strip()
                    if v:
                        return v
            v = f"{socket.gethostname()}-{secrets.token_hex(4)}"
            with open(path, "w", encoding="utf-8") as f:
                f.write(v)
            return v
        except Exception:
            # Не критично: отпечаток нужен только для аналитики.
            return socket.gethostname() or "unknown"

    # --- дисковый кэш lease (переживает рестарт в пределах срока) ---

    def _load_cached(self) -> None:
        try:
            with open(self._lease_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            payload = self._verify_lease(
                data["lease"], data["sig"], expect_nonce=data.get("nonce")
            )
            if payload is not None and time.time() < payload["exp"] - _CLOCK_SKEW:
                self._apply_payload(payload, data.get("reason"))
                logger.info(
                    "Лицензия: восстановлена из кэша (статус=%s, действует ещё %d мин)",
                    self._status, max(0, int((self._exp - time.time()) / 60)),
                )
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Лицензия: не удалось прочитать кэш lease: %s", e)

    def _save_cached(self, data: dict, nonce: str) -> None:
        try:
            payload = {
                "lease": data["lease"],
                "sig": data["sig"],
                "reason": data.get("reason"),
                "nonce": nonce,
            }
            tmp = self._lease_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self._lease_path)
        except Exception as e:
            logger.warning("Лицензия: не удалось сохранить кэш lease: %s", e)

    # --- проверка подписи ---

    def _verify_lease(
        self, lease_b64: str, sig_b64: str, expect_nonce: Optional[str]
    ) -> Optional[dict]:
        try:
            lease_bytes = _b64url_decode(lease_b64)
            sig = _b64url_decode(sig_b64)
        except Exception:
            logger.error("Лицензия: некорректный формат lease/подписи")
            return None
        try:
            self._pubkey.verify(sig, lease_bytes)
        except InvalidSignature:
            logger.error("Лицензия: НЕВЕРНАЯ подпись lease (ключ не совпадает)")
            return None
        try:
            payload = json.loads(lease_bytes)
        except Exception:
            logger.error("Лицензия: lease не является JSON")
            return None
        if payload.get("v") != LEASE_VERSION:
            logger.error("Лицензия: несовместимая версия lease: %s", payload.get("v"))
            return None
        if settings.license_id and payload.get("license_id") != settings.license_id:
            logger.error("Лицензия: lease выдан для другого license_id")
            return None
        if expect_nonce is not None and payload.get("nonce") != expect_nonce:
            logger.error("Лицензия: nonce lease не совпадает (возможен повтор ответа)")
            return None
        return payload

    def _apply_payload(self, payload: dict, reason: Optional[str]) -> None:
        self._status = payload.get("status", "suspended")
        self._exp = float(payload.get("exp", 0))
        self._tariff = payload.get("tariff")
        self._reason = reason

    # --- состояние ---

    @property
    def enforced(self) -> bool:
        return settings.license_enforce

    @property
    def configured(self) -> bool:
        return bool(
            settings.license_id and settings.license_secret and settings.deflow_api_url
        )

    @property
    def reason(self) -> str:
        return self._reason or self._status

    def is_active(self) -> bool:
        """True — боту разрешено торговать. При выключенном enforce всегда True.
        Иначе нужен неистёкший lease со статусом active."""
        if not self.enforced:
            return True
        return self._status == "active" and time.time() < (self._exp - _CLOCK_SKEW)

    def snapshot(self) -> dict:
        return {
            "enforced": self.enforced,
            "configured": self.configured,
            "status": self._status,
            "active": self.is_active(),
            "tariff": self._tariff,
            "reason": self.reason,
            "expires_at": int(self._exp) if self._exp else None,
            "expires_in_sec": max(0, int(self._exp - time.time())) if self._exp else None,
        }

    # --- сетевое обновление ---

    async def fetch(self, stats: Optional[dict] = None) -> None:
        """Один heartbeat к платформе: отправляет статистику, получает и
        применяет свежий lease. Никогда не бросает исключение."""
        if not self.configured:
            if self.enforced and not self._warned_unconfigured:
                self._warned_unconfigured = True
                logger.warning(
                    "Лицензия НЕ настроена (нужны LICENSE_ID, LICENSE_SECRET, "
                    "DEFLOW_API_URL), а LICENSE_ENFORCE=true — бот будет на паузе. "
                    "Получите лицензию в личном кабинете deflow и задайте переменные "
                    "окружения. Для локального теста без платформы: LICENSE_ENFORCE=false."
                )
            return

        # Ленивый импорт CA-бандла (см. примечание о циклическом импорте выше).
        from app.services.grid_bot.config import CA_BUNDLE_PATH

        nonce = secrets.token_urlsafe(16)
        url = settings.deflow_api_url.rstrip("/") + "/api/bot/telemetry"
        body = {
            "license_id": settings.license_id,
            "nonce": nonce,
            "ts": int(time.time()),
            "fingerprint": self._fingerprint,
            "stats": stats or {},
        }
        try:
            async with httpx.AsyncClient(verify=CA_BUNDLE_PATH, timeout=15.0) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {settings.license_secret}"},
                )
        except Exception as e:
            left = max(0, int((self._exp - time.time()) / 60)) if self._exp else 0
            logger.warning(
                "Лицензия: платформа недоступна (%s). Бот доживает на текущем "
                "lease ещё ~%d мин.", e, left,
            )
            return

        if resp.status_code in (401, 403):
            # Лицензия недействительна/отозвана — сразу закрываемся (fail-closed).
            logger.error(
                "Лицензия: платформа отклонила heartbeat (%s). Проверьте "
                "LICENSE_ID/LICENSE_SECRET. Бот встаёт на паузу.", resp.status_code,
            )
            self._status = "suspended"
            self._exp = 0.0
            self._reason = "rejected"
            return
        if resp.status_code == 429:
            logger.info("Лицензия: heartbeat слишком часто (429) — пропускаю.")
            return
        if resp.status_code != 200:
            logger.warning(
                "Лицензия: неожиданный ответ платформы %s — сохраняю текущий lease.",
                resp.status_code,
            )
            return

        try:
            data = resp.json()
        except Exception:
            logger.warning("Лицензия: ответ платформы не JSON.")
            return

        payload = self._verify_lease(data.get("lease", ""), data.get("sig", ""), nonce)
        if payload is None:
            # Подпись/nonce не сошлись — НЕ применяем, оставляем прежнее состояние.
            return

        prev_status = self._status
        self._apply_payload(payload, data.get("reason"))
        self._save_cached(data, nonce)
        if prev_status != self._status:
            logger.info(
                "Лицензия: статус %s -> %s (тариф=%s, причина=%s, действует %d мин)",
                prev_status, self._status, self._tariff, self.reason,
                max(0, int((self._exp - time.time()) / 60)),
            )


# Единственный экземпляр на процесс.
license_manager = LicenseManager()


async def license_loop(
    stats_provider: Callable[[], Awaitable[dict]],
    on_change: Optional[Callable[[bool], Awaitable[None]]] = None,
) -> None:
    """Фоновый цикл: раз в LICENSE_POLL_SECONDS обновляет лицензию, отдавая
    свежую статистику, и уведомляет об изменении активности (для мгновенной
    паузы/возобновления ботов)."""
    interval = max(60, int(settings.license_poll_seconds))
    while True:
        prev_active = license_manager.is_active()
        try:
            stats = await stats_provider()
        except Exception as e:
            logger.warning("Лицензия: не удалось собрать статистику: %s", e)
            stats = {}
        await license_manager.fetch(stats)
        now_active = license_manager.is_active()
        if on_change is not None and now_active != prev_active:
            try:
                await on_change(now_active)
            except Exception as e:
                logger.error("Лицензия: ошибка обработчика смены статуса: %s", e)
        await asyncio.sleep(interval)
