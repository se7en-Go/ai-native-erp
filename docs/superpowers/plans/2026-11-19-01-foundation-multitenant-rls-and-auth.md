# Foundation, Multi-Tenant RLS & Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the multi-tenant Phase 1–2 foundation kernel: Monorepo workspace, Docker/PgBouncer transaction-pooling infrastructure, fail-closed PostgreSQL 17 `FORCE RLS` schema, KMS envelope encryption (`EnvelopeCryptoService`), Argon2 + JWT 5-role RBAC with agent cost-field masking, immutable partitioned `audit_events` + GDPR workflows, self-serve hybrid onboarding state machine, OpenAPI 3.1 → `@hey-api/openapi-ts` zero-drift pipeline, and the idempotent Quanzhou Smart LED Hybrid Factory seed dataset.

**Architecture:** `Python 3.12 FastAPI` modular monolith (`apps/backend/src/erp_core/`) + `Taskiq` async workers (`Redis Streams`) communicating with `PostgreSQL 17` (`app_migrator` on `5432` for Alembic DDL only; `app_runtime` on `6432` PgBouncer `pool_mode = transaction` with transaction-scoped `set_config('app.current_tenant_id', :tid, true)`), `Redis 7`, and `MinIO / Cloudflare R2` S3 object storage. A thin `Next.js 15` TypeScript shell (`apps/web/`) consumes strictly generated OpenAPI client types (`contracts/openapi.json` → `apps/web/src/lib/api-client/`) with zero business SQL.

**Tech Stack:** `Python >=3.12`, `uv`, `FastAPI >=0.115`, `Pydantic v2`, `pydantic-settings`, `SQLAlchemy 2.0 (asyncpg)`, `Alembic`, `Taskiq + taskiq-redis`, `cryptography` (AES-256-GCM), `argon2-cffi`, `PyJWT`, `boto3`, `import-linter`, `pytest + pytest-asyncio`, `Node >=22`, `pnpm >=9`, `Next.js 15`, `React 19`, `TanStack Query v5`, `@hey-api/openapi-ts`.

## Global Constraints

1. **Runtime Versions & Package Managers**: Python `>=3.12` managed exclusively with `uv` (`apps/backend`); Node.js `>=22` managed exclusively with `pnpm >=9` (`apps/web`, `apps/widget`).
2. **Modular Monolith Boundaries**: All domain code lives under `apps/backend/src/erp_core/` in isolated modules (`shared`, `tenancy`, `channels`, `identity`, `crm`, `catalog`, `orders`, `finance`, `ai_engine`, `billing`). Cross-module database table imports are forbidden and enforced in CI via `import-linter` (`infra/importlinter.ini`).
3. **Zero Business SQL in Next.js**: `apps/web` is strictly a UI & thin Auth/SSE BFF shell. All business logic, SQL access, encryption, and LLM orchestration live in `apps/backend`.
4. **Zero Schema Drift**: FastAPI Pydantic v2 schemas are the single source of truth. Any schema change MUST regenerate `contracts/openapi.json` and `apps/web/src/lib/api-client/` via `@hey-api/openapi-ts`.
5. **Fail-Closed Multi-Tenant Isolation**: Every tenant-owned table MUST enable `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` and `ALTER TABLE ... FORCE ROW LEVEL SECURITY`, using `RESTRICTIVE` policy `tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid`. Runtime connections run ONLY as `app_runtime` (never `SUPERUSER`, never `BYPASSRLS`) behind PgBouncer `pool_mode = transaction`, using transaction-scoped `SELECT set_config('app.current_tenant_id', :tid, true)`.
6. **Envelope Encryption**: All third-party OAuth tokens, IMAP/SMTP secrets, and BYOK API keys MUST be encrypted with per-tenant 256-bit DEKs (wrapped by KMS/Master KEK) using AES-256-GCM bound to `AAD = f"{tenant_id}:{credential_id}:{credential_type}"`.
7. **Zero-Message-Loss Webhooks**: Webhook HTTP handlers verify raw-body HMAC signatures, persist into `raw_webhook_events` (`ON CONFLICT DO NOTHING`), enqueue to Redis Stream, and return `HTTP 200` in `<100ms` (never invoke LLMs or outbound HTTP inside webhook requests).
8. **Channel Compliance Guardrails**: WhatsApp outbound sends pass dual Redis Lua token buckets (`20 mps` Coexistence / `80 mps` Cloud + `1 msg/6s` pair limit, `4^X` exponential backoff on `131056`/`130429`), check `suppression_list` and `consent_records` before out-of-window templates, and enforce `bsuid` fallback. Marketplace outbound replies pass `OutboundComplianceFilter` blocking WhatsApp/email/`wa.me` off-platform leakage.
9. **17TRACK Strict Quota Protection**: `Tracking17TrackAdapter` MUST default to `cacheLevel=0` and reject `cacheLevel=1` in automated flows (preventing 10x quota burn).
10. **Financial Precision**: All monetary, FX rate, margin, and CVP calculations MUST use Python `decimal.Decimal` (`ROUND_HALF_UP`) and PostgreSQL `NUMERIC`, never IEEE-754 `float`.
11. **Zero-Hallucination AI Grounding**: Live prices, MOQs, stock levels, order statuses, and tracking events MUST come 100% from deterministic SQL tools (`ErpToolRegistry`) with `source_tool_call_id` verification—NEVER from static RAG chunks. `Strict Retrieval Mode` requires inline `[doc_id#chunk_id]` verification and forces abstention (`pending_human`) below Rerank score `0.62`.
12. **Strict TDD & Atomic Commits**: Every task follows Red → Green → Commit (`pytest` / `vitest` / `playwright`). Zero `TBD`, `TODO`, stub `pass` bodies, or skipped assertions.

---

### Task 1.1: Monorepo Root & Docker/PgBouncer Infrastructure

**Files:**
- Create: `pnpm-workspace.yaml`
- Create: `pyproject.toml`
- Create: `apps/backend/pyproject.toml`
- Create: `.env.example`
- Create: `infra/docker-compose.yml`
- Create: `infra/pgbouncer/pgbouncer.ini`
- Create: `infra/pgbouncer/userlist.txt`
- Create: `infra/postgres/init-roles.sql`
- Create: `infra/importlinter.ini`
- Create: `.github/workflows/ci.yml`
- Create: `apps/backend/src/erp_core/__init__.py`
- Create: `apps/backend/src/erp_core/shared/__init__.py`
- Create: `apps/backend/src/erp_core/shared/config.py`
- Create: `apps/backend/src/erp_core/shared/enums.py`
- Create: `apps/backend/src/erp_core/shared/errors.py`
- Create: `apps/backend/src/erp_core/shared/pagination.py`
- Create: `apps/backend/src/erp_core/shared/redis_client.py`
- Create: `apps/backend/src/erp_core/shared/storage.py`
- Create: `apps/backend/src/erp_core/tenancy/__init__.py`
- Create: `apps/backend/src/erp_core/channels/__init__.py`
- Create: `apps/backend/src/erp_core/identity/__init__.py`
- Create: `apps/backend/src/erp_core/catalog/__init__.py`
- Create: `apps/backend/src/erp_core/crm/__init__.py`
- Create: `apps/backend/src/erp_core/orders/__init__.py`
- Create: `apps/backend/src/erp_core/finance/__init__.py`
- Create: `apps/backend/src/erp_core/ai_engine/__init__.py`
- Create: `apps/backend/src/erp_core/billing/__init__.py`
- Create: `apps/backend/src/erp_core/api_main.py`
- Create: `apps/backend/src/erp_core/worker_main.py`
- Create: `apps/backend/tests/conftest.py`
- Test: `apps/backend/tests/test_infra_smoke.py`

**Interfaces:**
- Consumes: Environment variables defined in `.env.example`.
- Produces:
  - `erp_core.shared.config.Settings`, `get_settings() -> Settings`
  - `erp_core.shared.enums.*` (all 27 canonical `StrEnum` classes)
  - `erp_core.shared.errors.ProblemDetail`, `AppError`, `register_exception_handlers(app)`
  - `erp_core.shared.pagination.PaginationParams`, `PaginatedResponse[T]`
  - `erp_core.shared.redis_client.create_redis_client(settings)`, `get_redis`
  - `erp_core.shared.storage.ObjectStorageService`
  - `erp_core.api_main.create_app() -> FastAPI`, `app: FastAPI`
  - `erp_core.worker_main.broker`, `scheduler`

- [ ] **Step 1: Create workspace manifests, Docker/PgBouncer/Postgres/CI configs, and the failing smoke test**

Write `pnpm-workspace.yaml`:
```yaml
packages:
  - "apps/web"
  - "apps/widget"
  - "tests/e2e"
```

Write `pyproject.toml`:
```toml
[project]
name = "erp-monorepo"
version = "0.1.0"
description = "Omnichannel Hybrid B2C+B2B AI Workbench & Lightweight ERP"
requires-python = ">=3.12"
dependencies = [
    "erp-core[dev]",
]

[tool.uv.sources]
erp-core = { workspace = true }

[tool.uv.workspace]
members = ["apps/backend"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["apps/backend/tests"]
pythonpath = ["apps/backend/src"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.12"
strict = true
warn_unused_ignores = true
plugins = ["pydantic.mypy"]
```

Write `apps/backend/pyproject.toml`:
```toml
[project]
name = "erp-core"
version = "0.1.0"
description = "FastAPI Modular Monolith & Taskiq Workers for Omnichannel Hybrid ERP"
requires-python = ">=3.12"
dependencies = [
    "alembic>=1.14.0",
    "argon2-cffi>=23.1.0",
    "asyncpg>=0.30.0",
    "boto3>=1.35.0",
    "cryptography>=44.0.0",
    "email-validator>=2.2.0",
    "fastapi>=0.115.6",
    "greenlet>=3.1.1",
    "httpx>=0.28.0",
    "pydantic>=2.10.0",
    "pydantic-settings>=2.6.1",
    "pyjwt>=2.10.0",
    "python-multipart>=0.0.19",
    "redis>=5.2.0",
    "sqlalchemy[asyncio]>=2.0.36",
    "structlog>=24.4.0",
    "taskiq>=0.11.10",
    "taskiq-redis>=1.0.2",
    "uvicorn[standard]>=0.34.0",
]

[project.optional-dependencies]
dev = [
    "import-linter>=2.1",
    "mypy>=1.13.0",
    "pytest>=8.3.4",
    "pytest-asyncio>=0.25.0",
    "ruff>=0.8.4",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/erp_core"]
```

Write `.env.example`:
```dotenv
APP_ENV=development
LOG_LEVEL=INFO

# PostgreSQL 17 (direct migrator on 5432, PgBouncer transaction pool runtime on 6432)
DATABASE_MIGRATOR_URL=postgresql+asyncpg://app_migrator:migrator_dev_pass@localhost:5432/erp_saas
DATABASE_RUNTIME_URL=postgresql+asyncpg://app_runtime:runtime_dev_pass@localhost:6432/erp_saas

# Redis 7+
REDIS_URL=redis://localhost:6379/0

# S3 / Cloudflare R2 / MinIO
S3_ENDPOINT_URL=http://localhost:9000
S3_BUCKET=erp-assets
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_REGION=us-east-1

# Envelope Encryption KMS (replace MASTER_KEK_HEX in production)
KMS_PROVIDER=local_aes
MASTER_KEK_HEX=000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
KMS_KEY_ID=local-dev-kek-v1

# JWT Authentication
JWT_SECRET=dev-only-jwt-secret-change-me-minimum-32-bytes
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=14

# Channels & Compliance Webhooks
WHATSAPP_APP_ID=
WHATSAPP_APP_SECRET=
WHATSAPP_VERIFY_TOKEN=dev-wa-verify-token
EMAIL_PROVIDER=resend
RESEND_API_KEY=
RESEND_WEBHOOK_SECRET=
INBOUND_EMAIL_DOMAIN=inbound.mail.example.com
TRACK17_API_KEY=
TRACK17_WEBHOOK_SECRET=
SHOPIFY_CLIENT_ID=
SHOPIFY_CLIENT_SECRET=
TIKTOK_SHOP_APP_KEY=
TIKTOK_SHOP_APP_SECRET=

# LLM Providers & Billing
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
DEEPSEEK_API_KEY=
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=

# Frontend / BFF
CORS_ORIGINS=http://localhost:3000,http://localhost:3001
NEXT_PUBLIC_BACKEND_SSE_URL=http://localhost:8000
BFF_BACKEND_URL=http://localhost:8000
```

Write `infra/postgres/init-roles.sql`:
```sql
-- Strictly separate migration owner (DDL) and runtime application role (no SUPERUSER, no BYPASSRLS)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
        CREATE ROLE app_migrator NOINHERIT LOGIN PASSWORD 'migrator_dev_pass';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        CREATE ROLE app_runtime NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS LOGIN PASSWORD 'runtime_dev_pass';
    END IF;
END
$$;

-- Pre-install required PostgreSQL 17 extensions as superuser during DB init
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

GRANT ALL PRIVILEGES ON DATABASE erp_saas TO app_migrator;
GRANT USAGE, CREATE ON SCHEMA public TO app_migrator;

GRANT CONNECT ON DATABASE erp_saas TO app_runtime;
GRANT USAGE ON SCHEMA public TO app_runtime;
```

Write `infra/pgbouncer/pgbouncer.ini`:
```ini
[databases]
erp_saas = host=postgres port=5432 dbname=erp_saas

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
max_client_conn = 500
default_pool_size = 25
reserve_pool_size = 5
reserve_pool_timeout = 3
server_reset_query = DISCARD ALL
ignore_startup_parameters = extra_float_digits
admin_users = postgres
```

Write `infra/pgbouncer/userlist.txt`:
```text
"app_runtime" "runtime_dev_pass"
"app_migrator" "migrator_dev_pass"
"postgres" "postgres_dev_pass"
```

Write `infra/docker-compose.yml`:
```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17
    container_name: erp_postgres
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres_dev_pass
      POSTGRES_DB: erp_saas
    ports:
      - "5432:5432"
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./postgres/init-roles.sql:/docker-entrypoint-initdb.d/01-init-roles.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d erp_saas"]
      interval: 3s
      timeout: 3s
      retries: 15

  pgbouncer:
    image: edoburu/pgbouncer:latest
    container_name: erp_pgbouncer
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "6432:6432"
    volumes:
      - ./pgbouncer/pgbouncer.ini:/etc/pgbouncer/pgbouncer.ini:ro
      - ./pgbouncer/userlist.txt:/etc/pgbouncer/userlist.txt:ro
    healthcheck:
      test: ["CMD", "nc", "-z", "127.0.0.1", "6432"]
      interval: 3s
      timeout: 3s
      retries: 15

  redis:
    image: redis:7-alpine
    container_name: erp_redis
    command: ["redis-server", "--appendonly", "yes"]
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 3s
      timeout: 3s
      retries: 10

  minio:
    image: minio/minio:latest
    container_name: erp_minio
    command: ["server", "/data", "--console-address", ":9001"]
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports:
      - "9000:9000"
      - "9001:9001"
    volumes:
      - minio_data:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      timeout: 5s
      retries: 10

volumes:
  pg_data:
  redis_data:
  minio_data:
```

Write `infra/importlinter.ini`:
```ini
[importlinter]
root_package = erp_core
include_external_packages = False

[importlinter:contract:shared_no_domain_imports]
name = Shared module must not depend on any domain module
type = forbidden
source_modules =
    erp_core.shared
forbidden_modules =
    erp_core.tenancy
    erp_core.channels
    erp_core.identity
    erp_core.catalog
    erp_core.crm
    erp_core.orders
    erp_core.finance
    erp_core.ai_engine
    erp_core.billing

[importlinter:contract:tenancy_is_foundational]
name = Tenancy module must not depend on downstream business modules
type = forbidden
source_modules =
    erp_core.tenancy
forbidden_modules =
    erp_core.channels
    erp_core.identity
    erp_core.catalog
    erp_core.crm
    erp_core.orders
    erp_core.finance
    erp_core.ai_engine
    erp_core.billing

[importlinter:contract:billing_isolated]
name = Billing module must not import downstream domain internals
type = forbidden
source_modules =
    erp_core.billing
forbidden_modules =
    erp_core.channels
    erp_core.identity
    erp_core.catalog
    erp_core.crm
    erp_core.orders
    erp_core.finance
    erp_core.ai_engine

[importlinter:contract:identity_isolated]
name = Identity module must not import ERP, channel, billing, or AI modules
type = forbidden
source_modules =
    erp_core.identity
forbidden_modules =
    erp_core.channels
    erp_core.catalog
    erp_core.crm
    erp_core.orders
    erp_core.finance
    erp_core.ai_engine
    erp_core.billing

[importlinter:contract:catalog_isolated]
name = Catalog module must not import downstream order, finance, or AI modules
type = forbidden
source_modules =
    erp_core.catalog
forbidden_modules =
    erp_core.channels
    erp_core.identity
    erp_core.crm
    erp_core.orders
    erp_core.finance
    erp_core.ai_engine
    erp_core.billing

[importlinter:contract:domain_modules_never_import_ai_engine]
name = Core ERP and tenancy domains must never import ai_engine
type = forbidden
source_modules =
    erp_core.tenancy
    erp_core.identity
    erp_core.catalog
    erp_core.crm
    erp_core.orders
    erp_core.finance
    erp_core.billing
forbidden_modules =
    erp_core.ai_engine
```

Write `.github/workflows/ci.yml`:
```yaml
name: CI Gate

on:
  pull_request:
    branches: ["main", "master"]
  push:
    branches: ["main", "master"]

jobs:
  backend-and-contracts:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg17
        env:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres_dev_pass
          POSTGRES_DB: erp_saas
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres -d erp_saas"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
      minio:
        image: minio/minio:latest
        command: server /data
        env:
          MINIO_ROOT_USER: minioadmin
          MINIO_ROOT_PASSWORD: minioadmin
        ports:
          - 9000:9000

    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"
      - uses: pnpm/action-setup@v4
        with:
          version: 9
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: pnpm

      - name: Initialize PostgreSQL roles
        env:
          PGPASSWORD: postgres_dev_pass
        run: |
          psql -h 127.0.0.1 -U postgres -d erp_saas -f infra/postgres/init-roles.sql

      - name: Start PgBouncer container
        run: |
          cp infra/pgbouncer/pgbouncer.ini /tmp/pgbouncer-ci.ini
          sed -i 's/host=postgres/host=127.0.0.1/g' /tmp/pgbouncer-ci.ini
          docker run -d --name erp_pgbouncer --network host \
            -v "/tmp/pgbouncer-ci.ini:/etc/pgbouncer/pgbouncer.ini:ro" \
            -v "$PWD/infra/pgbouncer/userlist.txt:/etc/pgbouncer/userlist.txt:ro" \
            edoburu/pgbouncer:latest
          for i in $(seq 1 15); do nc -z 127.0.0.1 6432 && break; sleep 1; done

      - name: Install Python dependencies
        run: uv sync

      - name: Lint, typecheck, and verify module boundaries
        run: |
          uv run ruff check apps/backend
          uv run mypy apps/backend/src
          uv run lint-imports --config infra/importlinter.ini

      - name: Run Alembic migrations and pytest
        env:
          DATABASE_MIGRATOR_URL: postgresql+asyncpg://app_migrator:migrator_dev_pass@127.0.0.1:5432/erp_saas
          DATABASE_RUNTIME_URL: postgresql+asyncpg://app_runtime:runtime_dev_pass@127.0.0.1:6432/erp_saas
          REDIS_URL: redis://127.0.0.1:6379/0
          S3_ENDPOINT_URL: http://127.0.0.1:9000
          S3_BUCKET: erp-assets
          S3_ACCESS_KEY: minioadmin
          S3_SECRET_KEY: minioadmin
          MASTER_KEK_HEX: 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
          JWT_SECRET: ci-test-jwt-secret-must-be-at-least-32-bytes
        run: |
          uv run --directory apps/backend alembic upgrade head
          uv run pytest -v

      - name: Verify zero OpenAPI & TS client drift
        run: |
          uv run python apps/backend/scripts/export_openapi.py
          pnpm install --frozen-lockfile || pnpm install
          pnpm --filter web openapi-ts
          pnpm --filter web typecheck
          pnpm --filter web lint
          git diff --exit-code contracts/openapi.json apps/web/src/lib/api-client/
```

Write `apps/backend/tests/conftest.py`:
```python
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_MIGRATOR_URL",
    "postgresql+asyncpg://app_migrator:migrator_dev_pass@localhost:5432/erp_saas",
)
os.environ.setdefault(
    "DATABASE_RUNTIME_URL",
    "postgresql+asyncpg://app_runtime:runtime_dev_pass@localhost:6432/erp_saas",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("S3_BUCKET", "erp-assets")
os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
os.environ.setdefault(
    "MASTER_KEK_HEX",
    "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
)
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-key-minimum-32-bytes-long")


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    from erp_core.api_main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
```

