# Phase 1–2 Master Implementation Plan Index & Shared Contract Registry (Weeks 1–8 Core Kernel)

> **For agentic workers (Grok Bot / Superpowers):** REQUIRED SUB-SKILL: Execute task-by-task using strict TDD (`test-driven-development`). Every task in Plans `01` through `05` MUST conform 100% verbatim to the file paths, database schemas, enums, Python/TypeScript interfaces, and API contracts locked in this document.

**Goal:** Deliver a production-grade, multi-tenant **B2C + B2B Hybrid Cross-Border Omnichannel AI Workbench & Lightweight ERP** Phase 1–2 core kernel in 8 weeks, tailored out-of-the-box for **工贸一体 B2B+B2C 混合卖家** (Quanzhou Smart LED Lighting Factory preset).

**Architecture:** `Next.js 15 (App Router, TypeScript)` thin UI/BFF shell + `Python 3.12 FastAPI` Modular Monolith + `Taskiq` Async Workers sharing `PostgreSQL 17 (FORCE RLS + pgvector HNSW + FTS)` via `PgBouncer (Transaction Pooling)`, `Redis 7+` (Streams, Lua dual token buckets, semantic cache), and `Cloudflare R2 / MinIO S3` (attachments, B2B PI PDFs, WORM audit exports).

**Tech Stack:** `Python >=3.12`, `uv`, `FastAPI >=0.115`, `Pydantic v2`, `SQLAlchemy 2.0 (asyncpg)`, `Alembic`, `Taskiq + taskiq-redis`, `WeasyPrint`, `nh3`, `phonenumbers`, `cryptography`, `import-linter`, `pytest + pytest-asyncio`, `Node >=22`, `pnpm >=9`, `Next.js 15 (App Router)`, `React 19`, `TanStack Query v5`, `@hey-api/openapi-ts`, `shadcn/ui`, `Tailwind CSS v4`, `Vite (WebChat Widget)`, `Playwright`.

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

## 1. Subsystem Plan Suite & 8-Week Execution DAG

