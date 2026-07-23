
import base64
import json
import logging
import os
import secrets
import socket
import time
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key

from app.config import settings



logger = logging.getLogger(__name__)


EMBEDDED_PUBLIC_KEY_B64 = "MCowBQYDK2VwAyEAFAEHY72KUJHLZq3mrBV9mUPToRmZQre3Ja8BGsi66Xc="

LEASE_VERSION = 2
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
                data["lease"], data["sig"], expect_sub=data.get("sub")
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

    def _save_cached(self, lease_b64: str, sig_b64: str, sub: str) -> None:
        try:
            payload = {"lease": lease_b64, "sig": sig_b64, "sub": sub}
            tmp = self._lease_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self._lease_path)
        except Exception as e:
            logger.warning("Лицензия: не удалось сохранить кэш lease: %s", e)

    # --- проверка подписи ---

    def _verify_lease(
        self, lease_b64: str, sig_b64: str, expect_sub: Optional[str]
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
        if expect_sub is not None and payload.get("sub") != expect_sub:
            logger.error("Лицензия: lease привязан к другому серверу (sub не совпадает)")
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

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def apply_db_lease(
        self, lease_b64: str, sig_b64: str, server_key: str
    ) -> bool:
        if not lease_b64 or not sig_b64:
            self.mark_suspended("no_lease")
            return False
        payload = self._verify_lease(lease_b64, sig_b64, server_key)
        if payload is None:
            return False
        prev_status = self._status
        self._apply_payload(payload, None)
        self._save_cached(lease_b64, sig_b64, server_key)
        if prev_status != self._status:
            logger.info(
                "Лицензия: статус %s -> %s (тариф=%s, действует %d мин)",
                prev_status, self._status, self._tariff,
                max(0, int((self._exp - time.time()) / 60)),
            )
        return True

    def mark_suspended(self, reason: str) -> None:
        if self._status != "suspended" or self._reason != reason:
            logger.info("Лицензия: перевод в suspended (%s)", reason)
        self._status = "suspended"
        self._exp = 0.0
        self._reason = reason


# Единственный экземпляр на процесс.
license_manager = LicenseManager()