Write `apps/backend/tests/test_infra_smoke.py`:
```python
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from httpx import AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "apps" / "backend" / "src" / "erp_core"


async def test_healthz_and_readyz_endpoints(async_client: AsyncClient) -> None:
    health_resp = await async_client.get("/healthz")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"

    ready_resp = await async_client.get("/readyz")
    assert ready_resp.status_code == 200
    body = ready_resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["redis"] == "ok"


def test_import_linter_contracts_pass() -> None:
    lint_imports_bin = Path(sys.executable).parent / "lint-imports"
    result = subprocess.run(
        [str(lint_imports_bin), "--config", str(REPO_ROOT / "infra" / "importlinter.ini")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"import-linter failed:\n{result.stdout}\n{result.stderr}"


def test_no_cross_module_orm_model_imports() -> None:
    domain_dirs = {
        "tenancy",
        "channels",
        "identity",
        "catalog",
        "crm",
        "orders",
        "finance",
        "ai_engine",
        "billing",
    }
    violations: list[str] = []
    for py_file in SRC_ROOT.rglob("*.py"):
        rel_parts = py_file.relative_to(SRC_ROOT).parts
        if not rel_parts or rel_parts[0] not in domain_dirs:
            continue
        owner_module = rel_parts[0]
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for target in domain_dirs - {owner_module}:
                    if node.module == f"erp_core.{target}.models" or node.module.startswith(
                        f"erp_core.{target}.models."
                    ):
                        violations.append(f"{py_file}: imports {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for target in domain_dirs - {owner_module}:
                        if alias.name == f"erp_core.{target}.models" or alias.name.startswith(
                            f"erp_core.{target}.models."
                        ):
                            violations.append(f"{py_file}: imports {alias.name}")
    assert not violations, f"Cross-module ORM model imports forbidden: {violations}"


def test_canonical_enums_registered() -> None:
    from erp_core.shared import enums

    assert enums.UserRole.OWNER == "owner"
    assert enums.UserRole.FINANCE == "finance"
    assert enums.OnboardingStage.WORKSPACE_CREATED == "WorkspaceCreated"
    assert enums.OnboardingStage.LIVE_PRODUCTION == "LiveProduction"
    assert enums.TrackingProvider.TRACK17 == "17track"
    assert enums.AutonomyLevel.L1_PROPOSAL == "L1_proposal"
    assert enums.UsageMetricType.TRACKING_QUOTA == "tracking_quota"
```

- [ ] **Step 2: Run pytest to verify it fails before implementation**

Run:
```bash
uv sync
uv run pytest apps/backend/tests/test_infra_smoke.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core'`.

- [ ] **Step 3: Implement `erp_core/shared/*`, package markers, `api_main.py`, and `worker_main.py`**

Create package `__init__.py` markers:
- `apps/backend/src/erp_core/__init__.py`:
```python
"""Omnichannel Hybrid B2C+B2B AI Workbench & Lightweight ERP core package."""
```
- `apps/backend/src/erp_core/shared/__init__.py`:
```python
"""Shared kernel utilities: config, DB RLS context, crypto, Redis, S3, errors, enums."""
```
- `apps/backend/src/erp_core/tenancy/__init__.py`:
```python
"""Tenancy domain: auth, RBAC, memberships, audit log, GDPR, onboarding."""
```
- `apps/backend/src/erp_core/channels/__init__.py`:
```python
"""Channels domain: webhooks, routing, rate limiting, channel adapters."""
```
- `apps/backend/src/erp_core/identity/__init__.py`:
```python
"""Identity domain: unified customer graph, merge/split, local timezone."""
```
- `apps/backend/src/erp_core/catalog/__init__.py`:
```python
"""Catalog domain: SPUs, variants, B2B tiers, platform SKU mappings."""
```
- `apps/backend/src/erp_core/crm/__init__.py`:
```python
"""CRM domain: B2B companies, leads, opportunities, versioned quotes & PI PDFs."""
```
- `apps/backend/src/erp_core/orders/__init__.py`:
```python
"""Orders domain: dual-mode orders, atomic inventory ledger, 17TRACK shipments."""
```
- `apps/backend/src/erp_core/finance/__init__.py`:
```python
"""Finance domain: receivables/payables milestones, payment slips, CVP snapshots."""
```
- `apps/backend/src/erp_core/ai_engine/__init__.py`:
```python
"""AI Engine domain: hybrid RAG, tool registry, model router, proposals."""
```
- `apps/backend/src/erp_core/billing/__init__.py`:
```python
"""Billing domain: credit wallets, usage ledger, Stripe meter staging."""
```

Write `apps/backend/src/erp_core/shared/config.py`:
```python
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    DATABASE_MIGRATOR_URL: str = (
        "postgresql+asyncpg://app_migrator:migrator_dev_pass@localhost:5432/erp_saas"
    )
    DATABASE_RUNTIME_URL: str = (
        "postgresql+asyncpg://app_runtime:runtime_dev_pass@localhost:6432/erp_saas"
    )

    REDIS_URL: str = "redis://localhost:6379/0"

    S3_ENDPOINT_URL: str = "http://localhost:9000"
    S3_BUCKET: str = "erp-assets"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_REGION: str = "us-east-1"

    KMS_PROVIDER: str = "local_aes"
    MASTER_KEK_HEX: str = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    KMS_KEY_ID: str = "local-dev-kek-v1"

    JWT_SECRET: str = "dev-only-jwt-secret-change-me-minimum-32-bytes"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    CORS_ORIGINS: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:3001"]
    )
    DEK_CACHE_TTL_SECONDS: int = 300

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                parsed = json.loads(stripped)
                return [str(item) for item in parsed]
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
        if isinstance(value, list):
            return [str(item) for item in value]
        return ["http://localhost:3000", "http://localhost:3001"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
```

Write `apps/backend/src/erp_core/shared/enums.py`:
```python
from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    AGENT = "agent"
    FINANCE = "finance"


class OnboardingStage(StrEnum):
    WORKSPACE_CREATED = "WorkspaceCreated"
    CHANNEL_CONNECTING = "ChannelConnecting"
    CHANNEL_VERIFIED = "ChannelVerified"
    CATALOG_SYNCING = "CatalogSyncing"
    AI_SANDBOX_READY = "AISandboxReady"
    LIVE_PRODUCTION = "LiveProduction"


class ChannelType(StrEnum):
    WHATSAPP_CLOUD = "whatsapp_cloud"
    WHATSAPP_COEXISTENCE = "whatsapp_coexistence"
    EMAIL = "email"
    WEB_LIVECHAT = "web_livechat"
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    SHOPLINE = "shopline"
    SHOPLAZZA = "shoplazza"
    TIKTOK_SHOP = "tiktok_shop"
    MADE_IN_CHINA_EMAIL = "made_in_china_email"
    ALIBABA_EMAIL = "alibaba_email"
    MESSENGER = "messenger"
    INSTAGRAM_DM = "instagram_dm"
    TELEGRAM = "telegram"
    AMAZON_SP = "amazon_sp"
    EBAY = "ebay"
    MERCADO_LIBRE = "mercado_libre"
    TEMU = "temu"
    WALMART = "walmart"
    SHOPEE = "shopee"
    LAZADA = "lazada"


class ChannelStatus(StrEnum):
    ACTIVE = "active"
    TOKEN_EXPIRED = "token_expired"
    RATE_LIMITED = "rate_limited"
    DISCONNECTED = "disconnected"


class ConversationMode(StrEnum):
    B2C_SUPPORT = "b2c_support"
    B2B_INQUIRY = "b2b_inquiry"
    MIXED = "mixed"


class ConversationStatus(StrEnum):
    OPEN = "open"
    PENDING_HUMAN = "pending_human"
    SNOOZED = "snoozed"
    RESOLVED = "resolved"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL_NOTE = "internal_note"


class MessageSenderType(StrEnum):
    CUSTOMER = "customer"
    HUMAN_AGENT = "human_agent"
    AI_AGENT = "ai_agent"
    SYSTEM = "system"


class MessageContentType(StrEnum):
    TEXT = "text"
    RICH_HTML = "rich_html"
    TEMPLATE = "template"
    MEDIA = "media"
    QUOTE_CARD = "quote_card"
    ORDER_CARD = "order_card"


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class IdentityType(StrEnum):
    EMAIL = "email"
    PHONE_E164 = "phone_e164"
    WHATSAPP_WAID = "whatsapp_waid"
    WHATSAPP_BSUID = "whatsapp_bsuid"
    IG_PSID = "ig_psid"
    FB_PSID = "fb_psid"
    SHOPIFY_CUSTOMER_ID = "shopify_customer_id"
    TIKTOK_BUYER_ID = "tiktok_buyer_id"
    AMAZON_BUYER_EMAIL = "amazon_buyer_email"
    WEB_VISITOR_ID = "web_visitor_id"


class ConfidenceLevel(StrEnum):
    DETERMINISTIC = "deterministic"
    HUMAN_VERIFIED = "human_verified"
    PROBABILISTIC = "probabilistic"


class CustomerType(StrEnum):
    B2C_CONSUMER = "b2c_consumer"
    B2B_CONTACT = "b2b_contact"
    B2B_COMPANY_REP = "b2b_company_rep"


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    CONVERTED = "converted"


class OpportunityStage(StrEnum):
    DISCOVERY = "discovery"
    SAMPLING = "sampling"
    QUOTING = "quoting"
    NEGOTIATING = "negotiating"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


class Incoterm(StrEnum):
    EXW = "EXW"
    FOB = "FOB"
    CIF = "CIF"
    DDP = "DDP"
    DAP = "DAP"


class QuoteStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    SENT = "sent"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    CONVERTED = "converted"


class OrderMode(StrEnum):
    B2C_RETAIL = "b2c_retail"
    B2B_SAMPLE = "b2b_sample"
    B2B_WHOLESALE = "b2b_wholesale"


class FulfillmentStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED_RESERVED = "confirmed_reserved"
    IN_PRODUCTION = "in_production"
    PARTIALLY_SHIPPED = "partially_shipped"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    EXCEPTION = "exception"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentStatus(StrEnum):
    UNPAID = "unpaid"
    DEPOSIT_PAID = "deposit_paid"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    REFUNDED = "refunded"


class InventoryEventType(StrEnum):
    INBOUND_PO = "inbound_po"
    RESERVE_ORDER = "reserve_order"
    RELEASE_CANCEL = "release_cancel"
    DEDUCT_SHIPPED = "deduct_shipped"
    STOCKTAKE_ADJUST = "stocktake_adjust"


class TrackingProvider(StrEnum):
    TRACK17 = "17track"
    AFTERSHIP = "aftership"
    MANUAL = "manual"


class ProposalType(StrEnum):
    REPLY_DRAFT = "reply_draft"
    CRM_ENTITY_EXTRACT = "crm_entity_extract"
    QUOTE_DRAFT = "quote_draft"
    FOLLOW_UP_SUGGESTION = "follow_up_suggestion"
    IDENTITY_MERGE = "identity_merge"
    REFUND_OR_RESHIP = "refund_or_reship"
    PAYMENT_MATCH = "payment_match"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED_AND_APPROVED = "edited_and_approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


class AutonomyLevel(StrEnum):
    L0_COPILOT = "L0_copilot"
    L1_PROPOSAL = "L1_proposal"
    L2_AUTO = "L2_auto"


class AiCapability(StrEnum):
    CS_SUPPORT = "cs_support"
    SALES_QUOTING = "sales_quoting"
    ORDER_LOGISTICS = "order_logistics"
    CONTENT_STUDIO = "content_studio"
    BIZ_ANALYTICS = "biz_analytics"
    FINANCE_RECON = "finance_recon"


class UsageMetricType(StrEnum):
    AI_CREDIT = "ai_credit"
    WHATSAPP_TEMPLATE = "whatsapp_template"
    WHATSAPP_SERVICE = "whatsapp_service"
    TRACKING_QUOTA = "tracking_quota"
```

Write `apps/backend/src/erp_core/shared/errors.py`:
```python
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str


class AppError(Exception):
    def __init__(
        self,
        *,
        slug: str,
        title: str,
        status_code: int,
        detail: str,
        code: str,
    ) -> None:
        super().__init__(detail)
        self.slug = slug
        self.title = title
        self.status_code = status_code
        self.detail = detail
        self.code = code


def register_exception_handlers(app: FastAPI) -> None:
    async def handle_app_error(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, AppError):
            raise exc
        problem = ProblemDetail(
            type=f"https://erp.local/errors/{exc.slug}",
            title=exc.title,
            status=exc.status_code,
            detail=exc.detail,
            instance=str(request.url.path),
            code=exc.code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=problem.model_dump(),
            media_type="application/problem+json",
        )

    async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, RequestValidationError):
            raise exc
        problem = ProblemDetail(
            type="https://erp.local/errors/validation-error",
            title="Request Validation Failed",
            status=422,
            detail=str(exc.errors()),
            instance=str(request.url.path),
            code="ERR_VALIDATION",
        )
        return JSONResponse(
            status_code=422,
            content=problem.model_dump(),
            media_type="application/problem+json",
        )

    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
```

Write `apps/backend/src/erp_core/shared/pagination.py`:
```python
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginationParams(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
    has_more: bool

    @classmethod
    def build(
        cls,
        *,
        items: list[T],
        total: int,
        limit: int,
        offset: int,
    ) -> PaginatedResponse[T]:
        return cls(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )
```

Write `apps/backend/src/erp_core/shared/redis_client.py`:
```python
from __future__ import annotations

from fastapi import Request
from redis.asyncio import Redis

from erp_core.shared.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    return Redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)


async def get_redis(request: Request) -> Redis:
    client: Redis = request.app.state.redis
    return client
```

Write `apps/backend/src/erp_core/shared/storage.py`:
```python
from __future__ import annotations

import asyncio
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from erp_core.shared.config import Settings


class ObjectStorageService:
    """S3-compatible object storage adapter for MinIO (local) and Cloudflare R2 (prod)."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.S3_BUCKET
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    async def ensure_bucket_exists(self) -> None:
        def _ensure() -> None:
            try:
                self._client.head_bucket(Bucket=self._bucket)
            except ClientError:
                self._client.create_bucket(Bucket=self._bucket)

        await asyncio.to_thread(_ensure)

    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        def _put() -> None:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )

        await asyncio.to_thread(_put)
        return key

    async def download_bytes(self, key: str) -> bytes:
        def _get() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return bytes(response["Body"].read())

        return await asyncio.to_thread(_get)

    async def generate_presigned_get_url(
        self,
        key: str,
        expires_in: int = 604800,
    ) -> str:
        def _sign() -> str:
            return str(
                self._client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._bucket, "Key": key},
                    ExpiresIn=expires_in,
                )
            )

        return await asyncio.to_thread(_sign)

    async def delete_prefix(self, prefix: str) -> int:
        def _delete_all() -> int:
            deleted = 0
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                contents = page.get("Contents", [])
                if not contents:
                    continue
                objects = [{"Key": item["Key"]} for item in contents]
                self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
                deleted += len(objects)
            return deleted

        return await asyncio.to_thread(_delete_all)
```

Write `apps/backend/src/erp_core/api_main.py`:
```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from erp_core.shared.config import get_settings
from erp_core.shared.errors import register_exception_handlers
from erp_core.shared.redis_client import create_redis_client
from erp_core.shared.storage import ObjectStorageService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    redis_client = create_redis_client(settings)
    storage = ObjectStorageService(settings)
    app.state.settings = settings
    app.state.redis = redis_client
    app.state.storage = storage
    try:
        yield
    finally:
        await redis_client.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Omnichannel Hybrid ERP API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz(request: Request) -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            pong = await request.app.state.redis.ping()
            checks["redis"] = "ok" if pong else "error"
        except Exception as exc:
            checks["redis"] = f"error: {type(exc).__name__}"

        is_ready = all(val == "ok" for val in checks.values())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ready" if is_ready else "degraded", "checks": checks},
        )

    return app


app = create_app()
```

Write `apps/backend/src/erp_core/worker_main.py`:
```python
from __future__ import annotations

from taskiq import TaskiqScheduler
from taskiq_redis import ListRedisScheduleSource, RedisStreamBroker

from erp_core.shared.config import get_settings

_settings = get_settings()
broker = RedisStreamBroker(url=_settings.REDIS_URL)
schedule_source = ListRedisScheduleSource(url=_settings.REDIS_URL)
scheduler = TaskiqScheduler(broker=broker, sources=[schedule_source])


@broker.task
async def worker_ping() -> str:
    return "pong"
```

- [ ] **Step 4: Boot Docker infra and verify smoke tests, import-linter, ruff, and mypy pass**

Run:
```bash
docker compose -f infra/docker-compose.yml up -d
uv run pytest apps/backend/tests/test_infra_smoke.py -v
uv run lint-imports --config infra/importlinter.ini
uv run ruff check apps/backend
uv run mypy apps/backend/src
```
Expected: All 4 commands succeed; `pytest` reports `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add pnpm-workspace.yaml pyproject.toml apps/backend/pyproject.toml .env.example infra/ .github/ apps/backend/src/ apps/backend/tests/
git commit -m "chore(infra): bootstrap monorepo, docker/pgbouncer stack, shared kernel, and import-linter gates"
```

---

### Task 1.2: Database Roles, Alembic Async Setup, Initial Tenancy Migration & Fail-Closed RLS Concurrency Suite

**Files:**
- Create: `apps/backend/alembic.ini`
- Create: `apps/backend/alembic/env.py`
- Create: `apps/backend/alembic/script.py.mako`
- Create: `apps/backend/alembic/versions/0001_tenancy_rls_and_audit.py`
- Create: `apps/backend/src/erp_core/shared/db.py`
- Create: `apps/backend/src/erp_core/tenancy/models.py`
- Modify: `apps/backend/src/erp_core/api_main.py`
- Modify: `apps/backend/tests/conftest.py`
- Test: `apps/backend/tests/test_rls_isolation.py`

**Interfaces:**
- Consumes: `erp_core.shared.config.Settings`, `erp_core.shared.enums.UserRole`, `erp_core.shared.enums.OnboardingStage`
- Produces:
  - `erp_core.shared.db.Base`, `create_runtime_engine(settings)`, `create_migrator_engine(settings)`, `create_session_factory(engine)`, `tenant_session(session_factory, tenant_id)`, `system_session(session_factory)`
  - `erp_core.tenancy.models.Tenant`, `User`, `TenantMembership`, `TenantKey`, `ChannelCredential`, `OnboardingState`, `AuditEvent`
  - PostgreSQL schema: `tenants`, `users`, `tenant_memberships`, `tenant_keys`, `channel_credentials`, `onboarding_states`, monthly-partitioned `audit_events` + current & next 3 months partitions + `resolve_user_memberships(UUID)` `SECURITY DEFINER` function.

- [ ] **Step 1: Write the failing fail-closed RLS and 20-coroutine PgBouncer concurrency tests**

Write `apps/backend/tests/test_rls_isolation.py`:
```python
from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.db import system_session, tenant_session


async def test_rls_fails_closed_without_tenant_context(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Even when tenant rows exist in DB, querying without app.current_tenant_id returns 0 rows.
    async with system_session(runtime_session_factory) as session:
        rows = (await session.execute(text("SELECT id, tenant_id FROM tenant_memberships"))).all()
        assert rows == []

    # Writing without app.current_tenant_id MUST fail closed at PostgreSQL RLS layer.
    dummy_tenant_id = uuid4()
    dummy_user_id = uuid4()
    with pytest.raises(DBAPIError) as exc_info:
        async with system_session(runtime_session_factory) as session:
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                    "VALUES (:id, :tid, :uid, 'owner')"
                ),
                {"id": str(uuid4()), "tid": str(dummy_tenant_id), "uid": str(dummy_user_id)},
            )
    assert "row-level security" in str(exc_info.value).lower()


async def test_pgbouncer_20_concurrent_tasks_zero_cross_tenant_leakage(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    user_a = uuid4()
    user_b = uuid4()

    async with tenant_session(runtime_session_factory, tenant_a) as session:
        await session.execute(
            text("INSERT INTO users (id, email, full_name, password_hash) VALUES (:id, :em, :fn, 'h')"),
            {"id": str(user_a), "em": f"rls-a-{user_a}@example.com", "fn": "RLS A"},
        )
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Tenant A')"),
            {"id": str(tenant_a), "slug": f"tenant-a-{tenant_a.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:id, :tid, :uid, 'owner')"
            ),
            {"id": str(uuid4()), "tid": str(tenant_a), "uid": str(user_a)},
        )

    async with tenant_session(runtime_session_factory, tenant_b) as session:
        await session.execute(
            text("INSERT INTO users (id, email, full_name, password_hash) VALUES (:id, :em, :fn, 'h')"),
            {"id": str(user_b), "em": f"rls-b-{user_b}@example.com", "fn": "RLS B"},
        )
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Tenant B')"),
            {"id": str(tenant_b), "slug": f"tenant-b-{tenant_b.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:id, :tid, :uid, 'admin')"
            ),
            {"id": str(uuid4()), "tid": str(tenant_b), "uid": str(user_b)},
        )

    async def worker_task(idx: int) -> None:
        target_tenant: UUID = tenant_a if idx % 2 == 0 else tenant_b
        other_tenant: UUID = tenant_b if idx % 2 == 0 else tenant_a
        expected_role = "owner" if idx % 2 == 0 else "admin"

        for _ in range(5):
            async with tenant_session(runtime_session_factory, target_tenant) as session:
                current_tid = await session.scalar(
                    text("SELECT NULLIF(current_setting('app.current_tenant_id', true), '')")
                )
                assert current_tid == str(target_tenant)
                await asyncio.sleep(0.01)
                rows = (
                    await session.execute(
                        text("SELECT tenant_id, role FROM tenant_memberships")
                    )
                ).all()
                assert len(rows) == 1
                assert str(rows[0].tenant_id) == str(target_tenant)
                assert rows[0].role == expected_role

                # Attempt explicit cross-tenant read with WHERE clause must still return 0 rows
                leaked = (
                    await session.execute(
                        text("SELECT id FROM tenant_memberships WHERE tenant_id = :other"),
                        {"other": str(other_tenant)},
                    )
                ).all()
                assert leaked == []

    await asyncio.gather(*(worker_task(i) for i in range(20)))

    # Confirm PgBouncer connection returns with zero residual session GUC leakage
    async with system_session(runtime_session_factory) as session:
        residual = await session.scalar(
            text("SELECT NULLIF(current_setting('app.current_tenant_id', true), '')")
        )
        assert residual is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest apps/backend/tests/test_rls_isolation.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.shared.db'`.

