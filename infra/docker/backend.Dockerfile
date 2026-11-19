# syntax=docker/dockerfile:1.7
#
# ai-native-erp backend image.
#
# One image, four roles, selected by the compose `command`:
#   backend  -> uvicorn erp_core.api_main:app --host 0.0.0.0 --port 8000
#   worker   -> python -m erp_core.worker_main
#   migrate  -> alembic upgrade head           (one-shot, exits 0)
#   seed     -> python scripts/seed_hybrid_factory.py -- (one-shot, exits 0)
#
# Build from the repository root:
#   docker build -f infra/docker/backend.Dockerfile -t ai-native-erp/backend:local .

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.8.22

# Pinned uv binary, copied into the Python stage below (the uv image itself is
# distroless, so it is only used as a binary source).
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-bin

# ---------------------------------------------------------------------------
# Builder: resolve the frozen lockfile into /app/.venv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=uv-bin /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Manifests only, so the dependency layer stays cached across source edits.
COPY pyproject.toml uv.lock ./
COPY apps/backend/pyproject.toml apps/backend/pyproject.toml

# `--package erp-core` syncs only the backend workspace member, so the root
# project's `erp-core[dev]` extra (pytest/mypy/ruff) never enters the image.
RUN uv sync --frozen --no-dev --package erp-core --no-install-project

COPY apps/backend/src apps/backend/src

# Second sync installs erp-core itself (editable); dependencies are cached.
RUN uv sync --frozen --no-dev --package erp-core

# ---------------------------------------------------------------------------
# Runtime: non-root, no build tooling, no secrets
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    APP_HOME=/app/apps/backend

RUN groupadd --gid 10001 erp \
    && useradd --uid 10001 --gid erp --create-home --shell /usr/sbin/nologin erp

WORKDIR /app

COPY --from=builder --chown=erp:erp /app/.venv /app/.venv

# Paths are kept identical to the builder so the editable erp_core install in
# .venv resolves to /app/apps/backend/src at runtime.
COPY --chown=erp:erp apps/backend/src apps/backend/src
COPY --chown=erp:erp apps/backend/alembic apps/backend/alembic
COPY --chown=erp:erp apps/backend/alembic.ini apps/backend/alembic.ini
COPY --chown=erp:erp apps/backend/scripts apps/backend/scripts
# Shipped read-only for operator reference / KB review; not read at runtime.
COPY --chown=erp:erp docs/compliance/04-hybrid-factory-kb-seed-pack.md \
    /app/docs/compliance/04-hybrid-factory-kb-seed-pack.md

USER erp
WORKDIR /app/apps/backend

EXPOSE 8000

# python:3.12-slim ships no curl; probe with the interpreter instead.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "erp_core.api_main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
