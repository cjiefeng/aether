# syntax=docker/dockerfile:1.7
# Stages: base (python + uv) -> dev (tooling; repo bind-mounted) / build -> runtime (non-root, prod deps only)

FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

FROM python:3.12-slim AS base
COPY --from=uv /uv /uvx /usr/local/bin/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src

# Tooling image: the repo is bind-mounted at /src; the venv lives in a named volume at /opt/venv
# so Linux wheels never land on the macOS filesystem. `uv run` syncs lazily.
FROM base AS dev
ENV AETHER_DB_PATH=/tmp/aether-dev/aether.db

FROM base AS build
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
RUN useradd --uid 10001 --system --no-create-home --shell /usr/sbin/nologin aether \
    && mkdir -p /data /app \
    && chown aether:aether /data \
    && chmod 0700 /data
COPY --from=build /opt/venv /opt/venv
COPY config /app/config
COPY evals/results /app/evals/results
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AETHER_CONFIG_DIR=/app/config \
    AETHER_EVAL_RESULTS_DIR=/app/evals/results \
    AETHER_DB_PATH=/data/aether.db
WORKDIR /app
USER aether