- [ ] **Step 3: Implement `db.py`, `tenancy/models.py`, Alembic async setup, migration `0001`, and update `api_main.py` & `conftest.py`**

Write `apps/backend/src/erp_core/shared/db.py`:
```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from erp_core.shared.config import Settings


class Base(DeclarativeBase):
    """Shared SQLAlchemy 2.0 Declarative Base across modular monolith domains."""


def create_runtime_engine(settings: Settings) -> AsyncEngine:
    """Runtime engine for app_runtime connecting through PgBouncer transaction pool (6432).

    Disables asyncpg prepared statement caching for full PgBouncer transaction pooling safety.
    """
    return create_async_engine(
        settings.DATABASE_RUNTIME_URL,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        },
    )


def create_migrator_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.DATABASE_MIGRATOR_URL,
        pool_size=2,
        max_overflow=2,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def tenant_session(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
) -> AsyncIterator[AsyncSession]:
    """Transaction-scoped RLS database session using SET LOCAL via set_config(..., true)."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            yield session


@asynccontextmanager
async def system_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Transaction-scoped session with app.current_tenant_id explicitly cleared to fail closed."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', '', true)"),
            )
            yield session
```

Write `apps/backend/src/erp_core/tenancy/models.py`:
```python
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    business_preset: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="hybrid_factory_b2b_b2c"
    )
    base_currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="USD")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="Asia/Shanghai")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    shredded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_user"),
        CheckConstraint(
            "role IN ('owner', 'admin', 'manager', 'agent', 'finance')",
            name="ck_tenant_memberships_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenantKey(Base):
    __tablename__ = "tenant_keys"

    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    kek_key_arn: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    rotated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChannelCredential(Base):
    __tablename__ = "channel_credentials"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_account_id",
            "credential_type",
            name="uq_channel_credentials_tenant_account_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    credential_type: Mapped[str] = mapped_column(Text, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    auth_tag: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OnboardingState(Base):
    __tablename__ = "onboarding_states"
    __table_args__ = (
        CheckConstraint(
            "current_stage IN ('WorkspaceCreated', 'ChannelConnecting', 'ChannelVerified', "
            "'CatalogSyncing', 'AISandboxReady', 'LiveProduction')",
            name="ck_onboarding_states_stage",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    current_stage: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="WorkspaceCreated"
    )
    preset_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="hybrid_factory_b2b_b2c"
    )
    completed_steps: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('human_user', 'ai_agent', 'system_cron', 'webhook')",
            name="ck_audit_events_actor_type",
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), primary_key=True
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    proposal_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Write `apps/backend/alembic.ini`:
```ini
[alembic]
script_location = alembic
prepend_sys_path = src
version_path_separator = os

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =

[logger_alembic]
level = INFO
handlers =

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

Write `apps/backend/alembic/script.py.mako`:
```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

Write `apps/backend/alembic/env.py`:
```python
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from erp_core.shared.config import get_settings
from erp_core.shared.db import Base
from erp_core.tenancy import models as _tenancy_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = get_settings().DATABASE_MIGRATOR_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    settings = get_settings()
    connectable = create_async_engine(
        settings.DATABASE_MIGRATOR_URL,
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

Write `apps/backend/alembic/versions/0001_tenancy_rls_and_audit.py`:
```python
"""Tenancy core tables, fail-closed RLS, and monthly partitioned audit_events.

Revision ID: 0001_tenancy_rls_and_audit
Revises:
Create Date: 2026-11-19
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_tenancy_rls_and_audit"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tenants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            business_preset TEXT NOT NULL DEFAULT 'hybrid_factory_b2b_b2c',
            base_currency TEXT NOT NULL DEFAULT 'USD',
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            is_active BOOLEAN NOT NULL DEFAULT true,
            shredded_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE tenant_memberships (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'manager', 'agent', 'finance')),
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, user_id)
        );
        CREATE INDEX idx_tenant_memberships_tenant_user ON tenant_memberships (tenant_id, user_id);
        CREATE INDEX idx_tenant_memberships_user ON tenant_memberships (user_id);

        CREATE TABLE tenant_keys (
            tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            kek_key_arn TEXT NOT NULL,
            encrypted_dek BYTEA NOT NULL,
            key_version INT NOT NULL DEFAULT 1,
            rotated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE channel_credentials (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            channel_account_id UUID NOT NULL,
            credential_type TEXT NOT NULL,
            ciphertext BYTEA NOT NULL,
            nonce BYTEA NOT NULL,
            auth_tag BYTEA NOT NULL,
            key_version INT NOT NULL,
            expires_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, channel_account_id, credential_type)
        );
        CREATE INDEX idx_channel_credentials_tenant ON channel_credentials (tenant_id, channel_account_id);

        CREATE TABLE onboarding_states (
            tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            current_stage TEXT NOT NULL DEFAULT 'WorkspaceCreated'
                CHECK (current_stage IN (
                    'WorkspaceCreated',
                    'ChannelConnecting',
                    'ChannelVerified',
                    'CatalogSyncing',
                    'AISandboxReady',
                    'LiveProduction'
                )),
            preset_type TEXT NOT NULL DEFAULT 'hybrid_factory_b2b_b2c',
            completed_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE audit_events (
            tenant_id UUID NOT NULL,
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            actor_type TEXT NOT NULL CHECK (actor_type IN ('human_user', 'ai_agent', 'system_cron', 'webhook')),
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id UUID NOT NULL,
            proposal_id UUID,
            before_state JSONB,
            after_state JSONB,
            ip_address INET,
            user_agent TEXT,
            PRIMARY KEY (tenant_id, id, created_at)
        ) PARTITION BY RANGE (created_at);

        CREATE INDEX idx_audit_events_tenant_created ON audit_events (tenant_id, created_at DESC);
        CREATE INDEX idx_audit_events_tenant_action ON audit_events (tenant_id, action, created_at DESC);

        -- Create monthly partitions for current month + next 3 months
        DO $$
        DECLARE
            base_month TIMESTAMPTZ := date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
            m_start TIMESTAMPTZ;
            m_end TIMESTAMPTZ;
            part_name TEXT;
            i INT;
        BEGIN
            FOR i IN 0..3 LOOP
                m_start := base_month + (i || ' month')::interval;
                m_end := base_month + ((i + 1) || ' month')::interval;
                part_name := 'audit_events_' || to_char(m_start AT TIME ZONE 'UTC', 'YYYY_MM');
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF audit_events FOR VALUES FROM (%L) TO (%L)',
                    part_name, m_start, m_end
                );
            END LOOP;
        END
        $$;
        CREATE TABLE audit_events_default PARTITION OF audit_events DEFAULT;
        """
    )

    op.execute(
        """
        ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenants_isolation_permissive ON tenants
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        CREATE POLICY tenants_isolation_restrictive ON tenants
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        CREATE POLICY tenants_migrator_auth_select ON tenants
            AS PERMISSIVE FOR SELECT TO app_migrator
            USING (true);

        ALTER TABLE tenant_memberships ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_memberships FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_memberships_isolation_permissive ON tenant_memberships
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        CREATE POLICY tenant_memberships_isolation_restrictive ON tenant_memberships
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        CREATE POLICY tenant_memberships_migrator_auth_select ON tenant_memberships
            AS PERMISSIVE FOR SELECT TO app_migrator
            USING (true);

        ALTER TABLE tenant_keys ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_keys FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_keys_isolation_permissive ON tenant_keys
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        CREATE POLICY tenant_keys_isolation_restrictive ON tenant_keys
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

        ALTER TABLE channel_credentials ENABLE ROW LEVEL SECURITY;
        ALTER TABLE channel_credentials FORCE ROW LEVEL SECURITY;
        CREATE POLICY channel_credentials_isolation_permissive ON channel_credentials
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        CREATE POLICY channel_credentials_isolation_restrictive ON channel_credentials
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

        ALTER TABLE onboarding_states ENABLE ROW LEVEL SECURITY;
        ALTER TABLE onboarding_states FORCE ROW LEVEL SECURITY;
        CREATE POLICY onboarding_states_isolation_permissive ON onboarding_states
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        CREATE POLICY onboarding_states_isolation_restrictive ON onboarding_states
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

        ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY audit_events_isolation_permissive ON audit_events
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        CREATE POLICY audit_events_isolation_restrictive ON audit_events
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

        -- Narrow SECURITY DEFINER function for pre-tenant auth login membership discovery
        CREATE OR REPLACE FUNCTION resolve_user_memberships(p_user_id UUID)
        RETURNS TABLE (
            tenant_id UUID,
            tenant_slug TEXT,
            tenant_name TEXT,
            role TEXT,
            is_active BOOLEAN
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT m.tenant_id, t.slug, t.name, m.role, (m.is_active AND t.is_active AND t.shredded_at IS NULL)
            FROM tenant_memberships m
            JOIN tenants t ON t.id = m.tenant_id
            WHERE m.user_id = p_user_id
              AND m.is_active = true
              AND t.is_active = true
              AND t.shredded_at IS NULL
            ORDER BY m.created_at ASC;
        $$;

        REVOKE ALL ON FUNCTION resolve_user_memberships(UUID) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION resolve_user_memberships(UUID) TO app_runtime;

        -- Explicit table grants for app_runtime
        GRANT SELECT, INSERT, UPDATE, DELETE ON tenants, users, tenant_memberships, tenant_keys, channel_credentials, onboarding_states TO app_runtime;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_runtime;

        -- Immutable audit_events: ONLY SELECT & INSERT for app_runtime; UPDATE/DELETE/TRUNCATE strictly forbidden
        REVOKE ALL ON TABLE audit_events FROM PUBLIC, app_runtime;
        GRANT SELECT, INSERT ON TABLE audit_events TO app_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS resolve_user_memberships(UUID);
        DROP TABLE IF EXISTS audit_events CASCADE;
        DROP TABLE IF EXISTS onboarding_states CASCADE;
        DROP TABLE IF EXISTS channel_credentials CASCADE;
        DROP TABLE IF EXISTS tenant_keys CASCADE;
        DROP TABLE IF EXISTS tenant_memberships CASCADE;
        DROP TABLE IF EXISTS users CASCADE;
        DROP TABLE IF EXISTS tenants CASCADE;
        """
    )
```

Update `apps/backend/src/erp_core/api_main.py` to wire `runtime_engine`, `session_factory`, and PostgreSQL readiness check in `/readyz`:
```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from erp_core.shared.config import get_settings
from erp_core.shared.db import create_runtime_engine, create_session_factory, system_session
from erp_core.shared.errors import register_exception_handlers
from erp_core.shared.redis_client import create_redis_client
from erp_core.shared.storage import ObjectStorageService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    redis_client = create_redis_client(settings)
    storage = ObjectStorageService(settings)
    runtime_engine = create_runtime_engine(settings)
    session_factory = create_session_factory(runtime_engine)

    app.state.settings = settings
    app.state.redis = redis_client
    app.state.storage = storage
    app.state.runtime_engine = runtime_engine
    app.state.session_factory = session_factory
    try:
        yield
    finally:
        await runtime_engine.dispose()
        await redis_client.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Omnichannel Hybrid ERP API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz(request: Request) -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            pong = await request.app.state.redis.ping()
            checks["redis"] = "ok" if pong else "error"
        except Exception as exc:
            checks["redis"] = f"error: {type(exc).__name__}"

        try:
            async with system_session(request.app.state.session_factory) as session:
                val = await session.scalar(text("SELECT 1"))
                checks["postgres"] = "ok" if val == 1 else "error"
        except Exception as exc:
            checks["postgres"] = f"error: {type(exc).__name__}"

        is_ready = all(val == "ok" for val in checks.values())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ready" if is_ready else "degraded", "checks": checks},
        )

    return app


app = create_app()
```

Update `apps/backend/tests/conftest.py` to expose `runtime_session_factory` and `migrator_session_factory`:
```python
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_MIGRATOR_URL",
    "postgresql+asyncpg://app_migrator:migrator_dev_pass@localhost:5432/erp_saas",
)
os.environ.setdefault(
    "DATABASE_RUNTIME_URL",
    "postgresql+asyncpg://app_runtime:runtime_dev_pass@localhost:6432/erp_saas",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("S3_BUCKET", "erp-assets")
os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
os.environ.setdefault(
    "MASTER_KEK_HEX",
    "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
)
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-key-minimum-32-bytes-long")


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    from erp_core.api_main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.fixture
async def runtime_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from erp_core.shared.config import get_settings
    from erp_core.shared.db import create_runtime_engine, create_session_factory

    engine = create_runtime_engine(get_settings())
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
async def migrator_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from erp_core.shared.config import get_settings
    from erp_core.shared.db import create_migrator_engine, create_session_factory

    engine = create_migrator_engine(get_settings())
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()
```

- [ ] **Step 4: Run Alembic migration and execute RLS + smoke tests**

Run:
```bash
uv run --directory apps/backend alembic upgrade head
uv run pytest apps/backend/tests/test_infra_smoke.py apps/backend/tests/test_rls_isolation.py -v
uv run lint-imports --config infra/importlinter.ini
```
Expected: Alembic applies `0001_tenancy_rls_and_audit`; `test_infra_smoke.py` and `test_rls_isolation.py` PASS (`6 passed`); `import-linter` PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/alembic.ini apps/backend/alembic/ apps/backend/src/erp_core/shared/db.py apps/backend/src/erp_core/tenancy/models.py apps/backend/src/erp_core/api_main.py apps/backend/tests/conftest.py apps/backend/tests/test_rls_isolation.py
git commit -m "feat(db): configure async alembic, fail-closed RLS tenancy schema, and PgBouncer concurrency tests"
```

---
### Task 1.3: KMS Envelope Encryption Service (`EnvelopeCryptoService`)

**Files:**
- Create: `apps/backend/src/erp_core/shared/crypto.py`
- Test: `apps/backend/tests/test_crypto_envelope.py`

**Interfaces:**
- Consumes: `erp_core.shared.config.Settings`, `erp_core.shared.errors.AppError`, `tenant_keys` & `channel_credentials` tables via parameterized SQL (`AsyncSession`).
- Produces:
  - `erp_core.shared.crypto.EnvelopeCryptoService`:
    - `provision_tenant_dek(self, session: AsyncSession, tenant_id: UUID) -> int`
    - `encrypt_secret(self, session: AsyncSession, tenant_id: UUID, account_id: UUID, cred_type: str, plaintext: str) -> tuple[bytes, bytes, bytes, int]`
    - `decrypt_secret(self, session: AsyncSession, tenant_id: UUID, account_id: UUID, cred_type: str, ciphertext: bytes, nonce: bytes, auth_tag: bytes, key_version: int) -> str`
    - `crypto_shred_tenant(self, session: AsyncSession, tenant_id: UUID) -> None`

- [ ] **Step 1: Write the failing envelope encryption, tampered AAD, and crypto-shredding tests**

Write `apps/backend/tests/test_crypto_envelope.py`:
```python
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.errors import AppError


async def _create_tenant(session_factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    tid = uuid4()
    async with tenant_session(session_factory, tid) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, :name)"),
            {"id": str(tid), "slug": f"crypto-{tid.hex[:8]}", "name": "Crypto Test Tenant"},
        )
    return tid, uuid4()


