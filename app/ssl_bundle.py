import logging
import os
import shutil
import sys

import certifi

logger = logging.getLogger(__name__)

DEFAULT_BUNDLE_PATH = os.environ.get("CA_BUNDLE_PATH", "/app/certs/ca-bundle.crt")

CERTS_DIR = os.environ.get("CERTS_DIR", "/app/certs")
_CERT_EXTENSIONS = (".crt", ".pem", ".cer")


def build_ca_bundle(bundle_path: str = DEFAULT_BUNDLE_PATH, certs_dir: str = CERTS_DIR) -> str:

    bundle_dir = os.path.dirname(bundle_path) or "."
    os.makedirs(bundle_dir, exist_ok=True)

    bundle_name = os.path.basename(bundle_path)
    tmp_path = bundle_path + ".tmp"
    extra_count = 0


    if os.path.isdir(certs_dir):
        seen = os.listdir(certs_dir)
        logger.info("ssl_bundle: содержимое %s: %s", certs_dir, seen or "(пусто)")
    else:
        logger.warning("ssl_bundle: директория %s не существует", certs_dir)

    with open(tmp_path, "wb") as out:
        with open(certifi.where(), "rb") as base:
            shutil.copyfileobj(base, out)
        if os.path.isdir(certs_dir):
            for name in sorted(os.listdir(certs_dir)):

                if name == bundle_name or name == bundle_name + ".tmp":
                    continue
                if not name.lower().endswith(_CERT_EXTENSIONS):
                    continue
                path = os.path.join(certs_dir, name)
                if not os.path.isfile(path):
                    continue
                out.write(b"\n")
                with open(path, "rb") as extra:
                    shutil.copyfileobj(extra, out)
                extra_count += 1
    os.replace(tmp_path, bundle_path)
    logger.info(
        "CA bundle собран: %s (дополнительных сертификатов: %d)",
        bundle_path, extra_count,
    )
    if extra_count == 0:
        logger.warning(
            "Дополнительные сертификаты (Russian Trusted CA) не найдены в %s. "
            "Если TLS-соединение с API Т-Инвестиций не устанавливается, "
            "положите их туда и пересоберите образ.",
            certs_dir,
        )
    return bundle_path


def ensure_ca_bundle(bundle_path: str = DEFAULT_BUNDLE_PATH, certs_dir: str = CERTS_DIR) -> str:

    if os.path.isfile(bundle_path) and os.path.getsize(bundle_path) > 0:
        return bundle_path
    try:
        return build_ca_bundle(bundle_path, certs_dir)
    except OSError as e:
        logger.warning(
            "Не удалось собрать CA bundle (%s): %s — используется набор certifi",
            bundle_path, e,
        )
        return certifi.where()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    build_ca_bundle(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BUNDLE_PATH)
