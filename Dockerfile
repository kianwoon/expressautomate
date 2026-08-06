# syntax=docker/dockerfile:1

# The Next.js site is built here and copied into the Python image, so one
# service serves both. Frontend edits therefore redeploy the API too — the
# trade for running a single instance instead of two.
FROM node:22-alpine AS site
WORKDIR /site
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

# System OCR toolchain for scanned-PDF fallback (`app/services/cv/ocr.py`). The
# Python `ocrmypdf` wrapper orchestrates these three binaries; without any one
# of them it refuses to start. English ships in the base `tesseract-ocr` pack;
# additional languages are added via `tesseract-ocr-<code>` and surfaced through
# `CV_OCR_LANGUAGES`. Installed in the single shared image so the arq worker —
# the only process that runs OCR — has them; the api/supervisor carry the weight
# too, but never invoke them.
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr ghostscript qpdf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first — application edits do not invalidate it.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY --from=site /site/out ./app/static

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Run unprivileged.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app /opt/venv
USER appuser

EXPOSE 8000

# One image, three processes. Koyeb overrides the command per service; this
# default is the api. Recreating a service by hand means setting these exactly:
#
#   api         uvicorn app.main:app --host 0.0.0.0 --port $PORT
#               Serves the static site and the API, and receives Graph
#               change notifications. This is the only one with a health check.
#
#   supervisor  python -u -m app.workers.main
#               Periodic recovery — rescan_stuck, renew_subscriptions,
#               delta_sync_all, ensure_subscriptions. No port, no health check.
#
#   arq         arq app.workers.settings.WorkerSettings
#               Drains the queue. Without it the other two enqueue work that
#               nothing ever runs: rows sit at `pending`, Redis grows, and
#               every producer reports success.
#
# PORT is injected by Koyeb; 8000 is the local default.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