async def test_envelope_encrypt_decrypt_roundtrip_and_aad_binding(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    crypto = EnvelopeCryptoService(get_settings())
    tenant_a, account_a = await _create_tenant(runtime_session_factory)
    tenant_b, _ = await _create_tenant(runtime_session_factory)
    secret_value = "shpat_live_quanzhou_led_99887766"

    async with tenant_session(runtime_session_factory, tenant_a) as session:
        key_ver = await crypto.provision_tenant_dek(session, tenant_a)
        assert key_ver == 1
        # Idempotent re-provisioning returns existing version
        assert await crypto.provision_tenant_dek(session, tenant_a) == 1

        ciphertext, nonce, auth_tag, used_ver = await crypto.encrypt_secret(
            session, tenant_a, account_a, "oauth_token", secret_value
        )
        assert used_ver == 1
        assert len(nonce) == 12
        assert len(auth_tag) == 16
        assert secret_value.encode("utf-8") not in ciphertext

        decrypted = await crypto.decrypt_secret(
            session, tenant_a, account_a, "oauth_token", ciphertext, nonce, auth_tag, used_ver
        )
        assert decrypted == secret_value

        # Wrong credential_type (tampered AAD) MUST fail GCM verification
        with pytest.raises(AppError) as err_type:
            await crypto.decrypt_secret(
                session, tenant_a, account_a, "imap_password", ciphertext, nonce, auth_tag, used_ver
            )
        assert err_type.value.code == "ERR_INVALID_CREDENTIAL_TAG"
        assert isinstance(err_type.value.__cause__, InvalidTag)

        # Wrong account_id (tampered AAD) MUST fail GCM verification
        with pytest.raises(AppError) as err_acct:
            await crypto.decrypt_secret(
                session, tenant_a, uuid4(), "oauth_token", ciphertext, nonce, auth_tag, used_ver
            )
        assert isinstance(err_acct.value.__cause__, InvalidTag)

    # Tenant B attempting to decrypt Tenant A's ciphertext with Tenant B's DEK/AAD MUST fail
    async with tenant_session(runtime_session_factory, tenant_b) as session:
        await crypto.provision_tenant_dek(session, tenant_b)
        with pytest.raises(AppError) as err_cross:
            await crypto.decrypt_secret(
                session, tenant_b, account_a, "oauth_token", ciphertext, nonce, auth_tag, used_ver
            )
        assert err_cross.value.code == "ERR_INVALID_CREDENTIAL_TAG"
        assert isinstance(err_cross.value.__cause__, InvalidTag)


async def test_crypto_shred_tenant_renders_backup_ciphertext_permanently_unreadable(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    crypto = EnvelopeCryptoService(get_settings())
    tenant_id, account_id = await _create_tenant(runtime_session_factory)

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        ciphertext, nonce, auth_tag, key_ver = await crypto.encrypt_secret(
            session, tenant_id, account_id, "byok_api_key", "sk-live-byok-key-12345"
        )
        await session.execute(
            text(
                "INSERT INTO channel_credentials "
                "(id, tenant_id, channel_account_id, credential_type, ciphertext, nonce, auth_tag, key_version) "
                "VALUES (:id, :tid, :aid, 'byok_api_key', :ct, :nonce, :tag, :kv)"
            ),
            {
                "id": str(uuid4()),
                "tid": str(tenant_id),
                "aid": str(account_id),
                "ct": ciphertext,
                "nonce": nonce,
                "tag": auth_tag,
                "kv": key_ver,
            },
        )

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await crypto.crypto_shred_tenant(session, tenant_id)

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        key_count = await session.scalar(text("SELECT count(*) FROM tenant_keys"))
        cred_count = await session.scalar(text("SELECT count(*) FROM channel_credentials"))
        assert key_count == 0
        assert cred_count == 0

        with pytest.raises(AppError) as err_shred:
            await crypto.decrypt_secret(
                session, tenant_id, account_id, "byok_api_key", ciphertext, nonce, auth_tag, key_ver
            )
        assert err_shred.value.code == "ERR_TENANT_KEY_REVOKED"
        assert err_shred.value.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest apps/backend/tests/test_crypto_envelope.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.shared.crypto'`.

- [ ] **Step 3: Implement `apps/backend/src/erp_core/shared/crypto.py`**

Write `apps/backend/src/erp_core/shared/crypto.py`:
```python
from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from typing import Protocol
from uuid import UUID

import boto3
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.config import Settings
from erp_core.shared.errors import AppError


class KekProvider(Protocol):
    async def wrap_dek(self, tenant_id: UUID, dek: bytes) -> bytes: ...
    async def unwrap_dek(self, tenant_id: UUID, wrapped_dek: bytes) -> bytes: ...


class LocalAesKekProvider:
    """Development/self-hosted Master KEK wrapping per-tenant 256-bit DEKs via AES-256-GCM."""

    def __init__(self, master_kek_hex: str) -> None:
        kek_bytes = bytes.fromhex(master_kek_hex)
        if len(kek_bytes) != 32:
            raise ValueError("MASTER_KEK_HEX must decode to exactly 32 bytes (64 hex chars)")
        self._kek_aes = AESGCM(kek_bytes)

    async def wrap_dek(self, tenant_id: UUID, dek: bytes) -> bytes:
        nonce = os.urandom(12)
        aad = f"kek-wrap:{tenant_id}".encode()
        wrapped = self._kek_aes.encrypt(nonce, dek, aad)
        return nonce + wrapped

    async def unwrap_dek(self, tenant_id: UUID, wrapped_dek: bytes) -> bytes:
        if len(wrapped_dek) < 28:
            raise AppError(
                slug="corrupt-wrapped-dek",
                title="Corrupt Wrapped DEK",
                status_code=500,
                detail="Stored tenant encrypted DEK payload is malformed",
                code="ERR_CORRUPT_DEK",
            )
        nonce = wrapped_dek[:12]
        ciphertext_with_tag = wrapped_dek[12:]
        aad = f"kek-wrap:{tenant_id}".encode()
        try:
            return self._kek_aes.decrypt(nonce, ciphertext_with_tag, aad)
        except InvalidTag as exc:
            raise AppError(
                slug="kek-unwrap-failed",
                title="Master KEK Unwrap Failed",
                status_code=403,
                detail="Failed to unwrap tenant DEK under Master KEK",
                code="ERR_KEK_UNWRAP_FAILED",
            ) from exc


class AwsKmsKekProvider:
    """Production AWS KMS KEK wrapper using EncryptionContext bound to tenant_id."""

    def __init__(self, key_id: str, region_name: str) -> None:
        self._key_id = key_id
        self._kms = boto3.client("kms", region_name=region_name)

    async def wrap_dek(self, tenant_id: UUID, dek: bytes) -> bytes:
        def _encrypt() -> bytes:
            resp = self._kms.encrypt(
                KeyId=self._key_id,
                Plaintext=dek,
                EncryptionContext={"tenant_id": str(tenant_id)},
            )
            return bytes(resp["CiphertextBlob"])

        return await asyncio.to_thread(_encrypt)

    async def unwrap_dek(self, tenant_id: UUID, wrapped_dek: bytes) -> bytes:
        def _decrypt() -> bytes:
            resp = self._kms.decrypt(
                CiphertextBlob=wrapped_dek,
                EncryptionContext={"tenant_id": str(tenant_id)},
            )
            return bytes(resp["Plaintext"])

        return await asyncio.to_thread(_decrypt)


class _DekLruCache:
    """Thread/async-safe bounded LRU cache with strict 5-minute maximum TTL for plaintext DEKs."""

    def __init__(self, maxsize: int = 1024, ttl_seconds: int = 300) -> None:
        self._maxsize = maxsize
        self._ttl_seconds = ttl_seconds
        self._store: OrderedDict[tuple[UUID, int], tuple[bytes, float]] = OrderedDict()

    def get(self, tenant_id: UUID, key_version: int) -> bytes | None:
        key = (tenant_id, key_version)
        entry = self._store.get(key)
        if entry is None:
            return None
        dek, expires_at = entry
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return dek

    def put(self, tenant_id: UUID, key_version: int, dek: bytes) -> None:
        key = (tenant_id, key_version)
        expires_at = time.monotonic() + self._ttl_seconds
        self._store[key] = (dek, expires_at)
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    def evict_tenant(self, tenant_id: UUID) -> None:
        matching = [k for k in self._store if k[0] == tenant_id]
        for k in matching:
            self._store.pop(k, None)


class EnvelopeCryptoService:
    """Two-tier KMS Envelope Encryption (Master KEK -> Per-Tenant 256-bit DEK -> AES-256-GCM)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache = _DekLruCache(maxsize=1024, ttl_seconds=settings.DEK_CACHE_TTL_SECONDS)
        if settings.KMS_PROVIDER == "aws_kms":
            self._kek_provider: KekProvider = AwsKmsKekProvider(
                key_id=settings.KMS_KEY_ID,
                region_name=settings.S3_REGION,
            )
        elif settings.KMS_PROVIDER == "local_aes":
            self._kek_provider = LocalAesKekProvider(settings.MASTER_KEK_HEX)
        else:
            raise AppError(
                slug="unsupported-kms-provider",
                title="Unsupported KMS Provider",
                status_code=500,
                detail=f"Unsupported KMS_PROVIDER: {settings.KMS_PROVIDER}",
                code="ERR_UNSUPPORTED_KMS_PROVIDER",
            )

    @staticmethod
    def _build_aad(tenant_id: UUID, account_id: UUID, cred_type: str) -> bytes:
        return f"{tenant_id}:{account_id}:{cred_type}".encode()

    async def provision_tenant_dek(self, session: AsyncSession, tenant_id: UUID) -> int:
        existing_ver = await session.scalar(
            text("SELECT key_version FROM tenant_keys WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        if existing_ver is not None:
            return int(existing_ver)

        plaintext_dek = os.urandom(32)
        wrapped_dek = await self._kek_provider.wrap_dek(tenant_id, plaintext_dek)
        await session.execute(
            text(
                "INSERT INTO tenant_keys (tenant_id, kek_key_arn, encrypted_dek, key_version) "
                "VALUES (:tid, :kek, :edek, 1) "
                "ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {
                "tid": str(tenant_id),
                "kek": self._settings.KMS_KEY_ID,
                "edek": wrapped_dek,
            },
        )
        saved_ver = await session.scalar(
            text("SELECT key_version FROM tenant_keys WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        ver = int(saved_ver or 1)
        if ver == 1:
            self._cache.put(tenant_id, ver, plaintext_dek)
        return ver

    async def _resolve_tenant_dek(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        key_version: int | None = None,
    ) -> tuple[bytes, int]:
        if key_version is not None:
            cached = self._cache.get(tenant_id, key_version)
            if cached is not None:
                return cached, key_version

        if key_version is None:
            row = (
                await session.execute(
                    text(
                        "SELECT encrypted_dek, key_version FROM tenant_keys "
                        "WHERE tenant_id = :tid"
                    ),
                    {"tid": str(tenant_id)},
                )
            ).first()
        else:
            row = (
                await session.execute(
                    text(
                        "SELECT encrypted_dek, key_version FROM tenant_keys "
                        "WHERE tenant_id = :tid AND key_version = :kv"
                    ),
                    {"tid": str(tenant_id), "kv": key_version},
                )
            ).first()

        if row is None:
            raise AppError(
                slug="tenant-key-revoked",
                title="Tenant Encryption Key Missing or Revoked",
                status_code=403,
                detail="No active encryption DEK exists for this tenant",
                code="ERR_TENANT_KEY_REVOKED",
            )

        resolved_ver = int(row.key_version)
        cached_hit = self._cache.get(tenant_id, resolved_ver)
        if cached_hit is not None:
            return cached_hit, resolved_ver

        dek = await self._kek_provider.unwrap_dek(tenant_id, bytes(row.encrypted_dek))
        self._cache.put(tenant_id, resolved_ver, dek)
        return dek, resolved_ver

    async def encrypt_secret(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        account_id: UUID,
        cred_type: str,
        plaintext: str,
    ) -> tuple[bytes, bytes, bytes, int]:
        await self.provision_tenant_dek(session, tenant_id)
        dek, key_version = await self._resolve_tenant_dek(session, tenant_id)
        nonce = os.urandom(12)
        aad = self._build_aad(tenant_id, account_id, cred_type)
        encrypted = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), aad)
        ciphertext = encrypted[:-16]
        auth_tag = encrypted[-16:]
        return ciphertext, nonce, auth_tag, key_version

    async def decrypt_secret(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        account_id: UUID,
        cred_type: str,
        ciphertext: bytes,
        nonce: bytes,
        auth_tag: bytes,
        key_version: int,
    ) -> str:
        dek, _ = await self._resolve_tenant_dek(session, tenant_id, key_version=key_version)
        aad = self._build_aad(tenant_id, account_id, cred_type)
        try:
            plaintext_bytes = AESGCM(dek).decrypt(nonce, ciphertext + auth_tag, aad)
        except InvalidTag as exc:
            raise AppError(
                slug="credential-decryption-failed",
                title="Credential Decryption Failed",
                status_code=403,
                detail="GCM authentication tag or AAD verification failed",
                code="ERR_INVALID_CREDENTIAL_TAG",
            ) from exc
        return plaintext_bytes.decode("utf-8")

    async def crypto_shred_tenant(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._cache.evict_tenant(tenant_id)
        await session.execute(
            text("DELETE FROM channel_credentials WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        await session.execute(
            text("DELETE FROM tenant_keys WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        await session.execute(
            text(
                "UPDATE tenants SET is_active = false, shredded_at = now(), updated_at = now() "
                "WHERE id = :tid"
            ),
            {"tid": str(tenant_id)},
        )
```

- [ ] **Step 4: Run envelope crypto tests and static checks**

Run:
```bash
uv run pytest apps/backend/tests/test_crypto_envelope.py -v
uv run lint-imports --config infra/importlinter.ini
uv run ruff check apps/backend
uv run mypy apps/backend/src
```
Expected: `2 passed`, `import-linter`, `ruff`, and `mypy` all pass cleanly.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/shared/crypto.py apps/backend/tests/test_crypto_envelope.py
git commit -m "feat(crypto): implement KMS envelope encryption with AAD binding, TTL cache, and crypto-shredding"
```

---

### Task 1.4: JWT Auth, Argon2 Password Hashing, 5-Role RBAC & Cost Field Masking

**Files:**
- Create: `apps/backend/src/erp_core/tenancy/security.py`
- Create: `apps/backend/src/erp_core/tenancy/cost_visibility.py`
- Create: `apps/backend/src/erp_core/tenancy/dependencies.py`
- Create: `apps/backend/src/erp_core/tenancy/schemas.py`
- Create: `apps/backend/src/erp_core/tenancy/service.py`
- Create: `apps/backend/src/erp_core/tenancy/router.py`
- Modify: `apps/backend/src/erp_core/api_main.py`
- Test: `apps/backend/tests/test_auth_rbac.py`

**Interfaces:**
- Consumes: `erp_core.shared.config.Settings`, `erp_core.shared.db`, `erp_core.shared.crypto.EnvelopeCryptoService`, `erp_core.shared.enums.UserRole`, `erp_core.shared.pagination.PaginatedResponse`
- Produces:
  - `erp_core.tenancy.dependencies.CurrentPrincipal`, `get_current_principal`, `require_roles(*allowed_roles)`, `get_tenant_db`
  - `erp_core.tenancy.cost_visibility.can_view_cost_and_profit(role)`, `mask_cost_fields(payload, role)`, `apply_cost_visibility(model, role)`
  - `erp_core.tenancy.security.hash_password`, `verify_password`, `create_access_token`, `create_refresh_token`, `decode_token`
  - FastAPI routes: `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`, `GET /api/v1/auth/me`, `GET /api/v1/auth/my-tenants`, `GET /api/v1/tenants/memberships`, `POST /api/v1/tenants/memberships`, `PATCH /api/v1/tenants/memberships/{membership_id}`, `PATCH /api/v1/tenants/settings`, `GET /api/v1/tenants/cost-visibility-sample`

- [ ] **Step 1: Write the failing auth, 5-role RBAC, token rejection, and cost masking tests**

Write `apps/backend/tests/test_auth_rbac.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from httpx import AsyncClient

from erp_core.shared.config import get_settings
from erp_core.shared.enums import UserRole


async def _register_owner(async_client: AsyncClient) -> tuple[str, str, str]:
    suffix = uuid4().hex[:8]
    email = f"owner-{suffix}@factory.example.com"
    resp = await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "StrongPassword2026!",
            "full_name": "Jerry Lin",
            "tenant_name": f"Factory {suffix}",
            "tenant_slug": f"factory-{suffix}",
            "base_currency": "USD",
            "timezone": "Asia/Shanghai",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data["access_token"], data["refresh_token"], data["principal"]["tenant_id"]


async def test_register_login_refresh_and_invalid_jwt_rejected(
    async_client: AsyncClient,
) -> None:
    access_tok, refresh_tok, tenant_id = await _register_owner(async_client)

    me_resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_tok}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["role"] == "owner"
    assert me_resp.json()["tenant_id"] == tenant_id
    assert me_resp.json()["can_view_cost"] is True

    # Refresh token flow returns fresh pair
    ref_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_tok},
    )
    assert ref_resp.status_code == 200
    assert ref_resp.json()["access_token"]

    # Using refresh token as Bearer access token MUST be rejected
    wrong_type_resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {refresh_tok}"},
    )
    assert wrong_type_resp.status_code == 401
    assert wrong_type_resp.json()["code"] == "ERR_INVALID_TOKEN"

    # Expired JWT MUST be rejected with 401
    settings = get_settings()
    expired_token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tid": tenant_id,
            "role": UserRole.OWNER.value,
            "email": "expired@example.com",
            "type": "access",
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(hours=1),
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    exp_resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert exp_resp.status_code == 401
    assert exp_resp.json()["code"] == "ERR_INVALID_TOKEN"

    # Tampered signature MUST be rejected with 401
    bad_resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid.jwt.token"},
    )
    assert bad_resp.status_code == 401


async def test_five_roles_rbac_boundaries_and_agent_cost_masking(
    async_client: AsyncClient,
) -> None:
    owner_tok, _, _ = await _register_owner(async_client)
    role_tokens: dict[str, str] = {"owner": owner_tok}

    for role in ("admin", "manager", "agent", "finance"):
        email = f"{role}-{uuid4().hex[:6]}@factory.example.com"
        password = "RolePassword2026!"
        invite_resp = await async_client.post(
            "/api/v1/tenants/memberships",
            headers={"Authorization": f"Bearer {owner_tok}"},
            json={
                "email": email,
                "full_name": f"User {role}",
                "password": password,
                "role": role,
            },
        )
        assert invite_resp.status_code == 201, invite_resp.text

        login_resp = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert login_resp.status_code == 200, login_resp.text
        role_tokens[role] = login_resp.json()["access_token"]

    # 1. Owner-only settings update
    assert (
        await async_client.patch(
            "/api/v1/tenants/settings",
            headers={"Authorization": f"Bearer {role_tokens['owner']}"},
            json={"base_currency": "EUR"},
        )
    ).status_code == 200

    for denied_role in ("admin", "manager", "agent", "finance"):
        r = await async_client.patch(
            "/api/v1/tenants/settings",
            headers={"Authorization": f"Bearer {role_tokens[denied_role]}"},
            json={"base_currency": "GBP"},
        )
        assert r.status_code == 403, f"Expected 403 for {denied_role}, got {r.status_code}"
        assert r.json()["code"] == "ERR_INSUFFICIENT_ROLE"

    # 2. Membership management allowed for owner & admin; denied for manager, agent, finance
    for allowed in ("owner", "admin"):
        r = await async_client.get(
            "/api/v1/tenants/memberships",
            headers={"Authorization": f"Bearer {role_tokens[allowed]}"},
        )
        assert r.status_code == 200
        assert r.json()["total"] == 5

    for denied in ("manager", "agent", "finance"):
        r = await async_client.get(
            "/api/v1/tenants/memberships",
            headers={"Authorization": f"Bearer {role_tokens[denied]}"},
        )
        assert r.status_code == 403

    # 3. Cost & net profit visibility: visible to owner/admin/manager/finance; masked for agent
    for privileged in ("owner", "admin", "manager", "finance"):
        resp = await async_client.get(
            "/api/v1/tenants/cost-visibility-sample",
            headers={"Authorization": f"Bearer {role_tokens[privileged]}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cost_masked"] is False
        assert body["cost_price_base"] == "18.50"
        assert body["net_profit_base"] == "11.20"

    agent_resp = await async_client.get(
        "/api/v1/tenants/cost-visibility-sample",
        headers={"Authorization": f"Bearer {role_tokens['agent']}"},
    )
    assert agent_resp.status_code == 200
    agent_body = agent_resp.json()
    assert agent_body["cost_masked"] is True
    assert agent_body["cost_price_base"] is None
    assert agent_body["cogs_base"] is None
    assert agent_body["net_profit_base"] is None
    assert agent_body["net_margin_pct"] is None
    assert agent_body["unit_sale_price"] == "34.90"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest apps/backend/tests/test_auth_rbac.py -v
```
Expected: FAIL with 404 or `ModuleNotFoundError` because auth/tenancy routes do not exist yet.

- [ ] **Step 3: Implement `security.py`, `cost_visibility.py`, `dependencies.py`, `schemas.py`, `service.py`, `router.py`, and mount router in `api_main.py`**

Write `apps/backend/src/erp_core/tenancy/security.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import BaseModel

from erp_core.shared.config import Settings
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError

_password_hasher = PasswordHasher()


class TokenPayload(BaseModel):
    sub: UUID
    tid: UUID
    role: UserRole
    email: str
    type: Literal["access", "refresh"]
    jti: str


def hash_password(plain_password: str) -> str:
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def create_access_token(
    *,
    user_id: UUID,
    tenant_id: UUID,
    role: UserRole,
    email: str,
    settings: Settings,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role.value,
        "email": email,
        "type": "access",
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return str(jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM))


def create_refresh_token(
    *,
    user_id: UUID,
    tenant_id: UUID,
    role: UserRole,
    email: str,
    settings: Settings,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role.value,
        "email": email,
        "type": "refresh",
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    }
    return str(jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM))


def decode_token(
    token: str,
    settings: Settings,
    *,
    expected_type: Literal["access", "refresh"] = "access",
) -> TokenPayload:
    try:
        raw = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "tid", "role", "email", "type", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise AppError(
            slug="invalid-token",
            title="Invalid or Expired Token",
            status_code=401,
            detail="Authentication token is invalid, malformed, or expired",
            code="ERR_INVALID_TOKEN",
        ) from exc

    if raw.get("type") != expected_type:
        raise AppError(
            slug="invalid-token-type",
            title="Invalid Token Type",
            status_code=401,
            detail=f"Expected {expected_type} token",
            code="ERR_INVALID_TOKEN",
        )

    try:
        return TokenPayload(
            sub=UUID(str(raw["sub"])),
            tid=UUID(str(raw["tid"])),
            role=UserRole(str(raw["role"])),
            email=str(raw["email"]),
            type=expected_type,
            jti=str(raw.get("jti", "")),
        )
    except (ValueError, KeyError) as exc:
        raise AppError(
            slug="invalid-token-claims",
            title="Invalid Token Claims",
            status_code=401,
            detail="Token claims failed validation",
            code="ERR_INVALID_TOKEN",
        ) from exc
```

Write `apps/backend/src/erp_core/tenancy/cost_visibility.py`:
```python
from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from erp_core.shared.enums import UserRole

SENSITIVE_COST_FIELDS: tuple[str, ...] = (
    "cost_price_base",
    "cogs_base",
    "net_profit_base",
    "net_margin_pct",
)

TModel = TypeVar("TModel", bound=BaseModel)


def can_view_cost_and_profit(role: UserRole) -> bool:
    """Agent role cannot view purchase cost, COGS, or contribution net profit."""
    return role in {
        UserRole.OWNER,
        UserRole.ADMIN,
        UserRole.MANAGER,
        UserRole.FINANCE,
    }


def mask_cost_fields(
    payload: dict[str, Any],
    role: UserRole,
    sensitive_keys: tuple[str, ...] = SENSITIVE_COST_FIELDS,
) -> dict[str, Any]:
    if can_view_cost_and_profit(role):
        return {**payload, "cost_masked": False}
    masked = dict(payload)
    for key in sensitive_keys:
        if key in masked:
            masked[key] = None
    masked["cost_masked"] = True
    return masked


def apply_cost_visibility(model: TModel, role: UserRole) -> TModel:
    if can_view_cost_and_profit(role):
        updates = {"cost_masked": False} if hasattr(model, "cost_masked") else {}
        return model.model_copy(update=updates)
    updates: dict[str, Any] = {}
    for field_name in SENSITIVE_COST_FIELDS:
        if hasattr(model, field_name):
            updates[field_name] = None
    if hasattr(model, "cost_masked"):
        updates["cost_masked"] = True
    return model.model_copy(update=updates)
```

Write `apps/backend/src/erp_core/tenancy/dependencies.py`:
```python
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import Settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.security import decode_token


class CurrentPrincipal(BaseModel):
    user_id: UUID
    tenant_id: UUID
    role: UserRole
    email: str


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AppError(
            slug="missing-authorization",
            title="Authorization Header Missing",
            status_code=401,
            detail="Bearer token is required",
            code="ERR_MISSING_TOKEN",
        )
    parts = authorization.split(" ", maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AppError(
            slug="invalid-authorization-header",
            title="Invalid Authorization Header",
            status_code=401,
            detail="Authorization header must use Bearer scheme",
            code="ERR_INVALID_TOKEN",
        )
    return parts[1].strip()


async def get_current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentPrincipal:
    settings: Settings = request.app.state.settings
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    token = _extract_bearer_token(authorization)
    payload = decode_token(token, settings, expected_type="access")

    async with tenant_session(session_factory, payload.tid) as session:
        row = (
            await session.execute(
                text(
                    "SELECT m.role, u.email "
                    "FROM tenant_memberships m "
                    "JOIN tenants t ON t.id = m.tenant_id "
                    "JOIN users u ON u.id = m.user_id "
                    "WHERE m.tenant_id = :tid "
                    "  AND m.user_id = :uid "
                    "  AND m.is_active = true "
                    "  AND u.is_active = true "
                    "  AND t.is_active = true "
                    "  AND t.shredded_at IS NULL"
                ),
                {"tid": str(payload.tid), "uid": str(payload.sub)},
            )
        ).first()

    if row is None:
        raise AppError(
            slug="membership-inactive",
            title="Inactive Tenant Membership",
            status_code=401,
            detail="User membership or tenant workspace is inactive or revoked",
            code="ERR_INVALID_TOKEN",
        )

    return CurrentPrincipal(
        user_id=payload.sub,
        tenant_id=payload.tid,
        role=UserRole(str(row.role)),
        email=str(row.email),
    )


def require_roles(*allowed_roles: UserRole) -> Callable[..., Awaitable[CurrentPrincipal]]:
    allowed = frozenset(allowed_roles)

    async def _role_guard(
        principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
    ) -> CurrentPrincipal:
        if allowed and principal.role not in allowed:
            raise AppError(
                slug="insufficient-role",
                title="Insufficient Role Permissions",
                status_code=403,
                detail=f"Role '{principal.role.value}' is not authorized for this operation",
                code="ERR_INSUFFICIENT_ROLE",
            )
        return principal

    return _role_guard


async def get_tenant_db(
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
) -> AsyncIterator[AsyncSession]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with tenant_session(session_factory, principal.tenant_id) as session:
        yield session
```

Write `apps/backend/src/erp_core/tenancy/schemas.py`:
```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from erp_core.shared.enums import UserRole


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=120)
    tenant_name: str = Field(min_length=1, max_length=120)
    tenant_slug: str | None = Field(default=None, min_length=2, max_length=80)
    base_currency: str = Field(default="USD", min_length=3, max_length=3)
    timezone: str = Field(default="Asia/Shanghai", min_length=2, max_length=64)
    preset_type: str = Field(default="hybrid_factory_b2b_b2c", min_length=2, max_length=64)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    tenant_slug: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class PrincipalResponse(BaseModel):
    user_id: UUID
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    role: UserRole
    email: EmailStr
    full_name: str
    can_view_cost: bool


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    principal: PrincipalResponse


