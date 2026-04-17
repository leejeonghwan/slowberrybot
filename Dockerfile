FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8787 \
    DAUM_TRENDS_CACHE_TTL=60 \
    DAUM_TRENDS_STALE_IF_ERROR_SECONDS=21600 \
    DAUM_TRENDS_TIMEOUT_SECONDS=10 \
    DAUM_TRENDS_STATE_PATH=/app/runtime/daum_trends_snapshot.json

WORKDIR /app

COPY requirements-web.txt ./requirements-web.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements-web.txt

COPY . .

RUN mkdir -p /app/runtime \
    && useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8787

CMD ["gunicorn", "--bind", "0.0.0.0:8787", "--workers", "2", "--threads", "4", "--worker-class", "gthread", "--timeout", "30", "daum_trends_web.wsgi:app"]