| Plan File | Scope & Sprint Weeks | Tasks | Primary Deliverables |
| :--- | :--- | :---: | :--- |
| [2026-11-19-01-foundation-multitenant-rls-and-auth.md](file:///Users/seven/Documents/erp/docs/superpowers/plans/2026-11-19-01-foundation-multitenant-rls-and-auth.md) | **Plan 01 (Weeks 1–2)**<br/>Monorepo, RLS, Crypto, Auth, Onboarding | `1.1`–`1.8` | Docker/PgBouncer infra, PG17 `FORCE RLS`, `EnvelopeCryptoService`, 5-role RBAC, immutable `audit_events`, GDPR flows, OpenAPI-TS pipeline, Hybrid LED Factory seed data |
| [2026-11-19-02-omnichannel-inbox-and-p0-channels.md](file:///Users/seven/Documents/erp/docs/superpowers/plans/2026-11-19-02-omnichannel-inbox-and-p0-channels.md) | **Plan 02 (Weeks 2–4)**<br/>Inbox Core, Identity Graph & P0 Channels | `2.1`–`2.8` | `<100ms` Webhook Ingress + DLQ, Redis Lua dual token bucket, Identity Graph (`bsuid` + merge/split + `buyer_local_time`), WhatsApp Cloud + Coexistence, Zero-CASA Email + Made-in-China/Alibaba parser, `<35KB` WebChat Widget, Inbox REST/SSE API |
| [2026-11-19-03-dual-mode-erp-and-p0-ecommerce.md](file:///Users/seven/Documents/erp/docs/superpowers/plans/2026-11-19-03-dual-mode-erp-and-p0-ecommerce.md) | **Plan 03 (Weeks 4–6)**<br/>Dual-Mode ERP, 17TRACK & P0 Storefronts | `3.1`–`3.9` | Catalog & SKU mapping (`bundle_multiplier`), B2B Pipeline & Versioned PI PDF (`WeasyPrint`) + `1-Click` convert, Atomic `inventory_ledger`, 17TRACK (`cacheLevel=0`) + 72h alert, 30%/70% finance milestones + CVP `order_profit_snapshots`, Shopify GraphQL + Woo + SHOPLINE + Shoplazza + TikTok Shop Core + 15-min Reconciliation Cron |
| [2026-11-19-04-ai-engine-rag-governance-and-metering.md](file:///Users/seven/Documents/erp/docs/superpowers/plans/2026-11-19-04-ai-engine-rag-governance-and-metering.md) | **Plan 04 (Weeks 6–7)**<br/>Hybrid RAG, 6 AI Capabilities, L0–L2 & Metering | `4.1`–`4.8` | Hybrid BM25 + `pgvector` HNSW + Rerank + Strict Citations, Live SQL `ErpToolRegistry` + `ToolGroundedNumericGuard`, 4-tier model router + semantic cache, 6 capabilities + `ai_proposals` queue + `ai_eval_feedback`, `credit_wallets` + L0 graceful degradation, 100-conversation replay eval harness |
| [2026-11-19-05-nextjs-workbench-ui-and-e2e.md](file:///Users/seven/Documents/erp/docs/superpowers/plans/2026-11-19-05-nextjs-workbench-ui-and-e2e.md) | **Plan 05 (Weeks 7–8)**<br/>Next.js Workbench, Onboarding & E2E | `5.1`–`5.8` | Thin BFF shell + SSE bus + RBAC UI guards, 4-column `/inbox` + local timezone + translation + floating AI drawer, `/ai/proposals` & L0–L2 console, `/catalog` & `/b2b/quotes`, `/orders` & `/finance` CVP dashboard, `/onboarding` Hybrid Factory wizard, full Playwright E2E suite |

```mermaid
flowchart LR
    P1["Plan 01: Foundation, RLS, Crypto & OpenAPI Pipeline (Weeks 1-2)"]
    P2["Plan 02: Zero-Loss Pipeline, Identity Graph & P0 Channels (Weeks 2-4)"]
    P3["Plan 03: B2C+B2B Light ERP, 17TRACK & P0 Storefronts (Weeks 4-6)"]
    P4["Plan 04: Hybrid RAG, 6 AI Capabilities, L0-L2 & Metering (Weeks 6-7)"]
    P5["Plan 05: Next.js Workbench UI, Onboarding & E2E (Weeks 7-8)"]

    P1 --> P2
    P1 --> P3
    P2 --> P3
    P2 --> P4
    P3 --> P4
    P4 --> P5
    P3 --> P5
```

---

## 2. Canonical Repository File Tree

```text
erp/
├── .env.example
├── .github/
│   └── workflows/
│       └── ci.yml
├── pnpm-workspace.yaml
├── pyproject.toml                           # uv workspace / root ruff + mypy config
├── contracts/
│   └── openapi.json                         # Exported by FastAPI; CI enforced
├── docs/
│   ├── research/01..05-*.md                 # Completed primary-source research
│   └── superpowers/
│       ├── specs/2026-11-18-omnichannel-erp-deep-research-and-architecture.md
│       └── plans/2026-11-19-00..05-*.md
├── infra/
│   ├── docker-compose.yml                   # PG17(pgvector) + PgBouncer + Redis + MinIO
│   ├── pgbouncer/pgbouncer.ini              # pool_mode = transaction
│   ├── postgres/init-roles.sql              # Creates app_migrator & app_runtime
│   └── importlinter.ini                     # Module boundary contracts
├── apps/
│   ├── backend/
│   │   ├── pyproject.toml
│   │   ├── alembic.ini
│   │   ├── alembic/
│   │   │   ├── env.py
│   │   │   └── versions/
│   │   ├── scripts/
│   │   │   ├── export_openapi.py
│   │   │   ├── seed_hybrid_factory.py
│   │   │   └── run_ai_evals.py
│   │   ├── src/erp_core/
│   │   │   ├── __init__.py
│   │   │   ├── api_main.py
│   │   │   ├── worker_main.py
│   │   │   ├── shared/
│   │   │   │   ├── config.py
│   │   │   │   ├── db.py                    # tenant_session, system_session
│   │   │   │   ├── crypto.py                # EnvelopeCryptoService
│   │   │   │   ├── redis_client.py
│   │   │   │   ├── storage.py               # R2 / MinIO S3 client
│   │   │   │   ├── errors.py                # AppError + RFC 7807 ProblemDetail
│   │   │   │   ├── enums.py                 # Canonical StrEnums
│   │   │   │   └── pagination.py
│   │   │   ├── tenancy/                     # Auth, RBAC, Memberships, Audit, GDPR, Onboarding
│   │   │   ├── channels/                    # Routing, Webhooks, Rate Limiter, WA/Email/WebChat/Ecom Adapters
│   │   │   ├── identity/                    # Customers, Identities, Merge/Split, Local Timezone
│   │   │   ├── catalog/                     # Products, Variants, Platform SKU Mappings
│   │   │   ├── crm/                         # Companies, Leads, Opportunities, Quotes/PI PDF
│   │   │   ├── orders/                      # Orders, Atomic Inventory Ledger, 17TRACK Shipments
│   │   │   ├── finance/                     # Receivables/Payables, Payment Slips, CVP Snapshots
│   │   │   ├── ai_engine/                   # Hybrid RAG, Tool Registry, Router, 6 Capabilities, Proposals
│   │   │   └── billing/                     # Credit Wallets, Usage Ledger, Stripe Meter Staging
│   │   └── tests/
│   │       ├── conftest.py
│   │       ├── fixtures/hybrid_factory_eval_100.json
│   │       └── ... (unit, integration, concurrency, rls suites)
│   ├── widget/                              # <35KB Vite Shadow DOM Web LiveChat Bundle + Shopify Embed Liquid
│   │   ├── package.json
│   │   ├── vite.config.ts
│   │   ├── src/
│   │   │   ├── index.ts
│   │   │   ├── widget-app.ts
│   │   │   └── api.ts
│   │   └── shopify-theme-extension/blocks/livechat-embed.liquid
│   └── web/                                 # Next.js 15 App Router UI + thin Auth/SSE BFF
│       ├── package.json
│       ├── tsconfig.json
│       ├── openapi-ts.config.ts
│       ├── eslint.config.mjs                # Forbids DB/SQL imports in web
│       └── src/
│           ├── app/
│           │   ├── api/bff/[...path]/route.ts
│           │   ├── (auth)/login/page.tsx
│           │   ├── onboarding/page.tsx
│           │   ├── inbox/page.tsx
│           │   ├── ai/proposals/page.tsx
│           │   ├── ai/capabilities/page.tsx
│           │   ├── catalog/page.tsx
│           │   ├── b2b/quotes/page.tsx
│           │   ├── orders/page.tsx
│           │   ├── finance/page.tsx
│           │   └── settings/channels/page.tsx
│           ├── components/
│           ├── hooks/
│           └── lib/
│               ├── api-client/              # Auto-generated by @hey-api/openapi-ts
│               ├── auth-cookie.ts
│               └── i18n.ts
└── tests/
    └── e2e/                                 # Playwright full-stack Hybrid Factory verification
        ├── package.json
        ├── playwright.config.ts
        └── specs/
            ├── hybrid-b2b-quote-to-cvp.spec.ts
            ├── hybrid-b2c-wismo-and-rbac.spec.ts
            └── onboarding-smoke.spec.ts
```

---

## 3. Shared Contract Registry (Authoritative — Zero Drift Permitted)

### 3.1 Ports & Environment Variables (`.env.example`)

| Service / Variable | Value / Format | Purpose |
| :--- | :--- | :--- |
| PostgreSQL 17 (`pgvector/pgvector:pg17`) | Host port `5432` | Primary relational + vector + FTS DB |
| PgBouncer | Host port `6432` | `pool_mode = transaction` for `app_runtime` |
| Redis 7 | Host port `6379` | Streams, Lua rate limiters, semantic cache |
| MinIO (S3/R2 local emulator) | API `9000`, Console `9001` | Attachments, PI PDFs, GDPR ZIPs, WORM exports |
| FastAPI Backend | Host port `8000` | REST `/api/v1/*`, Webhooks, SSE |
| Next.js Web App | Host port `3000` | Workbench UI + thin BFF |
| WebChat Widget Dev Server | Host port `3001` | Standalone `<script>` bundle dev & Shopify embed |
| `DATABASE_MIGRATOR_URL` | `postgresql+asyncpg://app_migrator:...@localhost:5432/erp_saas` | Direct PostgreSQL connection for Alembic only |
| `DATABASE_RUNTIME_URL` | `postgresql+asyncpg://app_runtime:...@localhost:6432/erp_saas` | PgBouncer transaction pool for FastAPI & Taskiq |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis Streams + Taskiq broker + Lua buckets |
| `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION` | `http://localhost:9000`, `erp-assets` | MinIO locally / Cloudflare R2 in production |
| `KMS_PROVIDER`, `MASTER_KEK_HEX` | `local_aes` (dev) / `aws_kms` (prod); 64-hex chars | Wraps per-tenant DEKs |
| `JWT_SECRET`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS` | `HS256`, `30`, `14` | User & widget session signing |
| `WHATSAPP_APP_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` | Meta App credentials | Cloud API + Embedded Signup v4 |
| `EMAIL_PROVIDER`, `RESEND_API_KEY`, `RESEND_WEBHOOK_SECRET`, `INBOUND_EMAIL_DOMAIN` | `resend`, `inbound.mail.example.com` | Zero-CASA inbound/outbound email |
| `TRACK17_API_KEY`, `TRACK17_WEBHOOK_SECRET` | 17TRACK API v2.2 keys | Shipment registration & webhook verify |
| `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` | Shopify Dev Dashboard app keys | Public App OAuth & Webhook HMAC |
| `TIKTOK_SHOP_APP_KEY`, `TIKTOK_SHOP_APP_SECRET` | TTSPC App credentials | Core orders & fulfillment |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` | Provider keys | Tiered LLM routing & embeddings |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Stripe API keys | Billing meter staging (Plan 04) |
| `NEXT_PUBLIC_BACKEND_SSE_URL`, `BFF_BACKEND_URL` | `http://localhost:8000` | Next.js BFF & SSE routing |

### 3.2 Canonical Database Tables & Enums (`apps/backend/src/erp_core/shared/enums.py`)

**All Python `StrEnum` definitions and PostgreSQL `CHECK` constraints MUST use these exact lowercase string tokens:**

* **`UserRole`**: `owner`, `admin`, `manager`, `agent`, `finance`
* **`OnboardingStage`**: `WorkspaceCreated`, `ChannelConnecting`, `ChannelVerified`, `CatalogSyncing`, `AISandboxReady`, `LiveProduction`
* **`ChannelType`**: `whatsapp_cloud`, `whatsapp_coexistence`, `email`, `web_livechat`, `shopify`, `woocommerce`, `shopline`, `shoplazza`, `tiktok_shop`, `made_in_china_email`, `alibaba_email`, `messenger`, `instagram_dm`, `telegram`, `amazon_sp`, `ebay`, `mercado_libre`, `temu`, `walmart`, `shopee`, `lazada`
* **`ChannelStatus`**: `active`, `token_expired`, `rate_limited`, `disconnected`
* **`ConversationMode`**: `b2c_support`, `b2b_inquiry`, `mixed`
* **`ConversationStatus`**: `open`, `pending_human`, `snoozed`, `resolved`
* **`MessageDirection`**: `inbound`, `outbound`, `internal_note`
* **`MessageSenderType`**: `customer`, `human_agent`, `ai_agent`, `system`
* **`MessageContentType`**: `text`, `rich_html`, `template`, `media`, `quote_card`, `order_card`
* **`DeliveryStatus`**: `queued`, `sent`, `delivered`, `read`, `failed`
* **`IdentityType`**: `email`, `phone_e164`, `whatsapp_waid`, `whatsapp_bsuid`, `ig_psid`, `fb_psid`, `shopify_customer_id`, `tiktok_buyer_id`, `amazon_buyer_email`, `web_visitor_id`
* **`ConfidenceLevel`**: `deterministic`, `human_verified`, `probabilistic`
* **`CustomerType`**: `b2c_consumer`, `b2b_contact`, `b2b_company_rep`
* **`LeadStatus`**: `new`, `contacted`, `qualified`, `disqualified`, `converted`
* **`OpportunityStage`**: `discovery`, `sampling`, `quoting`, `negotiating`, `closed_won`, `closed_lost`
* **`Incoterm`**: `EXW`, `FOB`, `CIF`, `DDP`, `DAP`
* **`QuoteStatus`**: `draft`, `pending_approval`, `sent`, `accepted`, `expired`, `converted`
* **`OrderMode`**: `b2c_retail`, `b2b_sample`, `b2b_wholesale`
* **`FulfillmentStatus`**: `pending_payment`, `confirmed_reserved`, `in_production`, `partially_shipped`, `shipped`, `delivered`, `exception`, `cancelled`, `refunded`
* **`PaymentStatus`**: `unpaid`, `deposit_paid`, `partially_paid`, `paid`, `refunded`
* **`InventoryEventType`**: `inbound_po`, `reserve_order`, `release_cancel`, `deduct_shipped`, `stocktake_adjust`
* **`TrackingProvider`**: `17track`, `aftership`, `manual`
* **`ProposalType`**: `reply_draft`, `crm_entity_extract`, `quote_draft`, `follow_up_suggestion`, `identity_merge`, `refund_or_reship`, `payment_match`
* **`ProposalStatus`**: `pending`, `approved`, `edited_and_approved`, `rejected`, `expired`, `failed`
* **`AutonomyLevel`**: `L0_copilot`, `L1_proposal`, `L2_auto`
* **`AiCapability`**: `cs_support`, `sales_quoting`, `order_logistics`, `content_studio`, `biz_analytics`, `finance_recon`
* **`UsageMetricType`**: `ai_credit`, `whatsapp_template`, `whatsapp_service`, `tracking_quota`

**Authoritative Table Registry (35 Tables across 4 Migrations):**

| Migration Owner | Table Name | RLS Enabled + Forced? | Primary Key / Unique Constraints |
| :--- | :--- | :---: | :--- |
| Plan 01 | `tenants` | **Yes** (`id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid`) | `id UUID PK`, `slug TEXT UNIQUE` |
| Plan 01 | `users` | No (Global auth user; joined via `tenant_memberships`) | `id UUID PK`, `email TEXT UNIQUE` |
| Plan 01 | `tenant_memberships` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, user_id)` |
| Plan 01 | `tenant_keys` | **Yes** (`tenant_id`) | `tenant_id UUID PK` |
| Plan 01 | `channel_credentials` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, channel_account_id, credential_type)` |
| Plan 01 | `onboarding_states` | **Yes** (`tenant_id`) | `tenant_id UUID PK` |
| Plan 01 | `audit_events` *(Partitioned)* | **Yes** (`tenant_id`) | `PK (tenant_id, id, created_at)` (range partitioned monthly on `created_at`) |
| Plan 02 | `channel_routing_directory` | No (Non-PII webhook lookup via `SECURITY DEFINER` fn) | `PRIMARY KEY (channel_type, external_account_id)` |
| Plan 02 | `channel_accounts` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, channel_type, external_account_id)` |
| Plan 02 | `raw_webhook_events` | No (Ingress table; `tenant_id` nullable until resolved; accessed via `system_session` / scoped query) | `id UUID PK`, `UNIQUE (channel_type, external_event_id)` |
| Plan 02 | `dead_letter_events` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 02 | `b2b_companies` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 02 | `customers` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 02 | `customer_identities` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, identity_type, identity_value)` |
| Plan 02 | `identity_merge_events` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 02 | `conversations` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 02 | `messages` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, channel_type, external_message_id) WHERE external_message_id IS NOT NULL` |
| Plan 02 | `consent_records` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 02 | `suppression_list` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, channel_type, normalized_address)` |
| Plan 03 | `products` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, spu_code)` |
| Plan 03 | `product_variants` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, sku)` |
| Plan 03 | `platform_sku_mappings` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, channel_account_id, external_sku)` |
| Plan 03 | `leads` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 03 | `opportunities` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 03 | `quotes` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, quote_no, version)` |
| Plan 03 | `quote_items` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 03 | `orders` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, order_no)` |
| Plan 03 | `order_items` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 03 | `shipments` | **Yes** (`tenant_id`) | `id UUID PK`, `UNIQUE (tenant_id, tracking_number)` |
| Plan 03 | `inventory_balances` | **Yes** (`tenant_id`) | `PK (tenant_id, warehouse_id, variant_id)` |
| Plan 03 | `inventory_ledger` | **Yes** (`tenant_id`) | `PK (tenant_id, id)`, `UNIQUE (tenant_id, idempotency_key)` |
| Plan 03 | `receivables_payables` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 03 | `payment_records` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 03 | `order_profit_snapshots` | **Yes** (`tenant_id`) | `order_id UUID PK` |
| Plan 04 | `kb_documents` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 04 | `kb_chunks` | **Yes** (`tenant_id`) | `id UUID PK` (`tsv` GIN + `embedding vector(1536)` HNSW) |
| Plan 04 | `ai_proposals` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 04 | `ai_eval_feedback` | **Yes** (`tenant_id`) | `id UUID PK` |
| Plan 04 | `credit_wallets` | **Yes** (`tenant_id`) | `tenant_id UUID PK` |
| Plan 04 | `usage_ledger` | **Yes** (`tenant_id`) | `PK (tenant_id, id)`, `UNIQUE (tenant_id, idempotency_key)` |

### 3.3 Authoritative Python Cross-Module Interface Signatures

All Plans `01`–`05` MUST consume and expose these exact module paths and signatures:

```python
# erp_core/shared/db.py
@asynccontextmanager
async def tenant_session(session_factory: async_sessionmaker[AsyncSession], tenant_id: UUID) -> AsyncIterator[AsyncSession]: ...
@asynccontextmanager
async def system_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]: ...