class TenantMembershipOption(BaseModel):
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    role: UserRole
    is_active: bool


class MembershipItem(BaseModel):
    membership_id: UUID
    tenant_id: UUID
    user_id: UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime


class InviteMemberRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    role: UserRole


class UpdateMemberRoleRequest(BaseModel):
    role: UserRole
    is_active: bool | None = None


class UpdateTenantSettingsRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_currency: str | None = Field(default=None, min_length=3, max_length=3)
    timezone: str | None = Field(default=None, min_length=2, max_length=64)


class TenantSettingsResponse(BaseModel):
    tenant_id: UUID
    slug: str
    name: str
    business_preset: str
    base_currency: str
    timezone: str


class CostVisibilitySampleResponse(BaseModel):
    sku: str
    currency: str
    unit_sale_price: Decimal
    cost_price_base: Decimal | None = None
    cogs_base: Decimal | None = None
    net_profit_base: Decimal | None = None
    net_margin_pct: Decimal | None = None
    cost_masked: bool = False
```

Write `apps/backend/src/erp_core/tenancy/service.py`:
```python
from __future__ import annotations

import re
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import Settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import OnboardingStage, UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.cost_visibility import can_view_cost_and_profit
from erp_core.tenancy.dependencies import CurrentPrincipal
from erp_core.tenancy.schemas import (
    InviteMemberRequest,
    LoginRequest,
    MembershipItem,
    PrincipalResponse,
    RegisterRequest,
    TenantMembershipOption,
    TenantSettingsResponse,
    TokenResponse,
    UpdateMemberRoleRequest,
    UpdateTenantSettingsRequest,
)
from erp_core.tenancy.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or f"tenant-{uuid4().hex[:8]}"


class TenancyAuthService:
    def __init__(self, settings: Settings, crypto_service: EnvelopeCryptoService) -> None:
        self._settings = settings
        self._crypto = crypto_service

    def _build_token_response(self, principal: PrincipalResponse) -> TokenResponse:
        access = create_access_token(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            role=principal.role,
            email=principal.email,
            settings=self._settings,
        )
        refresh = create_refresh_token(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            role=principal.role,
            email=principal.email,
            settings=self._settings,
        )
        return TokenResponse(
            access_token=access,
            refresh_token=refresh,
            expires_in=self._settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            principal=principal,
        )

    async def register_workspace(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        req: RegisterRequest,
    ) -> TokenResponse:
        email = str(req.email).lower().strip()
        tenant_id = uuid4()
        user_id = uuid4()
        slug = _slugify(req.tenant_slug) if req.tenant_slug else f"{_slugify(req.tenant_name)}-{tenant_id.hex[:6]}"
        password_hash = hash_password(req.password)

        try:
            async with tenant_session(session_factory, tenant_id) as session:
                await session.execute(
                    text(
                        "INSERT INTO tenants (id, slug, name, business_preset, base_currency, timezone) "
                        "VALUES (:id, :slug, :name, :preset, :currency, :tz)"
                    ),
                    {
                        "id": str(tenant_id),
                        "slug": slug,
                        "name": req.tenant_name.strip(),
                        "preset": req.preset_type,
                        "currency": req.base_currency.upper(),
                        "tz": req.timezone,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO users (id, email, full_name, password_hash) "
                        "VALUES (:id, :email, :full_name, :ph)"
                    ),
                    {
                        "id": str(user_id),
                        "email": email,
                        "full_name": req.full_name.strip(),
                        "ph": password_hash,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                        "VALUES (:id, :tid, :uid, :role)"
                    ),
                    {
                        "id": str(uuid4()),
                        "tid": str(tenant_id),
                        "uid": str(user_id),
                        "role": UserRole.OWNER.value,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO onboarding_states (tenant_id, current_stage, preset_type) "
                        "VALUES (:tid, :stage, :preset)"
                    ),
                    {
                        "tid": str(tenant_id),
                        "stage": OnboardingStage.WORKSPACE_CREATED.value,
                        "preset": req.preset_type,
                    },
                )
                await self._crypto.provision_tenant_dek(session, tenant_id)
        except IntegrityError as exc:
            err_msg = str(exc).lower()
            if "users_email_key" in err_msg or "email" in err_msg:
                raise AppError(
                    slug="email-already-registered",
                    title="Email Already Registered",
                    status_code=409,
                    detail="An account with this email already exists",
                    code="ERR_EMAIL_EXISTS",
                ) from exc
            raise AppError(
                slug="tenant-slug-taken",
                title="Workspace Slug Taken",
                status_code=409,
                detail="Workspace slug is already in use",
                code="ERR_TENANT_SLUG_EXISTS",
            ) from exc

        principal = PrincipalResponse(
            user_id=user_id,
            tenant_id=tenant_id,
            tenant_slug=slug,
            tenant_name=req.tenant_name.strip(),
            role=UserRole.OWNER,
            email=email,
            full_name=req.full_name.strip(),
            can_view_cost=True,
        )
        return self._build_token_response(principal)

    async def login(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        req: LoginRequest,
    ) -> TokenResponse:
        email = str(req.email).lower().strip()
        async with system_session(session_factory) as session:
            user_row = (
                await session.execute(
                    text(
                        "SELECT id, email, full_name, password_hash, is_active "
                        "FROM users WHERE email = :email"
                    ),
                    {"email": email},
                )
            ).first()
            if user_row is None or not user_row.is_active:
                raise AppError(
                    slug="invalid-credentials",
                    title="Invalid Credentials",
                    status_code=401,
                    detail="Invalid email or password",
                    code="ERR_INVALID_CREDENTIALS",
                )
            if not verify_password(req.password, str(user_row.password_hash)):
                raise AppError(
                    slug="invalid-credentials",
                    title="Invalid Credentials",
                    status_code=401,
                    detail="Invalid email or password",
                    code="ERR_INVALID_CREDENTIALS",
                )

            memberships = (
                await session.execute(
                    text("SELECT * FROM resolve_user_memberships(:uid)"),
                    {"uid": str(user_row.id)},
                )
            ).all()

        if not memberships:
            raise AppError(
                slug="no-active-workspace",
                title="No Active Workspace",
                status_code=403,
                detail="No active tenant workspace membership found for user",
                code="ERR_NO_ACTIVE_TENANT",
            )

        selected = memberships[0]
        if req.tenant_slug:
            matched = [m for m in memberships if m.tenant_slug == req.tenant_slug]
            if not matched:
                raise AppError(
                    slug="workspace-access-denied",
                    title="Workspace Access Denied",
                    status_code=403,
                    detail=f"User does not belong to workspace '{req.tenant_slug}'",
                    code="ERR_TENANT_ACCESS_DENIED",
                )
            selected = matched[0]

        role = UserRole(str(selected.role))
        principal = PrincipalResponse(
            user_id=UUID(str(user_row.id)),
            tenant_id=UUID(str(selected.tenant_id)),
            tenant_slug=str(selected.tenant_slug),
            tenant_name=str(selected.tenant_name),
            role=role,
            email=str(user_row.email),
            full_name=str(user_row.full_name),
            can_view_cost=can_view_cost_and_profit(role),
        )
        return self._build_token_response(principal)

    async def refresh_tokens(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        refresh_token: str,
    ) -> TokenResponse:
        payload = decode_token(refresh_token, self._settings, expected_type="refresh")
        async with tenant_session(session_factory, payload.tid) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT u.id AS user_id, u.email, u.full_name, t.id AS tenant_id, "
                        "       t.slug AS tenant_slug, t.name AS tenant_name, m.role "
                        "FROM tenant_memberships m "
                        "JOIN users u ON u.id = m.user_id "
                        "JOIN tenants t ON t.id = m.tenant_id "
                        "WHERE m.tenant_id = :tid AND m.user_id = :uid "
                        "  AND m.is_active = true AND u.is_active = true "
                        "  AND t.is_active = true AND t.shredded_at IS NULL"
                    ),
                    {"tid": str(payload.tid), "uid": str(payload.sub)},
                )
            ).first()
        if row is None:
            raise AppError(
                slug="refresh-denied",
                title="Refresh Token Revoked",
                status_code=401,
                detail="User membership or tenant is inactive",
                code="ERR_INVALID_TOKEN",
            )
        role = UserRole(str(row.role))
        principal = PrincipalResponse(
            user_id=UUID(str(row.user_id)),
            tenant_id=UUID(str(row.tenant_id)),
            tenant_slug=str(row.tenant_slug),
            tenant_name=str(row.tenant_name),
            role=role,
            email=str(row.email),
            full_name=str(row.full_name),
            can_view_cost=can_view_cost_and_profit(role),
        )
        return self._build_token_response(principal)

    async def get_me(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
    ) -> PrincipalResponse:
        row = (
            await session.execute(
                text(
                    "SELECT u.full_name, t.slug AS tenant_slug, t.name AS tenant_name "
                    "FROM tenants t, users u "
                    "WHERE t.id = :tid AND u.id = :uid"
                ),
                {"tid": str(principal.tenant_id), "uid": str(principal.user_id)},
            )
        ).one()
        return PrincipalResponse(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            tenant_slug=str(row.tenant_slug),
            tenant_name=str(row.tenant_name),
            role=principal.role,
            email=principal.email,
            full_name=str(row.full_name),
            can_view_cost=can_view_cost_and_profit(principal.role),
        )

    async def list_my_tenants(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        user_id: UUID,
    ) -> list[TenantMembershipOption]:
        async with system_session(session_factory) as session:
            rows = (
                await session.execute(
                    text("SELECT * FROM resolve_user_memberships(:uid)"),
                    {"uid": str(user_id)},
                )
            ).all()
        return [
            TenantMembershipOption(
                tenant_id=UUID(str(r.tenant_id)),
                tenant_slug=str(r.tenant_slug),
                tenant_name=str(r.tenant_name),
                role=UserRole(str(r.role)),
                is_active=bool(r.is_active),
            )
            for r in rows
        ]

    async def list_tenant_memberships(
        self,
        session: AsyncSession,
        limit: int,
        offset: int,
    ) -> PaginatedResponse[MembershipItem]:
        total = int(
            await session.scalar(text("SELECT count(*) FROM tenant_memberships")) or 0
        )
        rows = (
            await session.execute(
                text(
                    "SELECT m.id AS membership_id, m.tenant_id, m.user_id, u.email, "
                    "       u.full_name, m.role, m.is_active, m.created_at "
                    "FROM tenant_memberships m "
                    "JOIN users u ON u.id = m.user_id "
                    "ORDER BY m.created_at ASC "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"limit": limit, "offset": offset},
            )
        ).all()
        items = [
            MembershipItem(
                membership_id=UUID(str(r.membership_id)),
                tenant_id=UUID(str(r.tenant_id)),
                user_id=UUID(str(r.user_id)),
                email=str(r.email),
                full_name=str(r.full_name),
                role=UserRole(str(r.role)),
                is_active=bool(r.is_active),
                created_at=r.created_at,
            )
            for r in rows
        ]
        return PaginatedResponse.build(items=items, total=total, limit=limit, offset=offset)

    async def invite_member(
        self,
        session: AsyncSession,
        actor: CurrentPrincipal,
        req: InviteMemberRequest,
    ) -> MembershipItem:
        if req.role == UserRole.OWNER and actor.role != UserRole.OWNER:
            raise AppError(
                slug="owner-role-restricted",
                title="Owner Assignment Restricted",
                status_code=403,
                detail="Only an existing owner can assign the owner role",
                code="ERR_INSUFFICIENT_ROLE",
            )
        email = str(req.email).lower().strip()
        existing_user = (
            await session.execute(
                text("SELECT id, full_name FROM users WHERE email = :email"),
                {"email": email},
            )
        ).first()
        if existing_user is None:
            user_id = uuid4()
            full_name = req.full_name.strip()
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, :fn, :ph)"
                ),
                {
                    "id": str(user_id),
                    "email": email,
                    "fn": full_name,
                    "ph": hash_password(req.password),
                },
            )
        else:
            user_id = UUID(str(existing_user.id))
            full_name = str(existing_user.full_name)

        membership_id = uuid4()
        try:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                        "VALUES (:id, :tid, :uid, :role) "
                        "RETURNING id, tenant_id, user_id, role, is_active, created_at"
                    ),
                    {
                        "id": str(membership_id),
                        "tid": str(actor.tenant_id),
                        "uid": str(user_id),
                        "role": req.role.value,
                    },
                )
            ).one()
        except IntegrityError as exc:
            raise AppError(
                slug="membership-exists",
                title="Member Already Exists",
                status_code=409,
                detail="User is already a member of this workspace",
                code="ERR_MEMBERSHIP_EXISTS",
            ) from exc

        return MembershipItem(
            membership_id=UUID(str(row.id)),
            tenant_id=UUID(str(row.tenant_id)),
            user_id=UUID(str(row.user_id)),
            email=email,
            full_name=full_name,
            role=UserRole(str(row.role)),
            is_active=bool(row.is_active),
            created_at=row.created_at,
        )

    async def update_member_role(
        self,
        session: AsyncSession,
        actor: CurrentPrincipal,
        membership_id: UUID,
        req: UpdateMemberRoleRequest,
    ) -> MembershipItem:
        if req.role == UserRole.OWNER and actor.role != UserRole.OWNER:
            raise AppError(
                slug="owner-role-restricted",
                title="Owner Assignment Restricted",
                status_code=403,
                detail="Only an existing owner can assign the owner role",
                code="ERR_INSUFFICIENT_ROLE",
            )
        row = (
            await session.execute(
                text(
                    "UPDATE tenant_memberships "
                    "SET role = :role, is_active = COALESCE(:is_active, is_active) "
                    "WHERE id = :mid AND tenant_id = :tid "
                    "RETURNING id, tenant_id, user_id, role, is_active, created_at"
                ),
                {
                    "role": req.role.value,
                    "is_active": req.is_active,
                    "mid": str(membership_id),
                    "tid": str(actor.tenant_id),
                },
            )
        ).first()
        if row is None:
            raise AppError(
                slug="membership-not-found",
                title="Membership Not Found",
                status_code=404,
                detail="Membership does not exist in this workspace",
                code="ERR_NOT_FOUND",
            )
        user_row = (
            await session.execute(
                text("SELECT email, full_name FROM users WHERE id = :uid"),
                {"uid": str(row.user_id)},
            )
        ).one()
        return MembershipItem(
            membership_id=UUID(str(row.id)),
            tenant_id=UUID(str(row.tenant_id)),
            user_id=UUID(str(row.user_id)),
            email=str(user_row.email),
            full_name=str(user_row.full_name),
            role=UserRole(str(row.role)),
            is_active=bool(row.is_active),
            created_at=row.created_at,
        )

    async def update_tenant_settings(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        req: UpdateTenantSettingsRequest,
    ) -> TenantSettingsResponse:
        row = (
            await session.execute(
                text(
                    "UPDATE tenants "
                    "SET name = COALESCE(:name, name), "
                    "    base_currency = COALESCE(:curr, base_currency), "
                    "    timezone = COALESCE(:tz, timezone), "
                    "    updated_at = now() "
                    "WHERE id = :tid "
                    "RETURNING id, slug, name, business_preset, base_currency, timezone"
                ),
                {
                    "name": req.name.strip() if req.name else None,
                    "curr": req.base_currency.upper() if req.base_currency else None,
                    "tz": req.timezone,
                    "tid": str(tenant_id),
                },
            )
        ).one()
        return TenantSettingsResponse(
            tenant_id=UUID(str(row.id)),
            slug=str(row.slug),
            name=str(row.name),
            business_preset=str(row.business_preset),
            base_currency=str(row.base_currency),
            timezone=str(row.timezone),
        )
```

Write `apps/backend/src/erp_core/tenancy/router.py`:
```python
from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import Settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.cost_visibility import apply_cost_visibility
from erp_core.tenancy.dependencies import (
    CurrentPrincipal,
    get_current_principal,
    get_tenant_db,
    require_roles,
)
from erp_core.tenancy.schemas import (
    CostVisibilitySampleResponse,
    InviteMemberRequest,
    LoginRequest,
    MembershipItem,
    PrincipalResponse,
    RefreshRequest,
    RegisterRequest,
    TenantMembershipOption,
    TenantSettingsResponse,
    TokenResponse,
    UpdateMemberRoleRequest,
    UpdateTenantSettingsRequest,
)
from erp_core.tenancy.service import TenancyAuthService

router = APIRouter(prefix="/api/v1", tags=["tenancy"])


def _get_auth_service(request: Request) -> TenancyAuthService:
    settings: Settings = request.app.state.settings
    crypto_service: EnvelopeCryptoService = request.app.state.crypto_service
    return TenancyAuthService(settings=settings, crypto_service=crypto_service)


