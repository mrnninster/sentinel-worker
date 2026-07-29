# syntax=docker/dockerfile:1
# Build context = this directory (standalone scrape-worker repo root).
#   docker build -t sentinel-scrape-worker .
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SCRAPER_ROOT=/app \
    SCRAPER_MODE=embedded \
    HOST=0.0.0.0 \
    PORT=10000

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      fonts-liberation \
      libasound2 \
      libatk-bridge2.0-0 \
      libatk1.0-0 \
      libcups2 \
      libdbus-1-3 \
      libdrm2 \
      libgbm1 \
      libgtk-3-0 \
      libnspr4 \
      libnss3 \
      libx11-xcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
      libxshmfence1 \
      wget \
      xdg-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
COPY *.py ./
COPY app ./app
COPY schedule ./schedule
COPY utils ./utils
COPY stream_control ./stream_control
COPY data ./data

RUN pip install --upgrade pip \
 && pip install -r requirements.txt \
 && playwright install chromium

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["sh", "-c", "uvicorn main:app --host ${HOST} --port ${PORT}"]