# erp_core/shared/crypto.py
class EnvelopeCryptoService:
    async def provision_tenant_dek(self, session: AsyncSession, tenant_id: UUID) -> int: ...
    async def encrypt_secret(self, session: AsyncSession, tenant_id: UUID, account_id: UUID, cred_type: str, plaintext: str) -> tuple[bytes, bytes, bytes, int]: ...
    async def decrypt_secret(self, session: AsyncSession, tenant_id: UUID, account_id: UUID, cred_type: str, ciphertext: bytes, nonce: bytes, auth_tag: bytes, key_version: int) -> str: ...
    async def crypto_shred_tenant(self, session: AsyncSession, tenant_id: UUID) -> None: ...

# erp_core/tenancy/dependencies.py
class CurrentPrincipal(BaseModel):
    user_id: UUID
    tenant_id: UUID
    role: UserRole
    email: str

def require_roles(*allowed_roles: UserRole) -> Callable[..., Awaitable[CurrentPrincipal]]: ...

# erp_core/tenancy/audit.py
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
) -> int: ...

# erp_core/channels/rate_limiter.py
class DualTokenBucketLimiter:
    async def acquire(self, channel_type: ChannelType, account_id: str, recipient_id: str | None = None, is_coexistence: bool = False) -> tuple[bool, int]: ...
    def compute_backoff_seconds(self, attempt: int, error_code: int | None = None) -> int: ...