@router.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> TokenResponse:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return await service.register_workspace(session_factory, payload)


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> TokenResponse:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return await service.login(session_factory, payload)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh_token(
    payload: RefreshRequest,
    request: Request,
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> TokenResponse:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return await service.refresh_tokens(session_factory, payload.refresh_token)


@router.get("/auth/me", response_model=PrincipalResponse)
async def get_me(
    principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> PrincipalResponse:
    return await service.get_me(session, principal)


@router.get("/auth/my-tenants", response_model=list[TenantMembershipOption])
async def list_my_tenants(
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> list[TenantMembershipOption]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return await service.list_my_tenants(session_factory, principal.user_id)


@router.get("/tenants/memberships", response_model=PaginatedResponse[MembershipItem])
async def list_memberships(
    _principal: Annotated[
        CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedResponse[MembershipItem]:
    return await service.list_tenant_memberships(session, limit=limit, offset=offset)


@router.post(
    "/tenants/memberships",
    response_model=MembershipItem,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    payload: InviteMemberRequest,
    principal: Annotated[
        CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> MembershipItem:
    return await service.invite_member(session, principal, payload)


@router.patch("/tenants/memberships/{membership_id}", response_model=MembershipItem)
async def update_member_role(
    membership_id: UUID,
    payload: UpdateMemberRoleRequest,
    principal: Annotated[
        CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> MembershipItem:
    return await service.update_member_role(session, principal, membership_id, payload)


@router.patch("/tenants/settings", response_model=TenantSettingsResponse)
async def update_tenant_settings(
    payload: UpdateTenantSettingsRequest,
    principal: Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER))],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    service: Annotated[TenancyAuthService, Depends(_get_auth_service)],
) -> TenantSettingsResponse:
    return await service.update_tenant_settings(session, principal.tenant_id, payload)


@router.get("/tenants/cost-visibility-sample", response_model=CostVisibilitySampleResponse)
async def get_cost_visibility_sample(
    principal: Annotated[
        CurrentPrincipal,
        Depends(
            require_roles(
                UserRole.OWNER,
                UserRole.ADMIN,
                UserRole.MANAGER,
                UserRole.AGENT,
                UserRole.FINANCE,
            )
        ),
    ],
) -> CostVisibilitySampleResponse:
    sample = CostVisibilitySampleResponse(
        sku="LED-HB-150W-IP65",
        currency="USD",
        unit_sale_price=Decimal("34.90"),
        cost_price_base=Decimal("18.50"),
        cogs_base=Decimal("18.50"),
        net_profit_base=Decimal("11.20"),
        net_margin_pct=Decimal("32.09"),
        cost_masked=False,
    )
    return apply_cost_visibility(sample, principal.role)
```

Update `apps/backend/src/erp_core/api_main.py` to initialize `EnvelopeCryptoService` in lifespan and include `tenancy_router`:
```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import create_runtime_engine, create_session_factory, system_session
from erp_core.shared.errors import register_exception_handlers
from erp_core.shared.redis_client import create_redis_client
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.router import router as tenancy_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    redis_client = create_redis_client(settings)
    storage = ObjectStorageService(settings)
    crypto_service = EnvelopeCryptoService(settings)
    runtime_engine = create_runtime_engine(settings)
    session_factory = create_session_factory(runtime_engine)

    app.state.settings = settings
    app.state.redis = redis_client
    app.state.storage = storage
    app.state.crypto_service = crypto_service
    app.state.runtime_engine = runtime_engine
    app.state.session_factory = session_factory
    try:
        yield
    finally:
        await runtime_engine.dispose()
        await redis_client.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Omnichannel Hybrid ERP API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    app.include_router(tenancy_router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz(request: Request) -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            pong = await request.app.state.redis.ping()
            checks["redis"] = "ok" if pong else "error"
        except Exception as exc:
            checks["redis"] = f"error: {type(exc).__name__}"

        try:
            async with system_session(request.app.state.session_factory) as session:
                val = await session.scalar(text("SELECT 1"))
                checks["postgres"] = "ok" if val == 1 else "error"
        except Exception as exc:
            checks["postgres"] = f"error: {type(exc).__name__}"

        is_ready = all(val == "ok" for val in checks.values())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ready" if is_ready else "degraded", "checks": checks},
        )

    return app


app = create_app()
```

- [ ] **Step 4: Run auth & RBAC tests plus static checks**

Run:
```bash
uv run pytest apps/backend/tests/test_auth_rbac.py -v
uv run lint-imports --config infra/importlinter.ini
uv run ruff check apps/backend
uv run mypy apps/backend/src
```
Expected: `2 passed`, `import-linter`, `ruff`, and `mypy` all pass cleanly.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/tenancy/ apps/backend/src/erp_core/api_main.py apps/backend/tests/test_auth_rbac.py
git commit -m "feat(auth): implement Argon2/JWT authentication, 5-role RBAC guards, and agent cost-field masking"
```

---

### Task 1.5: Immutable Partitioned `audit_events` & GDPR Compliance Workflows

**Files:**
- Create: `apps/backend/src/erp_core/tenancy/audit.py`
- Create: `apps/backend/src/erp_core/tenancy/gdpr_service.py`
- Modify: `apps/backend/src/erp_core/tenancy/schemas.py`
- Modify: `apps/backend/src/erp_core/tenancy/router.py`
- Test: `apps/backend/tests/test_audit_and_gdpr.py`

**Interfaces:**
- Consumes: `erp_core.shared.db.tenant_session`, `erp_core.shared.storage.ObjectStorageService`, `erp_core.shared.crypto.EnvelopeCryptoService`, `erp_core.tenancy.dependencies`
- Produces:
  - `erp_core.tenancy.audit.record_audit_event(...) -> int`, `list_audit_events(...)`
  - `erp_core.tenancy.gdpr_service.GdprService`: `export_customer_data(...)`, `anonymize_customer_pii(...)`, `redact_tenant_crypto_shred(...)`
  - FastAPI routes: `GET /api/v1/tenancy/audit-events`, `POST /api/v1/tenancy/gdpr/customers/{customer_id}/export`, `POST /api/v1/tenancy/gdpr/customers/{customer_id}/anonymize`, `POST /api/v1/tenancy/gdpr/tenant/redact`

- [ ] **Step 1: Write the failing immutable audit_events & GDPR workflow tests**

Write `apps/backend/tests/test_audit_and_gdpr.py`:
```python
from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx as raw_httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.audit import record_audit_event


async def _register_owner(async_client: AsyncClient) -> tuple[str, UUID, UUID]:
    suffix = uuid4().hex[:8]
    resp = await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"gdpr-{suffix}@factory.example.com",
            "password": "StrongPassword2026!",
            "full_name": "GDPR Owner",
            "tenant_name": f"GDPR Co {suffix}",
            "tenant_slug": f"gdpr-{suffix}",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    return (
        data["access_token"],
        UUID(data["principal"]["tenant_id"]),
        UUID(data["principal"]["user_id"]),
    )


async def test_audit_events_partitioned_and_immutable_for_app_runtime(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, tenant_id, user_id = await _register_owner(async_client)

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        part_count = await session.scalar(
            text(
                "SELECT count(*) FROM pg_inherits "
                "WHERE inhparent = 'audit_events'::regclass "
                "  AND inhrelid::regclass::text LIKE 'audit_events_20%'"
            )
        )
        assert int(part_count or 0) >= 4

        event_id = await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(user_id),
            "quote.sent",
            "quote",
            uuid4(),
            before_state={"status": "draft"},
            after_state={"status": "sent"},
            ip_address="203.0.113.10",
            user_agent="pytest-agent/1.0",
        )
        assert isinstance(event_id, int)
        assert event_id > 0

    # app_runtime MUST receive PostgreSQL permission denied on UPDATE and DELETE
    with pytest.raises(DBAPIError) as update_err:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text("UPDATE audit_events SET action = 'tampered' WHERE id = :id"),
                {"id": event_id},
            )
    assert "permission denied" in str(update_err.value).lower()

    with pytest.raises(DBAPIError) as delete_err:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text("DELETE FROM audit_events WHERE id = :id"),
                {"id": event_id},
            )
    assert "permission denied" in str(delete_err.value).lower()

    list_resp = await async_client.get(
        "/api/v1/tenancy/audit-events",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["total"] >= 1
    assert any(item["action"] == "quote.sent" for item in body["items"])


async def test_gdpr_export_anonymize_preserving_ledger_and_tenant_shred(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, tenant_id, _user_id = await _register_owner(async_client)
    storage = ObjectStorageService(get_settings())
    await storage.ensure_bucket_exists()

    customer_id = uuid4()
    order_id = uuid4()

    # If Plan 02/03 customer & order tables do not exist yet, stage minimal test tables
    # and clean them up in finally so this test passes in both Week 1 and Week 8.
    created_temp_tables = False
    async with migrator_session_factory() as m_sess:
        async with m_sess.begin():
            customers_exists = await m_sess.scalar(text("SELECT to_regclass('public.customers')"))
            if customers_exists is None:
                created_temp_tables = True
                await m_sess.execute(
                    text(
                        """
                        CREATE TABLE customers (
                            id UUID PRIMARY KEY,
                            tenant_id UUID NOT NULL,
                            display_name TEXT NOT NULL,
                            primary_email TEXT,
                            primary_phone TEXT,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        );
                        CREATE TABLE customer_identities (
                            id UUID PRIMARY KEY,
                            tenant_id UUID NOT NULL,
                            customer_id UUID NOT NULL,
                            identity_type TEXT NOT NULL,
                            identity_value TEXT NOT NULL
                        );
                        CREATE TABLE orders (
                            id UUID PRIMARY KEY,
                            tenant_id UUID NOT NULL,
                            customer_id UUID NOT NULL,
                            order_no TEXT NOT NULL,
                            total_amount NUMERIC(12, 2) NOT NULL
                        );
                        CREATE TABLE order_profit_snapshots (
                            order_id UUID PRIMARY KEY,
                            tenant_id UUID NOT NULL,
                            net_profit_base NUMERIC(12, 2) NOT NULL
                        );
                        GRANT SELECT, INSERT, UPDATE, DELETE ON customers, customer_identities, orders, order_profit_snapshots TO app_runtime;
                        """
                    )
                )

    try:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, display_name, primary_email, primary_phone) "
                    "VALUES (:cid, :tid, 'Hans Mueller', 'hans@berlin-lighting.de', '+491701234567')"
                ),
                {"cid": str(customer_id), "tid": str(tenant_id)},
            )
            await session.execute(
                text(
                    "INSERT INTO customer_identities (id, tenant_id, customer_id, identity_type, identity_value) "
                    "VALUES (:id, :tid, :cid, 'email', 'hans@berlin-lighting.de')"
                ),
                {"id": str(uuid4()), "tid": str(tenant_id), "cid": str(customer_id)},
            )
            await session.execute(
                text(
                    "INSERT INTO orders (id, tenant_id, customer_id, order_no, total_amount) "
                    "VALUES (:oid, :tid, :cid, 'ORD-2026-0001', 12450.00)"
                ),
                {"oid": str(order_id), "tid": str(tenant_id), "cid": str(customer_id)},
            )
            await session.execute(
                text(
                    "INSERT INTO order_profit_snapshots (order_id, tenant_id, net_profit_base) "
                    "VALUES (:oid, :tid, 3890.50)"
                ),
                {"oid": str(order_id), "tid": str(tenant_id)},
            )

        # 1. GDPR Export via API -> presigned URL yields JSON containing customer data
        export_resp = await async_client.post(
            f"/api/v1/tenancy/gdpr/customers/{customer_id}/export",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert export_resp.status_code == 200, export_resp.text
        export_body = export_resp.json()
        assert export_body["download_url"]

        async with raw_httpx.AsyncClient() as dl_client:
            downloaded = await dl_client.get(export_body["download_url"])
        assert downloaded.status_code == 200
        exported_json = json.loads(downloaded.content)
        assert exported_json["customer_id"] == str(customer_id)
        assert exported_json["tenant_id"] == str(tenant_id)
        assert len(exported_json["tables"]["customers"]) == 1

        # 2. GDPR Anonymize via API -> REDACTED_<sha256>, identities deleted, financial ledger preserved
        anon_resp = await async_client.post(
            f"/api/v1/tenancy/gdpr/customers/{customer_id}/anonymize",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert anon_resp.status_code == 200, anon_resp.text
        anon_body = anon_resp.json()
        assert anon_body["redacted_identifier"].startswith("REDACTED_")

        async with tenant_session(runtime_session_factory, tenant_id) as session:
            cust_row = (
                await session.execute(
                    text("SELECT display_name, primary_email, primary_phone FROM customers WHERE id = :cid"),
                    {"cid": str(customer_id)},
                )
            ).one()
            assert cust_row.display_name == anon_body["redacted_identifier"]
            assert cust_row.primary_phone is None

            ident_count = await session.scalar(
                text("SELECT count(*) FROM customer_identities WHERE customer_id = :cid"),
                {"cid": str(customer_id)},
            )
            assert ident_count == 0

            # Financial ledger rows remain intact
            order_total = await session.scalar(
                text("SELECT total_amount FROM orders WHERE id = :oid"),
                {"oid": str(order_id)},
            )
            profit_val = await session.scalar(
                text("SELECT net_profit_base FROM order_profit_snapshots WHERE order_id = :oid"),
                {"oid": str(order_id)},
            )
            assert str(order_total) == "12450.00"
            assert str(profit_val) == "3890.50"

        # 3. Tenant Crypto-Shred via API
        shred_resp = await async_client.post(
            "/api/v1/tenancy/gdpr/tenant/redact",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert shred_resp.status_code == 200, shred_resp.text
        assert shred_resp.json()["shredded"] is True

        # Subsequent call with old JWT fails because tenant is shredded
        me_after_shred = await async_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_after_shred.status_code == 401
    finally:
        if created_temp_tables:
            async with migrator_session_factory() as m_sess:
                async with m_sess.begin():
                    await m_sess.execute(
                        text(
                            "DROP TABLE IF EXISTS order_profit_snapshots, orders, customer_identities, customers CASCADE;"
                        )
                    )
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest apps/backend/tests/test_audit_and_gdpr.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.tenancy.audit'`.

- [ ] **Step 3: Implement `audit.py`, `gdpr_service.py`, and wire audit/GDPR schemas & routes**

Write `apps/backend/src/erp_core/tenancy/audit.py`:
```python
from __future__ import annotations

import ipaddress
import json
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.schemas import AuditEventRead


def _normalize_ip(ip_address: str | None) -> str | None:
    if not ip_address:
        return None
    try:
        return str(ipaddress.ip_address(ip_address.strip()))
    except ValueError:
        return None


async def record_audit_event(
    session: AsyncSession,
    tenant_id: UUID,
    actor_type: Literal["human_user", "ai_agent", "system_cron", "webhook"],
    actor_id: str,
    action: str,
    resource_type: str,
    resource_id: UUID,
    *,
    proposal_id: UUID | None = None,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> int:
    normalized_ip = _normalize_ip(ip_address)
    event_id = await session.scalar(
        text(
            "INSERT INTO audit_events "
            "(tenant_id, actor_type, actor_id, action, resource_type, resource_id, "
            " proposal_id, before_state, after_state, ip_address, user_agent) "
            "VALUES (:tid, :actor_type, :actor_id, :action, :resource_type, :resource_id, "
            "        :proposal_id, CAST(:before_state AS jsonb), CAST(:after_state AS jsonb), "
            "        CAST(:ip_address AS inet), :user_agent) "
            "RETURNING id"
        ),
        {
            "tid": str(tenant_id),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "proposal_id": str(proposal_id) if proposal_id else None,
            "before_state": json.dumps(before_state, default=str) if before_state is not None else None,
            "after_state": json.dumps(after_state, default=str) if after_state is not None else None,
            "ip_address": normalized_ip,
            "user_agent": user_agent,
        },
    )
    return int(event_id or 0)


async def list_audit_events(
    session: AsyncSession,
    *,
    action: str | None = None,
    resource_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> PaginatedResponse[AuditEventRead]:
    where_clauses: list[str] = ["1 = 1"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if action:
        where_clauses.append("action = :action")
        params["action"] = action
    if resource_type:
        where_clauses.append("resource_type = :resource_type")
        params["resource_type"] = resource_type

    where_sql = " AND ".join(where_clauses)
    total = int(
        await session.scalar(text(f"SELECT count(*) FROM audit_events WHERE {where_sql}"), params)
        or 0
    )
    rows = (
        await session.execute(
            text(
                f"SELECT id, tenant_id, created_at, actor_type, actor_id, action, "
                f"       resource_type, resource_id, proposal_id, before_state, after_state, "
                f"       host(ip_address) AS ip_address, user_agent "
                f"FROM audit_events WHERE {where_sql} "
                f"ORDER BY created_at DESC, id DESC "
                f"LIMIT :limit OFFSET :offset"
            ),
            params,
        )
    ).all()
    items = [
        AuditEventRead(
            id=int(r.id),
            tenant_id=UUID(str(r.tenant_id)),
            created_at=r.created_at,
            actor_type=str(r.actor_type),
            actor_id=str(r.actor_id),
            action=str(r.action),
            resource_type=str(r.resource_type),
            resource_id=UUID(str(r.resource_id)),
            proposal_id=UUID(str(r.proposal_id)) if r.proposal_id else None,
            before_state=r.before_state,
            after_state=r.after_state,
            ip_address=str(r.ip_address) if r.ip_address else None,
            user_agent=str(r.user_agent) if r.user_agent else None,
        )
        for r in rows
    ]
    return PaginatedResponse.build(items=items, total=total, limit=limit, offset=offset)
```

Write `apps/backend/src/erp_core/tenancy/gdpr_service.py`:
```python
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.schemas import (
    GdprAnonymizeResult,
    GdprExportResult,
    GdprTenantRedactResult,
)


class GdprService:
    """GDPR compliance workflows: customer export, PII anonymization, and tenant crypto-shred."""

    @staticmethod
    async def _table_exists(session: AsyncSession, table_name: str) -> bool:
        reg = await session.scalar(
            text("SELECT to_regclass(:name)"),
            {"name": f"public.{table_name}"},
        )
        return reg is not None

    @staticmethod
    def _serialize_row(row: Any) -> dict[str, Any]:
        return {k: v for k, v in dict(row._mapping).items()}

    async def export_customer_data(
        self,
        session: AsyncSession,
        storage: ObjectStorageService,
        tenant_id: UUID,
        customer_id: UUID,
        actor_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> GdprExportResult:
        tables_payload: dict[str, list[dict[str, Any]]] = {}

        if await self._table_exists(session, "customers"):
            rows = (
                await session.execute(
                    text("SELECT * FROM customers WHERE tenant_id = :tid AND id = :cid"),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["customers"] = [self._serialize_row(r) for r in rows]

        if await self._table_exists(session, "customer_identities"):
            rows = (
                await session.execute(
                    text(
                        "SELECT * FROM customer_identities "
                        "WHERE tenant_id = :tid AND customer_id = :cid"
                    ),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["customer_identities"] = [self._serialize_row(r) for r in rows]

        if await self._table_exists(session, "conversations"):
            rows = (
                await session.execute(
                    text("SELECT * FROM conversations WHERE tenant_id = :tid AND customer_id = :cid"),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["conversations"] = [self._serialize_row(r) for r in rows]

        if await self._table_exists(session, "messages") and await self._table_exists(
            session, "conversations"
        ):
            rows = (
                await session.execute(
                    text(
                        "SELECT m.* FROM messages m "
                        "JOIN conversations c ON c.id = m.conversation_id "
                        "WHERE m.tenant_id = :tid AND c.customer_id = :cid"
                    ),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["messages"] = [self._serialize_row(r) for r in rows]

        if await self._table_exists(session, "quotes"):
            rows = (
                await session.execute(
                    text("SELECT * FROM quotes WHERE tenant_id = :tid AND customer_id = :cid"),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["quotes"] = [self._serialize_row(r) for r in rows]

        if await self._table_exists(session, "orders"):
            rows = (
                await session.execute(
                    text("SELECT * FROM orders WHERE tenant_id = :tid AND customer_id = :cid"),
                    {"tid": str(tenant_id), "cid": str(customer_id)},
                )
            ).all()
            tables_payload["orders"] = [self._serialize_row(r) for r in rows]

        audit_rows = (
            await session.execute(
                text(
                    "SELECT id, created_at, actor_type, actor_id, action, resource_type, resource_id "
                    "FROM audit_events "
                    "WHERE tenant_id = :tid AND resource_id = :cid "
                    "ORDER BY created_at DESC"
                ),
                {"tid": str(tenant_id), "cid": str(customer_id)},
            )
        ).all()
        tables_payload["audit_events"] = [self._serialize_row(r) for r in audit_rows]

        export_id = uuid4()
        bundle = {
            "export_id": str(export_id),
            "tenant_id": str(tenant_id),
            "customer_id": str(customer_id),
            "exported_at": datetime.now(UTC).isoformat(),
            "tables": tables_payload,
        }
        object_key = f"tenants/{tenant_id}/gdpr/exports/{customer_id}/{export_id}.json"
        await storage.ensure_bucket_exists()
        await storage.upload_bytes(
            object_key,
            json.dumps(bundle, default=str, indent=2).encode("utf-8"),
            content_type="application/json",
        )
        expires_in = 604800  # 7 days
        download_url = await storage.generate_presigned_get_url(object_key, expires_in=expires_in)

        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            actor_id,
            "gdpr.customer_exported",
            "customer",
            customer_id,
            after_state={"export_id": str(export_id), "object_key": object_key},
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return GdprExportResult(
            export_id=export_id,
            tenant_id=tenant_id,
            customer_id=customer_id,
            object_key=object_key,
            download_url=download_url,
            expires_in_seconds=expires_in,
            tables_included=sorted(tables_payload.keys()),
        )

    async def anonymize_customer_pii(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        customer_id: UUID,
        actor_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> GdprAnonymizeResult:
        digest = hashlib.sha256(f"{tenant_id}:{customer_id}".encode()).hexdigest()[:16]
        redacted_identifier = f"REDACTED_{digest}"
        redacted_email = f"{redacted_identifier}@redacted.invalid"
        identities_deleted = 0

        if await self._table_exists(session, "customers"):
            await session.execute(
                text(
                    "UPDATE customers "
                    "SET display_name = :redacted, "
                    "    primary_email = :redacted_email, "
                    "    primary_phone = NULL, "
                    "    updated_at = now() "
                    "WHERE tenant_id = :tid AND id = :cid"
                ),
                {
                    "redacted": redacted_identifier,
                    "redacted_email": redacted_email,
                    "tid": str(tenant_id),
                    "cid": str(customer_id),
                },
            )

        if await self._table_exists(session, "customer_identities"):
            res = await session.execute(
                text(
                    "DELETE FROM customer_identities "
                    "WHERE tenant_id = :tid AND customer_id = :cid"
                ),
                {"tid": str(tenant_id), "cid": str(customer_id)},
            )
            identities_deleted = int(res.rowcount or 0)

        if await self._table_exists(session, "messages") and await self._table_exists(
            session, "conversations"
        ):
            await session.execute(
                text(
                    "UPDATE messages "
                    "SET body_text = '[REDACTED]', body_html = NULL, attachments_json = '[]'::jsonb "
                    "WHERE tenant_id = :tid "
                    "  AND conversation_id IN ("
                    "      SELECT id FROM conversations WHERE tenant_id = :tid AND customer_id = :cid"
                    "  )"
                ),
                {"tid": str(tenant_id), "cid": str(customer_id)},
            )

        preserved_tables = ["orders", "order_profit_snapshots"]
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            actor_id,
            "gdpr.customer_anonymized",
            "customer",
            customer_id,
            after_state={
                "redacted_identifier": redacted_identifier,
                "identities_deleted": identities_deleted,
                "preserved_financial_tables": preserved_tables,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return GdprAnonymizeResult(
            tenant_id=tenant_id,
            customer_id=customer_id,
            redacted_identifier=redacted_identifier,
            identities_deleted=identities_deleted,
            financial_ledger_preserved=True,
        )

    async def redact_tenant_crypto_shred(
        self,
        session: AsyncSession,
        storage: ObjectStorageService,
        crypto_service: EnvelopeCryptoService,
        tenant_id: UUID,
        actor_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> GdprTenantRedactResult:
        await crypto_service.crypto_shred_tenant(session, tenant_id)
        deleted_objects = await storage.delete_prefix(f"tenants/{tenant_id}/")
        await record_audit_event(
            session,
            tenant_id,
            "human_user",
            actor_id,
            "gdpr.tenant_crypto_shredded",
            "tenant",
            tenant_id,
            after_state={"deleted_s3_objects": deleted_objects, "shredded": True},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return GdprTenantRedactResult(
            tenant_id=tenant_id,
            shredded=True,
            deleted_s3_objects=deleted_objects,
        )
```

Update `apps/backend/src/erp_core/tenancy/schemas.py` imports at top of file to add `from typing import Any`:
```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
```

Append the Audit & GDPR Pydantic schemas to the end of `apps/backend/src/erp_core/tenancy/schemas.py`:
```python
class AuditEventRead(BaseModel):
    id: int
    tenant_id: UUID
    created_at: datetime
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: UUID
    proposal_id: UUID | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    ip_address: str | None = None
    user_agent: str | None = None


class GdprExportResult(BaseModel):
    export_id: UUID
    tenant_id: UUID
    customer_id: UUID
    object_key: str
    download_url: str
    expires_in_seconds: int
    tables_included: list[str]


class GdprAnonymizeResult(BaseModel):
    tenant_id: UUID
    customer_id: UUID
    redacted_identifier: str
    identities_deleted: int
    financial_ledger_preserved: bool


class GdprTenantRedactResult(BaseModel):
    tenant_id: UUID
    shredded: bool
    deleted_s3_objects: int
```

Update imports at the top of `apps/backend/src/erp_core/tenancy/router.py`:
```python
from erp_core.shared.config import Settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.audit import list_audit_events
from erp_core.tenancy.cost_visibility import apply_cost_visibility
from erp_core.tenancy.dependencies import (
    CurrentPrincipal,
    get_current_principal,
    get_tenant_db,
    require_roles,
)
from erp_core.tenancy.gdpr_service import GdprService
from erp_core.tenancy.schemas import (
    AuditEventRead,
    CostVisibilitySampleResponse,
    GdprAnonymizeResult,
    GdprExportResult,
    GdprTenantRedactResult,
    InviteMemberRequest,
    LoginRequest,
    MembershipItem,
    PrincipalResponse,
    RefreshRequest,
    RegisterRequest,
    TenantMembershipOption,
    TenantSettingsResponse,
    TokenResponse,
    UpdateMemberRoleRequest,
    UpdateTenantSettingsRequest,
)
from erp_core.tenancy.service import TenancyAuthService
```

Append the Audit & GDPR routes to the end of `apps/backend/src/erp_core/tenancy/router.py`:
```python
@router.get("/tenancy/audit-events", response_model=PaginatedResponse[AuditEventRead])
async def get_audit_events(
    _principal: Annotated[
        CurrentPrincipal,
        Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.FINANCE)),
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    action: str | None = None,
    resource_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedResponse[AuditEventRead]:
    return await list_audit_events(
        session,
        action=action,
        resource_type=resource_type,
        limit=limit,
        offset=offset,
    )


@router.post("/tenancy/gdpr/customers/{customer_id}/export", response_model=GdprExportResult)
async def gdpr_export_customer(
    customer_id: UUID,
    request: Request,
    principal: Annotated[
        CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> GdprExportResult:
    storage: ObjectStorageService = request.app.state.storage
    gdpr = GdprService()
    return await gdpr.export_customer_data(
        session,
        storage,
        principal.tenant_id,
        customer_id,
        str(principal.user_id),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post(
    "/tenancy/gdpr/customers/{customer_id}/anonymize",
    response_model=GdprAnonymizeResult,
)
async def gdpr_anonymize_customer(
    customer_id: UUID,
    request: Request,
    principal: Annotated[
        CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> GdprAnonymizeResult:
    gdpr = GdprService()
    return await gdpr.anonymize_customer_pii(
        session,
        principal.tenant_id,
        customer_id,
        str(principal.user_id),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/tenancy/gdpr/tenant/redact", response_model=GdprTenantRedactResult)
async def gdpr_redact_tenant(
    request: Request,
    principal: Annotated[CurrentPrincipal, Depends(require_roles(UserRole.OWNER))],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> GdprTenantRedactResult:
    storage: ObjectStorageService = request.app.state.storage
    crypto_service: EnvelopeCryptoService = request.app.state.crypto_service
    gdpr = GdprService()
    return await gdpr.redact_tenant_crypto_shred(
        session,
        storage,
        crypto_service,
        principal.tenant_id,
        str(principal.user_id),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
```

- [ ] **Step 4: Run audit & GDPR tests plus static checks**

Run:
```bash
uv run pytest apps/backend/tests/test_audit_and_gdpr.py -v
uv run lint-imports --config infra/importlinter.ini
uv run ruff check apps/backend
uv run mypy apps/backend/src
```
Expected: `2 passed`, `import-linter`, `ruff`, and `mypy` all pass cleanly.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/tenancy/ apps/backend/tests/test_audit_and_gdpr.py
git commit -m "feat(compliance): add immutable partitioned audit_events and GDPR export/anonymize/crypto-shred workflows"
```

---
### Task 1.6: Self-Serve Hybrid Onboarding State Machine

**Files:**
- Create: `apps/backend/src/erp_core/tenancy/onboarding.py`
- Modify: `apps/backend/src/erp_core/api_main.py`
- Test: `apps/backend/tests/test_onboarding_state_machine.py`

**Interfaces:**
- Consumes: `erp_core.shared.enums.OnboardingStage`, `erp_core.shared.enums.UserRole`, `erp_core.shared.errors.AppError`, `erp_core.tenancy.dependencies`, `erp_core.tenancy.audit.record_audit_event`
- Produces:
  - `erp_core.tenancy.onboarding.OnboardingService`, `onboarding_router`
  - FastAPI routes: `GET /api/v1/onboarding/state`, `POST /api/v1/onboarding/advance`, `PATCH /api/v1/onboarding/preset`

- [ ] **Step 1: Write the failing onboarding state machine tests**

Write `apps/backend/tests/test_onboarding_state_machine.py`:
```python
from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient


async def _register_factory_owner(async_client: AsyncClient) -> str:
    suffix = uuid4().hex[:8]
    resp = await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"onboard-{suffix}@factory.example.com",
            "password": "StrongPassword2026!",
            "full_name": "Onboard Owner",
            "tenant_name": f"LED Factory {suffix}",
            "tenant_slug": f"led-{suffix}",
        },
    )
    assert resp.status_code == 201
    return str(resp.json()["access_token"])


async def test_onboarding_linear_transitions_and_invalid_skip_422(
    async_client: AsyncClient,
) -> None:
    token = await _register_factory_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}

    # Default state & preset
    initial = await async_client.get("/api/v1/onboarding/state", headers=headers)
    assert initial.status_code == 200
    assert initial.json()["current_stage"] == "WorkspaceCreated"
    assert initial.json()["preset_type"] == "hybrid_factory_b2b_b2c"

    # Invalid skip WorkspaceCreated -> LiveProduction returns 422
    bad_skip = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "LiveProduction"},
    )
    assert bad_skip.status_code == 422
    assert bad_skip.json()["code"] == "ERR_INVALID_ONBOARDING_TRANSITION"

    # Advance WorkspaceCreated -> ChannelConnecting -> ChannelVerified
    r1 = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "ChannelConnecting", "step_metadata": {"channel": "whatsapp_coexistence"}},
    )
    assert r1.status_code == 200
    assert r1.json()["current_stage"] == "ChannelConnecting"

    r2 = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "ChannelVerified", "step_metadata": {"webhook_verified": True}},
    )
    assert r2.status_code == 200
    assert r2.json()["current_stage"] == "ChannelVerified"

    # Hybrid preset CANNOT skip CatalogSyncing unless skip_catalog_for_b2b=True
    denied_shortcut = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "AISandboxReady", "skip_catalog_for_b2b": False},
    )
    assert denied_shortcut.status_code == 422

    # Continue standard hybrid path: ChannelVerified -> CatalogSyncing -> AISandboxReady -> LiveProduction
    for next_stage in ("CatalogSyncing", "AISandboxReady", "LiveProduction"):
        res = await async_client.post(
            "/api/v1/onboarding/advance",
            headers=headers,
            json={"target_stage": next_stage},
        )
        assert res.status_code == 200, res.text
        assert res.json()["current_stage"] == next_stage

    # Terminal state rejects further transitions
    after_live = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "CatalogSyncing"},
    )
    assert after_live.status_code == 422


