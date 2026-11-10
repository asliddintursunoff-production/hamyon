FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TELETHON_SESSION_PATH=/data/telethon

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown -R app:app /data /app
USER app

# Local docker-compose mounts a volume at /data for the Telethon session file.
# On Railway set TELETHON_SESSION instead (no disk needed).
EXPOSE 8000

# Exactly one worker: the Telethon session must never be used by two processes.
# PORT is set by Railway; 8000 locally.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
