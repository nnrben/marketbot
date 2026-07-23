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

RUN mkdir -p /app/certs \
    && curl -o /app/certs/russian_trusted_root_ca.crt http://company.rt.ru/cdp/rootca_ssl_rsa2022.crt \
    && curl -o /app/certs/russian_trusted_sub_ca.crt http://company.rt.ru/cdp/subca_ssl_rsa2022.crt \
    && python -m app.ssl_bundle
# ---------------------------------------------------

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data /app/certs \
    && chmod -R 0777 /app/data /app/certs
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=4)" || exit 1

CMD ["python", "-m", "app.main"]
