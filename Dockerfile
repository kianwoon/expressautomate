# syntax=docker/dockerfile:1

# The heavy base — Python, uv, the OCR toolchain (tesseract/ghostscript/qpdf/
# libreoffice) and the pinned venv — lives in its own image, built only when
# Dockerfile.base, backend/pyproject.toml or backend/uv.lock change (workflow
# base_tag step). This image just layers application code on top, so a code
# commit never re-runs apt-get or reinstalls deps. BASE_TAG is pinned by the
# build job's build-args; the default keeps a plain local `docker build .`
# working against the latest published base.
# Declared before the first FROM so it is in global scope and usable in the
# base stage's FROM below (an ARG between stages is not).
ARG BASE_TAG=latest

# The Next.js site is built here and copied into the Python image, so one
# service serves both. Frontend edits therefore redeploy the API too — the
# trade for running a single instance instead of two.
FROM node:22-alpine AS site
WORKDIR /site
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Cache Next's own build cache so a frontend tweak rebuilds only what it
# changes instead of the whole static export. The layer cache cannot do this
# on its own: `COPY frontend/ ./` invalidates this layer on every frontend
# edit, while the cache mount survives across builds via the registry/GHA
# cache scopes the workflow passes to build-push-action.
RUN --mount=type=cache,target=/site/.next/cache npm run build

FROM ghcr.io/kianwoon/expressautomate-base:${BASE_TAG} AS base

WORKDIR /app

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
#   arq         python -u -m app.workers.run_arq
#               Drains the queue — both the default queue and the interactive
#               queue (job/candidate intelligence, which has its own worker and
#               slot budget so a background backlog can never starve a click).
#               Without it the other two enqueue work that nothing ever runs:
#               rows sit at `pending`, Redis grows, and every producer reports
#               success.
#
# PORT is injected by Koyeb; 8000 is the local default.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