# erp_core/identity/service.py
class IdentityResolverService:
    async def resolve_or_create_customer(self, session: AsyncSession, tenant_id: UUID, identity_type: IdentityType, raw_value: str, display_name: str, country_hint: str | None = None) -> tuple[UUID, bool]: ...
    async def merge_customers(self, session: AsyncSession, tenant_id: UUID, source_customer_id: UUID, target_customer_id: UUID, performed_by: str, reason: str) -> UUID: ...
    async def split_customer_identity(self, session: AsyncSession, tenant_id: UUID, merge_event_id: UUID, performed_by: str, reason: str) -> UUID: ...
    def compute_buyer_local_time(self, iana_timezone: str | None, now_utc: datetime | None = None) -> BuyerLocalTimeInfo: ...

# erp_core/orders/inventory_service.py
class InventoryLedgerService:
    async def reserve_for_order(self, session: AsyncSession, tenant_id: UUID, warehouse_id: UUID, order_id: UUID, items: list[tuple[UUID, int]], idempotency_prefix: str) -> list[int]: ...
    async def release_reservation(self, session: AsyncSession, tenant_id: UUID, warehouse_id: UUID, order_id: UUID, items: list[tuple[UUID, int]], idempotency_prefix: str) -> list[int]: ...
    async def deduct_shipped(self, session: AsyncSession, tenant_id: UUID, warehouse_id: UUID, shipment_id: UUID, items: list[tuple[UUID, int]], idempotency_prefix: str) -> list[int]: ...

