
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Сертификаты Russian Trusted CA (НУЦ Минцифры) — могут требоваться для TLS
# с invest-public-api.tbank.ru. Скачиваем на этапе сборки (best-effort: если
# источник недоступен, bundle соберётся только из certifi) и собираем единый
# CA bundle, который использует приложение.
RUN mkdir -p /app/certs/extra \
    && (curl -fsS --max-time 30 -o /app/certs/extra/russian_trusted_root_ca.crt \
        https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
        || echo "WARNING: russian_trusted_root_ca not downloaded") \
    && (curl -fsS --max-time 30 -o /app/certs/extra/russian_trusted_sub_ca.crt \
        https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt \
        || echo "WARNING: russian_trusted_sub_ca not downloaded") \
    && python -m app.ssl_bundle

# Запуск от непривилегированного пользователя. Каталоги данных и сертификатов
# делаем записываемыми при любом UID: некоторые хостинги (Timeweb Cloud Apps и
# т.п.) запускают контейнер под произвольным пользователем, не совпадающим с
# appuser, и без этого SQLite и сборка CA bundle падают на Permission denied.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data /app/certs \
    && chmod -R 0777 /app/data /app/certs
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=4)" || exit 1

CMD ["python", "-m", "app.main"]