async def test_onboarding_pure_b2b_shortcut_channel_verified_to_ai_sandbox(
    async_client: AsyncClient,
) -> None:
    token = await _register_factory_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}

    preset_resp = await async_client.patch(
        "/api/v1/onboarding/preset",
        headers=headers,
        json={"preset_type": "pure_b2b_inquiry"},
    )
    assert preset_resp.status_code == 200
    assert preset_resp.json()["preset_type"] == "pure_b2b_inquiry"

    await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "ChannelConnecting"},
    )
    await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "ChannelVerified"},
    )

    # Pure B2B inquiry tenant skips CatalogSyncing cleanly
    shortcut_resp = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "AISandboxReady"},
    )
    assert shortcut_resp.status_code == 200
    assert shortcut_resp.json()["current_stage"] == "AISandboxReady"
    assert "AISandboxReady" in shortcut_resp.json()["completed_steps"]

    live_resp = await async_client.post(
        "/api/v1/onboarding/advance",
        headers=headers,
        json={"target_stage": "LiveProduction"},
    )
    assert live_resp.status_code == 200
    assert live_resp.json()["current_stage"] == "LiveProduction"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest apps/backend/tests/test_onboarding_state_machine.py -v
```
Expected: FAIL with 404 because onboarding routes are not mounted yet.

- [ ] **Step 3: Implement `apps/backend/src/erp_core/tenancy/onboarding.py` and mount `onboarding_router` in `api_main.py`**

Write `apps/backend/src/erp_core/tenancy/onboarding.py`:
```python
from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.enums import OnboardingStage, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

ALLOWED_PRESETS: frozenset[str] = frozenset(
    {"hybrid_factory_b2b_b2c", "dtc_brand", "pure_b2b_inquiry"}
)

LINEAR_TRANSITIONS: dict[OnboardingStage, OnboardingStage] = {
    OnboardingStage.WORKSPACE_CREATED: OnboardingStage.CHANNEL_CONNECTING,
    OnboardingStage.CHANNEL_CONNECTING: OnboardingStage.CHANNEL_VERIFIED,
    OnboardingStage.CHANNEL_VERIFIED: OnboardingStage.CATALOG_SYNCING,
    OnboardingStage.CATALOG_SYNCING: OnboardingStage.AI_SANDBOX_READY,
    OnboardingStage.AI_SANDBOX_READY: OnboardingStage.LIVE_PRODUCTION,
}


class OnboardingStateRead(BaseModel):
    tenant_id: UUID
    current_stage: OnboardingStage
    preset_type: str
    completed_steps: list[str]
    metadata_json: dict[str, Any]
    updated_at: datetime


class AdvanceOnboardingRequest(BaseModel):
    target_stage: OnboardingStage
    step_metadata: dict[str, Any] = Field(default_factory=dict)
    skip_catalog_for_b2b: bool = False


class UpdateOnboardingPresetRequest(BaseModel):
    preset_type: Literal["hybrid_factory_b2b_b2c", "dtc_brand", "pure_b2b_inquiry"]