# erp_core/orders/tracking_adapter.py
class Tracking17TrackAdapter:
    async def register_tracking(self, tracking_number: str, carrier_code: str | None = None) -> dict[str, Any]: ...
    async def fetch_status(self, tracking_number: str, cache_level: int = 0) -> dict[str, Any]: ...

# erp_core/finance/profit_service.py
class OrderProfitService:
    async def recalculate_order_profit(self, session: AsyncSession, tenant_id: UUID, order_id: UUID) -> OrderProfitSnapshotRead: ...

# erp_core/ai_engine/rag_service.py
class HybridRagService:
    async def search(self, session: AsyncSession, tenant_id: UUID, query: str, top_k: int = 5, strict_mode: bool = False) -> RagSearchResult: ...

# erp_core/ai_engine/tools.py
class ErpToolRegistry:
    async def get_live_sku_pricing_and_stock(self, session: AsyncSession, tenant_id: UUID, sku: str, qty: int = 1, incoterm: Incoterm = Incoterm.FOB) -> ToolResult: ...
    async def get_live_order_tracking(self, session: AsyncSession, tenant_id: UUID, order_no: str) -> ToolResult: ...
    async def calc_b2b_quote_matrix(self, session: AsyncSession, tenant_id: UUID, items: list[dict[str, Any]], currency: str, incoterm: Incoterm) -> ToolResult: ...

