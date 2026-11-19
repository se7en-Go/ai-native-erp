# Repository Agent Engineering Contract (`ai-native-erp`)

> **Applies to all AI & human contributors** (Grok Cloud Agent, Muse, Cursor, Codex, Antigravity).

## 1. Single Source of Truth (Read First)

Before writing or editing ANY code, read:
- **Master Plan & Shared Contract Registry**: [`docs/superpowers/plans/2026-11-19-00-phase1-2-master-plan-index.md`](docs/superpowers/plans/2026-11-19-00-phase1-2-master-plan-index.md)
- **System Architecture Spec**: [`docs/superpowers/specs/2026-11-18-omnichannel-erp-deep-research-and-architecture.md`](docs/superpowers/specs/2026-11-18-omnichannel-erp-deep-research-and-architecture.md)

Do **not** rename or drift from any file path, environment variable, database table/column name, `StrEnum` token, Alembic revision ID (`0001_tenancy_rls_and_audit -> 0002_channels_identity_inbox -> 0003 -> 0004`), Python/TypeScript interface signature, or `/api/v1/*` route locked in `00-phase1-2-master-plan-index.md`.

## 2. Parallel Agent Directory Ownership (Zero Merge Conflicts)

- **Backend Agent** (`Plan 01`–`04`): Owns `apps/backend/**`, `infra/**`, `contracts/openapi.json`, `pyproject.toml`, `pnpm-workspace.yaml`, `.env.example`, `.github/workflows/**`.
- **Frontend & Widget Agent** (`Task 2.7` widget + `Plan 05`): Owns `apps/widget/**`, `apps/web/**`, `tests/e2e/**`. Before backend OpenAPI exports land on `main`, isolate frontend mocks strictly under `apps/web/src/lib/mock-fixtures/`.
- **Compliance / Eval Agent** (if active): Owns `docs/compliance/**` and `apps/backend/tests/fixtures/hybrid_factory_eval_100.json`.

Never modify files outside your assigned ownership boundary unless explicitly instructed.

## 3. Non-Negotiable Architecture & Security Invariants

1. **Multi-Tenant Fail-Closed RLS**: Every tenant table uses PostgreSQL 17 `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` with `RESTRICTIVE` `tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid`. Runtime connections run ONLY as non-superuser `app_runtime` through PgBouncer `pool_mode = transaction` using `SELECT set_config('app.current_tenant_id', :tid, true)`. Never downgrade RLS integration tests to SQLite or in-memory mocks.
2. **Modular Monolith Boundaries**: Cross-module imports inside `apps/backend/src/erp_core/` are governed by `infra/importlinter.ini`.
3. **Zero Business SQL in Next.js**: `apps/web` is strictly a UI + thin Auth/SSE BFF shell consuming `@hey-api/openapi-ts` generated clients. Never import ORMs or DB drivers in `apps/web`.
4. **Envelope Encryption**: Encrypt all channel OAuth tokens, IMAP/SMTP secrets, and BYOK keys using `EnvelopeCryptoService` (AES-256-GCM bound to `AAD = f"{tenant_id}:{account_id}:{cred_type}"`).
5. **Financial Precision**: All money, FX rates, quotes, COGS, and CVP margins use Python `decimal.Decimal` (`ROUND_HALF_UP`) and PostgreSQL `NUMERIC`, never IEEE-754 `float`.
6. **Zero-Message-Loss & Channel Compliance**: Webhooks verify raw-body HMAC signatures, persist `raw_webhook_events` (`ON CONFLICT DO NOTHING`), push to Redis Streams, and return `HTTP 200` in `<100ms`. Enforce Redis Lua dual token buckets, `STOP` zero-delay suppression, and `Tracking17TrackAdapter` `cacheLevel=0` strictly.
7. **Zero-Hallucination AI Grounding**: Live prices, MOQs, stock, orders, and tracking come 100% from deterministic SQL tools (`ErpToolRegistry`) with `source_tool_call_id` verification—never static RAG.
8. **Strict TDD & Zero Placeholders**: Follow Red -> Green -> atomic commit per Task. No `TODO`, `TBD`, stub `pass` bodies, or skipped assertions. Report only verified commands and real outputs.