class OnboardingService:
    async def get_state(self, session: AsyncSession, tenant_id: UUID) -> OnboardingStateRead:
        row = (
            await session.execute(
                text(
                    "SELECT tenant_id, current_stage, preset_type, completed_steps, "
                    "       metadata_json, updated_at "
                    "FROM onboarding_states WHERE tenant_id = :tid"
                ),
                {"tid": str(tenant_id)},
            )
        ).first()
        if row is None:
            raise AppError(
                slug="onboarding-state-missing",
                title="Onboarding State Not Found",
                status_code=404,
                detail="No onboarding state record exists for tenant",
                code="ERR_NOT_FOUND",
            )
        return OnboardingStateRead(
            tenant_id=UUID(str(row.tenant_id)),
            current_stage=OnboardingStage(str(row.current_stage)),
            preset_type=str(row.preset_type),
            completed_steps=list(row.completed_steps or []),
            metadata_json=dict(row.metadata_json or {}),
            updated_at=row.updated_at,
        )

    async def update_preset(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        req: UpdateOnboardingPresetRequest,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> OnboardingStateRead:
        current = await self.get_state(session, principal.tenant_id)
        if req.preset_type not in ALLOWED_PRESETS:
            raise AppError(
                slug="invalid-onboarding-preset",
                title="Invalid Business Preset",
                status_code=422,
                detail=f"Unsupported preset_type '{req.preset_type}'",
                code="ERR_INVALID_ONBOARDING_PRESET",
            )
        await session.execute(
            text(
                "UPDATE tenants SET business_preset = :preset, updated_at = now() WHERE id = :tid"
            ),
            {"preset": req.preset_type, "tid": str(principal.tenant_id)},
        )
        row = (
            await session.execute(
                text(
                    "UPDATE onboarding_states "
                    "SET preset_type = :preset, updated_at = now() "
                    "WHERE tenant_id = :tid "
                    "RETURNING tenant_id, current_stage, preset_type, completed_steps, metadata_json, updated_at"
                ),
                {"preset": req.preset_type, "tid": str(principal.tenant_id)},
            )
        ).one()
        await record_audit_event(
            session,
            principal.tenant_id,
            "human_user",
            str(principal.user_id),
            "onboarding.preset_updated",
            "onboarding_state",
            principal.tenant_id,
            before_state={"preset_type": current.preset_type},
            after_state={"preset_type": req.preset_type},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return OnboardingStateRead(
            tenant_id=UUID(str(row.tenant_id)),
            current_stage=OnboardingStage(str(row.current_stage)),
            preset_type=str(row.preset_type),
            completed_steps=list(row.completed_steps or []),
            metadata_json=dict(row.metadata_json or {}),
            updated_at=row.updated_at,
        )

    async def advance_stage(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        req: AdvanceOnboardingRequest,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> OnboardingStateRead:
        current = await self.get_state(session, principal.tenant_id)
        expected_next = LINEAR_TRANSITIONS.get(current.current_stage)
        b2b_shortcut_allowed = (
            current.current_stage == OnboardingStage.CHANNEL_VERIFIED
            and req.target_stage == OnboardingStage.AI_SANDBOX_READY
            and (req.skip_catalog_for_b2b or current.preset_type == "pure_b2b_inquiry")
        )

        if req.target_stage != expected_next and not b2b_shortcut_allowed:
            raise AppError(
                slug="invalid-onboarding-transition",
                title="Invalid Onboarding Stage Transition",
                status_code=422,
                detail=(
                    f"Cannot transition from '{current.current_stage.value}' "
                    f"to '{req.target_stage.value}'"
                ),
                code="ERR_INVALID_ONBOARDING_TRANSITION",
            )

        completed = list(current.completed_steps)
        if current.current_stage.value not in completed:
            completed.append(current.current_stage.value)
        if req.target_stage.value not in completed:
            completed.append(req.target_stage.value)

        merged_metadata = {**current.metadata_json, **req.step_metadata}
        if b2b_shortcut_allowed:
            merged_metadata["skipped_catalog_sync_for_b2b"] = True

        row = (
            await session.execute(
                text(
                    "UPDATE onboarding_states "
                    "SET current_stage = :stage, "
                    "    completed_steps = CAST(:steps AS jsonb), "
                    "    metadata_json = CAST(:meta AS jsonb), "
                    "    updated_at = now() "
                    "WHERE tenant_id = :tid "
                    "RETURNING tenant_id, current_stage, preset_type, completed_steps, metadata_json, updated_at"
                ),
                {
                    "stage": req.target_stage.value,
                    "steps": json.dumps(completed),
                    "meta": json.dumps(merged_metadata, default=str),
                    "tid": str(principal.tenant_id),
                },
            )
        ).one()

        await record_audit_event(
            session,
            principal.tenant_id,
            "human_user",
            str(principal.user_id),
            "onboarding.stage_advanced",
            "onboarding_state",
            principal.tenant_id,
            before_state={"current_stage": current.current_stage.value},
            after_state={
                "current_stage": req.target_stage.value,
                "b2b_shortcut": b2b_shortcut_allowed,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return OnboardingStateRead(
            tenant_id=UUID(str(row.tenant_id)),
            current_stage=OnboardingStage(str(row.current_stage)),
            preset_type=str(row.preset_type),
            completed_steps=list(row.completed_steps or []),
            metadata_json=dict(row.metadata_json or {}),
            updated_at=row.updated_at,
        )


onboarding_router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])


@onboarding_router.get("/state", response_model=OnboardingStateRead)
async def get_onboarding_state(
    principal: Annotated[
        CurrentPrincipal,
        Depends(
            require_roles(
                UserRole.OWNER,
                UserRole.ADMIN,
                UserRole.MANAGER,
                UserRole.AGENT,
                UserRole.FINANCE,
            )
        ),
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> OnboardingStateRead:
    return await OnboardingService().get_state(session, principal.tenant_id)


@onboarding_router.post("/advance", response_model=OnboardingStateRead)
async def advance_onboarding_stage(
    payload: AdvanceOnboardingRequest,
    request: Request,
    principal: Annotated[
        CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> OnboardingStateRead:
    return await OnboardingService().advance_stage(
        session,
        principal,
        payload,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@onboarding_router.patch("/preset", response_model=OnboardingStateRead)
async def update_onboarding_preset(
    payload: UpdateOnboardingPresetRequest,
    request: Request,
    principal: Annotated[
        CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
    ],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> OnboardingStateRead:
    return await OnboardingService().update_preset(
        session,
        principal,
        payload,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
```

Update `apps/backend/src/erp_core/api_main.py` to mount `onboarding_router`:
```python
from erp_core.tenancy.onboarding import onboarding_router
```
and inside `create_app()` right after `app.include_router(tenancy_router)`:
```python
    app.include_router(onboarding_router)
```

- [ ] **Step 4: Run onboarding state machine tests and static checks**

Run:
```bash
uv run pytest apps/backend/tests/test_onboarding_state_machine.py -v
uv run lint-imports --config infra/importlinter.ini
uv run ruff check apps/backend
uv run mypy apps/backend/src
```
Expected: `2 passed`, `import-linter`, `ruff`, and `mypy` all pass cleanly.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/tenancy/onboarding.py apps/backend/src/erp_core/api_main.py apps/backend/tests/test_onboarding_state_machine.py
git commit -m "feat(onboarding): implement self-serve hybrid onboarding state machine and B2B skip path"
```

---

### Task 1.7: OpenAPI 3.1 Export Script, Next.js 15 Base Workspace & `@hey-api/openapi-ts` CI Gate

**Files:**
- Create: `apps/backend/scripts/export_openapi.py`
- Create: `apps/web/package.json`
- Create: `apps/web/tsconfig.json`
- Create: `apps/web/next.config.ts`
- Create: `apps/web/openapi-ts.config.ts`
- Create: `apps/web/eslint.config.mjs`
- Create: `apps/web/src/app/layout.tsx`
- Create: `apps/web/src/app/page.tsx`
- Create: `apps/web/src/lib/api-client/.gitkeep`
- Test: `apps/backend/tests/test_openapi_contract.py`

**Interfaces:**
- Consumes: `erp_core.api_main.create_app()` FastAPI OpenAPI 3.1 schema.
- Produces:
  - `contracts/openapi.json`
  - `apps/web/src/lib/api-client/` generated TypeScript client & SDK types
  - ESLint rule blocking SQL/ORM/Redis imports inside `apps/web`

- [ ] **Step 1: Write the failing OpenAPI contract & TypeScript codegen gate test**

Write `apps/backend/tests/test_openapi_contract.py`:
```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_FILE = REPO_ROOT / "contracts" / "openapi.json"
WEB_DIR = REPO_ROOT / "apps" / "web"
API_CLIENT_DIR = WEB_DIR / "src" / "lib" / "api-client"


def test_export_openapi_and_generate_ts_client() -> None:
    export_script = REPO_ROOT / "apps" / "backend" / "scripts" / "export_openapi.py"
    assert export_script.exists(), "export_openapi.py script is missing"

    export_proc = subprocess.run(
        [sys.executable, str(export_script)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert export_proc.returncode == 0, f"export_openapi failed:\n{export_proc.stderr}"
    assert CONTRACTS_FILE.exists()

    spec = json.loads(CONTRACTS_FILE.read_text(encoding="utf-8"))
    assert spec["openapi"].startswith("3.1")
    for expected_path in (
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/me",
        "/api/v1/tenants/memberships",
        "/api/v1/tenancy/audit-events",
        "/api/v1/tenancy/gdpr/customers/{customer_id}/export",
        "/api/v1/onboarding/state",
        "/api/v1/onboarding/advance",
    ):
        assert expected_path in spec["paths"], f"Missing OpenAPI path: {expected_path}"

    install_proc = subprocess.run(
        ["pnpm", "install"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert install_proc.returncode == 0, f"pnpm install failed:\n{install_proc.stderr}"

    codegen_proc = subprocess.run(
        ["pnpm", "--filter", "web", "openapi-ts"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert codegen_proc.returncode == 0, f"openapi-ts failed:\n{codegen_proc.stdout}\n{codegen_proc.stderr}"
    generated_ts_files = list(API_CLIENT_DIR.glob("*.ts"))
    assert generated_ts_files, "Expected generated .ts files in apps/web/src/lib/api-client/"

    typecheck_proc = subprocess.run(
        ["pnpm", "--filter", "web", "typecheck"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert typecheck_proc.returncode == 0, f"web typecheck failed:\n{typecheck_proc.stdout}\n{typecheck_proc.stderr}"

    lint_proc = subprocess.run(
        ["pnpm", "--filter", "web", "lint"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert lint_proc.returncode == 0, f"web lint failed:\n{lint_proc.stdout}\n{lint_proc.stderr}"

    # Verify ESLint bans direct SQL/ORM imports in apps/web
    bad_import_proc = subprocess.run(
        [
            "pnpm",
            "--filter",
            "web",
            "exec",
            "eslint",
            "--stdin",
            "--stdin-filename",
            "src/app/bad-sql-check.ts",
        ],
        cwd=str(REPO_ROOT),
        input='import pg from "pg";\nexport default pg;\n',
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_import_proc.returncode != 0
    assert "no-restricted-imports" in bad_import_proc.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest apps/backend/tests/test_openapi_contract.py -v
```
Expected: FAIL with `AssertionError: export_openapi.py script is missing`.

- [ ] **Step 3: Create `export_openapi.py` and Next.js 15 base workspace (`apps/web/`)**

Write `apps/backend/scripts/export_openapi.py`:
```python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from erp_core.api_main import create_app


def export_openapi(output_path: Path, *, check_only: bool = False) -> int:
    app = create_app()
    schema = app.openapi()
    rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"

    if check_only:
        if not output_path.exists():
            print(f"Missing OpenAPI contract file: {output_path}", file=sys.stderr)
            return 1
        existing = output_path.read_text(encoding="utf-8")
        if existing != rendered:
            print(f"OpenAPI contract drift detected at {output_path}", file=sys.stderr)
            return 1
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    print(f"Exported OpenAPI 3.1 schema to {output_path}")
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[3]
    default_output = repo_root / "contracts" / "openapi.json"
    parser = argparse.ArgumentParser(description="Export FastAPI OpenAPI 3.1 JSON contract.")
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    return export_openapi(args.output, check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
```

Write `apps/web/package.json`:
```json
{
  "name": "web",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev --port 3000",
    "build": "next build",
    "start": "next start --port 3000",
    "lint": "eslint .",
    "typecheck": "tsc --noEmit",
    "openapi-ts": "openapi-ts"
  },
  "dependencies": {
    "@hey-api/client-fetch": "^0.8.2",
    "@tanstack/react-query": "^5.62.7",
    "next": "^15.1.0",
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  },
  "devDependencies": {
    "@eslint/eslintrc": "^3.2.0",
    "@hey-api/openapi-ts": "^0.66.0",
    "@types/node": "^22.10.2",
    "@types/react": "^19.0.2",
    "@types/react-dom": "^19.0.2",
    "eslint": "^9.17.0",
    "eslint-config-next": "^15.1.0",
    "typescript": "^5.7.2"
  }
}
```

Write `apps/web/tsconfig.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["dom", "dom.iterable", "esnext"],
    "allowJs": false,
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [{ "name": "next" }],
    "paths": {
      "@/*": ["./src/*"]
    }
  },
  "include": ["src/**/*.ts", "src/**/*.tsx", "next.config.ts"],
  "exclude": ["node_modules"]
}
```

Write `apps/web/next.config.ts`:
```ts
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
};

export default nextConfig;
```

Write `apps/web/openapi-ts.config.ts`:
```ts
import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "../../contracts/openapi.json",
  output: {
    path: "src/lib/api-client",
  },
  plugins: ["@hey-api/client-fetch", "@hey-api/typescript", "@hey-api/sdk"],
});
```

Write `apps/web/eslint.config.mjs`:
```js
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    ignores: ["src/lib/api-client/**"],
  },
  {
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: [
                "pg",
                "pg-*",
                "postgres",
                "mysql2",
                "sqlite3",
                "better-sqlite3",
                "prisma",
                "@prisma/*",
                "drizzle-orm",
                "drizzle-orm/*",
                "knex",
                "typeorm",
                "sequelize",
                "kysely",
                "@neondatabase/*",
                "ioredis",
                "redis",
              ],
              message:
                "apps/web is a thin UI/BFF shell. Direct SQL/ORM/Redis imports are forbidden; call apps/backend via src/lib/api-client.",
            },
          ],
        },
      ],
    },
  },
];

export default eslintConfig;
```

Write `apps/web/src/app/layout.tsx`:
```tsx
import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Omnichannel Hybrid ERP Workbench",
  description: "B2C + B2B Cross-Border AI Workbench & Lightweight ERP",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
```

Write `apps/web/src/app/page.tsx`:
```tsx
export default function HomePage() {
  return (
    <main>
      <h1>Omnichannel Hybrid ERP Workbench</h1>
      <p>FastAPI OpenAPI contract synchronized.</p>
    </main>
  );
}
```

Write `apps/web/src/lib/api-client/.gitkeep`:
```text
```

- [ ] **Step 4: Run OpenAPI export, TypeScript codegen, typecheck, and ESLint gate tests**

Run:
```bash
uv run pytest apps/backend/tests/test_openapi_contract.py -v
```
Expected: `1 passed`; `contracts/openapi.json` and `apps/web/src/lib/api-client/` generated with zero TS or ESLint errors.

- [ ] **Step 5: Commit**

```bash
git add contracts/openapi.json apps/backend/scripts/export_openapi.py apps/web/ apps/backend/tests/test_openapi_contract.py
git commit -m "feat(contracts): configure OpenAPI 3.1 export, Next.js 15 workspace, openapi-ts codegen, and SQL-ban lint gate"
```

---

### Task 1.8: Quanzhou Smart LED Hybrid Factory Seed Script (`seed_hybrid_factory.py`)

**Files:**
- Create: `apps/backend/scripts/seed_hybrid_factory.py`
- Test: `apps/backend/tests/test_seed_hybrid_factory.py`

**Interfaces:**
- Consumes: `erp_core.shared.db`, `erp_core.shared.crypto.EnvelopeCryptoService`, `erp_core.tenancy.security.hash_password`, `erp_core.shared.enums.UserRole`, `OnboardingStage`
- Produces:
  - `seed_hybrid_factory(session_factory, settings) -> SeedSummary`
  - Deterministic Tenant A (`quanzhou-led-factory`) with 5 users across all 5 roles, DEK, encrypted `ChannelCredentials`, and `OnboardingState`
  - Deterministic Tenant B (`shenzhen-audio-dtc`) for cross-tenant isolation verification

- [ ] **Step 1: Write the failing idempotent seed and Tenant A/B isolation tests**

Write `apps/backend/tests/test_seed_hybrid_factory.py`:
```python
from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.errors import AppError

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_SCRIPT_PATH = REPO_ROOT / "apps" / "backend" / "scripts" / "seed_hybrid_factory.py"


def _load_seed_module():
    assert SEED_SCRIPT_PATH.exists(), "seed_hybrid_factory.py does not exist"
    spec = importlib.util.spec_from_file_location("seed_hybrid_factory", SEED_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_seed_hybrid_factory_is_idempotent_and_isolated(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed_mod = _load_seed_module()
    settings = get_settings()

    # Run seed TWICE to prove strict idempotency
    summary_1 = await seed_mod.seed_hybrid_factory(runtime_session_factory, settings)
    summary_2 = await seed_mod.seed_hybrid_factory(runtime_session_factory, settings)

    assert summary_1.tenant_a_id == summary_2.tenant_a_id
    assert summary_1.tenant_b_id == summary_2.tenant_b_id
    assert summary_2.tenant_a_slug == "quanzhou-led-factory"
    assert summary_2.tenant_b_slug == "shenzhen-audio-dtc"

    crypto = EnvelopeCryptoService(settings)

    # Verify Tenant A isolation & encrypted credentials
    async with tenant_session(runtime_session_factory, summary_2.tenant_a_id) as session:
        tenants = (await session.execute(text("SELECT slug FROM tenants"))).all()
        assert [r.slug for r in tenants] == ["quanzhou-led-factory"]

        members = (
            await session.execute(text("SELECT role FROM tenant_memberships"))
        ).all()
        assert sorted(r.role for r in members) == sorted(
            ["owner", "admin", "manager", "agent", "finance"]
        )

        creds = (
            await session.execute(
                text(
                    "SELECT channel_account_id, credential_type, ciphertext, nonce, auth_tag, key_version "
                    "FROM channel_credentials ORDER BY credential_type"
                )
            )
        ).all()
        assert len(creds) == 3

        first = creds[0]
        decrypted = await crypto.decrypt_secret(
            session,
            summary_2.tenant_a_id,
            UUID(str(first.channel_account_id)),
            str(first.credential_type),
            bytes(first.ciphertext),
            bytes(first.nonce),
            bytes(first.auth_tag),
            int(first.key_version),
        )
        assert decrypted

        stage = await session.scalar(text("SELECT current_stage FROM onboarding_states"))
        assert stage == "LiveProduction"

    # Verify Tenant B isolation & inability to decrypt Tenant A's credentials
    async with tenant_session(runtime_session_factory, summary_2.tenant_b_id) as session:
        tenants_b = (await session.execute(text("SELECT slug FROM tenants"))).all()
        assert [r.slug for r in tenants_b] == ["shenzhen-audio-dtc"]

        members_b = (
            await session.execute(text("SELECT role FROM tenant_memberships"))
        ).all()
        assert [r.role for r in members_b] == ["owner"]

        creds_b = (
            await session.execute(text("SELECT id FROM channel_credentials"))
        ).all()
        assert len(creds_b) == 1

        with pytest.raises(AppError) as err:
            await crypto.decrypt_secret(
                session,
                summary_2.tenant_b_id,
                UUID(str(first.channel_account_id)),
                str(first.credential_type),
                bytes(first.ciphertext),
                bytes(first.nonce),
                bytes(first.auth_tag),
                int(first.key_version),
            )
        assert err.value.code == "ERR_INVALID_CREDENTIAL_TAG"

    # Verify HTTP login & RBAC cost visibility across seeded users
    owner_login = await async_client.post(
        "/api/v1/auth/login",
        json={
            "email": "owner@quanzhou-led.example.com",
            "password": seed_mod.SEED_DEMO_PASSWORD,
            "tenant_slug": "quanzhou-led-factory",
        },
    )
    assert owner_login.status_code == 200
    assert owner_login.json()["principal"]["role"] == "owner"
    assert owner_login.json()["principal"]["can_view_cost"] is True

    agent_login = await async_client.post(
        "/api/v1/auth/login",
        json={
            "email": "agent@quanzhou-led.example.com",
            "password": seed_mod.SEED_DEMO_PASSWORD,
            "tenant_slug": "quanzhou-led-factory",
        },
    )
    assert agent_login.status_code == 200
    assert agent_login.json()["principal"]["role"] == "agent"
    assert agent_login.json()["principal"]["can_view_cost"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest apps/backend/tests/test_seed_hybrid_factory.py -v
```
Expected: FAIL with `AssertionError: seed_hybrid_factory.py does not exist`.

- [ ] **Step 3: Implement `apps/backend/scripts/seed_hybrid_factory.py`**

Write `apps/backend/scripts/seed_hybrid_factory.py`:
```python
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from uuid import NAMESPACE_DNS, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import Settings, get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import create_runtime_engine, create_session_factory, tenant_session
from erp_core.shared.enums import OnboardingStage, UserRole
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.security import hash_password

SEED_DEMO_PASSWORD = "FactoryPass2026!"

TENANT_A_ID = uuid5(NAMESPACE_DNS, "quanzhou-led-factory.erp.local")
TENANT_B_ID = uuid5(NAMESPACE_DNS, "shenzhen-audio-dtc.erp.local")


@dataclass(frozen=True)
class SeedSummary:
    tenant_a_id: UUID
    tenant_a_slug: str
    tenant_b_id: UUID
    tenant_b_slug: str


async def _upsert_tenant(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    slug: str,
    name: str,
    preset: str,
    currency: str,
    timezone: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO tenants (id, slug, name, business_preset, base_currency, timezone) "
            "VALUES (:id, :slug, :name, :preset, :currency, :tz) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  name = EXCLUDED.name, "
            "  business_preset = EXCLUDED.business_preset, "
            "  base_currency = EXCLUDED.base_currency, "
            "  timezone = EXCLUDED.timezone, "
            "  is_active = true, "
            "  shredded_at = NULL, "
            "  updated_at = now()"
        ),
        {
            "id": str(tenant_id),
            "slug": slug,
            "name": name,
            "preset": preset,
            "currency": currency,
            "tz": timezone,
        },
    )


async def _upsert_user_and_membership(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    email: str,
    full_name: str,
    role: UserRole,
    password_hash: str,
) -> UUID:
    user_id = uuid5(NAMESPACE_DNS, f"user:{email}")
    saved_user_id = await session.scalar(
        text(
            "INSERT INTO users (id, email, full_name, password_hash, is_active) "
            "VALUES (:id, :email, :fn, :ph, true) "
            "ON CONFLICT (email) DO UPDATE SET "
            "  full_name = EXCLUDED.full_name, "
            "  password_hash = EXCLUDED.password_hash, "
            "  is_active = true, "
            "  updated_at = now() "
            "RETURNING id"
        ),
        {
            "id": str(user_id),
            "email": email,
            "fn": full_name,
            "ph": password_hash,
        },
    )
    resolved_user_id = UUID(str(saved_user_id))
    membership_id = uuid5(NAMESPACE_DNS, f"membership:{tenant_id}:{resolved_user_id}")
    await session.execute(
        text(
            "INSERT INTO tenant_memberships (id, tenant_id, user_id, role, is_active) "
            "VALUES (:mid, :tid, :uid, :role, true) "
            "ON CONFLICT (tenant_id, user_id) DO UPDATE SET "
            "  role = EXCLUDED.role, "
            "  is_active = true"
        ),
        {
            "mid": str(membership_id),
            "tid": str(tenant_id),
            "uid": str(resolved_user_id),
            "role": role.value,
        },
    )
    return resolved_user_id


async def _upsert_encrypted_credential(
    session: AsyncSession,
    crypto: EnvelopeCryptoService,
    *,
    tenant_id: UUID,
    account_id: UUID,
    cred_type: str,
    plaintext_secret: str,
) -> None:
    ciphertext, nonce, auth_tag, key_version = await crypto.encrypt_secret(
        session, tenant_id, account_id, cred_type, plaintext_secret
    )
    cred_id = uuid5(NAMESPACE_DNS, f"cred:{tenant_id}:{account_id}:{cred_type}")
    await session.execute(
        text(
            "INSERT INTO channel_credentials "
            "(id, tenant_id, channel_account_id, credential_type, ciphertext, nonce, auth_tag, key_version) "
            "VALUES (:id, :tid, :aid, :ctype, :ct, :nonce, :tag, :kv) "
            "ON CONFLICT (tenant_id, channel_account_id, credential_type) DO UPDATE SET "
            "  ciphertext = EXCLUDED.ciphertext, "
            "  nonce = EXCLUDED.nonce, "
            "  auth_tag = EXCLUDED.auth_tag, "
            "  key_version = EXCLUDED.key_version, "
            "  updated_at = now()"
        ),
        {
            "id": str(cred_id),
            "tid": str(tenant_id),
            "aid": str(account_id),
            "ctype": cred_type,
            "ct": ciphertext,
            "nonce": nonce,
            "tag": auth_tag,
            "kv": key_version,
        },
    )


async def seed_hybrid_factory(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> SeedSummary:
    crypto = EnvelopeCryptoService(settings)
    password_hash = hash_password(SEED_DEMO_PASSWORD)

    # Tenant A: Quanzhou Smart LED Hybrid Factory (5 RBAC roles + 3 channel credentials)
    async with tenant_session(session_factory, TENANT_A_ID) as session:
        await _upsert_tenant(
            session,
            tenant_id=TENANT_A_ID,
            slug="quanzhou-led-factory",
            name="Quanzhou Smart LED Co., Ltd.",
            preset="hybrid_factory_b2b_b2c",
            currency="USD",
            timezone="Asia/Shanghai",
        )
        await crypto.provision_tenant_dek(session, TENANT_A_ID)

        tenant_a_users = [
            ("owner@quanzhou-led.example.com", "Jerry Lin", UserRole.OWNER),
            ("admin@quanzhou-led.example.com", "Alice Chen", UserRole.ADMIN),
            ("manager@quanzhou-led.example.com", "Bob Wu", UserRole.MANAGER),
            ("agent@quanzhou-led.example.com", "Carol Huang", UserRole.AGENT),
            ("finance@quanzhou-led.example.com", "David Zheng", UserRole.FINANCE),
        ]
        owner_uid = TENANT_A_ID
        for email, full_name, role in tenant_a_users:
            uid = await _upsert_user_and_membership(
                session,
                tenant_id=TENANT_A_ID,
                email=email,
                full_name=full_name,
                role=role,
                password_hash=password_hash,
            )
            if role == UserRole.OWNER:
                owner_uid = uid

        await _upsert_encrypted_credential(
            session,
            crypto,
            tenant_id=TENANT_A_ID,
            account_id=uuid5(NAMESPACE_DNS, "quanzhou-wa-coexistence"),
            cred_type="wa_oauth_token",
            plaintext_secret="EAA_DEMO_QUANZHOU_WHATSAPP_COEXISTENCE_TOKEN",
        )
        await _upsert_encrypted_credential(
            session,
            crypto,
            tenant_id=TENANT_A_ID,
            account_id=uuid5(NAMESPACE_DNS, "quanzhou-shopify"),
            cred_type="shopify_oauth_token",
            plaintext_secret="shpat_demo_quanzhou_led_shopify_token",
        )
        await _upsert_encrypted_credential(
            session,
            crypto,
            tenant_id=TENANT_A_ID,
            account_id=uuid5(NAMESPACE_DNS, "quanzhou-email"),
            cred_type="resend_api_key",
            plaintext_secret="re_demo_quanzhou_led_resend_key",
        )

        completed = [
            OnboardingStage.WORKSPACE_CREATED.value,
            OnboardingStage.CHANNEL_CONNECTING.value,
            OnboardingStage.CHANNEL_VERIFIED.value,
            OnboardingStage.CATALOG_SYNCING.value,
            OnboardingStage.AI_SANDBOX_READY.value,
            OnboardingStage.LIVE_PRODUCTION.value,
        ]
        await session.execute(
            text(
                "INSERT INTO onboarding_states (tenant_id, current_stage, preset_type, completed_steps, metadata_json) "
                "VALUES (:tid, :stage, 'hybrid_factory_b2b_b2c', CAST(:steps AS jsonb), CAST(:meta AS jsonb)) "
                "ON CONFLICT (tenant_id) DO UPDATE SET "
                "  current_stage = EXCLUDED.current_stage, "
                "  preset_type = EXCLUDED.preset_type, "
                "  completed_steps = EXCLUDED.completed_steps, "
                "  metadata_json = EXCLUDED.metadata_json, "
                "  updated_at = now()"
            ),
            {
                "tid": str(TENANT_A_ID),
                "stage": OnboardingStage.LIVE_PRODUCTION.value,
                "steps": json.dumps(completed),
                "meta": json.dumps({"seeded": True, "factory_city": "Quanzhou"}),
            },
        )
        await record_audit_event(
            session,
            TENANT_A_ID,
            "system_cron",
            str(owner_uid),
            "seed.hybrid_factory_applied",
            "tenant",
            TENANT_A_ID,
            after_state={"slug": "quanzhou-led-factory", "roles": 5},
        )

    # Tenant B: Shenzhen Audio DTC Ltd. (Isolation benchmark tenant)
    async with tenant_session(session_factory, TENANT_B_ID) as session:
        await _upsert_tenant(
            session,
            tenant_id=TENANT_B_ID,
            slug="shenzhen-audio-dtc",
            name="Shenzhen Audio DTC Ltd.",
            preset="dtc_brand",
            currency="USD",
            timezone="Asia/Shanghai",
        )
        await crypto.provision_tenant_dek(session, TENANT_B_ID)
        await _upsert_user_and_membership(
            session,
            tenant_id=TENANT_B_ID,
            email="owner@shenzhen-audio.example.com",
            full_name="Kevin Zhou",
            role=UserRole.OWNER,
            password_hash=password_hash,
        )
        await _upsert_encrypted_credential(
            session,
            crypto,
            tenant_id=TENANT_B_ID,
            account_id=uuid5(NAMESPACE_DNS, "shenzhen-shopify"),
            cred_type="shopify_oauth_token",
            plaintext_secret="shpat_demo_shenzhen_audio_shopify_token",
        )
        await session.execute(
            text(
                "INSERT INTO onboarding_states (tenant_id, current_stage, preset_type, completed_steps, metadata_json) "
                "VALUES (:tid, :stage, 'dtc_brand', CAST(:steps AS jsonb), CAST(:meta AS jsonb)) "
                "ON CONFLICT (tenant_id) DO UPDATE SET "
                "  current_stage = EXCLUDED.current_stage, "
                "  preset_type = EXCLUDED.preset_type, "
                "  completed_steps = EXCLUDED.completed_steps, "
                "  updated_at = now()"
            ),
            {
                "tid": str(TENANT_B_ID),
                "stage": OnboardingStage.CHANNEL_VERIFIED.value,
                "steps": json.dumps(
                    [
                        OnboardingStage.WORKSPACE_CREATED.value,
                        OnboardingStage.CHANNEL_CONNECTING.value,
                        OnboardingStage.CHANNEL_VERIFIED.value,
                    ]
                ),
                "meta": json.dumps({"seeded": True}),
            },
        )

    return SeedSummary(
        tenant_a_id=TENANT_A_ID,
        tenant_a_slug="quanzhou-led-factory",
        tenant_b_id=TENANT_B_ID,
        tenant_b_slug="shenzhen-audio-dtc",
    )


async def _cli_main() -> None:
    settings = get_settings()
    engine = create_runtime_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        summary = await seed_hybrid_factory(session_factory, settings)
        print(
            f"Seeded Tenant A ({summary.tenant_a_slug}: {summary.tenant_a_id}) "
            f"and Tenant B ({summary.tenant_b_slug}: {summary.tenant_b_id})"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_cli_main())
```

- [ ] **Step 4: Run seed tests, execute CLI seed script twice, and run full Plan 01 verification suite**

Run:
```bash
uv run python apps/backend/scripts/seed_hybrid_factory.py
uv run python apps/backend/scripts/seed_hybrid_factory.py
uv run pytest apps/backend/tests/ -v
uv run lint-imports --config infra/importlinter.ini
uv run ruff check apps/backend
uv run mypy apps/backend/src
```
Expected: Seed CLI runs cleanly twice; full `pytest` suite (`16 passed`) + `import-linter` + `ruff` + `mypy` all pass with zero errors.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/scripts/seed_hybrid_factory.py apps/backend/tests/test_seed_hybrid_factory.py
git commit -m "feat(seed): add idempotent Quanzhou Smart LED Hybrid Factory and Tenant B isolation seed"
```

---

## Plan Self-Review Checklist (Passed)
- All 7 Plan 01 table names (`tenants`, `users`, `tenant_memberships`, `tenant_keys`, `channel_credentials`, `onboarding_states`, monthly-partitioned `audit_events`) match `2026-11-19-00-phase1-2-master-plan-index.md` verbatim.
- All 27 canonical `StrEnum` classes and exact lowercase/canonical string values match Section 3.2 verbatim.
- Authoritative Python signatures (`tenant_session`, `system_session`, `EnvelopeCryptoService`, `CurrentPrincipal`, `require_roles`, `record_audit_event`) match Section 3.3 verbatim.
- Zero `TBD` / `TODO` / `pass` stubs; every step contains complete runnable code and exact test commands.