# erp_core/ai_engine/runtime.py
class PolicyRuntime:
    async def run_capability(self, session: AsyncSession, tenant_id: UUID, capability: AiCapability, input_payload: dict[str, Any], strict_rag: bool = False) -> CapabilityExecutionResult: ...

# erp_core/billing/wallet_service.py
class CreditWalletService:
    async def check_ai_execution_allowed(self, session: AsyncSession, tenant_id: UUID, estimated_credits: int = 1) -> tuple[bool, AutonomyLevel | None]: ...
    async def record_usage(self, session: AsyncSession, tenant_id: UUID, idempotency_key: str, metric_type: UsageMetricType, units: Decimal, raw_cost_usd: Decimal, provider: str) -> None: ...
```

### 3.4 Standard API Prefix, Pagination & Error Envelope

* **Base Route Prefix**: `/api/v1` (except `/healthz` and `/readyz`).
* **Pagination Response (`PaginatedResponse[T]`)**:
  `{"items": [...], "total": 42, "limit": 20, "offset": 0, "has_more": true}`
* **RFC 7807 Structured Error (`ProblemDetail`)**:
  `{"type": "https://erp.local/errors/<slug>", "title": "...", "status": 403, "detail": "...", "instance": "/api/v1/...", "code": "ERR_INSUFFICIENT_ROLE"}`

---

## 4. Grok Bot Delegation Protocol & Prompt Templates

### 4.1 Execution Order for Grok Bot

Feed **one Task at a time** (e.g., `Task 1.1`, then `Task 1.2`, ..., through `Task 5.8`) in strict dependency order. Never paste multiple plans into one Grok session simultaneously; start a fresh Grok context per Plan file (or every 3–4 Tasks) to keep context sharp.

For each delegation turn to Grok Bot, attach **2 files** + specify the **Target Task**:
1. Attach `docs/superpowers/plans/2026-11-19-00-phase1-2-master-plan-index.md` (Shared Contract Registry)
2. Attach the current subsystem plan file (`01` .. `05`)
3. Specify exact instruction: `"Implement Task X.Y only, following every step in order."`

### 4.2 Ready-to-Copy Grok Bot System Prompt

```text
你是本项目的高级全栈工程师。你必须严格按照我提供的 Master Plan Index（共享契约唯一真相源）与当前子模块 Plan 文件，以严格的 TDD（先写失败测试 -> 验证失败 -> 写最小完整可运行实现 -> 验证测试通过 -> 原子化 Git 提交）逐条执行指定的单一 Task（例如 Task 1.2）。

强制工程纪律：
1. 绝对不允许更改 Master Plan Index 中锁定的文件路径、数据库表名/列名、StrEnum 枚举值、Python/TS 接口签名和错误码。
2. 绝对不允许留下 TODO、TBD、pass 占位符、省略号 `...` 或假测试；所有 SQL、加密、限流、事务锁与 React 组件必须写全可运行代码。
3. 所有金额、汇率、毛利计算必须用 Python Decimal（禁止 float）；所有 Next.js 代码严禁直连数据库。
4. 若某一步修改了 FastAPI Pydantic Schema，必须在结束前运行 export_openapi.py 与 openapi-ts 同步前端类型。
5. 结束时只报告你实际运行过的测试/命令及其真实输出、改动文件清单和 Git commit hash；未运行的检查不得声称通过。
```

---

## 5. Day-1 Parallel External Compliance Checklist (Non-Blocking Track)

While Grok Bot executes Weeks 1–8 coding tasks locally, initiate these external partner approvals in **Week 1** so P1/P2 channels unlock without waiting:

- [ ] **Meta Business Portfolio**: Complete corporate verification + submit **Tech Provider** + **Embedded Signup v4/v5** screencast (`whatsapp_business_messaging`, `whatsapp_business_management`, `pages_messaging`, `instagram_manage_messages`, `instagram_manage_comments`).
- [ ] **Shopify Partner Dashboard**: Submit Public App shell with GraphQL-only architecture + **Protected Customer Data Level 2** declaration + 3 mandatory GDPR webhooks (`customers/data_request`, `customers/redact`, `shop/redact`).
- [ ] **TikTok Shop Partner Center**: Register Public App + US **DSPR** security questionnaire (use Custom App `<25` stores / `TikTok Shop Seller` BYOK during Phase 1–2 seed rollout).
- [ ] **Amazon SP-API (for P1)**: Submit Developer Profile + verify automated 30-day PII redaction & 7-day critical vulnerability SLA readiness.
- [ ] **Alibaba ICBU / 1688 (for P2, if entity >= 1 yr & >= 10 staff)**: Evaluate Aliyun Jushita cloud instance & 50,000 RMB service deposit readiness.
