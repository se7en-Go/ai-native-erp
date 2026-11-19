# Dual-Mode Lightweight ERP & P0 E-Commerce Connectors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the B2C + B2B dual-mode lightweight ERP core (multi-language catalog & SKU bundle mapping, versioned B2B Quotes & WeasyPrint Proforma Invoice PDFs with 1-Click order conversion, deadlock-free atomic `inventory_ledger`, 17TRACK `cacheLevel=0` logistics & 72h proactive WISMO alerting, 30%/70% finance milestones & `Decimal` CVP profit snapshots) and all P0 e-commerce connectors (Shopify GraphQL-only + Plus B2B + GDPR, WooCommerce REST v3, SHOPLINE, Shoplazza, TikTok Shop Core / BYOK, and 15-minute reconciliation cron).

**Architecture:** All domain logic resides inside isolated `erp_core` modular monolith packages (`catalog`, `crm`, `orders`, `finance`, `channels/ecommerce`) backed by PostgreSQL 17 with `FORCE ROW LEVEL SECURITY` + `RESTRICTIVE` tenant isolation policies behind PgBouncer `pool_mode = transaction`. Cross-module database table (`.models`) imports are strictly forbidden and enforced by `import-linter`; cross-module collaboration happens exclusively through typed service interfaces, Pydantic v2 DTOs, parameterized RLS-scoped reads, and Redis Stream events processed by Taskiq workers.

**Tech Stack:** `Python >=3.12`, `uv`, `FastAPI >=0.115`, `Pydantic v2`, `SQLAlchemy 2.0 (asyncpg)`, `Alembic`, `Taskiq + taskiq-redis`, `WeasyPrint`, `httpx`, `boto3`, `import-linter`, `pytest + pytest-asyncio`.

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

### Task 3.1: Alembic Migration `0003_dual_mode_erp_and_cvp.py` & Domain Models

**Files:**
- Create: `apps/backend/alembic/versions/0003_dual_mode_erp_and_cvp.py`
- Create: `apps/backend/src/erp_core/catalog/__init__.py`
- Create: `apps/backend/src/erp_core/catalog/models.py`
- Create: `apps/backend/src/erp_core/crm/__init__.py`
- Create: `apps/backend/src/erp_core/crm/models.py`
- Create: `apps/backend/src/erp_core/orders/__init__.py`
- Create: `apps/backend/src/erp_core/orders/models.py`
- Create: `apps/backend/src/erp_core/finance/__init__.py`
- Create: `apps/backend/src/erp_core/finance/models.py`
- Modify: `apps/backend/alembic/env.py` (import Plan 03 model modules onto `Base.metadata`)
- Test: `apps/backend/tests/test_erp_migration.py`

**Interfaces:**
- Consumes:
  - `erp_core.shared.db`: `Base`, `tenant_session(session_factory, tenant_id)`, `system_session(session_factory)`
  - `erp_core.shared.enums`: `LeadStatus`, `OpportunityStage`, `Incoterm`, `QuoteStatus`, `OrderMode`, `FulfillmentStatus`, `PaymentStatus`, `InventoryEventType`, `TrackingProvider`
  - Existing Plan 01 & Plan 02 tables: `tenants`, `channel_accounts`, `customers`, `b2b_companies`
- Produces:
  - Migration revision `"0003"` (`down_revision = "0002_channels_identity_inbox"`) creating all 15 ERP tables with `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, `RESTRICTIVE` `tenant_isolation_policy` + `PERMISSIVE` `tenant_allow_runtime` policies, `GENERATED ALWAYS AS (...) STORED` `net_contribution_margin_base`, and `CHECK (on_hand_qty >= reserved_qty)`
  - Isolated SQLAlchemy 2.0 ORM models in `catalog/models.py`, `crm/models.py`, `orders/models.py`, `finance/models.py`

> **Note for worker:** Before applying the migration, inspect `apps/backend/alembic/versions/0002_*.py` and verify its exact `revision` literal matches `down_revision` in `0003_dual_mode_erp_and_cvp.py`. Also verify `alembic/env.py` imports `catalog.models`, `crm.models`, `orders.models`, and `finance.models` alongside Plan 01/02 models.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_erp_migration.py`:

```python
from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.shared.db import tenant_session


def _migrator_url() -> str:
    return os.getenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_secret@localhost:5432/erp_saas",
    )


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )


ERP_TABLES = [
    "products",
    "product_variants",
    "platform_sku_mappings",
    "leads",
    "opportunities",
    "quotes",
    "quote_items",
    "orders",
    "order_items",
    "shipments",
    "inventory_balances",
    "inventory_ledger",
    "receivables_payables",
    "payment_records",
    "order_profit_snapshots",
]


@pytest.mark.asyncio
async def test_rls_enabled_and_forced_on_all_15_erp_tables() -> None:
    engine = create_async_engine(_migrator_url(), echo=False)
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = ANY(:tables)
                    """
                ),
                {"tables": ERP_TABLES},
            )
            rows = {row.relname: (row.relrowsecurity, row.relforcerowsecurity) for row in res}
            assert set(rows.keys()) == set(ERP_TABLES)
            for tbl, (rls_enabled, rls_forced) in rows.items():
                assert rls_enabled is True, f"{tbl} missing ENABLE ROW LEVEL SECURITY"
                assert rls_forced is True, f"{tbl} missing FORCE ROW LEVEL SECURITY"

            pol_res = await conn.execute(
                text(
                    """
                    SELECT tablename, policyname, permissive
                    FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename = ANY(:tables)
                      AND policyname = 'tenant_isolation_policy'
                    """
                ),
                {"tables": ERP_TABLES},
            )
            policies = {row.tablename: row.permissive for row in pol_res}
            assert set(policies.keys()) == set(ERP_TABLES)
            for tbl, permissive in policies.items():
                assert permissive == "RESTRICTIVE", f"{tbl} policy must be RESTRICTIVE"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cvp_generated_column_inventory_check_and_rls_isolation() -> None:
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    rt_engine = create_async_engine(_runtime_url(), echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_a = uuid4()
    tenant_b = uuid4()
    customer_a = uuid4()
    customer_b = uuid4()
    product_a = uuid4()
    variant_a = uuid4()
    warehouse_a = uuid4()
    order_a = uuid4()

    try:
        async with mig_engine.begin() as conn:
            for tid, slug in [(tenant_a, f"t-a-{tenant_a.hex[:8]}"), (tenant_b, f"t-b-{tenant_b.hex[:8]}")]:
                await conn.execute(
                    text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                    {"id": tid, "name": slug, "slug": slug},
                )
            for tid, cid in [(tenant_a, customer_a), (tenant_b, customer_b)]:
                await conn.execute(
                    text(
                        """
                        INSERT INTO customers (id, tenant_id, customer_type, display_name)
                        VALUES (:id, :tid, 'b2c_consumer', 'Buyer Test')
                        """
                    ),
                    {"id": cid, "tid": tid},
                )

        async with tenant_session(rt_factory, tenant_a) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO products (id, tenant_id, spu_code, category, i18n_specs)
                    VALUES (:pid, :tid, 'SPU-LED-5M', 'led_strip', '{"en": {"title": "LED Strip"}}'::jsonb)
                    """
                ),
                {"pid": product_a, "tid": tenant_a},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO product_variants (
                        id, tenant_id, product_id, sku, cost_price_base, base_currency,
                        min_margin_floor_pct, retail_price_usd, weight_grams
                    ) VALUES (
                        :vid, :tid, :pid, 'LED-STRIP-5M-IP65', 32.0000, 'CNY',
                        15.00, 19.99, 320
                    )
                    """
                ),
                {"vid": variant_a, "tid": tenant_a, "pid": product_a},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty)
                    VALUES (:tid, :wid, :vid, 10, 4)
                    """
                ),
                {"tid": tenant_a, "wid": warehouse_a, "vid": variant_a},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO orders (
                        id, tenant_id, order_no, order_mode, customer_id, currency,
                        fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status, payment_status
                    ) VALUES (
                        :oid, :tid, 'ORD-TEST-001', 'b2c_retail', :cid, 'USD',
                        7.200000, 100.00, 100.00, 'confirmed_reserved', 'paid'
                    )
                    """
                ),
                {"oid": order_a, "tid": tenant_a, "cid": customer_a},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO order_profit_snapshots (
                        order_id, tenant_id, base_currency, gross_revenue_base, cogs_base,
                        platform_commission_base, payment_gateway_fee_base, shipping_and_duty_base,
                        channel_msg_cost_base, ad_attribution_cost_base, fx_gain_loss_base
                    ) VALUES (
                        :oid, :tid, 'CNY', 720.00, 200.00, 36.00, 21.60, 65.00, 2.40, 50.00, 5.00
                    )
                    """
                ),
                {"oid": order_a, "tid": tenant_a},
            )
            snap = (
                await session.execute(
                    text("SELECT net_contribution_margin_base FROM order_profit_snapshots WHERE order_id = :oid"),
                    {"oid": order_a},
                )
            ).scalar_one()
            # 720.00 - 200.00 - 36.00 - 21.60 - 65.00 - 2.40 - 50.00 + 5.00 = 350.00
            assert Decimal(str(snap)) == Decimal("350.00")

        # Verify CHECK (on_hand_qty >= reserved_qty) fails cleanly
        with pytest.raises(IntegrityError):
            async with tenant_session(rt_factory, tenant_a) as session:
                await session.execute(
                    text(
                        """
                        UPDATE inventory_balances
                        SET reserved_qty = 11
                        WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                        """
                    ),
                    {"tid": tenant_a, "wid": warehouse_a, "vid": variant_a},
                )

        # Verify Tenant B cannot read Tenant A rows
        async with tenant_session(rt_factory, tenant_b) as session_b:
            count_orders = (
                await session_b.execute(text("SELECT COUNT(*) FROM orders WHERE id = :oid"), {"oid": order_a})
            ).scalar_one()
            count_snaps = (
                await session_b.execute(
                    text("SELECT COUNT(*) FROM order_profit_snapshots WHERE order_id = :oid"), {"oid": order_a}
                )
            ).scalar_one()
            assert count_orders == 0
            assert count_snaps == 0

        # Verify fail-closed when app.current_tenant_id is empty/unset
        async with rt_factory() as raw_session:
            async with raw_session.begin():
                await raw_session.execute(text("SELECT set_config('app.current_tenant_id', '', true)"))
                count_products = (await raw_session.execute(text("SELECT COUNT(*) FROM products"))).scalar_one()
                assert count_products == 0
    finally:
        async with mig_engine.begin() as conn:
            await conn.execute(text("DELETE FROM order_profit_snapshots WHERE tenant_id = ANY(:tids)"), {"tids": [tenant_a, tenant_b]})
            await conn.execute(text("DELETE FROM orders WHERE tenant_id = ANY(:tids)"), {"tids": [tenant_a, tenant_b]})
            await conn.execute(text("DELETE FROM inventory_balances WHERE tenant_id = ANY(:tids)"), {"tids": [tenant_a, tenant_b]})
            await conn.execute(text("DELETE FROM product_variants WHERE tenant_id = ANY(:tids)"), {"tids": [tenant_a, tenant_b]})
            await conn.execute(text("DELETE FROM products WHERE tenant_id = ANY(:tids)"), {"tids": [tenant_a, tenant_b]})
            await conn.execute(text("DELETE FROM customers WHERE tenant_id = ANY(:tids)"), {"tids": [tenant_a, tenant_b]})
            await conn.execute(text("DELETE FROM tenants WHERE id = ANY(:tids)"), {"tids": [tenant_a, tenant_b]})
        await mig_engine.dispose()
        await rt_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_erp_migration.py -v`
Expected: FAIL because tables (`products`, `orders`, `order_profit_snapshots`, etc.) do not exist yet.

- [ ] **Step 3: Write migration `0003_dual_mode_erp_and_cvp.py` and ORM models**

Create `apps/backend/alembic/versions/0003_dual_mode_erp_and_cvp.py`:

```python
"""Dual-mode ERP tables, inventory ledger, 17TRACK shipments, and CVP profit snapshots.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19 02:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002_channels_identity_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_ERP_TABLES = [
    "products",
    "product_variants",
    "platform_sku_mappings",
    "leads",
    "opportunities",
    "quotes",
    "quote_items",
    "orders",
    "order_items",
    "shipments",
    "inventory_balances",
    "inventory_ledger",
    "receivables_payables",
    "payment_records",
    "order_profit_snapshots",
]


def _apply_tenant_rls(table_name: str, append_only: bool = False) -> None:
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                EXECUTE 'CREATE POLICY tenant_allow_runtime ON {table_name} AS PERMISSIVE FOR ALL TO app_runtime USING (true) WITH CHECK (true)';
                EXECUTE 'CREATE POLICY tenant_isolation_policy ON {table_name} AS RESTRICTIVE FOR ALL TO app_runtime USING (tenant_id = NULLIF(current_setting(''app.current_tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting(''app.current_tenant_id'', true), '''')::uuid)';
                IF {str(append_only).lower()} THEN
                    EXECUTE 'REVOKE ALL ON {table_name} FROM app_runtime';
                    EXECUTE 'GRANT SELECT, INSERT ON {table_name} TO app_runtime';
                ELSE
                    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {table_name} TO app_runtime';
                END IF;
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    # 1. products
    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("spu_code", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("hs_code", sa.Text(), nullable=True),
        sa.Column("origin_country", sa.CHAR(2), server_default=sa.text("'CN'"), nullable=False),
        sa.Column("i18n_specs", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("certifications", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("ai_faq_facts", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "spu_code", name="uq_products_tenant_spu"),
    )
    op.create_index("idx_products_tenant_category", "products", ["tenant_id", "category"])

    # 2. product_variants
    op.create_table(
        "product_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("cost_price_base", sa.Numeric(12, 4), nullable=False),
        sa.Column("base_currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column("min_margin_floor_pct", sa.Numeric(5, 2), server_default=sa.text("15.00"), nullable=False),
        sa.Column("retail_price_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("b2b_moq_tiers", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("weight_grams", sa.Integer(), nullable=False),
        sa.Column("carton_dims_cm", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), server_default=sa.text("7"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "sku", name="uq_product_variants_tenant_sku"),
    )
    op.create_index("idx_product_variants_tenant_product", "product_variants", ["tenant_id", "product_id"])

    # 3. platform_sku_mappings
    op.create_table(
        "platform_sku_mappings",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_product_id", sa.Text(), nullable=False),
        sa.Column("external_sku", sa.Text(), nullable=False),
        sa.Column("bundle_multiplier", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("sync_inventory_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("bundle_multiplier >= 1", name="chk_bundle_multiplier_positive"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_account_id"], ["channel_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "channel_account_id", "external_sku", name="uq_sku_map_channel_ext"),
    )

    # 4. leads
    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_channel", sa.Text(), nullable=False),
        sa.Column("utm_campaign", sa.Text(), nullable=True),
        sa.Column("lead_type", sa.Text(), nullable=False),
        sa.Column("qualification_score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("extracted_requirements", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("lead_type IN ('b2b_inquiry', 'b2c_vip_pre_sale')", name="chk_leads_lead_type"),
        sa.CheckConstraint("status IN ('new', 'contacted', 'qualified', 'disqualified', 'converted')", name="chk_leads_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_leads_tenant_status", "leads", ["tenant_id", "status", "created_at"])

    # 5. opportunities
    op.create_table(
        "opportunities",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("expected_amount", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'USD'"), nullable=False),
        sa.Column("expected_close_date", sa.Date(), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "stage IN ('discovery', 'sampling', 'quoting', 'negotiating', 'closed_won', 'closed_lost')",
            name="chk_opportunities_stage",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["b2b_companies.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_opportunities_tenant_stage", "opportunities", ["tenant_id", "stage"])

    # 6. quotes
    op.create_table(
        "quotes",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opportunity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_no", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_proforma_invoice", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("incoterm", sa.Text(), nullable=False),
        sa.Column("port_of_loading", sa.Text(), nullable=True),
        sa.Column("port_of_destination", sa.Text(), nullable=True),
        sa.Column("payment_terms", sa.Text(), nullable=False),
        sa.Column("base_currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column("quote_currency", sa.CHAR(3), server_default=sa.text("'USD'"), nullable=False),
        sa.Column("fx_rate_snapshot", sa.Numeric(12, 6), nullable=False),
        sa.Column("fx_locked_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("freight_fee", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("insurance_fee", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("customs_duty_estimate", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("pdf_r2_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("converted_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("incoterm IN ('EXW', 'FOB', 'CIF', 'DDP', 'DAP')", name="chk_quotes_incoterm"),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'sent', 'accepted', 'expired', 'converted')",
            name="chk_quotes_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "quote_no", "version", name="uq_quotes_tenant_no_version"),
    )
    op.create_index("idx_quotes_tenant_opp", "quotes", ["tenant_id", "opportunity_id", "version"])

    # 7. quote_items
    op.create_table(
        "quote_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku_snapshot", sa.Text(), nullable=False),
        sa.Column("description_i18n", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("moq_tier_applied", sa.Integer(), nullable=True),
        sa.Column("unit_price_quote_ccy", sa.Numeric(12, 4), nullable=False),
        sa.Column("unit_cost_base_snapshot", sa.Numeric(12, 4), nullable=False),
        sa.Column("line_total_quote_ccy", sa.Numeric(14, 2), nullable=False),
        sa.CheckConstraint("quantity > 0", name="chk_quote_items_qty_positive"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_quote_items_tenant_quote", "quote_items", ["tenant_id", "quote_id"])

    # 8. orders
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_no", sa.Text(), nullable=False),
        sa.Column("order_mode", sa.Text(), nullable=False),
        sa.Column("channel_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_order_id", sa.Text(), nullable=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("fx_rate_to_base", sa.Numeric(12, 6), nullable=False),
        sa.Column("subtotal_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("shipping_charged", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("discount_amount", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("tax_collected", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("fulfillment_status", sa.Text(), nullable=False),
        sa.Column("payment_status", sa.Text(), nullable=False),
        sa.Column("channel_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("order_mode IN ('b2c_retail', 'b2b_sample', 'b2b_wholesale')", name="chk_orders_mode"),
        sa.CheckConstraint(
            "fulfillment_status IN ('pending_payment', 'confirmed_reserved', 'in_production', 'partially_shipped', 'shipped', 'delivered', 'exception', 'cancelled', 'refunded')",
            name="chk_orders_fulfillment_status",
        ),
        sa.CheckConstraint(
            "payment_status IN ('unpaid', 'deposit_paid', 'partially_paid', 'paid', 'refunded')",
            name="chk_orders_payment_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["channel_account_id"], ["channel_accounts.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "order_no", name="uq_orders_tenant_order_no"),
    )
    op.create_index("idx_orders_tenant_created", "orders", ["tenant_id", sa.text("created_at DESC")])
    op.create_index(
        "uq_orders_tenant_channel_ext",
        "orders",
        ["tenant_id", "channel_account_id", "external_order_id"],
        unique=True,
        postgresql_where=sa.text("external_order_id IS NOT NULL"),
    )

    # 9. order_items
    op.create_table(
        "order_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku_snapshot", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("unit_cost_base_snapshot", sa.Numeric(12, 4), nullable=False),
        sa.Column("line_total", sa.Numeric(14, 2), nullable=False),
        sa.CheckConstraint("quantity > 0", name="chk_order_items_qty_positive"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_order_items_tenant_order", "order_items", ["tenant_id", "order_id"])

    # 10. shipments
    op.create_table(
        "shipments",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tracking_provider", sa.Text(), server_default=sa.text("'17track'"), nullable=False),
        sa.Column("tracking_number", sa.Text(), nullable=False),
        sa.Column("carrier_code", sa.Text(), nullable=True),
        sa.Column("shipment_mode", sa.Text(), server_default=sa.text("'parcel'"), nullable=False),
        sa.Column("standardized_status", sa.Text(), server_default=sa.text("'not_found'"), nullable=False),
        sa.Column("sub_status", sa.Text(), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tracking_events", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("actual_shipping_cost_base", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("exception_alert_sent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("tracking_provider IN ('17track', 'aftership', 'manual')", name="chk_shipments_provider"),
        sa.CheckConstraint("shipment_mode IN ('parcel', 'air_freight', 'sea_lcl', 'sea_fcl')", name="chk_shipments_mode"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "tracking_number", name="uq_shipments_tenant_tracking"),
    )
    op.create_index("idx_shipments_tenant_stalled", "shipments", ["tenant_id", "standardized_status", "last_event_at"])

    # 11. inventory_balances
    op.create_table(
        "inventory_balances",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("on_hand_qty", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_qty", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("on_hand_qty >= 0", name="chk_inventory_on_hand_non_negative"),
        sa.CheckConstraint("reserved_qty >= 0", name="chk_inventory_reserved_non_negative"),
        sa.CheckConstraint("on_hand_qty >= reserved_qty", name="chk_inventory_non_negative"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"]),
        sa.PrimaryKeyConstraint("tenant_id", "warehouse_id", "variant_id"),
    )

    # 12. inventory_ledger (append-only)
    op.create_table(
        "inventory_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("qty_delta_on_hand", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("qty_delta_reserved", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reference_type", sa.Text(), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('inbound_po', 'reserve_order', 'release_cancel', 'deduct_shipped', 'stocktake_adjust')",
            name="chk_inventory_ledger_event_type",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"]),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_inventory_ledger_idempotency"),
    )
    op.create_index("idx_inventory_ledger_tenant_variant", "inventory_ledger", ["tenant_id", "warehouse_id", "variant_id"])

    # 13. receivables_payables
    op.create_table(
        "receivables_payables",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("milestone_label", sa.Text(), nullable=False),
        sa.Column("due_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("paid_amount", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("direction IN ('receivable', 'payable')", name="chk_rp_direction"),
        sa.CheckConstraint("status IN ('open', 'partial', 'settled', 'overdue', 'void')", name="chk_rp_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_rp_tenant_order", "receivables_payables", ["tenant_id", "order_id"])

    # 14. payment_records
    op.create_table(
        "payment_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rp_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_method", sa.Text(), nullable=False),
        sa.Column("external_tx_ref", sa.Text(), nullable=True),
        sa.Column("amount_received", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("actual_fx_rate", sa.Numeric(12, 6), nullable=False),
        sa.Column("gateway_fee", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("receipt_r2_key", sa.Text(), nullable=True),
        sa.Column("verified_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["rp_id"], ["receivables_payables.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_payment_records_tenant_rp", "payment_records", ["tenant_id", "rp_id"])

    # 15. order_profit_snapshots (with GENERATED ALWAYS AS STORED column)
    op.create_table(
        "order_profit_snapshots",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column("gross_revenue_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("cogs_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("platform_commission_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("payment_gateway_fee_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("shipping_and_duty_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("channel_msg_cost_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("ad_attribution_cost_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("fx_gain_loss_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "net_contribution_margin_base",
            sa.Numeric(14, 2),
            sa.Computed(
                "gross_revenue_base - cogs_base - platform_commission_base "
                "- payment_gateway_fee_base - shipping_and_duty_base "
                "- channel_msg_cost_base - ad_attribution_cost_base + fx_gain_loss_base",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("recalculated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("order_id"),
    )
    op.create_index("idx_order_profit_snapshots_tenant", "order_profit_snapshots", ["tenant_id", "recalculated_at"])

    for table in TENANT_ERP_TABLES:
        _apply_tenant_rls(table, append_only=(table == "inventory_ledger"))


def downgrade() -> None:
    for table in reversed(TENANT_ERP_TABLES):
        op.drop_table(table)
```

Create `apps/backend/src/erp_core/catalog/__init__.py`:

```python
"""Catalog, multi-language specs, MOQ tier pricing, and multi-platform SKU mappings."""
```

Create `apps/backend/src/erp_core/catalog/models.py`:

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "spu_code", name="uq_products_tenant_spu"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    spu_code: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    category: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    hs_code: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    origin_country: Mapped[str] = mapped_column(sa.CHAR(2), nullable=False, default="CN")
    i18n_specs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    certifications: Mapped[list[str]] = mapped_column(ARRAY(sa.Text()), nullable=False, default=list)
    ai_faq_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class ProductVariant(Base):
    __tablename__ = "product_variants"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "sku", name="uq_product_variants_tenant_sku"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    product_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    sku: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cost_price_base: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    base_currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="CNY")
    min_margin_floor_pct: Mapped[Decimal] = mapped_column(sa.Numeric(5, 2), nullable=False, default=Decimal("15.00"))
    retail_price_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    b2b_moq_tiers: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    weight_grams: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    carton_dims_cm: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    lead_time_days: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=7)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class PlatformSkuMapping(Base):
    __tablename__ = "platform_sku_mappings"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "channel_account_id", "external_sku", name="uq_sku_map_channel_ext"),
        sa.CheckConstraint("bundle_multiplier >= 1", name="chk_bundle_multiplier_positive"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    variant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False
    )
    channel_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("channel_accounts.id", ondelete="CASCADE"), nullable=False
    )
    external_product_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    external_sku: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    bundle_multiplier: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=1)
    sync_inventory_enabled: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
```

Create `apps/backend/src/erp_core/crm/__init__.py`:

```python
"""B2B CRM pipeline, versioned quotes, Proforma Invoice PDF rendering, and order conversion."""
```

Create `apps/backend/src/erp_core/crm/models.py`:

```python
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base
from erp_core.shared.enums import Incoterm, LeadStatus, OpportunityStage, QuoteStatus


def _str_enum_col(enum_cls: type) -> sa.Enum:
    return sa.Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=64,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False)
    source_channel: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    utm_campaign: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    lead_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    qualification_score: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    extracted_requirements: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[LeadStatus] = mapped_column(_str_enum_col(LeadStatus), nullable=False, default=LeadStatus.NEW)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    lead_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("leads.id"), nullable=True)
    company_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("b2b_companies.id"), nullable=True
    )
    customer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False)
    title: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    stage: Mapped[OpportunityStage] = mapped_column(
        _str_enum_col(OpportunityStage), nullable=False, default=OpportunityStage.DISCOVERY
    )
    expected_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="USD")
    expected_close_date: Mapped[date | None] = mapped_column(sa.Date(), nullable=True)
    owner_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "quote_no", "version", name="uq_quotes_tenant_no_version"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    opportunity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("opportunities.id"), nullable=False
    )
    quote_no: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=1)
    is_proforma_invoice: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    incoterm: Mapped[Incoterm] = mapped_column(_str_enum_col(Incoterm), nullable=False, default=Incoterm.FOB)
    port_of_loading: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    port_of_destination: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    payment_terms: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    base_currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="CNY")
    quote_currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="USD")
    fx_rate_snapshot: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False)
    fx_locked_until: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    freight_fee: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    insurance_fee: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    customs_duty_estimate: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    total_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    pdf_r2_key: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    status: Mapped[QuoteStatus] = mapped_column(_str_enum_col(QuoteStatus), nullable=False, default=QuoteStatus.DRAFT)
    converted_order_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class QuoteItem(Base):
    __tablename__ = "quote_items"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    quote_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("product_variants.id"), nullable=False)
    sku_snapshot: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    description_i18n: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    quantity: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    moq_tier_applied: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    unit_price_quote_ccy: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    unit_cost_base_snapshot: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    line_total_quote_ccy: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
```

Create `apps/backend/src/erp_core/orders/__init__.py`:

```python
"""Dual-mode orders, atomic inventory ledger, 17TRACK logistics, and platform reconciliation."""
```

Create `apps/backend/src/erp_core/orders/models.py`:

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base
from erp_core.shared.enums import (
    FulfillmentStatus,
    InventoryEventType,
    OrderMode,
    PaymentStatus,
    TrackingProvider,
)


def _str_enum_col(enum_cls: type) -> sa.Enum:
    return sa.Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=64,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "order_no", name="uq_orders_tenant_order_no"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    order_no: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    order_mode: Mapped[OrderMode] = mapped_column(_str_enum_col(OrderMode), nullable=False)
    channel_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("channel_accounts.id"), nullable=True
    )
    external_order_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    customer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False)
    quote_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("quotes.id"), nullable=True)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    fx_rate_to_base: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False)
    subtotal_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    shipping_charged: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    discount_amount: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    tax_collected: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    total_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    fulfillment_status: Mapped[FulfillmentStatus] = mapped_column(
        _str_enum_col(FulfillmentStatus), nullable=False, default=FulfillmentStatus.PENDING_PAYMENT
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        _str_enum_col(PaymentStatus), nullable=False, default=PaymentStatus.UNPAID
    )
    channel_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("product_variants.id"), nullable=False)
    sku_snapshot: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    quantity: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    unit_cost_base_snapshot: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "tracking_number", name="uq_shipments_tenant_tracking"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    order_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False)
    tracking_provider: Mapped[TrackingProvider] = mapped_column(
        _str_enum_col(TrackingProvider), nullable=False, default=TrackingProvider("17track")
    )
    tracking_number: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    carrier_code: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    shipment_mode: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="parcel")
    standardized_status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="not_found")
    sub_status: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    estimated_delivery_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    tracking_events: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    actual_shipping_cost_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    exception_alert_sent: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class InventoryBalance(Base):
    __tablename__ = "inventory_balances"
    __table_args__ = (sa.CheckConstraint("on_hand_qty >= reserved_qty", name="chk_inventory_non_negative"),)

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True)
    warehouse_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    variant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("product_variants.id"), primary_key=True
    )
    on_hand_qty: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    reserved_qty: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)


class InventoryLedger(Base):
    __tablename__ = "inventory_ledger"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_inventory_ledger_idempotency"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger(), sa.Identity(always=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True)
    warehouse_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    variant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("product_variants.id"), nullable=False
    )
    event_type: Mapped[InventoryEventType] = mapped_column(_str_enum_col(InventoryEventType), nullable=False)
    qty_delta_on_hand: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    qty_delta_reserved: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    reference_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    reference_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
```

Create `apps/backend/src/erp_core/finance/__init__.py`:

```python
"""Lightweight B2B/B2C receivables, bank slip verification, and CVP order profit snapshots."""
```

Create `apps/backend/src/erp_core/finance/models.py`:

```python
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class ReceivablePayable(Base):
    __tablename__ = "receivables_payables"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    order_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False)
    direction: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    milestone_label: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    due_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    paid_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    due_date: Mapped[date | None] = mapped_column(sa.Date(), nullable=True)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class PaymentRecord(Base):
    __tablename__ = "payment_records"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    rp_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("receivables_payables.id"), nullable=False
    )
    payment_method: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    external_tx_ref: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    amount_received: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    actual_fx_rate: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False)
    gateway_fee: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    receipt_r2_key: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    verified_by_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    paid_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class OrderProfitSnapshot(Base):
    __tablename__ = "order_profit_snapshots"

    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False)
    base_currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="CNY")
    gross_revenue_base: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    cogs_base: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    platform_commission_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    payment_gateway_fee_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    shipping_and_duty_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    channel_msg_cost_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    ad_attribution_cost_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    fx_gain_loss_base: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    net_contribution_margin_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2),
        sa.Computed(
            "gross_revenue_base - cogs_base - platform_commission_base "
            "- payment_gateway_fee_base - shipping_and_duty_base "
            "- channel_msg_cost_base - ad_attribution_cost_base + fx_gain_loss_base",
            persisted=True,
        ),
        nullable=False,
    )
    recalculated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
```

Ensure `apps/backend/alembic/env.py` imports these four model modules alongside Plan 01/02 models:

```python
import erp_core.catalog.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
```

- [ ] **Step 4: Run migration and tests to verify it passes**

Run:
```bash
cd apps/backend && uv run alembic upgrade head
cd apps/backend && uv run pytest tests/test_erp_migration.py -v
cd apps/backend && uv run lint-imports --config ../../infra/importlinter.ini
```
Expected: `alembic upgrade head` reaches `0003`; `test_erp_migration.py` PASSES (2 passed); `lint-imports` reports 0 broken contracts.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/alembic/versions/0003_dual_mode_erp_and_cvp.py apps/backend/alembic/env.py apps/backend/src/erp_core/catalog/ apps/backend/src/erp_core/crm/ apps/backend/src/erp_core/orders/ apps/backend/src/erp_core/finance/ apps/backend/tests/test_erp_migration.py
git commit -m "feat(erp): add migration 0003 dual-mode ERP & CVP tables with FORCE RLS"
```

---

### Task 3.2: Catalog, Multi-Language Specs, MOQ Tier Pricing & Multi-Platform SKU Mapping

**Files:**
- Create: `apps/backend/src/erp_core/catalog/schemas.py`
- Create: `apps/backend/src/erp_core/catalog/service.py`
- Create: `apps/backend/src/erp_core/catalog/router.py`
- Modify: `apps/backend/src/erp_core/api_main.py` (include `catalog_router` on `/api/v1`)
- Test: `apps/backend/tests/test_catalog_service.py`

**Interfaces:**
- Consumes:
  - `erp_core.shared.db`: `tenant_session`
  - `erp_core.shared.enums`: `UserRole`
  - `erp_core.shared.errors`: `AppError`
  - `erp_core.shared.pagination`: `PaginatedResponse`
  - `erp_core.tenancy.dependencies`: `CurrentPrincipal`, `require_roles`
  - `erp_core.tenancy.audit`: `record_audit_event`
- Produces:
  - `CatalogService` (`create_product`, `list_products`, `create_variant`, `list_variants`, `get_variant_for_role`, `get_variant_pricing_internal`, `resolve_moq_unit_price`, `create_sku_mapping`, `list_sku_mappings`, `resolve_sku_for_channel`)
  - DTOs: `VariantPricingInternal`, `ResolvedChannelSku`, `ProductRead`, `VariantRead`, `SkuMappingRead`
  - HTTP endpoints: `/api/v1/catalog/products`, `/api/v1/catalog/variants`, `/api/v1/catalog/sku-mappings`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_catalog_service.py`:

```python
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.api_main import app
from erp_core.catalog.schemas import (
    MoqTierInput,
    ProductCreate,
    SkuMappingCreate,
    VariantCreate,
)
from erp_core.catalog.service import CatalogService
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.tenancy.auth import create_access_token


def _migrator_url() -> str:
    return os.getenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_secret@localhost:5432/erp_saas",
    )


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )


def _make_jwt(user_id, tenant_id, role: UserRole, email: str) -> str:
    return create_access_token(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email=email,
        settings=get_settings(),
    )


@pytest.mark.asyncio
async def test_catalog_moq_tiers_bundle_multiplier_and_agent_cost_masking() -> None:
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    rt_engine = create_async_engine(_runtime_url(), echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    admin_id = uuid4()
    agent_id = uuid4()
    channel_account_id = uuid4()

    try:
        async with mig_engine.begin() as conn:
            slug = f"cat-{tenant_id.hex[:8]}"
            await conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tenant_id, "name": "Quanzhou LED Co", "slug": slug},
            )
            for uid, email, role in [
                (admin_id, f"admin-{tenant_id.hex[:6]}@qz.cn", UserRole.ADMIN.value),
                (agent_id, f"agent-{tenant_id.hex[:6]}@qz.cn", UserRole.AGENT.value),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, password_hash, full_name) VALUES (:id, :email, 'hash', :name)"
                    ),
                    {"id": uid, "email": email, "name": role},
                )
                await conn.execute(
                    text(
                        "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) VALUES (:id, :tid, :uid, :role)"
                    ),
                    {"id": uuid4(), "tid": tenant_id, "uid": uid, "role": role},
                )
            await conn.execute(
                text(
                    """
                    INSERT INTO channel_accounts (
                        id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile
                    ) VALUES (
                        :id, :tid, 'shopify', 'qz-led.myshopify.com', 'QZ Shopify', 'active', '{}'::jsonb
                    )
                    """
                ),
                {"id": channel_account_id, "tid": tenant_id},
            )

        svc = CatalogService()
        async with tenant_session(rt_factory, tenant_id) as session:
            prod = await svc.create_product(
                session,
                tenant_id,
                actor_id=str(admin_id),
                payload=ProductCreate(
                    spu_code="QZ-STRIP-5M",
                    category="smart_lighting",
                    hs_code="9405429000",
                    origin_country="CN",
                    i18n_specs={
                        "en": {
                            "title": "5M IP65 Waterproof RGBW LED Strip",
                            "selling_points": ["IP65 Silicon Tube", "CRI>90"],
                            "materials": "FPCB + Silicone",
                        },
                        "es": {
                            "title": "Tira LED RGBW 5M IP65 Impermeable",
                            "selling_points": ["Silicona IP65"],
                        },
                    },
                    certifications=["CE", "FCC", "RoHS"],
                    ai_faq_facts={"warranty_months": 24, "voltage": "24V DC"},
                ),
            )
            variant = await svc.create_variant(
                session,
                tenant_id,
                actor_id=str(admin_id),
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=prod.id,
                    sku="LED-STRIP-5M-IP65",
                    attributes={"color": "RGBW", "plug": "EU"},
                    cost_price_base=Decimal("32.5000"),
                    base_currency="CNY",
                    min_margin_floor_pct=Decimal("18.00"),
                    retail_price_usd=Decimal("19.90"),
                    b2b_moq_tiers=[
                        MoqTierInput(min_qty=50, unit_price_usd=Decimal("12.50")),
                        MoqTierInput(min_qty=500, unit_price_usd=Decimal("8.90")),
                    ],
                    weight_grams=310,
                    carton_dims_cm={"l": 42, "w": 32, "h": 28, "pcs_per_carton": 40},
                    lead_time_days=10,
                ),
            )
            assert variant.cost_price_base == Decimal("32.5000")
            assert variant.min_margin_floor_pct == Decimal("18.00")

            # Verify MOQ tier selection with pure Decimal
            p_sample, tier_sample = await svc.resolve_moq_unit_price(
                variant.retail_price_usd,
                [t.model_dump(mode="json") for t in variant.b2b_moq_tiers],
                qty=10,
            )
            assert p_sample == Decimal("19.90")
            assert tier_sample is None

            p_50, tier_50 = await svc.resolve_moq_unit_price(
                variant.retail_price_usd,
                [t.model_dump(mode="json") for t in variant.b2b_moq_tiers],
                qty=200,
            )
            assert p_50 == Decimal("12.50")
            assert tier_50 == 50

            p_500, tier_500 = await svc.resolve_moq_unit_price(
                variant.retail_price_usd,
                [t.model_dump(mode="json") for t in variant.b2b_moq_tiers],
                qty=500,
            )
            assert p_500 == Decimal("8.90")
            assert tier_500 == 500

            # Create 2-pack bundle SKU mapping
            await svc.create_sku_mapping(
                session,
                tenant_id,
                actor_id=str(admin_id),
                payload=SkuMappingCreate(
                    variant_id=variant.id,
                    channel_account_id=channel_account_id,
                    external_product_id="gid://shopify/Product/99001",
                    external_sku="SHOP-LED-5M-2PACK",
                    bundle_multiplier=2,
                    sync_inventory_enabled=True,
                ),
            )
            resolved = await svc.resolve_sku_for_channel(
                session,
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                external_sku="SHOP-LED-5M-2PACK",
                external_qty=3,
            )
            assert resolved is not None
            assert resolved.internal_sku == "LED-STRIP-5M-IP65"
            assert resolved.bundle_multiplier == 2
            assert resolved.deduct_qty == 6

        # HTTP RBAC check: agent MUST NOT receive cost_price_base or min_margin_floor_pct
        app.state.session_factory = rt_factory
        app.state.settings = get_settings()
        agent_token = _make_jwt(agent_id, tenant_id, UserRole.AGENT, f"agent-{tenant_id.hex[:6]}@qz.cn")
        admin_token = _make_jwt(admin_id, tenant_id, UserRole.ADMIN, f"admin-{tenant_id.hex[:6]}@qz.cn")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            agent_resp = await client.get(
                "/api/v1/catalog/variants",
                headers={"Authorization": f"Bearer {agent_token}"},
            )
            assert agent_resp.status_code == 200
            agent_item = agent_resp.json()["items"][0]
            assert agent_item["sku"] == "LED-STRIP-5M-IP65"
            assert agent_item["cost_price_base"] is None
            assert agent_item["min_margin_floor_pct"] is None

            admin_resp = await client.get(
                "/api/v1/catalog/variants",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert admin_resp.status_code == 200
            admin_item = admin_resp.json()["items"][0]
            assert Decimal(str(admin_item["cost_price_base"])) == Decimal("32.5000")
            assert Decimal(str(admin_item["min_margin_floor_pct"])) == Decimal("18.00")
    finally:
        async with mig_engine.begin() as conn:
            await conn.execute(text("DELETE FROM platform_sku_mappings WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM product_variants WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM products WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM channel_accounts WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM tenant_memberships WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM users WHERE id = ANY(:uids)"), {"uids": [admin_id, agent_id]})
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_catalog_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.catalog.schemas'`

- [ ] **Step 3: Write `catalog/schemas.py`, `catalog/service.py`, `catalog/router.py`, and register router in `api_main.py`**

Create `apps/backend/src/erp_core/catalog/schemas.py`:

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MoqTierInput(BaseModel):
    min_qty: int = Field(ge=1)
    unit_price_usd: Decimal = Field(gt=Decimal("0"))


class ProductCreate(BaseModel):
    spu_code: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    hs_code: str | None = Field(default=None, max_length=32)
    origin_country: str = Field(default="CN", min_length=2, max_length=2)
    i18n_specs: dict[str, Any] = Field(default_factory=dict)
    certifications: list[str] = Field(default_factory=list)
    ai_faq_facts: dict[str, Any] = Field(default_factory=dict)


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    spu_code: str
    category: str
    hs_code: str | None
    origin_country: str
    i18n_specs: dict[str, Any]
    certifications: list[str]
    ai_faq_facts: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class VariantCreate(BaseModel):
    product_id: UUID
    sku: str = Field(min_length=1, max_length=128)
    attributes: dict[str, Any] = Field(default_factory=dict)
    cost_price_base: Decimal = Field(ge=Decimal("0"))
    base_currency: str = Field(default="CNY", min_length=3, max_length=3)
    min_margin_floor_pct: Decimal = Field(default=Decimal("15.00"), ge=Decimal("0"), le=Decimal("99.99"))
    retail_price_usd: Decimal = Field(gt=Decimal("0"))
    b2b_moq_tiers: list[MoqTierInput] = Field(default_factory=list)
    weight_grams: int = Field(ge=0)
    carton_dims_cm: dict[str, Any] | None = None
    lead_time_days: int = Field(default=7, ge=0)


class VariantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    product_id: UUID
    sku: str
    attributes: dict[str, Any]
    cost_price_base: Decimal | None = None
    base_currency: str
    min_margin_floor_pct: Decimal | None = None
    retail_price_usd: Decimal
    b2b_moq_tiers: list[MoqTierInput]
    weight_grams: int
    carton_dims_cm: dict[str, Any] | None
    lead_time_days: int
    created_at: datetime


class VariantPricingInternal(BaseModel):
    """Internal service-to-service DTO carrying full cost and margin floor fields."""

    id: UUID
    product_id: UUID
    sku: str
    cost_price_base: Decimal
    base_currency: str
    min_margin_floor_pct: Decimal
    retail_price_usd: Decimal
    b2b_moq_tiers: list[dict[str, Any]]
    i18n_specs: dict[str, Any]
    hs_code: str | None


class SkuMappingCreate(BaseModel):
    variant_id: UUID
    channel_account_id: UUID
    external_product_id: str = Field(min_length=1)
    external_sku: str = Field(min_length=1)
    bundle_multiplier: int = Field(default=1, ge=1)
    sync_inventory_enabled: bool = True


class SkuMappingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    variant_id: UUID
    channel_account_id: UUID
    external_product_id: str
    external_sku: str
    bundle_multiplier: int
    sync_inventory_enabled: bool
    created_at: datetime


class VariantUpdate(BaseModel):
    retail_price_usd: Decimal | None = Field(default=None, gt=Decimal("0"))
    b2b_moq_tiers: list[MoqTierInput] | None = None
    min_margin_floor_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("99.99"))
    lead_time_days: int | None = Field(default=None, ge=0)


class SkuMappingUpdate(BaseModel):
    external_sku: str | None = Field(default=None, min_length=1)
    bundle_multiplier: int | None = Field(default=None, ge=1)
    sync_inventory_enabled: bool | None = None


class VariantMappingWithLabelRead(SkuMappingRead):
    channel_label: str | None = None


class VariantMappingsBundleRead(BaseModel):
    variant: VariantRead
    mappings: list[VariantMappingWithLabelRead]


class LiveSkuCheckRead(BaseModel):
    sku: str
    available_qty: int
    unit_price_quote_ccy: Decimal
    moq_tier_applied: int | None
    incoterm: str
    source_tool_call_id: str


class ResolvedChannelSku(BaseModel):
    variant_id: UUID
    internal_sku: str
    bundle_multiplier: int
    deduct_qty: int
    cost_price_base: Decimal
    base_currency: str
```

Create `apps/backend/src/erp_core/catalog/service.py`:

```python
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.models import PlatformSkuMapping, Product, ProductVariant
from erp_core.catalog.schemas import (
    LiveSkuCheckRead,
    MoqTierInput,
    ProductCreate,
    ProductRead,
    ResolvedChannelSku,
    SkuMappingCreate,
    SkuMappingRead,
    SkuMappingUpdate,
    VariantCreate,
    VariantMappingsBundleRead,
    VariantMappingWithLabelRead,
    VariantPricingInternal,
    VariantRead,
    VariantUpdate,
)
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event


class CatalogService:
    """Product, variant, MOQ tier pricing, and multi-platform SKU bundle resolution."""

    @staticmethod
    def serialize_variant_for_role(variant: ProductVariant, role: UserRole) -> VariantRead:
        """Mask cost_price_base and min_margin_floor_pct for agent role."""
        tiers = [
            MoqTierInput(
                min_qty=int(t["min_qty"]),
                unit_price_usd=Decimal(str(t["unit_price_usd"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            )
            for t in (variant.b2b_moq_tiers or [])
        ]
        hide_costs = role == UserRole.AGENT
        return VariantRead(
            id=variant.id,
            tenant_id=variant.tenant_id,
            product_id=variant.product_id,
            sku=variant.sku,
            attributes=variant.attributes,
            cost_price_base=None if hide_costs else Decimal(str(variant.cost_price_base)),
            base_currency=variant.base_currency,
            min_margin_floor_pct=None if hide_costs else Decimal(str(variant.min_margin_floor_pct)),
            retail_price_usd=Decimal(str(variant.retail_price_usd)),
            b2b_moq_tiers=tiers,
            weight_grams=variant.weight_grams,
            carton_dims_cm=variant.carton_dims_cm,
            lead_time_days=variant.lead_time_days,
            created_at=variant.created_at,
        )

    async def create_product(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        payload: ProductCreate,
    ) -> ProductRead:
        product = Product(
            tenant_id=tenant_id,
            spu_code=payload.spu_code.strip(),
            category=payload.category.strip(),
            hs_code=payload.hs_code.strip() if payload.hs_code else None,
            origin_country=payload.origin_country.upper(),
            i18n_specs=payload.i18n_specs,
            certifications=payload.certifications,
            ai_faq_facts=payload.ai_faq_facts,
        )
        session.add(product)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise AppError(
                slug="duplicate-spu",
                status_code=409,
                code="ERR_DUPLICATE_SPU",
                title="Duplicate SPU",
                detail=f"Product with spu_code '{payload.spu_code}' already exists.",
            ) from exc

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.product.created",
            resource_type="product",
            resource_id=product.id,
            after_state={"spu_code": product.spu_code, "category": product.category},
        )
        return ProductRead.model_validate(product)

    async def list_products(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        limit: int = 20,
        offset: int = 0,
        search: str | None = None,
    ) -> PaginatedResponse[ProductRead]:
        stmt = select(Product).where(Product.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(Product).where(Product.tenant_id == tenant_id)
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(Product.spu_code.ilike(pattern) | Product.category.ilike(pattern))
            count_stmt = count_stmt.where(Product.spu_code.ilike(pattern) | Product.category.ilike(pattern))

        total = int((await session.execute(count_stmt)).scalar_one())
        rows = (await session.execute(stmt.order_by(Product.created_at.desc()).limit(limit).offset(offset))).scalars().all()
        items = [ProductRead.model_validate(r) for r in rows]
        return PaginatedResponse(items=items, total=total, limit=limit, offset=offset, has_more=(offset + len(items)) < total)

    async def create_variant(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        payload: VariantCreate,
        role: UserRole,
    ) -> VariantRead:
        product = (
            await session.execute(
                select(Product).where(Product.tenant_id == tenant_id, Product.id == payload.product_id)
            )
        ).scalar_one_or_none()
        if product is None:
            raise AppError(
                slug="product-not-found",
                status_code=404,
                code="ERR_PRODUCT_NOT_FOUND",
                title="Product Not Found",
                detail=f"Product '{payload.product_id}' does not exist.",
            )

        serialized_tiers = [
            {
                "min_qty": tier.min_qty,
                "unit_price_usd": str(tier.unit_price_usd.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            }
            for tier in sorted(payload.b2b_moq_tiers, key=lambda t: t.min_qty)
        ]
        variant = ProductVariant(
            tenant_id=tenant_id,
            product_id=payload.product_id,
            sku=payload.sku.strip(),
            attributes=payload.attributes,
            cost_price_base=payload.cost_price_base.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            base_currency=payload.base_currency.upper(),
            min_margin_floor_pct=payload.min_margin_floor_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            retail_price_usd=payload.retail_price_usd.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            b2b_moq_tiers=serialized_tiers,
            weight_grams=payload.weight_grams,
            carton_dims_cm=payload.carton_dims_cm,
            lead_time_days=payload.lead_time_days,
        )
        session.add(variant)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise AppError(
                slug="duplicate-sku",
                status_code=409,
                code="ERR_DUPLICATE_SKU",
                title="Duplicate SKU",
                detail=f"Variant with sku '{payload.sku}' already exists.",
            ) from exc

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.variant.created",
            resource_type="product_variant",
            resource_id=variant.id,
            after_state={"sku": variant.sku, "retail_price_usd": str(variant.retail_price_usd)},
        )
        return self.serialize_variant_for_role(variant, role)

    async def list_variants(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        role: UserRole,
        product_id: UUID | None = None,
        sku: str | None = None,
        q: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[VariantRead]:
        stmt = select(ProductVariant).where(ProductVariant.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(ProductVariant).where(ProductVariant.tenant_id == tenant_id)
        if product_id is not None:
            stmt = stmt.where(ProductVariant.product_id == product_id)
            count_stmt = count_stmt.where(ProductVariant.product_id == product_id)
        if sku is not None:
            stmt = stmt.where(ProductVariant.sku == sku.strip())
            count_stmt = count_stmt.where(ProductVariant.sku == sku.strip())
        if q is not None and q.strip():
            pat = f"%{q.strip()}%"
            stmt = stmt.where(ProductVariant.sku.ilike(pat))
            count_stmt = count_stmt.where(ProductVariant.sku.ilike(pat))

        total = int((await session.execute(count_stmt)).scalar_one())
        rows = (
            await session.execute(stmt.order_by(ProductVariant.created_at.desc()).limit(limit).offset(offset))
        ).scalars().all()
        items = [self.serialize_variant_for_role(row, role) for row in rows]
        return PaginatedResponse(items=items, total=total, limit=limit, offset=offset, has_more=(offset + len(items)) < total)

    async def get_variant_for_role(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID,
        role: UserRole,
    ) -> VariantRead:
        variant = (
            await session.execute(
                select(ProductVariant).where(ProductVariant.tenant_id == tenant_id, ProductVariant.id == variant_id)
            )
        ).scalar_one_or_none()
        if variant is None:
            raise AppError(
                slug="variant-not-found",
                status_code=404,
                code="ERR_VARIANT_NOT_FOUND",
                title="Variant Not Found",
                detail=f"Variant '{variant_id}' not found.",
            )
        return self.serialize_variant_for_role(variant, role)

    async def get_variant_pricing_internal(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID,
    ) -> VariantPricingInternal:
        row = (
            await session.execute(
                select(ProductVariant, Product)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(ProductVariant.tenant_id == tenant_id, ProductVariant.id == variant_id)
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                slug="variant-not-found",
                status_code=404,
                code="ERR_VARIANT_NOT_FOUND",
                title="Variant Not Found",
                detail=f"Variant '{variant_id}' not found.",
            )
        variant, product = row
        return VariantPricingInternal(
            id=variant.id,
            product_id=variant.product_id,
            sku=variant.sku,
            cost_price_base=Decimal(str(variant.cost_price_base)),
            base_currency=variant.base_currency,
            min_margin_floor_pct=Decimal(str(variant.min_margin_floor_pct)),
            retail_price_usd=Decimal(str(variant.retail_price_usd)),
            b2b_moq_tiers=list(variant.b2b_moq_tiers or []),
            i18n_specs=dict(product.i18n_specs or {}),
            hs_code=product.hs_code,
        )

    async def resolve_moq_unit_price(
        self,
        retail_price_usd: Decimal,
        b2b_moq_tiers: list[dict[str, Any]],
        qty: int,
    ) -> tuple[Decimal, int | None]:
        """Select highest matching MOQ tier unit price using Decimal; fall back to retail price."""
        sorted_tiers = sorted(b2b_moq_tiers, key=lambda item: int(item["min_qty"]), reverse=True)
        for tier in sorted_tiers:
            min_qty = int(tier["min_qty"])
            if qty >= min_qty:
                price = Decimal(str(tier["unit_price_usd"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                return price, min_qty
        return Decimal(str(retail_price_usd)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), None

    async def create_sku_mapping(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        payload: SkuMappingCreate,
    ) -> SkuMappingRead:
        mapping = PlatformSkuMapping(
            tenant_id=tenant_id,
            variant_id=payload.variant_id,
            channel_account_id=payload.channel_account_id,
            external_product_id=payload.external_product_id.strip(),
            external_sku=payload.external_sku.strip(),
            bundle_multiplier=payload.bundle_multiplier,
            sync_inventory_enabled=payload.sync_inventory_enabled,
        )
        session.add(mapping)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise AppError(
                slug="duplicate-sku-mapping",
                status_code=409,
                code="ERR_DUPLICATE_SKU_MAPPING",
                title="Duplicate SKU Mapping",
                detail=f"External SKU '{payload.external_sku}' is already mapped for channel account '{payload.channel_account_id}'.",
            ) from exc

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.sku_mapping.created",
            resource_type="platform_sku_mapping",
            resource_id=mapping.id,
            after_state={
                "external_sku": mapping.external_sku,
                "variant_id": str(mapping.variant_id),
                "bundle_multiplier": mapping.bundle_multiplier,
            },
        )
        return SkuMappingRead.model_validate(mapping)

    async def list_sku_mappings(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[SkuMappingRead]:
        stmt = select(PlatformSkuMapping).where(PlatformSkuMapping.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(PlatformSkuMapping).where(PlatformSkuMapping.tenant_id == tenant_id)
        if channel_account_id is not None:
            stmt = stmt.where(PlatformSkuMapping.channel_account_id == channel_account_id)
            count_stmt = count_stmt.where(PlatformSkuMapping.channel_account_id == channel_account_id)

        total = int((await session.execute(count_stmt)).scalar_one())
        rows = (
            await session.execute(stmt.order_by(PlatformSkuMapping.created_at.desc()).limit(limit).offset(offset))
        ).scalars().all()
        items = [SkuMappingRead.model_validate(r) for r in rows]
        return PaginatedResponse(items=items, total=total, limit=limit, offset=offset, has_more=(offset + len(items)) < total)

    async def resolve_sku_for_channel(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        external_sku: str,
        external_qty: int,
    ) -> ResolvedChannelSku | None:
        if external_qty <= 0:
            raise ValueError("external_qty must be positive")
        cleaned_sku = external_sku.strip()
        row = (
            await session.execute(
                select(PlatformSkuMapping, ProductVariant)
                .join(ProductVariant, ProductVariant.id == PlatformSkuMapping.variant_id)
                .where(
                    PlatformSkuMapping.tenant_id == tenant_id,
                    PlatformSkuMapping.channel_account_id == channel_account_id,
                    PlatformSkuMapping.external_sku == cleaned_sku,
                )
            )
        ).one_or_none()

        if row is not None:
            mapping, variant = row
            return ResolvedChannelSku(
                variant_id=variant.id,
                internal_sku=variant.sku,
                bundle_multiplier=mapping.bundle_multiplier,
                deduct_qty=external_qty * mapping.bundle_multiplier,
                cost_price_base=Decimal(str(variant.cost_price_base)),
                base_currency=variant.base_currency,
            )

        # Direct fallback if the external SKU matches the internal SKU verbatim
        direct_variant = (
            await session.execute(
                select(ProductVariant).where(ProductVariant.tenant_id == tenant_id, ProductVariant.sku == cleaned_sku)
            )
        ).scalar_one_or_none()
        if direct_variant is None:
            return None
        return ResolvedChannelSku(
            variant_id=direct_variant.id,
            internal_sku=direct_variant.sku,
            bundle_multiplier=1,
            deduct_qty=external_qty,
            cost_price_base=Decimal(str(direct_variant.cost_price_base)),
            base_currency=direct_variant.base_currency,
        )

    async def update_variant(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        variant_id: UUID,
        payload: VariantUpdate,
        role: UserRole,
    ) -> VariantRead:
        variant = (
            await session.execute(
                select(ProductVariant).where(ProductVariant.tenant_id == tenant_id, ProductVariant.id == variant_id)
            )
        ).scalar_one_or_none()
        if variant is None:
            raise AppError(
                slug="variant-not-found",
                status_code=404,
                code="ERR_VARIANT_NOT_FOUND",
                title="Variant Not Found",
                detail=f"Variant '{variant_id}' not found.",
            )
        if payload.retail_price_usd is not None:
            variant.retail_price_usd = payload.retail_price_usd.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if payload.b2b_moq_tiers is not None:
            variant.b2b_moq_tiers = [
                {"min_qty": t.min_qty, "unit_price_usd": str(t.unit_price_usd)}
                for t in sorted(payload.b2b_moq_tiers, key=lambda x: x.min_qty)
            ]
        if payload.min_margin_floor_pct is not None:
            variant.min_margin_floor_pct = payload.min_margin_floor_pct.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        if payload.lead_time_days is not None:
            variant.lead_time_days = payload.lead_time_days
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.variant.updated",
            resource_type="product_variant",
            resource_id=variant.id,
            after_state={"sku": variant.sku, "retail_price_usd": str(variant.retail_price_usd)},
        )
        return self.serialize_variant_for_role(variant, role)

    async def list_variant_mappings(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID,
        role: UserRole,
    ) -> VariantMappingsBundleRead:
        variant_read = await self.get_variant_for_role(session, tenant_id, variant_id, role)
        rows = (
            await session.execute(
                text(
                    """
                    SELECT m.id, m.tenant_id, m.variant_id, m.channel_account_id,
                           ca.display_name AS channel_label, m.external_product_id,
                           m.external_sku, m.bundle_multiplier, m.sync_inventory_enabled, m.created_at
                    FROM platform_sku_mappings m
                    LEFT JOIN channel_accounts ca
                        ON ca.id = m.channel_account_id AND ca.tenant_id = m.tenant_id
                    WHERE m.tenant_id = :tid AND m.variant_id = :vid
                    ORDER BY m.created_at DESC
                    """
                ),
                {"tid": tenant_id, "vid": variant_id},
            )
        ).all()
        mappings = [
            VariantMappingWithLabelRead(
                id=r.id,
                tenant_id=r.tenant_id,
                variant_id=r.variant_id,
                channel_account_id=r.channel_account_id,
                channel_label=r.channel_label,
                external_product_id=r.external_product_id,
                external_sku=r.external_sku,
                bundle_multiplier=int(r.bundle_multiplier),
                sync_inventory_enabled=bool(r.sync_inventory_enabled),
                created_at=r.created_at,
            )
            for r in rows
        ]
        return VariantMappingsBundleRead(variant=variant_read, mappings=mappings)

    async def update_sku_mapping(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        mapping_id: UUID,
        payload: SkuMappingUpdate,
    ) -> SkuMappingRead:
        mapping = (
            await session.execute(
                select(PlatformSkuMapping).where(
                    PlatformSkuMapping.tenant_id == tenant_id, PlatformSkuMapping.id == mapping_id
                )
            )
        ).scalar_one_or_none()
        if mapping is None:
            raise AppError(
                slug="sku-mapping-not-found",
                status_code=404,
                code="ERR_SKU_MAPPING_NOT_FOUND",
                title="SKU Mapping Not Found",
                detail=f"SKU mapping '{mapping_id}' not found.",
            )
        if payload.external_sku is not None:
            mapping.external_sku = payload.external_sku.strip()
        if payload.bundle_multiplier is not None:
            mapping.bundle_multiplier = payload.bundle_multiplier
        if payload.sync_inventory_enabled is not None:
            mapping.sync_inventory_enabled = payload.sync_inventory_enabled
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="catalog.sku_mapping.updated",
            resource_type="platform_sku_mapping",
            resource_id=mapping.id,
            after_state={
                "external_sku": mapping.external_sku,
                "bundle_multiplier": mapping.bundle_multiplier,
                "sync_inventory_enabled": mapping.sync_inventory_enabled,
            },
        )
        return SkuMappingRead.model_validate(mapping)

    async def live_check_sku(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        sku: str,
        qty: int = 1,
        incoterm: str = "FOB",
    ) -> LiveSkuCheckRead:
        variant = (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.tenant_id == tenant_id,
                    func.lower(ProductVariant.sku) == sku.strip().lower(),
                )
            )
        ).scalar_one_or_none()
        if variant is None:
            raise AppError(
                slug="variant-not-found",
                status_code=404,
                code="ERR_VARIANT_NOT_FOUND",
                title="Variant Not Found",
                detail=f"SKU '{sku}' not found.",
            )
        avail_raw = (
            await session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(on_hand_qty - reserved_qty), 0)
                    FROM inventory_balances
                    WHERE tenant_id = :tid AND variant_id = :vid
                    """
                ),
                {"tid": tenant_id, "vid": variant.id},
            )
        ).scalar_one()
        unit_price, moq_tier = await self.resolve_moq_unit_price(
            Decimal(str(variant.retail_price_usd)), list(variant.b2b_moq_tiers or []), qty
        )
        return LiveSkuCheckRead(
            sku=variant.sku,
            available_qty=int(avail_raw),
            unit_price_quote_ccy=unit_price,
            moq_tier_applied=moq_tier,
            incoterm=incoterm.upper(),
            source_tool_call_id=f"sql-sku-{uuid4().hex[:8]}",
        )
```

Create `apps/backend/src/erp_core/catalog/router.py`:

```python
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from erp_core.catalog.schemas import (
    LiveSkuCheckRead,
    ProductCreate,
    ProductRead,
    SkuMappingCreate,
    SkuMappingRead,
    SkuMappingUpdate,
    VariantCreate,
    VariantMappingsBundleRead,
    VariantRead,
    VariantUpdate,
)
from erp_core.catalog.service import CatalogService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/catalog", tags=["catalog"])
_service = CatalogService()


def _get_session_factory(request: Request):
    factory = getattr(request.app.state, "session_factory", None) or getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


@router.get("/products", response_model=PaginatedResponse[ProductRead])
async def list_products(
    request: Request,
    search: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> PaginatedResponse[ProductRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_products(session, principal.tenant_id, limit=limit, offset=offset, search=search)


@router.post("/products", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_product(
    request: Request,
    payload: ProductCreate,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)),
) -> ProductRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_product(session, principal.tenant_id, str(principal.user_id), payload)


@router.get("/variants", response_model=PaginatedResponse[VariantRead])
async def list_variants(
    request: Request,
    product_id: UUID | None = None,
    sku: str | None = None,
    q: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> PaginatedResponse[VariantRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_variants(
            session,
            principal.tenant_id,
            role=principal.role,
            product_id=product_id,
            sku=sku,
            q=q,
            limit=limit,
            offset=offset,
        )


@router.get("/live-check", response_model=LiveSkuCheckRead)
async def live_check_sku(
    request: Request,
    sku: str = Query(..., min_length=1),
    qty: int = Query(default=1, ge=1),
    incoterm: str = Query(default="FOB", min_length=3, max_length=8),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> LiveSkuCheckRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.live_check_sku(session, principal.tenant_id, sku=sku, qty=qty, incoterm=incoterm)


@router.post("/variants", response_model=VariantRead, status_code=status.HTTP_201_CREATED)
async def create_variant(
    request: Request,
    payload: VariantCreate,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)),
) -> VariantRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_variant(
            session, principal.tenant_id, str(principal.user_id), payload, role=principal.role
        )


@router.patch("/variants/{variant_id}", response_model=VariantRead)
async def update_variant(
    request: Request,
    variant_id: UUID,
    payload: VariantUpdate,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)),
) -> VariantRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.update_variant(
            session, principal.tenant_id, str(principal.user_id), variant_id, payload, role=principal.role
        )


@router.get("/variants/{variant_id}/mappings", response_model=VariantMappingsBundleRead)
async def list_variant_mappings(
    request: Request,
    variant_id: UUID,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> VariantMappingsBundleRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_variant_mappings(session, principal.tenant_id, variant_id, role=principal.role)


@router.get("/sku-mappings", response_model=PaginatedResponse[SkuMappingRead])
async def list_sku_mappings(
    request: Request,
    channel_account_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> PaginatedResponse[SkuMappingRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_sku_mappings(
            session, principal.tenant_id, channel_account_id=channel_account_id, limit=limit, offset=offset
        )


@router.post("/sku-mappings", response_model=SkuMappingRead, status_code=status.HTTP_201_CREATED)
async def create_sku_mapping(
    request: Request,
    payload: SkuMappingCreate,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)),
) -> SkuMappingRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_sku_mapping(session, principal.tenant_id, str(principal.user_id), payload)


@router.put("/mappings/{mapping_id}", response_model=SkuMappingRead)
async def update_sku_mapping(
    request: Request,
    mapping_id: UUID,
    payload: SkuMappingUpdate,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)),
) -> SkuMappingRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.update_sku_mapping(
            session, principal.tenant_id, str(principal.user_id), mapping_id, payload
        )
```

Modify `apps/backend/src/erp_core/api_main.py` to mount `catalog_router`:

```python
from erp_core.catalog.router import router as catalog_router

app.include_router(catalog_router, prefix="/api/v1")
```

- [ ] **Step 4: Run tests, contract export, and import-linter**

Run:
```bash
cd apps/backend && uv run pytest tests/test_catalog_service.py -v
cd apps/backend && uv run python scripts/export_openapi.py > ../../contracts/openapi.json
cd apps/backend && uv run lint-imports --config ../../infra/importlinter.ini
```
Expected: `test_catalog_service.py` PASSES; `openapi.json` includes `/api/v1/catalog/*` routes; `lint-imports` passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/catalog/ apps/backend/src/erp_core/api_main.py apps/backend/tests/test_catalog_service.py contracts/openapi.json
git commit -m "feat(catalog): add catalog specs, MOQ Decimal tiers, bundle SKU mapping & agent cost masking"
```

---

### Task 3.3: B2B Pipeline, Versioned Quote/PI Engine, WeasyPrint PDF & `1-Click` Order Conversion

**Files:**
- Create: `apps/backend/src/erp_core/crm/schemas.py`
- Create: `apps/backend/src/erp_core/crm/pdf_renderer.py`
- Create: `apps/backend/src/erp_core/crm/quote_service.py`
- Create: `apps/backend/src/erp_core/crm/router.py`
- Modify: `apps/backend/src/erp_core/api_main.py` (include `crm_router` on `/api/v1`)
- Test: `apps/backend/tests/test_b2b_quotes_and_pi.py`

**Interfaces:**
- Consumes:
  - `erp_core.catalog.service`: `CatalogService`
  - `erp_core.tenancy.audit`: `record_audit_event`
  - `erp_core.tenancy.dependencies`: `CurrentPrincipal`, `require_roles`
  - Port Protocols (`QuoteOrderPort`, `QuotePaymentPort`, `QuoteProfitPort`) lazily resolved to `OrderService` (Task 3.4), `PaymentService` (Task 3.6), and `OrderProfitService` (Task 3.6) at runtime when not explicitly injected
- Produces:
  - `QuoteService` (`create_lead`, `list_leads`, `create_opportunity`, `list_opportunities`, `create_quote`, `reprice_quote_version`, `send_quote`, `accept_quote`, `generate_pi_pdf`, `convert_quote_to_order`)
  - `ProformaInvoicePdfRenderer` (`render_pdf_bytes`, `render_and_upload`)
  - HTTP endpoints: `/api/v1/b2b/leads`, `/api/v1/b2b/opportunities`, `/api/v1/b2b/quotes`, `/api/v1/b2b/quotes/{id}/reprice-version`, `/api/v1/b2b/quotes/{id}/pdf`, `/api/v1/b2b/quotes/{id}/convert-to-order`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_b2b_quotes_and_pi.py`:

```python
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.catalog.schemas import MoqTierInput, ProductCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.crm.pdf_renderer import InMemoryPdfStorage, ProformaInvoicePdfRenderer
from erp_core.crm.quote_service import QuoteService
from erp_core.crm.schemas import (
    ConvertQuoteRequest,
    ConvertedOrderSummary,
    ConvertedReceivableSummary,
    LeadCreate,
    OpportunityCreate,
    QuoteCreate,
    QuoteItemInput,
    QuoteRepriceRequest,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, Incoterm, OrderMode, PaymentStatus, QuoteStatus, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal


def _migrator_url() -> str:
    return os.getenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_secret@localhost:5432/erp_saas",
    )


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )


class _PostgresConversionBridge:
    """Real PostgreSQL port implementation used in Task 3.3 before Tasks 3.4 & 3.6 land."""

    async def create_order_from_quote(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        order_mode: OrderMode,
        customer_id: UUID,
        quote_id: UUID,
        warehouse_id: UUID,
        currency: str,
        fx_rate_to_base: Decimal,
        subtotal_amount: Decimal,
        shipping_charged: Decimal,
        tax_collected: Decimal,
        total_amount: Decimal,
        channel_metadata: dict,
        items: list[dict],
    ) -> ConvertedOrderSummary:
        order_id = uuid4()
        order_no = f"ORD-{uuid4().hex[:8].upper()}"
        await session.execute(
            text(
                """
                INSERT INTO orders (
                    id, tenant_id, order_no, order_mode, customer_id, quote_id, currency,
                    fx_rate_to_base, subtotal_amount, shipping_charged, discount_amount,
                    tax_collected, total_amount, fulfillment_status, payment_status, channel_metadata
                ) VALUES (
                    :id, :tid, :ono, :mode, :cid, :qid, :ccy,
                    :fx, :sub, :ship, 0, :tax, :tot, 'pending_payment', 'unpaid', CAST(:meta AS jsonb)
                )
                """
            ),
            {
                "id": order_id,
                "tid": tenant_id,
                "ono": order_no,
                "mode": order_mode.value,
                "cid": customer_id,
                "qid": quote_id,
                "ccy": currency,
                "fx": fx_rate_to_base,
                "sub": subtotal_amount,
                "ship": shipping_charged,
                "tax": tax_collected,
                "tot": total_amount,
                "meta": __import__("json").dumps(channel_metadata),
            },
        )
        for item in sorted(items, key=lambda x: str(x["variant_id"])):
            await session.execute(
                text(
                    """
                    INSERT INTO order_items (
                        id, tenant_id, order_id, variant_id, sku_snapshot,
                        quantity, unit_price, unit_cost_base_snapshot, line_total
                    ) VALUES (
                        :id, :tid, :oid, :vid, :sku, :qty, :up, :uc, :lt
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "tid": tenant_id,
                    "oid": order_id,
                    "vid": item["variant_id"],
                    "sku": item["sku_snapshot"],
                    "qty": item["quantity"],
                    "up": item["unit_price"],
                    "uc": item["unit_cost_base_snapshot"],
                    "lt": item["line_total"],
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO inventory_ledger (
                        tenant_id, warehouse_id, variant_id, event_type,
                        qty_delta_on_hand, qty_delta_reserved, reference_type, reference_id, idempotency_key
                    ) VALUES (
                        :tid, :wid, :vid, 'reserve_order', 0, :qty, 'order', :oid, :ikey
                    )
                    """
                ),
                {
                    "tid": tenant_id,
                    "wid": warehouse_id,
                    "vid": item["variant_id"],
                    "qty": item["quantity"],
                    "oid": order_id,
                    "ikey": f"quote-conv:{order_id}:{item['variant_id']}",
                },
            )
            updated = await session.execute(
                text(
                    """
                    UPDATE inventory_balances
                    SET reserved_qty = reserved_qty + :qty
                    WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                      AND (on_hand_qty - reserved_qty) >= :qty
                    RETURNING reserved_qty
                    """
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": item["variant_id"], "qty": item["quantity"]},
            )
            if updated.scalar_one_or_none() is None:
                raise AppError(slug="insufficient-stock", status_code=409, code="ERR_INSUFFICIENT_STOCK", title="Insufficient Stock", detail="Not enough stock.")

        return ConvertedOrderSummary(
            id=order_id,
            order_no=order_no,
            order_mode=order_mode,
            fulfillment_status=FulfillmentStatus.PENDING_PAYMENT,
            payment_status=PaymentStatus.UNPAID,
            total_amount=total_amount,
            currency=currency,
        )

    async def create_b2b_milestone_receivables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        total_amount: Decimal,
        currency: str,
        deposit_ratio: Decimal,
    ) -> list[ConvertedReceivableSummary]:
        dep = (total_amount * deposit_ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        bal = (total_amount - dep).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        out: list[ConvertedReceivableSummary] = []
        for label, amt in [("b2b_30_deposit", dep), ("b2b_70_balance", bal)]:
            rid = uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO receivables_payables (
                        id, tenant_id, order_id, direction, milestone_label, due_amount, paid_amount, currency, status
                    ) VALUES (
                        :id, :tid, :oid, 'receivable', :lbl, :amt, 0, :ccy, 'open'
                    )
                    """
                ),
                {"id": rid, "tid": tenant_id, "oid": order_id, "lbl": label, "amt": amt, "ccy": currency},
            )
            out.append(
                ConvertedReceivableSummary(
                    id=rid,
                    milestone_label=label,
                    due_amount=amt,
                    paid_amount=Decimal("0.00"),
                    currency=currency,
                    status="open",
                )
            )
        return out

    async def recalculate_order_profit(self, session: AsyncSession, tenant_id: UUID, order_id: UUID) -> None:
        await session.execute(
            text(
                """
                INSERT INTO order_profit_snapshots (order_id, tenant_id, base_currency)
                VALUES (:oid, :tid, 'CNY')
                ON CONFLICT (order_id) DO NOTHING
                """
            ),
            {"oid": order_id, "tid": tenant_id},
        )


@pytest.mark.asyncio
async def test_quanzhou_led_500pc_fob_quote_pdf_expired_fx_and_convert() -> None:
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    rt_engine = create_async_engine(_runtime_url(), echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    manager_id = uuid4()
    agent_id = uuid4()
    customer_id = uuid4()
    company_id = uuid4()
    warehouse_id = uuid4()

    manager = CurrentPrincipal(user_id=manager_id, tenant_id=tenant_id, role=UserRole.MANAGER, email="mgr@qz.cn")
    agent = CurrentPrincipal(user_id=agent_id, tenant_id=tenant_id, role=UserRole.AGENT, email="agent@qz.cn")

    try:
        async with mig_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tenant_id, "name": "Quanzhou Smart LED Factory", "slug": f"qz-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO b2b_companies (id, tenant_id, legal_name, country_code, default_incoterm)
                    VALUES (:id, :tid, 'Iberia Lighting Distributors SL', 'ES', 'FOB')
                    """
                ),
                {"id": company_id, "tid": tenant_id},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO customers (id, tenant_id, customer_type, company_id, display_name, primary_email, preferred_language)
                    VALUES (:id, :tid, 'b2b_contact', :cid, 'Carlos Mendez', 'carlos@iberialight.es', 'es')
                    """
                ),
                {"id": customer_id, "tid": tenant_id, "cid": company_id},
            )

        catalog_svc = CatalogService()
        storage = InMemoryPdfStorage()
        bridge = _PostgresConversionBridge()
        quote_svc = QuoteService(
            order_port=bridge,
            payment_port=bridge,
            profit_port=bridge,
            pdf_renderer=ProformaInvoicePdfRenderer(storage=storage),
        )

        async with tenant_session(rt_factory, tenant_id) as session:
            prod = await catalog_svc.create_product(
                session,
                tenant_id,
                actor_id=str(manager_id),
                payload=ProductCreate(
                    spu_code="QZ-LED-STRIP-5M",
                    category="led_lighting",
                    hs_code="9405429000",
                    origin_country="CN",
                    i18n_specs={
                        "en": {"title": "5M IP65 Smart LED Strip 24V"},
                        "es": {"title": "Tira LED Inteligente 5M IP65 24V"},
                    },
                    certifications=["CE", "RoHS"],
                ),
            )
            variant = await catalog_svc.create_variant(
                session,
                tenant_id,
                actor_id=str(manager_id),
                role=UserRole.MANAGER,
                payload=VariantCreate(
                    product_id=prod.id,
                    sku="LED-STRIP-5M-IP65",
                    cost_price_base=Decimal("42.0000"),
                    base_currency="CNY",
                    min_margin_floor_pct=Decimal("15.00"),
                    retail_price_usd=Decimal("18.50"),
                    b2b_moq_tiers=[
                        MoqTierInput(min_qty=50, unit_price_usd=Decimal("12.00")),
                        MoqTierInput(min_qty=500, unit_price_usd=Decimal("8.50")),
                    ],
                    weight_grams=300,
                ),
            )
            # Seed 600 units on hand
            await session.execute(
                text(
                    """
                    INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty)
                    VALUES (:tid, :wid, :vid, 600, 0)
                    """
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
            )

            lead = await quote_svc.create_lead(
                session,
                tenant_id,
                LeadCreate(
                    customer_id=customer_id,
                    source_channel="made_in_china_email",
                    lead_type="b2b_inquiry",
                    qualification_score=88,
                    extracted_requirements={"sku": "LED-STRIP-5M-IP65", "qty": 500, "incoterm": "FOB"},
                ),
            )
            opp = await quote_svc.create_opportunity(
                session,
                tenant_id,
                OpportunityCreate(
                    lead_id=lead.id,
                    company_id=company_id,
                    customer_id=customer_id,
                    title="Iberia Lighting 500pcs LED Strip FOB Xiamen",
                    expected_amount=Decimal("4450.00"),
                    currency="USD",
                    owner_user_id=manager_id,
                ),
            )

            # Agent trying to override below margin floor must be rejected (403)
            with pytest.raises(AppError) as exc_override:
                await quote_svc.create_quote(
                    session,
                    tenant_id,
                    principal=agent,
                    payload=QuoteCreate(
                        opportunity_id=opp.id,
                        is_proforma_invoice=True,
                        incoterm=Incoterm.FOB,
                        port_of_loading="Xiamen, China",
                        port_of_destination="Valencia, Spain",
                        payment_terms="30% T/T deposit, 70% before shipment",
                        base_currency="CNY",
                        quote_currency="USD",
                        fx_rate_snapshot=Decimal("7.200000"),
                        fx_locked_until=datetime.now(timezone.utc) - timedelta(hours=1),
                        freight_fee=Decimal("200.00"),
                        override_margin_floor=True,
                        items=[QuoteItemInput(variant_id=variant.id, quantity=500, unit_price_quote_ccy=Decimal("6.00"))],
                    ),
                )
            assert exc_override.value.status_code == 403

            # Create valid v1 quote (expired FX lock for testing rejection)
            v1 = await quote_svc.create_quote(
                session,
                tenant_id,
                principal=manager,
                payload=QuoteCreate(
                    opportunity_id=opp.id,
                    is_proforma_invoice=True,
                    incoterm=Incoterm.FOB,
                    port_of_loading="Xiamen, China",
                    port_of_destination="Valencia, Spain",
                    payment_terms="30% T/T deposit, 70% before shipment",
                    base_currency="CNY",
                    quote_currency="USD",
                    fx_rate_snapshot=Decimal("7.200000"),
                    fx_locked_until=datetime.now(timezone.utc) - timedelta(hours=2),
                    freight_fee=Decimal("200.00"),
                    items=[QuoteItemInput(variant_id=variant.id, quantity=500, language="es")],
                ),
            )
            assert v1.version == 1
            assert v1.total_amount == Decimal("4450.00")
            assert v1.items[0].moq_tier_applied == 500
            assert v1.items[0].unit_price_quote_ccy == Decimal("8.5000")

            # Generate multilingual WeasyPrint PDF
            pdf_result = await quote_svc.generate_pi_pdf(session, tenant_id, v1.id, language="es")
            assert pdf_result.pdf_r2_key.endswith(f"{v1.quote_no}-v1.pdf")
            stored_bytes = storage.objects[pdf_result.pdf_r2_key]
            assert stored_bytes.startswith(b"%PDF-")

            await quote_svc.send_quote(session, tenant_id, manager, v1.id)
            await quote_svc.accept_quote(session, tenant_id, manager, v1.id)

            # Converting expired-FX quote MUST fail
            with pytest.raises(AppError) as exc_fx:
                await quote_svc.convert_quote_to_order(
                    session,
                    tenant_id,
                    principal=manager,
                    quote_id=v1.id,
                    payload=ConvertQuoteRequest(warehouse_id=warehouse_id, order_mode=OrderMode.B2B_WHOLESALE),
                )
            assert exc_fx.value.code == "ERR_QUOTE_FX_EXPIRED"

            # Reprice creates immutable v2 with fresh FX lock (leaving v1 untouched)
            v2 = await quote_svc.reprice_quote_version(
                session,
                tenant_id,
                principal=manager,
                quote_id=v1.id,
                payload=QuoteRepriceRequest(
                    fx_rate_snapshot=Decimal("7.250000"),
                    fx_locked_until=datetime.now(timezone.utc) + timedelta(days=7),
                    freight_fee=Decimal("250.00"),
                ),
            )
            assert v2.version == 2
            assert v2.quote_no == v1.quote_no
            assert v2.total_amount == Decimal("4500.00")
            v1_check = await quote_svc.get_quote(session, tenant_id, v1.id)
            assert v1_check.total_amount == Decimal("4450.00")

            await quote_svc.send_quote(session, tenant_id, manager, v2.id)
            await quote_svc.accept_quote(session, tenant_id, manager, v2.id)

            # 1-Click convert v2 -> B2B Wholesale Order + 30%/70% Receivables + Reserved Stock
            conv = await quote_svc.convert_quote_to_order(
                session,
                tenant_id,
                principal=manager,
                quote_id=v2.id,
                payload=ConvertQuoteRequest(warehouse_id=warehouse_id, order_mode=OrderMode.B2B_WHOLESALE),
            )
            assert conv.quote.status == QuoteStatus.CONVERTED
            assert conv.order.order_mode == OrderMode.B2B_WHOLESALE
            assert conv.order.fulfillment_status == FulfillmentStatus.PENDING_PAYMENT
            assert conv.order.payment_status == PaymentStatus.UNPAID
            assert conv.order.total_amount == Decimal("4500.00")
            assert len(conv.receivables) == 2
            milestones = {r.milestone_label: r.due_amount for r in conv.receivables}
            assert milestones["b2b_30_deposit"] == Decimal("1350.00")
            assert milestones["b2b_70_balance"] == Decimal("3150.00")

            bal_row = (
                await session.execute(
                    text(
                        "SELECT on_hand_qty, reserved_qty FROM inventory_balances WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid"
                    ),
                    {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
                )
            ).one()
            assert bal_row.on_hand_qty == 600
            assert bal_row.reserved_qty == 500
    finally:
        async with mig_engine.begin() as conn:
            await conn.execute(text("DELETE FROM order_profit_snapshots WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM receivables_payables WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM inventory_ledger WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM inventory_balances WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM order_items WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM orders WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM quote_items WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM quotes WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM opportunities WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM leads WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM product_variants WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM products WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM b2b_companies WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_b2b_quotes_and_pi.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.crm.schemas'`

- [ ] **Step 3: Write `crm/schemas.py`, `crm/pdf_renderer.py`, `crm/quote_service.py`, `crm/router.py`, and wire `api_main.py`**

Create `apps/backend/src/erp_core/crm/schemas.py`:

```python
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.shared.enums import FulfillmentStatus, Incoterm, LeadStatus, OpportunityStage, OrderMode, PaymentStatus, QuoteStatus


class LeadCreate(BaseModel):
    customer_id: UUID
    source_channel: str = Field(min_length=1, max_length=64)
    utm_campaign: str | None = None
    lead_type: Literal["b2b_inquiry", "b2c_vip_pre_sale"] = "b2b_inquiry"
    qualification_score: int = Field(default=0, ge=0, le=100)
    extracted_requirements: dict[str, Any] = Field(default_factory=dict)


class LeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    customer_id: UUID
    source_channel: str
    utm_campaign: str | None
    lead_type: str
    qualification_score: int
    extracted_requirements: dict[str, Any]
    status: LeadStatus
    created_at: datetime


class OpportunityCreate(BaseModel):
    lead_id: UUID | None = None
    company_id: UUID | None = None
    customer_id: UUID
    title: str = Field(min_length=1, max_length=255)
    stage: OpportunityStage = OpportunityStage.QUOTING
    expected_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    currency: str = Field(default="USD", min_length=3, max_length=3)
    expected_close_date: date | None = None
    owner_user_id: UUID


class OpportunityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    lead_id: UUID | None
    company_id: UUID | None
    customer_id: UUID
    title: str
    stage: OpportunityStage
    expected_amount: Decimal
    currency: str
    expected_close_date: date | None
    owner_user_id: UUID
    created_at: datetime
    company_name: str | None = None


class OpportunityStageUpdate(BaseModel):
    stage: OpportunityStage


class QuoteItemInput(BaseModel):
    variant_id: UUID
    quantity: int = Field(gt=0)
    unit_price_quote_ccy: Decimal | None = Field(default=None, gt=Decimal("0"))
    description_override: str | None = None
    language: str = "en"


class QuoteItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    quote_id: UUID
    variant_id: UUID
    sku_snapshot: str
    description_i18n: str
    quantity: int
    moq_tier_applied: int | None
    unit_price_quote_ccy: Decimal
    unit_cost_base_snapshot: Decimal
    line_total_quote_ccy: Decimal


class QuoteCreate(BaseModel):
    opportunity_id: UUID
    is_proforma_invoice: bool = True
    incoterm: Incoterm = Incoterm.FOB
    port_of_loading: str | None = "Xiamen, China"
    port_of_destination: str | None = None
    payment_terms: str = "30% T/T deposit, 70% before shipment"
    base_currency: str = Field(default="CNY", min_length=3, max_length=3)
    quote_currency: str = Field(default="USD", min_length=3, max_length=3)
    fx_rate_snapshot: Decimal = Field(gt=Decimal("0"))
    fx_locked_until: datetime
    freight_fee: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    insurance_fee: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    customs_duty_estimate: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    override_margin_floor: bool = False
    items: list[QuoteItemInput] = Field(min_length=1)


class QuoteRepriceRequest(BaseModel):
    fx_rate_snapshot: Decimal = Field(gt=Decimal("0"))
    fx_locked_until: datetime
    freight_fee: Decimal | None = Field(default=None, ge=Decimal("0"))
    insurance_fee: Decimal | None = Field(default=None, ge=Decimal("0"))
    customs_duty_estimate: Decimal | None = Field(default=None, ge=Decimal("0"))
    payment_terms: str | None = None
    override_margin_floor: bool = False
    items: list[QuoteItemInput] | None = None


class QuoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    opportunity_id: UUID
    quote_no: str
    version: int
    is_proforma_invoice: bool
    incoterm: Incoterm
    port_of_loading: str | None
    port_of_destination: str | None
    payment_terms: str
    base_currency: str
    quote_currency: str
    fx_rate_snapshot: Decimal
    fx_locked_until: datetime
    freight_fee: Decimal
    insurance_fee: Decimal
    customs_duty_estimate: Decimal
    total_amount: Decimal
    pdf_r2_key: str | None
    status: QuoteStatus
    converted_order_id: UUID | None
    created_at: datetime
    items: list[QuoteItemRead] = Field(default_factory=list)


class QuotePdfResponse(BaseModel):
    quote_id: UUID
    quote_no: str
    version: int
    pdf_r2_key: str
    download_url: str


class ConvertedOrderSummary(BaseModel):
    id: UUID
    order_no: str
    order_mode: OrderMode
    fulfillment_status: FulfillmentStatus
    payment_status: PaymentStatus
    total_amount: Decimal
    currency: str


class ConvertedReceivableSummary(BaseModel):
    id: UUID
    milestone_label: str
    due_amount: Decimal
    paid_amount: Decimal
    currency: str
    status: str


class ConvertQuoteRequest(BaseModel):
    warehouse_id: UUID
    order_mode: Literal[OrderMode.B2B_WHOLESALE, OrderMode.B2B_SAMPLE] = OrderMode.B2B_WHOLESALE
    deposit_ratio: Decimal = Field(default=Decimal("0.30"), gt=Decimal("0"), le=Decimal("1.00"))


class ConvertQuoteResponse(BaseModel):
    quote: QuoteRead
    order: ConvertedOrderSummary
    receivables: list[ConvertedReceivableSummary]
```

Create `apps/backend/src/erp_core/crm/pdf_renderer.py`:

```python
from __future__ import annotations

import asyncio
import html
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

import boto3
import weasyprint


class PdfStoragePort(Protocol):
    async def put_bytes(self, key: str, data: bytes, content_type: str = "application/pdf") -> str: ...
    async def get_presigned_url(self, key: str, expires_seconds: int = 3600) -> str: ...


class InMemoryPdfStorage:
    """In-memory object storage implementation for unit and integration tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes, content_type: str = "application/pdf") -> str:
        self.objects[key] = data
        return key

    async def get_presigned_url(self, key: str, expires_seconds: int = 3600) -> str:
        return f"https://storage.local/{key}?expires={expires_seconds}"


class S3PdfStorage:
    """MinIO / Cloudflare R2 S3 storage adapter backed by boto3."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        bucket: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url or os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
        self.bucket = bucket or os.getenv("S3_BUCKET", "erp-assets")
        self.access_key = access_key or os.getenv("S3_ACCESS_KEY", "minioadmin")
        self.secret_key = secret_key or os.getenv("S3_SECRET_KEY", "minioadmin")
        self.region = region or os.getenv("S3_REGION", "auto")

    def _client(self):
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
        )

    async def put_bytes(self, key: str, data: bytes, content_type: str = "application/pdf") -> str:
        def _upload() -> None:
            client = self._client()
            client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

        await asyncio.to_thread(_upload)
        return key

    async def get_presigned_url(self, key: str, expires_seconds: int = 3600) -> str:
        def _presign() -> str:
            client = self._client()
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )

        return await asyncio.to_thread(_presign)


PI_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "title": "PROFORMA INVOICE",
        "quote_title": "COMMERCIAL QUOTATION",
        "no": "PI / Quote No.",
        "date": "Issue Date",
        "valid_until": "FX & Price Valid Until",
        "seller": "Seller",
        "buyer": "Buyer",
        "incoterm": "Incoterm",
        "loading": "Port of Loading",
        "destination": "Port of Destination",
        "payment_terms": "Payment Terms",
        "sku": "SKU",
        "description": "Description",
        "qty": "Qty",
        "unit_price": "Unit Price",
        "line_total": "Line Total",
        "freight": "Freight",
        "insurance": "Insurance",
        "duty": "Estimated Duty",
        "total": "Total Amount",
    },
    "es": {
        "title": "FACTURA PROFORMA",
        "quote_title": "COTIZACION COMERCIAL",
        "no": "N. de Documento",
        "date": "Fecha de Emision",
        "valid_until": "Valido Hasta (TC Bloqueado)",
        "seller": "Vendedor",
        "buyer": "Comprador",
        "incoterm": "Incoterm",
        "loading": "Puerto de Embarque",
        "destination": "Puerto de Destino",
        "payment_terms": "Condiciones de Pago",
        "sku": "SKU",
        "description": "Descripcion",
        "qty": "Cant.",
        "unit_price": "Precio Unit.",
        "line_total": "Subtotal",
        "freight": "Flete",
        "insurance": "Seguro",
        "duty": "Aranceles Est.",
        "total": "Importe Total",
    },
    "zh": {
        "title": "形式发票 (PROFORMA INVOICE)",
        "quote_title": "商业报价单 (QUOTATION)",
        "no": "单据编号",
        "date": "签发日期",
        "valid_until": "锁汇有效期至",
        "seller": "卖方",
        "buyer": "买方",
        "incoterm": "贸易术语",
        "loading": "装运港",
        "destination": "目的港",
        "payment_terms": "付款条款",
        "sku": "SKU",
        "description": "品名与规格",
        "qty": "数量",
        "unit_price": "单价",
        "line_total": "小计",
        "freight": "运费",
        "insurance": "保险费",
        "duty": "预估关税",
        "total": "总金额",
    },
}


@dataclass(slots=True)
class PiLineRenderItem:
    sku: str
    description: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


@dataclass(slots=True)
class PiRenderContext:
    tenant_id: UUID
    seller_name: str
    buyer_company: str
    buyer_contact: str
    buyer_email: str | None
    quote_no: str
    version: int
    is_proforma_invoice: bool
    incoterm: str
    port_of_loading: str
    port_of_destination: str
    payment_terms: str
    quote_currency: str
    fx_rate_snapshot: Decimal
    fx_locked_until: datetime
    issued_at: datetime
    freight_fee: Decimal
    insurance_fee: Decimal
    customs_duty_estimate: Decimal
    total_amount: Decimal
    items: list[PiLineRenderItem]
    language: str = "en"


def build_pi_html(ctx: PiRenderContext) -> str:
    labels = PI_LABELS.get(ctx.language, PI_LABELS["en"])
    heading = labels["title"] if ctx.is_proforma_invoice else labels["quote_title"]
    rows_html = []
    for item in ctx.items:
        rows_html.append(
            f"""
            <tr>
                <td>{html.escape(item.sku)}</td>
                <td>{html.escape(item.description)}</td>
                <td class="num">{item.quantity}</td>
                <td class="num">{html.escape(ctx.quote_currency)} {item.unit_price:.4f}</td>
                <td class="num">{html.escape(ctx.quote_currency)} {item.line_total:.2f}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="{html.escape(ctx.language)}">
<head>
<meta charset="utf-8" />
<style>
  @page {{ size: A4; margin: 18mm; }}
  body {{ font-family: "DejaVu Sans", Arial, sans-serif; color: #111827; font-size: 10pt; }}
  .header {{ display: flex; justify-content: space-between; border-bottom: 2px solid #1f2937; padding-bottom: 10px; margin-bottom: 16px; }}
  .title {{ font-size: 18pt; font-weight: 700; color: #0f172a; }}
  .meta {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; }}
  .meta td {{ padding: 4px 8px; vertical-align: top; border: 1px solid #e5e7eb; }}
  .items {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; }}
  .items th {{ background: #f3f4f6; text-align: left; padding: 8px; border: 1px solid #d1d5db; }}
  .items td {{ padding: 8px; border: 1px solid #e5e7eb; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .summary {{ width: 45%; margin-left: auto; border-collapse: collapse; }}
  .summary td {{ padding: 6px 8px; border-bottom: 1px solid #e5e7eb; }}
  .summary tr.total td {{ font-weight: 700; font-size: 11pt; border-top: 2px solid #111827; }}
</style>
</head>
<body>
  <div class="header">
    <div>
      <div class="title">{html.escape(heading)}</div>
      <div>{html.escape(labels["no"])}: <strong>{html.escape(ctx.quote_no)} v{ctx.version}</strong></div>
    </div>
    <div>
      <div>{html.escape(labels["date"])}: {ctx.issued_at:%Y-%m-%d}</div>
      <div>{html.escape(labels["valid_until"])}: {ctx.fx_locked_until:%Y-%m-%d %H:%M UTC}</div>
    </div>
  </div>
  <table class="meta">
    <tr>
      <td><strong>{html.escape(labels["seller"])}</strong><br/>{html.escape(ctx.seller_name)}</td>
      <td><strong>{html.escape(labels["buyer"])}</strong><br/>{html.escape(ctx.buyer_company)}<br/>{html.escape(ctx.buyer_contact)} {html.escape(ctx.buyer_email or "")}</td>
    </tr>
    <tr>
      <td><strong>{html.escape(labels["incoterm"])}:</strong> {html.escape(ctx.incoterm)}<br/><strong>{html.escape(labels["loading"])}:</strong> {html.escape(ctx.port_of_loading)}</td>
      <td><strong>{html.escape(labels["destination"])}:</strong> {html.escape(ctx.port_of_destination)}<br/><strong>{html.escape(labels["payment_terms"])}:</strong> {html.escape(ctx.payment_terms)}</td>
    </tr>
  </table>
  <table class="items">
    <thead>
      <tr>
        <th>{html.escape(labels["sku"])}</th>
        <th>{html.escape(labels["description"])}</th>
        <th class="num">{html.escape(labels["qty"])}</th>
        <th class="num">{html.escape(labels["unit_price"])}</th>
        <th class="num">{html.escape(labels["line_total"])}</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows_html)}
    </tbody>
  </table>
  <table class="summary">
    <tr><td>{html.escape(labels["freight"])}</td><td class="num">{html.escape(ctx.quote_currency)} {ctx.freight_fee:.2f}</td></tr>
    <tr><td>{html.escape(labels["insurance"])}</td><td class="num">{html.escape(ctx.quote_currency)} {ctx.insurance_fee:.2f}</td></tr>
    <tr><td>{html.escape(labels["duty"])}</td><td class="num">{html.escape(ctx.quote_currency)} {ctx.customs_duty_estimate:.2f}</td></tr>
    <tr class="total"><td>{html.escape(labels["total"])}</td><td class="num">{html.escape(ctx.quote_currency)} {ctx.total_amount:.2f}</td></tr>
  </table>
</body>
</html>"""


class ProformaInvoicePdfRenderer:
    """WeasyPrint HTML-to-PDF renderer with MinIO/R2 persistence."""

    def __init__(self, storage: PdfStoragePort | None = None) -> None:
        self.storage: PdfStoragePort = storage or S3PdfStorage()

    @staticmethod
    def render_pdf_bytes(ctx: PiRenderContext) -> bytes:
        html_doc = build_pi_html(ctx)
        pdf_bytes = weasyprint.HTML(string=html_doc).write_pdf()
        if not pdf_bytes:
            raise RuntimeError("WeasyPrint produced empty PDF bytes")
        return pdf_bytes

    async def render_and_upload(self, ctx: PiRenderContext) -> tuple[str, str]:
        pdf_bytes = await asyncio.to_thread(self.render_pdf_bytes, ctx)
        object_key = f"tenants/{ctx.tenant_id}/quotes/{ctx.quote_no}-v{ctx.version}.pdf"
        await self.storage.put_bytes(object_key, pdf_bytes, content_type="application/pdf")
        url = await self.storage.get_presigned_url(object_key)
        return object_key, url
```

Create `apps/backend/src/erp_core/crm/quote_service.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.service import CatalogService
from erp_core.crm.models import Lead, Opportunity, Quote, QuoteItem
from erp_core.crm.pdf_renderer import PiLineRenderItem, PiRenderContext, ProformaInvoicePdfRenderer
from erp_core.crm.schemas import (
    ConvertQuoteRequest,
    ConvertQuoteResponse,
    ConvertedOrderSummary,
    ConvertedReceivableSummary,
    LeadCreate,
    LeadRead,
    OpportunityCreate,
    OpportunityRead,
    OpportunityStageUpdate,
    QuoteCreate,
    QuoteItemInput,
    QuoteItemRead,
    QuotePdfResponse,
    QuoteRead,
    QuoteRepriceRequest,
)
from erp_core.shared.enums import FulfillmentStatus, LeadStatus, OpportunityStage, OrderMode, QuoteStatus, UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal


class QuoteOrderPort(Protocol):
    async def create_order_from_quote(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        order_mode: OrderMode,
        customer_id: UUID,
        quote_id: UUID,
        warehouse_id: UUID,
        currency: str,
        fx_rate_to_base: Decimal,
        subtotal_amount: Decimal,
        shipping_charged: Decimal,
        tax_collected: Decimal,
        total_amount: Decimal,
        channel_metadata: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> ConvertedOrderSummary: ...


class QuotePaymentPort(Protocol):
    async def create_b2b_milestone_receivables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        total_amount: Decimal,
        currency: str,
        deposit_ratio: Decimal,
    ) -> list[ConvertedReceivableSummary]: ...


class QuoteProfitPort(Protocol):
    async def recalculate_order_profit(self, session: AsyncSession, tenant_id: UUID, order_id: UUID) -> Any: ...


class _RuntimeConversionAdapter:
    """Lazy runtime bridge to Tasks 3.4 OrderService and 3.6 Finance services."""

    async def create_order_from_quote(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        order_mode: OrderMode,
        customer_id: UUID,
        quote_id: UUID,
        warehouse_id: UUID,
        currency: str,
        fx_rate_to_base: Decimal,
        subtotal_amount: Decimal,
        shipping_charged: Decimal,
        tax_collected: Decimal,
        total_amount: Decimal,
        channel_metadata: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> ConvertedOrderSummary:
        from erp_core.orders.order_service import OrderService
        from erp_core.orders.schemas import OrderCreateInternal, OrderItemInternal

        order = await OrderService().create_order_atomic(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            payload=OrderCreateInternal(
                order_mode=order_mode,
                customer_id=customer_id,
                quote_id=quote_id,
                warehouse_id=warehouse_id,
                currency=currency,
                fx_rate_to_base=fx_rate_to_base,
                subtotal_amount=subtotal_amount,
                shipping_charged=shipping_charged,
                discount_amount=Decimal("0.00"),
                tax_collected=tax_collected,
                total_amount=total_amount,
                initial_fulfillment_status=FulfillmentStatus.PENDING_PAYMENT,
                channel_metadata=channel_metadata,
                items=[OrderItemInternal(**item) for item in items],
            ),
        )
        return ConvertedOrderSummary(
            id=order.id,
            order_no=order.order_no,
            order_mode=order.order_mode,
            fulfillment_status=order.fulfillment_status,
            payment_status=order.payment_status,
            total_amount=order.total_amount,
            currency=order.currency,
        )

    async def create_b2b_milestone_receivables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        total_amount: Decimal,
        currency: str,
        deposit_ratio: Decimal,
    ) -> list[ConvertedReceivableSummary]:
        from erp_core.finance.payment_service import PaymentService

        rows = await PaymentService().create_b2b_milestone_receivables(
            session,
            tenant_id=tenant_id,
            order_id=order_id,
            total_amount=total_amount,
            currency=currency,
            deposit_ratio=deposit_ratio,
        )
        return [
            ConvertedReceivableSummary(
                id=r.id,
                milestone_label=r.milestone_label,
                due_amount=r.due_amount,
                paid_amount=r.paid_amount,
                currency=r.currency,
                status=r.status,
            )
            for r in rows
        ]

    async def recalculate_order_profit(self, session: AsyncSession, tenant_id: UUID, order_id: UUID) -> Any:
        from erp_core.finance.profit_service import OrderProfitService

        return await OrderProfitService().recalculate_order_profit(session, tenant_id=tenant_id, order_id=order_id)


class QuoteService:
    """B2B leads, opportunities, immutable versioned quotes/PIs, PDF export, and 1-click conversion."""

    def __init__(
        self,
        catalog_service: CatalogService | None = None,
        order_port: QuoteOrderPort | None = None,
        payment_port: QuotePaymentPort | None = None,
        profit_port: QuoteProfitPort | None = None,
        pdf_renderer: ProformaInvoicePdfRenderer | None = None,
    ) -> None:
        self.catalog = catalog_service or CatalogService()
        runtime_adapter = _RuntimeConversionAdapter()
        self.order_port: QuoteOrderPort = order_port or runtime_adapter
        self.payment_port: QuotePaymentPort = payment_port or runtime_adapter
        self.profit_port: QuoteProfitPort = profit_port or runtime_adapter
        self.pdf_renderer = pdf_renderer or ProformaInvoicePdfRenderer()

    async def create_lead(self, session: AsyncSession, tenant_id: UUID, payload: LeadCreate) -> LeadRead:
        lead = Lead(
            tenant_id=tenant_id,
            customer_id=payload.customer_id,
            source_channel=payload.source_channel,
            utm_campaign=payload.utm_campaign,
            lead_type=payload.lead_type,
            qualification_score=payload.qualification_score,
            extracted_requirements=payload.extracted_requirements,
            status=LeadStatus.NEW,
        )
        session.add(lead)
        await session.flush()
        return LeadRead.model_validate(lead)

    async def list_leads(
        self, session: AsyncSession, tenant_id: UUID, limit: int = 20, offset: int = 0
    ) -> PaginatedResponse[LeadRead]:
        total = int(
            (await session.execute(select(func.count()).select_from(Lead).where(Lead.tenant_id == tenant_id))).scalar_one()
        )
        rows = (
            await session.execute(
                select(Lead).where(Lead.tenant_id == tenant_id).order_by(Lead.created_at.desc()).limit(limit).offset(offset)
            )
        ).scalars().all()
        items = [LeadRead.model_validate(r) for r in rows]
        return PaginatedResponse(items=items, total=total, limit=limit, offset=offset, has_more=(offset + len(items)) < total)

    async def create_opportunity(
        self, session: AsyncSession, tenant_id: UUID, payload: OpportunityCreate
    ) -> OpportunityRead:
        opp = Opportunity(
            tenant_id=tenant_id,
            lead_id=payload.lead_id,
            company_id=payload.company_id,
            customer_id=payload.customer_id,
            title=payload.title.strip(),
            stage=payload.stage,
            expected_amount=payload.expected_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            currency=payload.currency.upper(),
            expected_close_date=payload.expected_close_date,
            owner_user_id=payload.owner_user_id,
        )
        session.add(opp)
        await session.flush()
        return OpportunityRead.model_validate(opp)

    async def list_opportunities(
        self, session: AsyncSession, tenant_id: UUID, limit: int = 20, offset: int = 0
    ) -> PaginatedResponse[OpportunityRead]:
        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(Opportunity).where(Opportunity.tenant_id == tenant_id)
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                select(Opportunity)
                .where(Opportunity.tenant_id == tenant_id)
                .order_by(Opportunity.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        items = [OpportunityRead.model_validate(r) for r in rows]
        return PaginatedResponse(items=items, total=total, limit=limit, offset=offset, has_more=(offset + len(items)) < total)

    async def update_opportunity_stage(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        opportunity_id: UUID,
        stage: OpportunityStage,
    ) -> OpportunityRead:
        opp = (
            await session.execute(
                select(Opportunity).where(Opportunity.tenant_id == tenant_id, Opportunity.id == opportunity_id)
            )
        ).scalar_one_or_none()
        if opp is None:
            raise AppError(
                slug="opportunity-not-found",
                status_code=404,
                code="ERR_OPPORTUNITY_NOT_FOUND",
                title="Opportunity Not Found",
                detail=f"Opportunity '{opportunity_id}' not found.",
            )
        prev_stage = opp.stage.value
        opp.stage = stage
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="b2b.opportunity.stage_updated",
            resource_type="opportunity",
            resource_id=opp.id,
            before_state={"stage": prev_stage},
            after_state={"stage": stage.value},
        )
        return OpportunityRead.model_validate(opp)

    async def _build_quote_items_and_check_margin(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        item_inputs: list[QuoteItemInput],
        fx_rate_snapshot: Decimal,
        override_margin_floor: bool,
    ) -> tuple[list[QuoteItem], Decimal, bool]:
        if override_margin_floor and principal.role == UserRole.AGENT:
            raise AppError(
                slug="margin-floor-override-forbidden",
                status_code=403,
                code="ERR_MARGIN_FLOOR_OVERRIDE_FORBIDDEN",
                title="Margin Override Forbidden",
                detail="Agent role cannot override variant min_margin_floor_pct.",
            )

        built_items: list[QuoteItem] = []
        subtotal_quote_ccy = Decimal("0.00")
        margin_override_used = False

        for item_in in item_inputs:
            vp = await self.catalog.get_variant_pricing_internal(session, tenant_id, item_in.variant_id)
            if item_in.unit_price_quote_ccy is not None:
                unit_price = item_in.unit_price_quote_ccy.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                moq_tier = None
            else:
                unit_price, moq_tier = await self.catalog.resolve_moq_unit_price(
                    vp.retail_price_usd, vp.b2b_moq_tiers, item_in.quantity
                )

            unit_price_base = (unit_price * fx_rate_snapshot).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if unit_price_base <= Decimal("0"):
                margin_pct = Decimal("-100.00")
            else:
                margin_pct = (
                    (unit_price_base - vp.cost_price_base) / unit_price_base * Decimal("100")
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            if margin_pct < vp.min_margin_floor_pct:
                if not override_margin_floor:
                    raise AppError(
                        slug="margin-floor-violation",
                        status_code=422,
                        code="ERR_MARGIN_FLOOR_VIOLATION",
                        title="Margin Floor Violation",
                        detail=(
                            f"SKU '{vp.sku}' margin {margin_pct}% is below floor {vp.min_margin_floor_pct}%."
                        ),
                    )
                margin_override_used = True

            line_total = (Decimal(item_in.quantity) * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            subtotal_quote_ccy += line_total

            lang_specs = vp.i18n_specs.get(item_in.language) or vp.i18n_specs.get("en") or {}
            description = item_in.description_override or lang_specs.get("title") or vp.sku

            built_items.append(
                QuoteItem(
                    tenant_id=tenant_id,
                    variant_id=vp.id,
                    sku_snapshot=vp.sku,
                    description_i18n=str(description),
                    quantity=item_in.quantity,
                    moq_tier_applied=moq_tier,
                    unit_price_quote_ccy=unit_price,
                    unit_cost_base_snapshot=vp.cost_price_base,
                    line_total_quote_ccy=line_total,
                )
            )

        return built_items, subtotal_quote_ccy, margin_override_used

    async def _serialize_quote(self, session: AsyncSession, tenant_id: UUID, quote: Quote) -> QuoteRead:
        rows = (
            await session.execute(
                select(QuoteItem).where(QuoteItem.tenant_id == tenant_id, QuoteItem.quote_id == quote.id)
            )
        ).scalars().all()
        return QuoteRead(
            id=quote.id,
            tenant_id=quote.tenant_id,
            opportunity_id=quote.opportunity_id,
            quote_no=quote.quote_no,
            version=quote.version,
            is_proforma_invoice=quote.is_proforma_invoice,
            incoterm=quote.incoterm,
            port_of_loading=quote.port_of_loading,
            port_of_destination=quote.port_of_destination,
            payment_terms=quote.payment_terms,
            base_currency=quote.base_currency,
            quote_currency=quote.quote_currency,
            fx_rate_snapshot=Decimal(str(quote.fx_rate_snapshot)),
            fx_locked_until=quote.fx_locked_until,
            freight_fee=Decimal(str(quote.freight_fee)),
            insurance_fee=Decimal(str(quote.insurance_fee)),
            customs_duty_estimate=Decimal(str(quote.customs_duty_estimate)),
            total_amount=Decimal(str(quote.total_amount)),
            pdf_r2_key=quote.pdf_r2_key,
            status=quote.status,
            converted_order_id=quote.converted_order_id,
            created_at=quote.created_at,
            items=[QuoteItemRead.model_validate(item) for item in rows],
        )

    async def get_quote(self, session: AsyncSession, tenant_id: UUID, quote_id: UUID) -> QuoteRead:
        quote = (
            await session.execute(select(Quote).where(Quote.tenant_id == tenant_id, Quote.id == quote_id))
        ).scalar_one_or_none()
        if quote is None:
            raise AppError(slug="quote-not-found", status_code=404, code="ERR_QUOTE_NOT_FOUND", title="Quote Not Found", detail=f"Quote '{quote_id}' not found.")
        return await self._serialize_quote(session, tenant_id, quote)

    async def list_quotes(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        opportunity_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[QuoteRead]:
        stmt = select(Quote).where(Quote.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(Quote).where(Quote.tenant_id == tenant_id)
        if opportunity_id is not None:
            stmt = stmt.where(Quote.opportunity_id == opportunity_id)
            count_stmt = count_stmt.where(Quote.opportunity_id == opportunity_id)
        total = int((await session.execute(count_stmt)).scalar_one())
        rows = (
            await session.execute(
                stmt.order_by(Quote.created_at.desc(), Quote.version.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        items = [await self._serialize_quote(session, tenant_id, q) for q in rows]
        return PaginatedResponse(items=items, total=total, limit=limit, offset=offset, has_more=(offset + len(items)) < total)

    async def create_quote(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: QuoteCreate,
    ) -> QuoteRead:
        opp = (
            await session.execute(
                select(Opportunity).where(Opportunity.tenant_id == tenant_id, Opportunity.id == payload.opportunity_id)
            )
        ).scalar_one_or_none()
        if opp is None:
            raise AppError(
                slug="opportunity-not-found",
                status_code=404,
                code="ERR_OPPORTUNITY_NOT_FOUND",
                title="Opportunity Not Found",
                detail=f"Opportunity '{payload.opportunity_id}' not found.",
            )

        built_items, subtotal, margin_override_used = await self._build_quote_items_and_check_margin(
            session,
            tenant_id,
            principal,
            payload.items,
            payload.fx_rate_snapshot,
            payload.override_margin_floor,
        )
        freight = payload.freight_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        insurance = payload.insurance_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        duty = payload.customs_duty_estimate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_amount = (subtotal + freight + insurance + duty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        now = datetime.now(timezone.utc)
        quote_no = f"PI-{now:%Y%m}-{uuid4().hex[:6].upper()}"
        quote = Quote(
            tenant_id=tenant_id,
            opportunity_id=opp.id,
            quote_no=quote_no,
            version=1,
            is_proforma_invoice=payload.is_proforma_invoice,
            incoterm=payload.incoterm,
            port_of_loading=payload.port_of_loading,
            port_of_destination=payload.port_of_destination,
            payment_terms=payload.payment_terms,
            base_currency=payload.base_currency.upper(),
            quote_currency=payload.quote_currency.upper(),
            fx_rate_snapshot=payload.fx_rate_snapshot.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            fx_locked_until=payload.fx_locked_until,
            freight_fee=freight,
            insurance_fee=insurance,
            customs_duty_estimate=duty,
            total_amount=total_amount,
            status=QuoteStatus.DRAFT,
        )
        session.add(quote)
        await session.flush()

        for qi in built_items:
            qi.quote_id = quote.id
            session.add(qi)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="quote.margin_override" if margin_override_used else "quote.created",
            resource_type="quote",
            resource_id=quote.id,
            after_state={"quote_no": quote.quote_no, "version": quote.version, "total_amount": str(quote.total_amount)},
        )
        return await self._serialize_quote(session, tenant_id, quote)

    async def reprice_quote_version(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        quote_id: UUID,
        payload: QuoteRepriceRequest,
    ) -> QuoteRead:
        """Create a brand new immutable version (max_version + 1) leaving all historical versions untouched."""
        prev = (
            await session.execute(select(Quote).where(Quote.tenant_id == tenant_id, Quote.id == quote_id).with_for_update())
        ).scalar_one_or_none()
        if prev is None:
            raise AppError(slug="quote-not-found", status_code=404, code="ERR_QUOTE_NOT_FOUND", title="Quote Not Found", detail=f"Quote '{quote_id}' not found.")
        if prev.status == QuoteStatus.CONVERTED:
            raise AppError(
                slug="quote-already-converted",
                status_code=409,
                code="ERR_QUOTE_ALREADY_CONVERTED",
                title="Quote Already Converted",
                detail="Cannot reprice a quote that has already been converted to an order.",
            )

        max_ver = int(
            (
                await session.execute(
                    select(func.max(Quote.version)).where(Quote.tenant_id == tenant_id, Quote.quote_no == prev.quote_no)
                )
            ).scalar_one()
        )
        if payload.items is not None:
            item_inputs = payload.items
        else:
            prev_items = (
                await session.execute(
                    select(QuoteItem).where(QuoteItem.tenant_id == tenant_id, QuoteItem.quote_id == prev.id)
                )
            ).scalars().all()
            item_inputs = [
                QuoteItemInput(
                    variant_id=pi.variant_id,
                    quantity=pi.quantity,
                    unit_price_quote_ccy=Decimal(str(pi.unit_price_quote_ccy)),
                    description_override=pi.description_i18n,
                )
                for pi in prev_items
            ]

        built_items, subtotal, margin_override_used = await self._build_quote_items_and_check_margin(
            session,
            tenant_id,
            principal,
            item_inputs,
            payload.fx_rate_snapshot,
            payload.override_margin_floor,
        )
        freight = (payload.freight_fee if payload.freight_fee is not None else Decimal(str(prev.freight_fee))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        insurance = (
            payload.insurance_fee if payload.insurance_fee is not None else Decimal(str(prev.insurance_fee))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        duty = (
            payload.customs_duty_estimate
            if payload.customs_duty_estimate is not None
            else Decimal(str(prev.customs_duty_estimate))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_amount = (subtotal + freight + insurance + duty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        new_quote = Quote(
            tenant_id=tenant_id,
            opportunity_id=prev.opportunity_id,
            quote_no=prev.quote_no,
            version=max_ver + 1,
            is_proforma_invoice=prev.is_proforma_invoice,
            incoterm=prev.incoterm,
            port_of_loading=prev.port_of_loading,
            port_of_destination=prev.port_of_destination,
            payment_terms=payload.payment_terms or prev.payment_terms,
            base_currency=prev.base_currency,
            quote_currency=prev.quote_currency,
            fx_rate_snapshot=payload.fx_rate_snapshot.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            fx_locked_until=payload.fx_locked_until,
            freight_fee=freight,
            insurance_fee=insurance,
            customs_duty_estimate=duty,
            total_amount=total_amount,
            status=QuoteStatus.DRAFT,
        )
        session.add(new_quote)
        await session.flush()

        for qi in built_items:
            qi.quote_id = new_quote.id
            session.add(qi)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="quote.repriced_version",
            resource_type="quote",
            resource_id=new_quote.id,
            before_state={"previous_quote_id": str(prev.id), "previous_version": prev.version},
            after_state={"version": new_quote.version, "total_amount": str(new_quote.total_amount)},
        )
        return await self._serialize_quote(session, tenant_id, new_quote)

    async def send_quote(
        self, session: AsyncSession, tenant_id: UUID, principal: CurrentPrincipal, quote_id: UUID
    ) -> QuoteRead:
        quote = (
            await session.execute(select(Quote).where(Quote.tenant_id == tenant_id, Quote.id == quote_id).with_for_update())
        ).scalar_one_or_none()
        if quote is None:
            raise AppError(slug="quote-not-found", status_code=404, code="ERR_QUOTE_NOT_FOUND", title="Quote Not Found", detail=f"Quote '{quote_id}' not found.")
        if quote.status not in {QuoteStatus.DRAFT, QuoteStatus.PENDING_APPROVAL}:
            raise AppError(slug="invalid-quote-state", status_code=409, code="ERR_INVALID_QUOTE_STATE", title="Invalid Quote State", detail=f"Cannot send quote in status '{quote.status}'.")
        if quote.status == QuoteStatus.PENDING_APPROVAL and principal.role == UserRole.AGENT:
            raise AppError(slug="insufficient-role", status_code=403, code="ERR_INSUFFICIENT_ROLE", title="Approval Required", detail="Pending approval quote requires manager/admin/owner.")
        quote.status = QuoteStatus.SENT
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="quote.sent",
            resource_type="quote",
            resource_id=quote.id,
            after_state={"status": quote.status.value},
        )
        return await self._serialize_quote(session, tenant_id, quote)

    async def accept_quote(
        self, session: AsyncSession, tenant_id: UUID, principal: CurrentPrincipal, quote_id: UUID
    ) -> QuoteRead:
        quote = (
            await session.execute(select(Quote).where(Quote.tenant_id == tenant_id, Quote.id == quote_id).with_for_update())
        ).scalar_one_or_none()
        if quote is None:
            raise AppError(slug="quote-not-found", status_code=404, code="ERR_QUOTE_NOT_FOUND", title="Quote Not Found", detail=f"Quote '{quote_id}' not found.")
        if quote.status != QuoteStatus.SENT:
            raise AppError(slug="invalid-quote-state", status_code=409, code="ERR_INVALID_QUOTE_STATE", title="Invalid Quote State", detail=f"Quote must be 'sent' before acceptance (found '{quote.status}').")
        quote.status = QuoteStatus.ACCEPTED
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="quote.accepted",
            resource_type="quote",
            resource_id=quote.id,
            after_state={"status": quote.status.value},
        )
        return await self._serialize_quote(session, tenant_id, quote)

    async def generate_pi_pdf(
        self, session: AsyncSession, tenant_id: UUID, quote_id: UUID, language: str = "en"
    ) -> QuotePdfResponse:
        quote = (
            await session.execute(select(Quote).where(Quote.tenant_id == tenant_id, Quote.id == quote_id))
        ).scalar_one_or_none()
        if quote is None:
            raise AppError(slug="quote-not-found", status_code=404, code="ERR_QUOTE_NOT_FOUND", title="Quote Not Found", detail=f"Quote '{quote_id}' not found.")
        items = (
            await session.execute(select(QuoteItem).where(QuoteItem.tenant_id == tenant_id, QuoteItem.quote_id == quote.id))
        ).scalars().all()

        info_row = (
            await session.execute(
                text(
                    """
                    SELECT
                        t.name AS seller_name,
                        c.display_name AS buyer_contact,
                        c.primary_email AS buyer_email,
                        COALESCE(bc.legal_name, c.display_name) AS buyer_company
                    FROM opportunities o
                    JOIN tenants t ON t.id = o.tenant_id
                    JOIN customers c ON c.id = o.customer_id
                    LEFT JOIN b2b_companies bc ON bc.id = o.company_id
                    WHERE o.tenant_id = :tid AND o.id = :oid
                    """
                ),
                {"tid": tenant_id, "oid": quote.opportunity_id},
            )
        ).one()

        ctx = PiRenderContext(
            tenant_id=tenant_id,
            seller_name=info_row.seller_name,
            buyer_company=info_row.buyer_company,
            buyer_contact=info_row.buyer_contact,
            buyer_email=info_row.buyer_email,
            quote_no=quote.quote_no,
            version=quote.version,
            is_proforma_invoice=quote.is_proforma_invoice,
            incoterm=quote.incoterm.value,
            port_of_loading=quote.port_of_loading or "",
            port_of_destination=quote.port_of_destination or "",
            payment_terms=quote.payment_terms,
            quote_currency=quote.quote_currency,
            fx_rate_snapshot=Decimal(str(quote.fx_rate_snapshot)),
            fx_locked_until=quote.fx_locked_until,
            issued_at=quote.created_at,
            freight_fee=Decimal(str(quote.freight_fee)),
            insurance_fee=Decimal(str(quote.insurance_fee)),
            customs_duty_estimate=Decimal(str(quote.customs_duty_estimate)),
            total_amount=Decimal(str(quote.total_amount)),
            items=[
                PiLineRenderItem(
                    sku=qi.sku_snapshot,
                    description=qi.description_i18n,
                    quantity=qi.quantity,
                    unit_price=Decimal(str(qi.unit_price_quote_ccy)),
                    line_total=Decimal(str(qi.line_total_quote_ccy)),
                )
                for qi in items
            ],
            language=language,
        )
        key, url = await self.pdf_renderer.render_and_upload(ctx)
        quote.pdf_r2_key = key
        await session.flush()
        return QuotePdfResponse(
            quote_id=quote.id,
            quote_no=quote.quote_no,
            version=quote.version,
            pdf_r2_key=key,
            download_url=url,
        )

    async def convert_quote_to_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        quote_id: UUID,
        payload: ConvertQuoteRequest,
    ) -> ConvertQuoteResponse:
        quote = (
            await session.execute(select(Quote).where(Quote.tenant_id == tenant_id, Quote.id == quote_id).with_for_update())
        ).scalar_one_or_none()
        if quote is None:
            raise AppError(slug="quote-not-found", status_code=404, code="ERR_QUOTE_NOT_FOUND", title="Quote Not Found", detail=f"Quote '{quote_id}' not found.")
        if quote.status == QuoteStatus.CONVERTED:
            raise AppError(slug="quote-already-converted", status_code=409, code="ERR_QUOTE_ALREADY_CONVERTED", title="Already Converted", detail="Quote was already converted to an order.")
        if quote.status != QuoteStatus.ACCEPTED:
            raise AppError(slug="quote-not-accepted", status_code=422, code="ERR_QUOTE_NOT_ACCEPTED", title="Quote Not Accepted", detail="Quote must be in 'accepted' status before 1-click conversion.")

        now = datetime.now(timezone.utc)
        if now > quote.fx_locked_until:
            raise AppError(
                slug="quote-fx-expired",
                status_code=422,
                code="ERR_QUOTE_FX_EXPIRED",
                title="Quote FX Lock Expired",
                detail=f"Quote FX lock expired at {quote.fx_locked_until.isoformat()}; create a repriced quote version.",
            )

        opp = (
            await session.execute(
                select(Opportunity).where(Opportunity.tenant_id == tenant_id, Opportunity.id == quote.opportunity_id)
            )
        ).scalar_one()
        q_items = (
            await session.execute(select(QuoteItem).where(QuoteItem.tenant_id == tenant_id, QuoteItem.quote_id == quote.id))
        ).scalars().all()

        subtotal = sum((Decimal(str(i.line_total_quote_ccy)) for i in q_items), Decimal("0.00")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        shipping_charged = (Decimal(str(quote.freight_fee)) + Decimal(str(quote.insurance_fee))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        order = await self.order_port.create_order_from_quote(
            session,
            tenant_id=tenant_id,
            actor_id=str(principal.user_id),
            order_mode=payload.order_mode,
            customer_id=opp.customer_id,
            quote_id=quote.id,
            warehouse_id=payload.warehouse_id,
            currency=quote.quote_currency,
            fx_rate_to_base=Decimal(str(quote.fx_rate_snapshot)),
            subtotal_amount=subtotal,
            shipping_charged=shipping_charged,
            tax_collected=Decimal(str(quote.customs_duty_estimate)),
            total_amount=Decimal(str(quote.total_amount)),
            channel_metadata={
                "incoterm": quote.incoterm.value,
                "quote_no": quote.quote_no,
                "quote_version": quote.version,
            },
            items=[
                {
                    "variant_id": qi.variant_id,
                    "sku_snapshot": qi.sku_snapshot,
                    "quantity": qi.quantity,
                    "unit_price": Decimal(str(qi.unit_price_quote_ccy)),
                    "unit_cost_base_snapshot": Decimal(str(qi.unit_cost_base_snapshot)),
                    "line_total": Decimal(str(qi.line_total_quote_ccy)),
                }
                for qi in q_items
            ],
        )

        receivables = await self.payment_port.create_b2b_milestone_receivables(
            session,
            tenant_id=tenant_id,
            order_id=order.id,
            total_amount=Decimal(str(order.total_amount)),
            currency=order.currency,
            deposit_ratio=payload.deposit_ratio,
        )
        await self.profit_port.recalculate_order_profit(session, tenant_id=tenant_id, order_id=order.id)

        quote.status = QuoteStatus.CONVERTED
        quote.converted_order_id = order.id
        opp.stage = OpportunityStage.CLOSED_WON
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="quote.converted_to_order",
            resource_type="quote",
            resource_id=quote.id,
            after_state={"order_id": str(order.id), "order_no": order.order_no},
        )
        quote_read = await self._serialize_quote(session, tenant_id, quote)
        return ConvertQuoteResponse(quote=quote_read, order=order, receivables=receivables)
```

Create `apps/backend/src/erp_core/crm/router.py`:

```python
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from erp_core.crm.quote_service import QuoteService
from erp_core.crm.schemas import (
    ConvertQuoteRequest,
    ConvertQuoteResponse,
    LeadCreate,
    LeadRead,
    OpportunityCreate,
    OpportunityRead,
    OpportunityStageUpdate,
    QuoteCreate,
    QuotePdfResponse,
    QuoteRead,
    QuoteRepriceRequest,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/b2b", tags=["b2b"])
_service = QuoteService()


def _get_session_factory(request: Request):
    factory = getattr(request.app.state, "session_factory", None) or getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


@router.get("/leads", response_model=PaginatedResponse[LeadRead])
async def list_leads(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
) -> PaginatedResponse[LeadRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_leads(session, principal.tenant_id, limit=limit, offset=offset)


@router.post("/leads", response_model=LeadRead, status_code=status.HTTP_201_CREATED)
async def create_lead(
    request: Request,
    payload: LeadCreate,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
) -> LeadRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_lead(session, principal.tenant_id, payload)


@router.get("/opportunities", response_model=PaginatedResponse[OpportunityRead])
async def list_opportunities(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
) -> PaginatedResponse[OpportunityRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_opportunities(session, principal.tenant_id, limit=limit, offset=offset)


@router.post("/opportunities", response_model=OpportunityRead, status_code=status.HTTP_201_CREATED)
async def create_opportunity(
    request: Request,
    payload: OpportunityCreate,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
) -> OpportunityRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_opportunity(session, principal.tenant_id, payload)


@router.patch("/opportunities/{opportunity_id}", response_model=OpportunityRead)
async def update_opportunity_stage(
    request: Request,
    opportunity_id: UUID,
    payload: OpportunityStageUpdate,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
) -> OpportunityRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.update_opportunity_stage(
            session, principal.tenant_id, str(principal.user_id), opportunity_id, payload.stage
        )


@router.get("/quotes", response_model=PaginatedResponse[QuoteRead])
async def list_quotes(
    request: Request,
    opportunity_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> PaginatedResponse[QuoteRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_quotes(
            session, principal.tenant_id, opportunity_id=opportunity_id, limit=limit, offset=offset
        )


@router.post("/quotes", response_model=QuoteRead, status_code=status.HTTP_201_CREATED)
async def create_quote(
    request: Request,
    payload: QuoteCreate,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_quote(session, principal.tenant_id, principal, payload)


@router.post("/quotes/{quote_id}/reprice-version", response_model=QuoteRead, status_code=status.HTTP_201_CREATED)
async def reprice_quote_version(
    request: Request,
    quote_id: UUID,
    payload: QuoteRepriceRequest,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.reprice_quote_version(session, principal.tenant_id, principal, quote_id, payload)


@router.post("/quotes/{quote_id}/send", response_model=QuoteRead)
async def send_quote(
    request: Request,
    quote_id: UUID,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.send_quote(session, principal.tenant_id, principal, quote_id)


@router.post("/quotes/{quote_id}/accept", response_model=QuoteRead)
async def accept_quote(
    request: Request,
    quote_id: UUID,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.accept_quote(session, principal.tenant_id, principal, quote_id)


@router.post("/quotes/{quote_id}/pdf", response_model=QuotePdfResponse)
async def generate_quote_pdf(
    request: Request,
    quote_id: UUID,
    language: str = Query(default="en", min_length=2, max_length=5),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> QuotePdfResponse:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.generate_pi_pdf(session, principal.tenant_id, quote_id, language=language)


@router.post("/quotes/{quote_id}/convert-to-order", response_model=ConvertQuoteResponse)
async def convert_quote_to_order(
    request: Request,
    quote_id: UUID,
    payload: ConvertQuoteRequest,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)),
) -> ConvertQuoteResponse:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.convert_quote_to_order(session, principal.tenant_id, principal, quote_id, payload)
```

Modify `apps/backend/src/erp_core/api_main.py` to mount `crm_router`:

```python
from erp_core.crm.router import router as crm_router

app.include_router(crm_router, prefix="/api/v1")
```

- [ ] **Step 4: Run tests, export OpenAPI, and check import-linter**

Run:
```bash
cd apps/backend && uv run pytest tests/test_b2b_quotes_and_pi.py -v
cd apps/backend && uv run python scripts/export_openapi.py > ../../contracts/openapi.json
cd apps/backend && uv run lint-imports --config ../../infra/importlinter.ini
```
Expected: `test_b2b_quotes_and_pi.py` PASSES immediately using `_PostgresConversionBridge` (and full runtime wiring with `OrderService` + `PaymentService` is verified again in Task 3.6); `openapi.json` includes `/api/v1/b2b/*` routes; `lint-imports` passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/crm/ apps/backend/src/erp_core/api_main.py apps/backend/tests/test_b2b_quotes_and_pi.py contracts/openapi.json
git commit -m "feat(crm): add B2B pipeline, immutable versioned quotes, WeasyPrint PI PDF & 1-click convert"
```

---

### Task 3.4: `InventoryLedgerService` & Unified Dual-Mode Order Service (50-Coroutine Oversell Test)

**Files:**
- Create: `apps/backend/src/erp_core/orders/schemas.py`
- Create: `apps/backend/src/erp_core/orders/inventory_service.py`
- Create: `apps/backend/src/erp_core/orders/order_service.py`
- Create: `apps/backend/src/erp_core/orders/router.py`
- Modify: `apps/backend/src/erp_core/api_main.py` (include `orders_router` on `/api/v1`)
- Test: `apps/backend/tests/test_inventory_concurrency.py`

**Interfaces:**
- Consumes:
  - `erp_core.catalog.service`: `CatalogService`
  - `erp_core.tenancy.audit`: `record_audit_event`
  - `erp_core.tenancy.dependencies`: `CurrentPrincipal`, `require_roles`
  - `erp_core.shared.errors`: `AppError`
  - `erp_core.shared.pagination`: `PaginatedResponse`
- Produces:
  - `InventoryLedgerService`:
    - `reserve_for_order(self, session: AsyncSession, tenant_id: UUID, warehouse_id: UUID, order_id: UUID, items: list[tuple[UUID, int]], idempotency_prefix: str) -> list[int]`
    - `release_reservation(self, session: AsyncSession, tenant_id: UUID, warehouse_id: UUID, order_id: UUID, items: list[tuple[UUID, int]], idempotency_prefix: str) -> list[int]`
    - `deduct_shipped(self, session: AsyncSession, tenant_id: UUID, warehouse_id: UUID, shipment_id: UUID, items: list[tuple[UUID, int]], idempotency_prefix: str) -> list[int]`
    - `inbound_stock(...)`, `get_balance(...)`
  - `InsufficientStockError(AppError)`
  - `OrderService` (`create_order_atomic`, `get_order`, `find_by_external_order`, `list_orders`, `cancel_order`, `ship_order`, `apply_payment_milestone_state`, `update_external_order_status`)
  - HTTP endpoints: `/api/v1/orders`, `/api/v1/orders/{order_id}`, `/api/v1/orders/{order_id}/cancel`, `/api/v1/orders/{order_id}/ship`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_inventory_concurrency.py`:

```python
from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.catalog.schemas import ProductCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.orders.inventory_service import InsufficientStockError, InventoryLedgerService
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import OrderCreateInternal, OrderItemInternal
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, OrderMode, UserRole


def _migrator_url() -> str:
    return os.getenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_secret@localhost:5432/erp_saas",
    )


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )


@pytest.mark.asyncio
async def test_50_concurrent_reservations_for_last_unit_no_oversell() -> None:
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    # Use pool_size >= 55 so all 50 coroutines genuinely contend concurrently in PostgreSQL
    rt_engine = create_async_engine(_runtime_url(), echo=False, pool_size=55, max_overflow=10)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    customer_id = uuid4()
    warehouse_id = uuid4()

    try:
        async with mig_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tenant_id, "name": "Race Test Co", "slug": f"race-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, customer_type, display_name) VALUES (:id, :tid, 'b2c_consumer', 'Flash Buyer')"
                ),
                {"id": customer_id, "tid": tenant_id},
            )

        catalog_svc = CatalogService()
        inv_svc = InventoryLedgerService()

        async with tenant_session(rt_factory, tenant_id) as session:
            prod = await catalog_svc.create_product(
                session,
                tenant_id,
                actor_id="seed",
                payload=ProductCreate(spu_code="SPU-LED-LAST", category="lighting"),
            )
            variant = await catalog_svc.create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=prod.id,
                    sku="LED-STRIP-5M-IP65",
                    cost_price_base=Decimal("30.0000"),
                    retail_price_usd=Decimal("19.99"),
                    weight_grams=300,
                ),
            )
            await inv_svc.inbound_stock(
                session,
                tenant_id=tenant_id,
                warehouse_id=warehouse_id,
                variant_id=variant.id,
                qty=1,
                reference_id=uuid4(),
                idempotency_key=f"inbound-last-1-{variant.id}",
            )

        async def _attempt_reserve(idx: int) -> str:
            async with tenant_session(rt_factory, tenant_id) as sess:
                await inv_svc.reserve_for_order(
                    sess,
                    tenant_id=tenant_id,
                    warehouse_id=warehouse_id,
                    order_id=uuid4(),
                    items=[(variant.id, 1)],
                    idempotency_prefix=f"race-res-{idx}",
                )
            return "reserved"

        results = await asyncio.gather(*[_attempt_reserve(i) for i in range(50)], return_exceptions=True)
        successes = [r for r in results if r == "reserved"]
        stock_failures = [r for r in results if isinstance(r, InsufficientStockError)]
        unexpected = [r for r in results if r != "reserved" and not isinstance(r, InsufficientStockError)]

        assert unexpected == [], f"Unexpected exceptions: {unexpected}"
        assert len(successes) == 1
        assert len(stock_failures) == 49

        async with tenant_session(rt_factory, tenant_id) as session:
            bal = await inv_svc.get_balance(session, tenant_id, warehouse_id, variant.id)
            assert bal.on_hand_qty == 1
            assert bal.reserved_qty == 1
            assert bal.available_qty == 0

            ledger_sums = (
                await session.execute(
                    text(
                        """
                        SELECT
                            COALESCE(SUM(qty_delta_on_hand), 0) AS sum_on_hand,
                            COALESCE(SUM(qty_delta_reserved), 0) AS sum_reserved
                        FROM inventory_ledger
                        WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                        """
                    ),
                    {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
                )
            ).one()
            assert ledger_sums.sum_on_hand == bal.on_hand_qty == 1
            assert ledger_sums.sum_reserved == bal.reserved_qty == 1

            # Verify idempotent order creation + cancellation releases stock back to 1
            await inv_svc.inbound_stock(
                session,
                tenant_id=tenant_id,
                warehouse_id=warehouse_id,
                variant_id=variant.id,
                qty=5,
                reference_id=uuid4(),
                idempotency_key=f"inbound-more-{variant.id}",
            )
            order_svc = OrderService(inventory_service=inv_svc)
            order = await order_svc.create_order_atomic(
                session,
                tenant_id=tenant_id,
                actor_type="human_user",
                actor_id=str(uuid4()),
                payload=OrderCreateInternal(
                    order_mode=OrderMode.B2C_RETAIL,
                    customer_id=customer_id,
                    warehouse_id=warehouse_id,
                    currency="USD",
                    fx_rate_to_base=Decimal("7.200000"),
                    subtotal_amount=Decimal("39.98"),
                    total_amount=Decimal("39.98"),
                    initial_fulfillment_status=FulfillmentStatus.CONFIRMED_RESERVED,
                    items=[
                        OrderItemInternal(
                            variant_id=variant.id,
                            sku_snapshot="LED-STRIP-5M-IP65",
                            quantity=2,
                            unit_price=Decimal("19.9900"),
                            unit_cost_base_snapshot=Decimal("30.0000"),
                            line_total=Decimal("39.98"),
                        )
                    ],
                ),
            )
            bal_after_order = await inv_svc.get_balance(session, tenant_id, warehouse_id, variant.id)
            assert bal_after_order.on_hand_qty == 6
            assert bal_after_order.reserved_qty == 3
            assert bal_after_order.available_qty == 3

            cancelled = await order_svc.cancel_order(
                session, tenant_id=tenant_id, actor_id="ops-user", order_id=order.id, warehouse_id=warehouse_id, reason="buyer cancelled"
            )
            assert cancelled.fulfillment_status == FulfillmentStatus.CANCELLED
            bal_after_cancel = await inv_svc.get_balance(session, tenant_id, warehouse_id, variant.id)
            assert bal_after_cancel.reserved_qty == 1
            assert bal_after_cancel.available_qty == 5
    finally:
        async with mig_engine.begin() as conn:
            await conn.execute(text("DELETE FROM inventory_ledger WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM inventory_balances WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM order_items WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM orders WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM product_variants WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM products WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_inventory_concurrency.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.orders.schemas'`

- [ ] **Step 3: Write `orders/schemas.py`, `orders/inventory_service.py`, `orders/order_service.py`, `orders/router.py`, and mount router**

Create `apps/backend/src/erp_core/orders/schemas.py`:

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus, TrackingProvider


class InventoryBalanceRead(BaseModel):
    tenant_id: UUID
    warehouse_id: UUID
    variant_id: UUID
    on_hand_qty: int
    reserved_qty: int
    available_qty: int


class OrderItemInput(BaseModel):
    variant_id: UUID
    quantity: int = Field(gt=0)
    unit_price: Decimal | None = Field(default=None, gt=Decimal("0"))


class OrderItemInternal(BaseModel):
    variant_id: UUID
    sku_snapshot: str
    quantity: int = Field(gt=0)
    unit_price: Decimal
    unit_cost_base_snapshot: Decimal
    line_total: Decimal


class OrderItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_id: UUID
    variant_id: UUID
    sku_snapshot: str
    quantity: int
    unit_price: Decimal
    unit_cost_base_snapshot: Decimal
    line_total: Decimal


class OrderCreateRequest(BaseModel):
    order_mode: OrderMode = OrderMode.B2C_RETAIL
    customer_id: UUID
    warehouse_id: UUID
    currency: str = Field(default="USD", min_length=3, max_length=3)
    fx_rate_to_base: Decimal = Field(default=Decimal("7.200000"), gt=Decimal("0"))
    shipping_charged: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    discount_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    tax_collected: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    items: list[OrderItemInput] = Field(min_length=1)


class OrderCreateInternal(BaseModel):
    order_mode: OrderMode
    customer_id: UUID
    warehouse_id: UUID
    currency: str
    fx_rate_to_base: Decimal
    subtotal_amount: Decimal
    shipping_charged: Decimal = Decimal("0.00")
    discount_amount: Decimal = Decimal("0.00")
    tax_collected: Decimal = Decimal("0.00")
    total_amount: Decimal
    channel_account_id: UUID | None = None
    external_order_id: str | None = None
    quote_id: UUID | None = None
    initial_fulfillment_status: FulfillmentStatus = FulfillmentStatus.CONFIRMED_RESERVED
    initial_payment_status: PaymentStatus = PaymentStatus.UNPAID
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    items: list[OrderItemInternal] = Field(min_length=1)
    order_no_override: str | None = None


class ShipmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    order_id: UUID
    tracking_provider: TrackingProvider
    tracking_number: str
    carrier_code: str | None
    shipment_mode: str
    standardized_status: str
    sub_status: str | None
    last_event_at: datetime | None
    last_synced_at: datetime | None
    estimated_delivery_at: datetime | None
    tracking_events: list[dict[str, Any]]
    actual_shipping_cost_base: Decimal
    exception_alert_sent: bool
    is_stagnant_72h: bool = False
    created_at: datetime


class InventoryBalanceItemRead(BaseModel):
    warehouse_id: UUID
    variant_id: UUID
    sku: str
    on_hand_qty: int
    reserved_qty: int
    available_qty: int


class InventoryLedgerRead(BaseModel):
    id: int
    variant_id: UUID
    event_type: str
    qty_delta_on_hand: int
    qty_delta_reserved: int
    reference_type: str
    reference_id: UUID
    idempotency_key: str
    created_at: datetime


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    order_no: str
    order_mode: OrderMode
    channel_account_id: UUID | None
    external_order_id: str | None
    customer_id: UUID
    customer_name: str | None = None
    quote_id: UUID | None
    currency: str
    fx_rate_to_base: Decimal
    subtotal_amount: Decimal
    shipping_charged: Decimal
    discount_amount: Decimal
    tax_collected: Decimal
    total_amount: Decimal
    fulfillment_status: FulfillmentStatus
    payment_status: PaymentStatus
    channel_metadata: dict[str, Any]
    has_72h_stagnation_alert: bool = False
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemRead] = Field(default_factory=list)
    shipments: list[ShipmentRead] = Field(default_factory=list)


class CancelOrderRequest(BaseModel):
    warehouse_id: UUID
    reason: str = Field(min_length=1, max_length=500)


class ShipOrderRequest(BaseModel):
    warehouse_id: UUID
    tracking_number: str = Field(min_length=3, max_length=128)
    carrier_code: str | None = None
    tracking_provider: TrackingProvider = TrackingProvider("17track")
    shipment_mode: str = Field(default="parcel", pattern="^(parcel|air_freight|sea_lcl|sea_fcl)$")
    actual_shipping_cost_base: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    override_unpaid_balance: bool = False
    override_reason: str | None = None
```

Create `apps/backend/src/erp_core/orders/inventory_service.py`:

```python
from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.orders.schemas import InventoryBalanceItemRead, InventoryBalanceRead, InventoryLedgerRead
from erp_core.shared.enums import InventoryEventType
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse


class InsufficientStockError(AppError):
    """Raised when on_hand_qty - reserved_qty cannot satisfy a reservation."""

    def __init__(self, variant_id: UUID, requested: int, available: int) -> None:
        super().__init__(
            slug="insufficient-stock",
            status_code=409,
            code="ERR_INSUFFICIENT_STOCK",
            title="Insufficient Stock",
            detail=f"Variant '{variant_id}' requested {requested}, available {available}.",
        )
        self.variant_id = variant_id
        self.requested = requested
        self.available = available


class InventoryLedgerService:
    """Deadlock-free, idempotent atomic inventory ledger & balance engine."""

    @staticmethod
    def _normalize_sorted_items(items: list[tuple[UUID, int]]) -> list[tuple[UUID, int]]:
        merged: dict[UUID, int] = defaultdict(int)
        for variant_id, qty in items:
            if qty <= 0:
                raise ValueError(f"Inventory quantity must be positive, got {qty}")
            merged[variant_id] += qty
        # Sort ascending by variant_id string to enforce global lock acquisition order
        return sorted(merged.items(), key=lambda pair: str(pair[0]))

    async def inbound_stock(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        warehouse_id: UUID,
        variant_id: UUID,
        qty: int,
        reference_id: UUID,
        idempotency_key: str,
        event_type: InventoryEventType = InventoryEventType.INBOUND_PO,
    ) -> int | None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        async with session.begin_nested():
            ledger_res = await session.execute(
                text(
                    """
                    INSERT INTO inventory_ledger (
                        tenant_id, warehouse_id, variant_id, event_type,
                        qty_delta_on_hand, qty_delta_reserved, reference_type, reference_id, idempotency_key
                    ) VALUES (
                        :tid, :wid, :vid, :etype, :d_hand, 0, 'po', :ref_id, :ikey
                    )
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "tid": tenant_id,
                    "wid": warehouse_id,
                    "vid": variant_id,
                    "etype": event_type.value,
                    "d_hand": qty,
                    "ref_id": reference_id,
                    "ikey": idempotency_key,
                },
            )
            ledger_id = ledger_res.scalar_one_or_none()
            if ledger_id is None:
                return None

            await session.execute(
                text(
                    """
                    INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty)
                    VALUES (:tid, :wid, :vid, :qty, 0)
                    ON CONFLICT (tenant_id, warehouse_id, variant_id)
                    DO UPDATE SET on_hand_qty = inventory_balances.on_hand_qty + EXCLUDED.on_hand_qty
                    """
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id, "qty": qty},
            )
            return int(ledger_id)

    async def reserve_for_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        warehouse_id: UUID,
        order_id: UUID,
        items: list[tuple[UUID, int]],
        idempotency_prefix: str,
    ) -> list[int]:
        sorted_items = self._normalize_sorted_items(items)
        ledger_ids: list[int] = []

        async with session.begin_nested():
            for variant_id, qty in sorted_items:
                # 1. Lock balance row in deterministic ascending variant_id order
                bal_row = (
                    await session.execute(
                        text(
                            """
                            SELECT on_hand_qty, reserved_qty
                            FROM inventory_balances
                            WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                            FOR UPDATE
                            """
                        ),
                        {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
                    )
                ).one_or_none()

                # 2. Insert immutable ledger record idempotently
                idem_key = f"{idempotency_prefix}:{variant_id}"
                ledger_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO inventory_ledger (
                                tenant_id, warehouse_id, variant_id, event_type,
                                qty_delta_on_hand, qty_delta_reserved, reference_type, reference_id, idempotency_key
                            ) VALUES (
                                :tid, :wid, :vid, 'reserve_order',
                                0, :qty, 'order', :oid, :ikey
                            )
                            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                            RETURNING id
                            """
                        ),
                        {
                            "tid": tenant_id,
                            "wid": warehouse_id,
                            "vid": variant_id,
                            "qty": qty,
                            "oid": order_id,
                            "ikey": idem_key,
                        },
                    )
                ).scalar_one_or_none()
                if ledger_id is None:
                    continue

                available = 0 if bal_row is None else int(bal_row.on_hand_qty - bal_row.reserved_qty)
                if available < qty:
                    raise InsufficientStockError(variant_id=variant_id, requested=qty, available=available)

                updated = (
                    await session.execute(
                        text(
                            """
                            UPDATE inventory_balances
                            SET reserved_qty = reserved_qty + :qty
                            WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                              AND (on_hand_qty - reserved_qty) >= :qty
                            RETURNING reserved_qty
                            """
                        ),
                        {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id, "qty": qty},
                    )
                ).scalar_one_or_none()
                if updated is None:
                    raise InsufficientStockError(variant_id=variant_id, requested=qty, available=available)

                ledger_ids.append(int(ledger_id))

        return ledger_ids

    async def release_reservation(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        warehouse_id: UUID,
        order_id: UUID,
        items: list[tuple[UUID, int]],
        idempotency_prefix: str,
    ) -> list[int]:
        sorted_items = self._normalize_sorted_items(items)
        ledger_ids: list[int] = []

        async with session.begin_nested():
            for variant_id, qty in sorted_items:
                bal_row = (
                    await session.execute(
                        text(
                            """
                            SELECT on_hand_qty, reserved_qty
                            FROM inventory_balances
                            WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                            FOR UPDATE
                            """
                        ),
                        {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
                    )
                ).one_or_none()

                idem_key = f"{idempotency_prefix}:{variant_id}"
                ledger_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO inventory_ledger (
                                tenant_id, warehouse_id, variant_id, event_type,
                                qty_delta_on_hand, qty_delta_reserved, reference_type, reference_id, idempotency_key
                            ) VALUES (
                                :tid, :wid, :vid, 'release_cancel',
                                0, :neg_qty, 'order', :oid, :ikey
                            )
                            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                            RETURNING id
                            """
                        ),
                        {
                            "tid": tenant_id,
                            "wid": warehouse_id,
                            "vid": variant_id,
                            "neg_qty": -qty,
                            "oid": order_id,
                            "ikey": idem_key,
                        },
                    )
                ).scalar_one_or_none()
                if ledger_id is None:
                    continue

                if bal_row is None or int(bal_row.reserved_qty) < qty:
                    raise AppError(
                        slug="invalid-reservation-release",
                        status_code=409,
                        code="ERR_INVALID_RESERVATION_RELEASE",
                        title="Invalid Reservation Release",
                        detail=f"Cannot release {qty} reserved units for variant '{variant_id}'.",
                    )

                await session.execute(
                    text(
                        """
                        UPDATE inventory_balances
                        SET reserved_qty = reserved_qty - :qty
                        WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                          AND reserved_qty >= :qty
                        """
                    ),
                    {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id, "qty": qty},
                )
                ledger_ids.append(int(ledger_id))

        return ledger_ids

    async def deduct_shipped(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        warehouse_id: UUID,
        shipment_id: UUID,
        items: list[tuple[UUID, int]],
        idempotency_prefix: str,
    ) -> list[int]:
        sorted_items = self._normalize_sorted_items(items)
        ledger_ids: list[int] = []

        async with session.begin_nested():
            for variant_id, qty in sorted_items:
                bal_row = (
                    await session.execute(
                        text(
                            """
                            SELECT on_hand_qty, reserved_qty
                            FROM inventory_balances
                            WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                            FOR UPDATE
                            """
                        ),
                        {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
                    )
                ).one_or_none()

                idem_key = f"{idempotency_prefix}:{variant_id}"
                ledger_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO inventory_ledger (
                                tenant_id, warehouse_id, variant_id, event_type,
                                qty_delta_on_hand, qty_delta_reserved, reference_type, reference_id, idempotency_key
                            ) VALUES (
                                :tid, :wid, :vid, 'deduct_shipped',
                                :neg_qty, :neg_qty, 'shipment', :sid, :ikey
                            )
                            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                            RETURNING id
                            """
                        ),
                        {
                            "tid": tenant_id,
                            "wid": warehouse_id,
                            "vid": variant_id,
                            "neg_qty": -qty,
                            "sid": shipment_id,
                            "ikey": idem_key,
                        },
                    )
                ).scalar_one_or_none()
                if ledger_id is None:
                    continue

                if bal_row is None or int(bal_row.on_hand_qty) < qty or int(bal_row.reserved_qty) < qty:
                    raise InsufficientStockError(
                        variant_id=variant_id,
                        requested=qty,
                        available=0 if bal_row is None else int(bal_row.reserved_qty),
                    )

                await session.execute(
                    text(
                        """
                        UPDATE inventory_balances
                        SET on_hand_qty = on_hand_qty - :qty,
                            reserved_qty = reserved_qty - :qty
                        WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                          AND on_hand_qty >= :qty AND reserved_qty >= :qty
                        """
                    ),
                    {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id, "qty": qty},
                )
                ledger_ids.append(int(ledger_id))

        return ledger_ids

    async def get_balance(
        self, session: AsyncSession, tenant_id: UUID, warehouse_id: UUID, variant_id: UUID
    ) -> InventoryBalanceRead:
        row = (
            await session.execute(
                text(
                    """
                    SELECT on_hand_qty, reserved_qty
                    FROM inventory_balances
                    WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid
                    """
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
            )
        ).one_or_none()
        on_hand = 0 if row is None else int(row.on_hand_qty)
        reserved = 0 if row is None else int(row.reserved_qty)
        return InventoryBalanceRead(
            tenant_id=tenant_id,
            warehouse_id=warehouse_id,
            variant_id=variant_id,
            on_hand_qty=on_hand,
            reserved_qty=reserved,
            available_qty=on_hand - reserved,
        )

    async def list_balances(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[InventoryBalanceItemRead]:
        filter_sql = " AND ib.variant_id = :vid" if variant_id is not None else ""
        params: dict[str, object] = {"tid": tenant_id, "lim": limit, "off": offset}
        if variant_id is not None:
            params["vid"] = variant_id

        total = int(
            (
                await session.execute(
                    text(f"SELECT COUNT(*) FROM inventory_balances ib WHERE ib.tenant_id = :tid{filter_sql}"),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT ib.warehouse_id, ib.variant_id,
                           COALESCE(pv.sku, ib.variant_id::text) AS sku,
                           ib.on_hand_qty, ib.reserved_qty
                    FROM inventory_balances ib
                    LEFT JOIN product_variants pv
                        ON pv.id = ib.variant_id AND pv.tenant_id = ib.tenant_id
                    WHERE ib.tenant_id = :tid{filter_sql}
                    ORDER BY sku ASC, ib.warehouse_id ASC
                    LIMIT :lim OFFSET :off
                    """
                ),
                params,
            )
        ).all()
        items = [
            InventoryBalanceItemRead(
                warehouse_id=r.warehouse_id,
                variant_id=r.variant_id,
                sku=str(r.sku),
                on_hand_qty=int(r.on_hand_qty),
                reserved_qty=int(r.reserved_qty),
                available_qty=int(r.on_hand_qty) - int(r.reserved_qty),
            )
            for r in rows
        ]
        return PaginatedResponse(items=items, total=total, limit=limit, offset=offset, has_more=(offset + len(items)) < total)

    async def list_ledger(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        variant_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[InventoryLedgerRead]:
        filter_sql = " AND variant_id = :vid" if variant_id is not None else ""
        params: dict[str, object] = {"tid": tenant_id, "lim": limit, "off": offset}
        if variant_id is not None:
            params["vid"] = variant_id

        total = int(
            (
                await session.execute(
                    text(f"SELECT COUNT(*) FROM inventory_ledger WHERE tenant_id = :tid{filter_sql}"),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id, variant_id, event_type, qty_delta_on_hand, qty_delta_reserved,
                           reference_type, reference_id, idempotency_key, created_at
                    FROM inventory_ledger
                    WHERE tenant_id = :tid{filter_sql}
                    ORDER BY created_at DESC, id DESC
                    LIMIT :lim OFFSET :off
                    """
                ),
                params,
            )
        ).all()
        items = [
            InventoryLedgerRead(
                id=int(r.id),
                variant_id=r.variant_id,
                event_type=str(r.event_type),
                qty_delta_on_hand=int(r.qty_delta_on_hand),
                qty_delta_reserved=int(r.qty_delta_reserved),
                reference_type=str(r.reference_type),
                reference_id=r.reference_id,
                idempotency_key=str(r.idempotency_key),
                created_at=r.created_at,
            )
            for r in rows
        ]
        return PaginatedResponse(items=items, total=total, limit=limit, offset=offset, has_more=(offset + len(items)) < total)
```

Create `apps/backend/src/erp_core/orders/order_service.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.service import CatalogService
from erp_core.orders.inventory_service import InventoryLedgerService
from erp_core.orders.models import Order, OrderItem, Shipment
from erp_core.orders.schemas import (
    OrderCreateInternal,
    OrderCreateRequest,
    OrderItemInternal,
    OrderItemRead,
    OrderRead,
    ShipOrderRequest,
    ShipmentRead,
)
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus, TrackingProvider, UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal


class TrackingRegistrationPort(Protocol):
    async def register_tracking(self, tracking_number: str, carrier_code: str | None = None) -> dict[str, Any]: ...


class OrderService:
    """Unified B2C + B2B order lifecycle with atomic inventory and payment milestone gates."""

    def __init__(
        self,
        inventory_service: InventoryLedgerService | None = None,
        catalog_service: CatalogService | None = None,
    ) -> None:
        self.inventory = inventory_service or InventoryLedgerService()
        self.catalog = catalog_service or CatalogService()

    async def _serialize_order(self, session: AsyncSession, tenant_id: UUID, order: Order) -> OrderRead:
        items = (
            await session.execute(
                select(OrderItem).where(OrderItem.tenant_id == tenant_id, OrderItem.order_id == order.id)
            )
        ).scalars().all()
        ship_rows = (
            await session.execute(
                select(Shipment)
                .where(Shipment.tenant_id == tenant_id, Shipment.order_id == order.id)
                .order_by(Shipment.created_at.desc())
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        serialized_shipments: list[ShipmentRead] = []
        for s in ship_rows:
            stagnant = bool(
                s.exception_alert_sent
                or (
                    s.standardized_status not in ("delivered", "cancelled", "expired")
                    and s.last_event_at is not None
                    and (now - s.last_event_at) >= timedelta(hours=72)
                )
            )
            sr = ShipmentRead.model_validate(s)
            serialized_shipments.append(sr.model_copy(update={"is_stagnant_72h": stagnant}))
        cust_name = (
            await session.execute(
                text("SELECT display_name FROM customers WHERE tenant_id = :tid AND id = :cid"),
                {"tid": tenant_id, "cid": order.customer_id},
            )
        ).scalar_one_or_none()
        return OrderRead(
            id=order.id,
            tenant_id=order.tenant_id,
            order_no=order.order_no,
            order_mode=order.order_mode,
            channel_account_id=order.channel_account_id,
            external_order_id=order.external_order_id,
            customer_id=order.customer_id,
            customer_name=cust_name,
            quote_id=order.quote_id,
            currency=order.currency,
            fx_rate_to_base=Decimal(str(order.fx_rate_to_base)),
            subtotal_amount=Decimal(str(order.subtotal_amount)),
            shipping_charged=Decimal(str(order.shipping_charged)),
            discount_amount=Decimal(str(order.discount_amount)),
            tax_collected=Decimal(str(order.tax_collected)),
            total_amount=Decimal(str(order.total_amount)),
            fulfillment_status=order.fulfillment_status,
            payment_status=order.payment_status,
            channel_metadata=dict(order.channel_metadata or {}),
            has_72h_stagnation_alert=any(sh.is_stagnant_72h for sh in serialized_shipments),
            created_at=order.created_at,
            updated_at=order.updated_at,
            items=[OrderItemRead.model_validate(i) for i in items],
            shipments=serialized_shipments,
        )

    async def create_manual_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: OrderCreateRequest,
    ) -> OrderRead:
        internal_items: list[OrderItemInternal] = []
        subtotal = Decimal("0.00")
        for line in payload.items:
            vp = await self.catalog.get_variant_pricing_internal(session, tenant_id, line.variant_id)
            unit_price = (
                line.unit_price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                if line.unit_price is not None
                else vp.retail_price_usd
            )
            line_total = (Decimal(line.quantity) * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            subtotal += line_total
            internal_items.append(
                OrderItemInternal(
                    variant_id=vp.id,
                    sku_snapshot=vp.sku,
                    quantity=line.quantity,
                    unit_price=unit_price,
                    unit_cost_base_snapshot=vp.cost_price_base,
                    line_total=line_total,
                )
            )

        total = (subtotal + payload.shipping_charged - payload.discount_amount + payload.tax_collected).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        initial_status = (
            FulfillmentStatus.PENDING_PAYMENT
            if payload.order_mode in {OrderMode.B2B_WHOLESALE, OrderMode.B2B_SAMPLE}
            else FulfillmentStatus.CONFIRMED_RESERVED
        )
        return await self.create_order_atomic(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            payload=OrderCreateInternal(
                order_mode=payload.order_mode,
                customer_id=payload.customer_id,
                warehouse_id=payload.warehouse_id,
                currency=payload.currency.upper(),
                fx_rate_to_base=payload.fx_rate_to_base,
                subtotal_amount=subtotal,
                shipping_charged=payload.shipping_charged,
                discount_amount=payload.discount_amount,
                tax_collected=payload.tax_collected,
                total_amount=total,
                initial_fulfillment_status=initial_status,
                items=internal_items,
            ),
        )

    async def create_order_atomic(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_type: Literal["human_user", "ai_agent", "system_cron", "webhook"],
        actor_id: str,
        payload: OrderCreateInternal,
    ) -> OrderRead:
        now = datetime.now(timezone.utc)
        order_no = payload.order_no_override or f"ORD-{now:%Y%m%d}-{uuid4().hex[:6].upper()}"
        order = Order(
            tenant_id=tenant_id,
            order_no=order_no,
            order_mode=payload.order_mode,
            channel_account_id=payload.channel_account_id,
            external_order_id=payload.external_order_id,
            customer_id=payload.customer_id,
            quote_id=payload.quote_id,
            currency=payload.currency.upper(),
            fx_rate_to_base=payload.fx_rate_to_base.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            subtotal_amount=payload.subtotal_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            shipping_charged=payload.shipping_charged.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            discount_amount=payload.discount_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            tax_collected=payload.tax_collected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            total_amount=payload.total_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            fulfillment_status=payload.initial_fulfillment_status,
            payment_status=payload.initial_payment_status,
            channel_metadata=payload.channel_metadata,
        )
        session.add(order)
        await session.flush()

        for item in payload.items:
            session.add(
                OrderItem(
                    tenant_id=tenant_id,
                    order_id=order.id,
                    variant_id=item.variant_id,
                    sku_snapshot=item.sku_snapshot,
                    quantity=item.quantity,
                    unit_price=item.unit_price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                    unit_cost_base_snapshot=item.unit_cost_base_snapshot.quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    ),
                    line_total=item.line_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                )
            )
        await session.flush()

        reserve_pairs = [(item.variant_id, item.quantity) for item in payload.items]
        await self.inventory.reserve_for_order(
            session,
            tenant_id=tenant_id,
            warehouse_id=payload.warehouse_id,
            order_id=order.id,
            items=reserve_pairs,
            idempotency_prefix=f"order-reserve:{order.id}",
        )

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="order.created",
            resource_type="order",
            resource_id=order.id,
            after_state={
                "order_no": order.order_no,
                "order_mode": order.order_mode.value,
                "total_amount": str(order.total_amount),
            },
        )
        return await self._serialize_order(session, tenant_id, order)

    async def get_order(self, session: AsyncSession, tenant_id: UUID, order_id: UUID) -> OrderRead:
        order = (
            await session.execute(select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id))
        ).scalar_one_or_none()
        if order is None:
            raise AppError(slug="order-not-found", status_code=404, code="ERR_ORDER_NOT_FOUND", title="Order Not Found", detail=f"Order '{order_id}' not found.")
        return await self._serialize_order(session, tenant_id, order)

    async def find_by_external_order(
        self, session: AsyncSession, tenant_id: UUID, channel_account_id: UUID, external_order_id: str
    ) -> OrderRead | None:
        order = (
            await session.execute(
                select(Order).where(
                    Order.tenant_id == tenant_id,
                    Order.channel_account_id == channel_account_id,
                    Order.external_order_id == external_order_id,
                )
            )
        ).scalar_one_or_none()
        if order is None:
            return None
        return await self._serialize_order(session, tenant_id, order)

    async def list_orders(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_mode: OrderMode | None = None,
        fulfillment_status: FulfillmentStatus | None = None,
        payment_status: PaymentStatus | None = None,
        customer_id: UUID | None = None,
        search: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[OrderRead]:
        stmt = select(Order).where(Order.tenant_id == tenant_id)
        cnt = select(func.count()).select_from(Order).where(Order.tenant_id == tenant_id)
        if order_mode is not None:
            stmt = stmt.where(Order.order_mode == order_mode)
            cnt = cnt.where(Order.order_mode == order_mode)
        if fulfillment_status is not None:
            stmt = stmt.where(Order.fulfillment_status == fulfillment_status)
            cnt = cnt.where(Order.fulfillment_status == fulfillment_status)
        if payment_status is not None:
            stmt = stmt.where(Order.payment_status == payment_status)
            cnt = cnt.where(Order.payment_status == payment_status)
        if customer_id is not None:
            stmt = stmt.where(Order.customer_id == customer_id)
            cnt = cnt.where(Order.customer_id == customer_id)
        if search:
            pat = f"%{search.strip()}%"
            stmt = stmt.where(Order.order_no.ilike(pat) | Order.external_order_id.ilike(pat))
            cnt = cnt.where(Order.order_no.ilike(pat) | Order.external_order_id.ilike(pat))

        total = int((await session.execute(cnt)).scalar_one())
        rows = (await session.execute(stmt.order_by(Order.created_at.desc()).limit(limit).offset(offset))).scalars().all()
        items = [await self._serialize_order(session, tenant_id, row) for row in rows]
        return PaginatedResponse(items=items, total=total, limit=limit, offset=offset, has_more=(offset + len(items)) < total)

    async def cancel_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        actor_id: str,
        order_id: UUID,
        warehouse_id: UUID,
        reason: str,
    ) -> OrderRead:
        order = (
            await session.execute(select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id).with_for_update())
        ).scalar_one_or_none()
        if order is None:
            raise AppError(slug="order-not-found", status_code=404, code="ERR_ORDER_NOT_FOUND", title="Order Not Found", detail=f"Order '{order_id}' not found.")
        if order.fulfillment_status in {FulfillmentStatus.SHIPPED, FulfillmentStatus.DELIVERED, FulfillmentStatus.CANCELLED}:
            raise AppError(
                slug="order-cannot-cancel",
                status_code=409,
                code="ERR_ORDER_CANNOT_CANCEL",
                title="Cannot Cancel Order",
                detail=f"Order '{order.order_no}' in status '{order.fulfillment_status}' cannot be cancelled.",
            )

        items = (
            await session.execute(select(OrderItem).where(OrderItem.tenant_id == tenant_id, OrderItem.order_id == order.id))
        ).scalars().all()
        await self.inventory.release_reservation(
            session,
            tenant_id=tenant_id,
            warehouse_id=warehouse_id,
            order_id=order.id,
            items=[(i.variant_id, i.quantity) for i in items],
            idempotency_prefix=f"order-cancel:{order.id}",
        )
        prev_status = order.fulfillment_status.value
        order.fulfillment_status = FulfillmentStatus.CANCELLED
        order.updated_at = datetime.now(timezone.utc)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=actor_id,
            action="order.cancelled",
            resource_type="order",
            resource_id=order.id,
            before_state={"fulfillment_status": prev_status},
            after_state={"fulfillment_status": order.fulfillment_status.value, "reason": reason},
        )
        return await self._serialize_order(session, tenant_id, order)

    async def ship_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        order_id: UUID,
        payload: ShipOrderRequest,
        tracking_adapter: TrackingRegistrationPort | None = None,
    ) -> ShipmentRead:
        order = (
            await session.execute(select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id).with_for_update())
        ).scalar_one_or_none()
        if order is None:
            raise AppError(slug="order-not-found", status_code=404, code="ERR_ORDER_NOT_FOUND", title="Order Not Found", detail=f"Order '{order_id}' not found.")
        if order.fulfillment_status in {FulfillmentStatus.CANCELLED, FulfillmentStatus.REFUNDED}:
            raise AppError(slug="order-inactive", status_code=409, code="ERR_ORDER_INACTIVE", title="Order Inactive", detail="Cannot ship cancelled/refunded order.")

        # Enforce 70% balance gate on B2B wholesale orders
        if order.order_mode == OrderMode.B2B_WHOLESALE:
            unsettled_count = int(
                (
                    await session.execute(
                        text(
                            """
                            SELECT COUNT(*)
                            FROM receivables_payables
                            WHERE tenant_id = :tid AND order_id = :oid
                              AND direction = 'receivable'
                              AND status IN ('open', 'partial', 'overdue')
                            """
                        ),
                        {"tid": tenant_id, "oid": order.id},
                    )
                ).scalar_one()
            )
            if order.payment_status != PaymentStatus.PAID or unsettled_count > 0:
                if not payload.override_unpaid_balance:
                    raise AppError(
                        slug="balance-payment-required",
                        status_code=409,
                        code="ERR_BALANCE_PAYMENT_REQUIRED",
                        title="Balance Payment Required",
                        detail="B2B wholesale order cannot ship until 70% balance receivable is settled (or Owner/Finance override).",
                    )
                if principal.role not in {UserRole.OWNER, UserRole.FINANCE}:
                    raise AppError(
                        slug="insufficient-role",
                        status_code=403,
                        code="ERR_INSUFFICIENT_ROLE",
                        title="Override Forbidden",
                        detail="Only Owner or Finance may authorize shipping with an unpaid balance.",
                    )
                if not payload.override_reason or not payload.override_reason.strip():
                    raise AppError(
                        slug="override-reason-required",
                        status_code=422,
                        code="ERR_OVERRIDE_REASON_REQUIRED",
                        title="Override Reason Required",
                        detail="override_reason is mandatory when overriding unpaid balance gate.",
                    )

        items = (
            await session.execute(select(OrderItem).where(OrderItem.tenant_id == tenant_id, OrderItem.order_id == order.id))
        ).scalars().all()

        shipment = Shipment(
            tenant_id=tenant_id,
            order_id=order.id,
            tracking_provider=payload.tracking_provider,
            tracking_number=payload.tracking_number.strip(),
            carrier_code=payload.carrier_code,
            shipment_mode=payload.shipment_mode,
            standardized_status="info_received",
            actual_shipping_cost_base=payload.actual_shipping_cost_base.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        session.add(shipment)
        await session.flush()

        await self.inventory.deduct_shipped(
            session,
            tenant_id=tenant_id,
            warehouse_id=payload.warehouse_id,
            shipment_id=shipment.id,
            items=[(i.variant_id, i.quantity) for i in items],
            idempotency_prefix=f"order-ship:{shipment.id}",
        )

        if tracking_adapter is not None and payload.tracking_provider == TrackingProvider("17track"):
            await tracking_adapter.register_tracking(shipment.tracking_number, shipment.carrier_code)

        prev_status = order.fulfillment_status.value
        order.fulfillment_status = FulfillmentStatus.SHIPPED
        order.updated_at = datetime.now(timezone.utc)
        await session.flush()

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="shipment.unpaid_balance_override" if payload.override_unpaid_balance else "order.shipped",
            resource_type="order",
            resource_id=order.id,
            before_state={"fulfillment_status": prev_status},
            after_state={
                "fulfillment_status": order.fulfillment_status.value,
                "tracking_number": shipment.tracking_number,
                "override_reason": payload.override_reason,
            },
        )
        return ShipmentRead.model_validate(shipment)

    async def apply_payment_milestone_state(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        new_payment_status: PaymentStatus,
        unlock_production: bool = False,
    ) -> OrderRead:
        order = (
            await session.execute(select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id).with_for_update())
        ).scalar_one_or_none()
        if order is None:
            raise AppError(slug="order-not-found", status_code=404, code="ERR_ORDER_NOT_FOUND", title="Order Not Found", detail=f"Order '{order_id}' not found.")

        order.payment_status = new_payment_status
        if unlock_production and order.fulfillment_status == FulfillmentStatus.PENDING_PAYMENT:
            order.fulfillment_status = (
                FulfillmentStatus.IN_PRODUCTION
                if order.order_mode in {OrderMode.B2B_WHOLESALE, OrderMode.B2B_SAMPLE}
                else FulfillmentStatus.CONFIRMED_RESERVED
            )
        order.updated_at = datetime.now(timezone.utc)
        await session.flush()
        return await self._serialize_order(session, tenant_id, order)

    async def update_external_order_status(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        fulfillment_status: FulfillmentStatus | None = None,
        payment_status: PaymentStatus | None = None,
        channel_metadata_patch: dict[str, Any] | None = None,
    ) -> OrderRead:
        order = (
            await session.execute(select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id).with_for_update())
        ).scalar_one_or_none()
        if order is None:
            raise AppError(slug="order-not-found", status_code=404, code="ERR_ORDER_NOT_FOUND", title="Order Not Found", detail=f"Order '{order_id}' not found.")
        if fulfillment_status is not None:
            order.fulfillment_status = fulfillment_status
        if payment_status is not None:
            order.payment_status = payment_status
        if channel_metadata_patch:
            merged = dict(order.channel_metadata or {})
            merged.update(channel_metadata_patch)
            order.channel_metadata = merged
        order.updated_at = datetime.now(timezone.utc)
        await session.flush()
        return await self._serialize_order(session, tenant_id, order)
```

Create `apps/backend/src/erp_core/orders/router.py`:

```python
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import (
    CancelOrderRequest,
    InventoryBalanceItemRead,
    InventoryLedgerRead,
    OrderCreateRequest,
    OrderRead,
    ShipOrderRequest,
    ShipmentRead,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/orders", tags=["orders"])
_service = OrderService()


def _get_session_factory(request: Request):
    factory = getattr(request.app.state, "session_factory", None) or getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


@router.get("", response_model=PaginatedResponse[OrderRead])
async def list_orders(
    request: Request,
    order_mode: OrderMode | None = None,
    fulfillment_status: FulfillmentStatus | None = None,
    payment_status: PaymentStatus | None = None,
    customer_id: UUID | None = None,
    search: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> PaginatedResponse[OrderRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_orders(
            session,
            principal.tenant_id,
            order_mode=order_mode,
            fulfillment_status=fulfillment_status,
            payment_status=payment_status,
            customer_id=customer_id,
            search=search,
            limit=limit,
            offset=offset,
        )


@router.get("/inventory/balances", response_model=PaginatedResponse[InventoryBalanceItemRead])
async def list_inventory_balances(
    request: Request,
    variant_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> PaginatedResponse[InventoryBalanceItemRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.inventory.list_balances(
            session, principal.tenant_id, variant_id=variant_id, limit=limit, offset=offset
        )


@router.get("/inventory/ledger", response_model=PaginatedResponse[InventoryLedgerRead])
async def list_inventory_ledger(
    request: Request,
    variant_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> PaginatedResponse[InventoryLedgerRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.inventory.list_ledger(
            session, principal.tenant_id, variant_id=variant_id, limit=limit, offset=offset
        )


@router.get("/{order_id}", response_model=OrderRead)
async def get_order(
    request: Request,
    order_id: UUID,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> OrderRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.get_order(session, principal.tenant_id, order_id)


@router.post("", response_model=OrderRead, status_code=status.HTTP_201_CREATED)
async def create_order(
    request: Request,
    payload: OrderCreateRequest,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)),
) -> OrderRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_manual_order(session, principal.tenant_id, principal, payload)


@router.post("/{order_id}/cancel", response_model=OrderRead)
async def cancel_order(
    request: Request,
    order_id: UUID,
    payload: CancelOrderRequest,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)),
) -> OrderRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.cancel_order(
            session,
            principal.tenant_id,
            actor_id=str(principal.user_id),
            order_id=order_id,
            warehouse_id=payload.warehouse_id,
            reason=payload.reason,
        )


@router.post("/{order_id}/ship", response_model=ShipmentRead, status_code=status.HTTP_201_CREATED)
async def ship_order(
    request: Request,
    order_id: UUID,
    payload: ShipOrderRequest,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.FINANCE)
    ),
) -> ShipmentRead:
    tracking_adapter = getattr(request.app.state, "tracking_adapter", None)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.ship_order(
            session,
            principal.tenant_id,
            principal=principal,
            order_id=order_id,
            payload=payload,
            tracking_adapter=tracking_adapter,
        )
```

Modify `apps/backend/src/erp_core/api_main.py` to mount `orders_router`:

```python
from erp_core.orders.router import router as orders_router

app.include_router(orders_router, prefix="/api/v1")
```

- [ ] **Step 4: Run concurrency tests, export OpenAPI, and check import-linter**

Run:
```bash
cd apps/backend && uv run pytest tests/test_inventory_concurrency.py -v
cd apps/backend && uv run python scripts/export_openapi.py > ../../contracts/openapi.json
cd apps/backend && uv run lint-imports --config ../../infra/importlinter.ini
```
Expected: `test_inventory_concurrency.py` PASSES (exactly 1 success, 49 `InsufficientStockError`, `SUM(inventory_ledger) == inventory_balances`); `openapi.json` updated; `lint-imports` passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/orders/ apps/backend/src/erp_core/api_main.py apps/backend/tests/test_inventory_concurrency.py contracts/openapi.json
git commit -m "feat(orders): add deadlock-free InventoryLedgerService & unified dual-mode OrderService"
```

---

### Task 3.5: 17TRACK Logistics Adapter (`Tracking17TrackAdapter`) & 72h Stalled Shipment Monitor

**Files:**
- Create: `apps/backend/src/erp_core/orders/tracking_adapter.py`
- Create: `apps/backend/src/erp_core/orders/tracking_worker.py`
- Create: `apps/backend/src/erp_core/orders/tracking_router.py`
- Modify: `apps/backend/src/erp_core/api_main.py` (include `tracking_router` on `/api/v1`)
- Modify: `apps/backend/src/erp_core/worker_main.py` (register `process_17track_webhook` & `monitor_stalled_shipments_cron`)
- Test: `apps/backend/tests/test_17track_adapter.py`

**Interfaces:**
- Consumes:
  - `erp_core.shared.db`: `tenant_session`, `system_session`
  - `erp_core.tenancy.audit`: `record_audit_event`
  - `erp_core.tenancy.dependencies`: `CurrentPrincipal`, `require_roles`
- Produces:
  - `Tracking17TrackAdapter`:
    - `async def register_tracking(self, tracking_number: str, carrier_code: str | None = None) -> dict[str, Any]`
    - `async def fetch_status(self, tracking_number: str, cache_level: int = 0) -> dict[str, Any]`
    - `verify_webhook_signature(raw_body: bytes, received_sign: str, webhook_secret: str) -> bool`
    - `map_17track_status(main_status: str | int | None, sub_status: str | None = None) -> tuple[str, str | None]`
  - `detect_stalled_shipments_for_tenant`, `process_17track_webhook_event`, `monitor_stalled_shipments_cron`
  - HTTP endpoints: `/api/v1/webhooks/17track`, `/api/v1/orders/{order_id}/shipments`, `/api/v1/orders/{order_id}/shipments/{shipment_id}/refresh`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_17track_adapter.py`:

```python
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.orders.tracking_adapter import Tracking17TrackAdapter, map_17track_status, verify_17track_signature
from erp_core.orders.tracking_worker import detect_stalled_shipments_for_tenant, process_17track_webhook_event
from erp_core.shared.db import tenant_session


def _migrator_url() -> str:
    return os.getenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_secret@localhost:5432/erp_saas",
    )


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )


@pytest.mark.asyncio
async def test_17track_cache_level_0_enforcement_and_signature() -> None:
    captured_payloads: list[tuple[str, object]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_payloads.append((request.url.path, body))
        if request.url.path.endswith("/register"):
            return httpx.Response(200, json={"code": 0, "data": {"accepted": body, "rejected": []}})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "accepted": [
                        {
                            "number": body[0]["number"],
                            "track_info": {
                                "latest_status": {"status": "InTransit", "sub_status": "InTransit_Departure"},
                                "latest_event": {"time_utc": "2026-11-18T10:00:00Z", "description": "Departed Shenzhen"},
                            },
                        }
                    ]
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="https://api.17track.net/track/v2.2")
    adapter = Tracking17TrackAdapter(api_key="test-17-key", http_client=client)

    try:
        reg = await adapter.register_tracking("YT2631900123456")
        assert reg["code"] == 0

        # cacheLevel=1 MUST raise ValueError immediately to prevent 10x quota burn
        with pytest.raises(ValueError, match="cacheLevel"):
            await adapter.fetch_status("YT2631900123456", cache_level=1)

        info = await adapter.fetch_status("YT2631900123456", cache_level=0)
        assert info["code"] == 0
        fetch_calls = [p for p in captured_payloads if p[0].endswith("/gettrackinfo")]
        assert len(fetch_calls) == 1
        assert fetch_calls[0][1][0]["cacheLevel"] == 0

        # Signature verification: sha256(raw_body + "/" + secret)
        raw = b'{"event":"TRACKING_UPDATED","data":{"number":"YT2631900123456"}}'
        secret = "wh-secret-17"
        expected_sign = hashlib.sha256(raw + b"/" + secret.encode("utf-8")).hexdigest()
        assert verify_17track_signature(raw, expected_sign, secret) is True
        assert verify_17track_signature(raw, "0" * 64, secret) is False

        # 9+30 mapping verification
        assert map_17track_status("DeliveryFailure", "DeliveryFailure_InvalidAddress") == (
            "undelivered",
            "invalid_address",
        )
        assert map_17track_status("Exception", "Exception_CustomsHold") == ("alert", "customs_hold")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_17track_webhook_update_and_72h_stalled_proactive_wismo() -> None:
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    rt_engine = create_async_engine(_runtime_url(), echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    customer_id = uuid4()
    order_id = uuid4()
    stalled_shipment_id = uuid4()
    alert_shipment_id = uuid4()

    class _FakeStreamRedis:
        def __init__(self) -> None:
            self.entries: list[tuple[str, dict]] = []

        async def xadd(self, stream: str, fields: dict) -> str:
            self.entries.append((stream, fields))
            return f"{len(self.entries)}-0"

    try:
        async with mig_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tenant_id, "name": "17TRACK Test", "slug": f"t17-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text("INSERT INTO customers (id, tenant_id, customer_type, display_name) VALUES (:id, :tid, 'b2c_consumer', 'Jane Doe')"),
                {"id": customer_id, "tid": tenant_id},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO orders (
                        id, tenant_id, order_no, order_mode, customer_id, currency,
                        fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status, payment_status
                    ) VALUES (
                        :oid, :tid, 'ORD-17T-001', 'b2c_retail', :cid, 'USD',
                        7.200000, 50.00, 50.00, 'shipped', 'paid'
                    )
                    """
                ),
                {"oid": order_id, "tid": tenant_id, "cid": customer_id},
            )
            stalled_time = datetime.now(timezone.utc) - timedelta(hours=80)
            await conn.execute(
                text(
                    """
                    INSERT INTO shipments (
                        id, tenant_id, order_id, tracking_provider, tracking_number,
                        standardized_status, sub_status, last_event_at, exception_alert_sent
                    ) VALUES
                    (:sid1, :tid, :oid, '17track', 'YT-STALLED-72H', 'in_transit', 'departure', :ts, false),
                    (:sid2, :tid, :oid, '17track', 'YT-CUSTOMS-HOLD', 'info_received', NULL, now(), false)
                    """
                ),
                {"sid1": stalled_shipment_id, "sid2": alert_shipment_id, "tid": tenant_id, "oid": order_id, "ts": stalled_time},
            )

        # Simulate webhook moving YT-CUSTOMS-HOLD to Exception / Exception_CustomsHold
        await process_17track_webhook_event(
            rt_factory,
            {
                "event": "TRACKING_UPDATED",
                "data": {
                    "number": "YT-CUSTOMS-HOLD",
                    "track_info": {
                        "latest_status": {"status": "Exception", "sub_status": "Exception_CustomsHold"},
                        "latest_event": {"time_utc": "2026-11-19T01:00:00Z", "description": "Held at customs"},
                    },
                },
            },
        )

        fake_redis = _FakeStreamRedis()
        async with tenant_session(rt_factory, tenant_id) as session:
            events = await detect_stalled_shipments_for_tenant(session, tenant_id, redis_client=fake_redis)
            assert len(events) == 2
            reasons_by_trk = {e.tracking_number: e.trigger_reason for e in events}
            assert reasons_by_trk["YT-STALLED-72H"] == "in_transit_stalled_72h"
            assert reasons_by_trk["YT-CUSTOMS-HOLD"] == "status_alert"
            assert len(fake_redis.entries) == 2
            assert all(fields["event_type"] == "PROACTIVE_WISMO_OUTREACH" for _, fields in fake_redis.entries)

            # Second run must NOT emit duplicate alerts
            events_second = await detect_stalled_shipments_for_tenant(session, tenant_id, redis_client=fake_redis)
            assert events_second == []
    finally:
        async with mig_engine.begin() as conn:
            await conn.execute(text("DELETE FROM shipments WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM orders WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_17track_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.orders.tracking_adapter'`

- [ ] **Step 3: Write `orders/tracking_adapter.py`, `orders/tracking_worker.py`, `orders/tracking_router.py`, and wire `api_main.py` & `worker_main.py`**

Create `apps/backend/src/erp_core/orders/tracking_adapter.py`:

```python
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

import httpx

MAIN_STATUS_MAP: dict[str, str] = {
    "0": "not_found",
    "NotFound": "not_found",
    "10": "info_received",
    "InfoReceived": "info_received",
    "20": "in_transit",
    "InTransit": "in_transit",
    "30": "expired",
    "Expired": "expired",
    "35": "available_for_pickup",
    "AvailableForPickup": "available_for_pickup",
    "40": "out_for_delivery",
    "OutForDelivery": "out_for_delivery",
    "50": "undelivered",
    "DeliveryFailure": "undelivered",
    "60": "delivered",
    "Delivered": "delivered",
    "70": "alert",
    "Exception": "alert",
}

SUB_STATUS_MAP: dict[str, str] = {
    "NotFound_Other": "not_found_other",
    "NotFound_InvalidCode": "invalid_code",
    "InfoReceived_NoTrackingYet": "no_tracking_yet",
    "InTransit_PickedUp": "picked_up",
    "InTransit_Other": "in_transit_other",
    "InTransit_Departure": "departure",
    "InTransit_Arrival": "arrival",
    "InTransit_CustomsProcessing": "customs_processing",
    "InTransit_CustomsReleased": "customs_released",
    "InTransit_CustomsRequiringInformation": "customs_info_required",
    "Expired_Other": "expired_other",
    "AvailableForPickup_Other": "pickup_other",
    "AvailableForPickup_Ready": "ready_for_pickup",
    "AvailableForPickup_Overdue": "pickup_overdue",
    "OutForDelivery_Other": "out_for_delivery_other",
    "OutForDelivery_Delay": "out_for_delivery_delay",
    "DeliveryFailure_Other": "delivery_failure_other",
    "DeliveryFailure_NoBody": "no_recipient",
    "DeliveryFailure_Security": "security_issue",
    "DeliveryFailure_Rejected": "rejected_by_recipient",
    "DeliveryFailure_InvalidAddress": "invalid_address",
    "Delivered_Other": "delivered_other",
    "Delivered_Signed": "signed",
    "Exception_Other": "exception_other",
    "Exception_Returning": "returning_to_sender",
    "Exception_Returned": "returned_to_sender",
    "Exception_NoBody": "undeliverable_no_body",
    "Exception_Lost": "lost_in_transit",
    "Exception_Damaged": "damaged",
    "Exception_CustomsHold": "customs_hold",
}


def verify_17track_signature(raw_body: bytes, received_sign: str, webhook_secret: str) -> bool:
    """Verify 17TRACK v2.2 webhook `sign` header: sha256(raw_body + '/' + secret)."""
    if not received_sign or not webhook_secret:
        return False
    try:
        body_text = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        body_text = raw_body.decode("latin1")
    source = f"{body_text}/{webhook_secret}".encode("utf-8")
    expected = hashlib.sha256(source).hexdigest()
    return hmac.compare_digest(expected, received_sign.strip().lower())


def map_17track_status(main_status: str | int | None, sub_status: str | None = None) -> tuple[str, str | None]:
    key = str(main_status) if main_status is not None else "NotFound"
    mapped_main = MAIN_STATUS_MAP.get(key, "not_found")
    mapped_sub = SUB_STATUS_MAP.get(sub_status) if sub_status else None
    if sub_status and mapped_sub is None:
        mapped_sub = sub_status.lower()
    return mapped_main, mapped_sub


class Tracking17TrackAdapter:
    """17TRACK API v2.2 adapter enforcing cacheLevel=0 quota protection."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.17track.net/track/v2.2",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("TRACK17_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self._client = http_client

    async def _post(self, path: str, payload: list[dict[str, Any]]) -> dict[str, Any]:
        headers = {"17token": self.api_key, "Content-Type": "application/json"}
        if self._client is not None:
            resp = await self._client.post(f"{self.base_url}{path}", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self.base_url}{path}", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def register_tracking(self, tracking_number: str, carrier_code: str | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {"number": tracking_number.strip()}
        if carrier_code and carrier_code.isdigit():
            item["carrier"] = int(carrier_code)
        else:
            item["auto_detection"] = True
        return await self._post("/register", [item])

    async def fetch_status(self, tracking_number: str, cache_level: int = 0) -> dict[str, Any]:
        if cache_level != 0:
            raise ValueError(
                f"17TRACK cacheLevel={cache_level} is strictly forbidden in automated flows (10x quota burn); must be 0."
            )
        return await self._post("/gettrackinfo", [{"number": tracking_number.strip(), "cacheLevel": 0}])
```

Create `apps/backend/src/erp_core/orders/tracking_worker.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.orders.tracking_adapter import map_17track_status
from erp_core.shared.db import system_session, tenant_session
from erp_core.tenancy.audit import record_audit_event
from erp_core.worker_main import broker


@dataclass(slots=True)
class ProactiveWismoEvent:
    tenant_id: UUID
    shipment_id: UUID
    order_id: UUID
    tracking_number: str
    standardized_status: str
    sub_status: str | None
    trigger_reason: str


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def process_17track_webhook_event(
    session_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
) -> int:
    """Apply a verified 17TRACK webhook update inside the owning tenant's RLS context."""
    data = payload.get("data") or {}
    tracking_number = str(data.get("number") or "").strip()
    if not tracking_number:
        return 0

    track_info = data.get("track_info") or {}
    latest_status = track_info.get("latest_status") or {}
    latest_event = track_info.get("latest_event") or {}
    std_status, sub_status = map_17track_status(latest_status.get("status"), latest_status.get("sub_status"))
    event_time = _parse_iso_dt(latest_event.get("time_utc") or latest_event.get("time_iso")) or datetime.now(
        timezone.utc
    )

    async with system_session(session_factory) as sys_sess:
        matches = (
            await sys_sess.execute(
                text("SELECT id, tenant_id FROM shipments WHERE tracking_number = :tn"),
                {"tn": tracking_number},
            )
        ).all()

    updated = 0
    for match in matches:
        async with tenant_session(session_factory, match.tenant_id) as t_sess:
            await t_sess.execute(
                text(
                    """
                    UPDATE shipments
                    SET standardized_status = :std_status,
                        sub_status = :sub_status,
                        last_event_at = :event_at,
                        last_synced_at = now(),
                        tracking_events = COALESCE(tracking_events, '[]'::jsonb) || CAST(:new_evt AS jsonb)
                    WHERE tenant_id = :tid AND id = :sid
                    """
                ),
                {
                    "std_status": std_status,
                    "sub_status": sub_status,
                    "event_at": event_time,
                    "new_evt": json.dumps([latest_event] if latest_event else []),
                    "tid": match.tenant_id,
                    "sid": match.id,
                },
            )
            if std_status == "delivered":
                await t_sess.execute(
                    text(
                        """
                        UPDATE orders
                        SET fulfillment_status = 'delivered', updated_at = now()
                        WHERE tenant_id = :tid
                          AND id = (SELECT order_id FROM shipments WHERE tenant_id = :tid AND id = :sid)
                          AND fulfillment_status IN ('shipped', 'partially_shipped')
                        """
                    ),
                    {"tid": match.tenant_id, "sid": match.id},
                )
            updated += 1
    return updated


async def detect_stalled_shipments_for_tenant(
    session: AsyncSession,
    tenant_id: UUID,
    redis_client: Any | None = None,
    now: datetime | None = None,
) -> list[ProactiveWismoEvent]:
    """Detect Alert/Undelivered or In-Transit > 72h shipments and emit PROACTIVE_WISMO_OUTREACH."""
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(hours=72)

    rows = (
        await session.execute(
            text(
                """
                SELECT id, order_id, tracking_number, standardized_status, sub_status, last_event_at
                FROM shipments
                WHERE tenant_id = :tid
                  AND exception_alert_sent = false
                  AND (
                    standardized_status IN ('alert', 'undelivered')
                    OR (standardized_status = 'in_transit' AND last_event_at IS NOT NULL AND last_event_at < :cutoff)
                  )
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"tid": tenant_id, "cutoff": cutoff},
        )
    ).all()

    if not rows:
        return []

    has_proposals_table = bool(
        (await session.execute(text("SELECT to_regclass('public.ai_proposals') IS NOT NULL"))).scalar_one()
    )

    events: list[ProactiveWismoEvent] = []
    for row in rows:
        reason = (
            "status_alert"
            if row.standardized_status in ("alert", "undelivered")
            else "in_transit_stalled_72h"
        )
        await session.execute(
            text("UPDATE shipments SET exception_alert_sent = true WHERE tenant_id = :tid AND id = :sid"),
            {"tid": tenant_id, "sid": row.id},
        )
        evt = ProactiveWismoEvent(
            tenant_id=tenant_id,
            shipment_id=row.id,
            order_id=row.order_id,
            tracking_number=row.tracking_number,
            standardized_status=row.standardized_status,
            sub_status=row.sub_status,
            trigger_reason=reason,
        )
        events.append(evt)

        payload_dict = {
            "event_type": "PROACTIVE_WISMO_OUTREACH",
            "tenant_id": str(tenant_id),
            "shipment_id": str(row.id),
            "order_id": str(row.order_id),
            "tracking_number": row.tracking_number,
            "standardized_status": row.standardized_status,
            "sub_status": row.sub_status or "",
            "trigger_reason": reason,
        }
        if redis_client is not None:
            await redis_client.xadd("stream:erp:events", payload_dict)

        if has_proposals_table:
            await session.execute(
                text(
                    """
                    INSERT INTO ai_proposals (
                        tenant_id, capability, proposal_type, target_entity_type,
                        target_entity_id, proposed_payload, confidence_score, status
                    ) VALUES (
                        :tid, 'order_logistics', 'follow_up_suggestion', 'shipment',
                        :sid, CAST(:payload AS jsonb), 0.950, 'pending'
                    )
                    """
                ),
                {"tid": tenant_id, "sid": row.id, "payload": json.dumps(payload_dict)},
            )

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="system_cron",
            actor_id="tracking_stalled_monitor",
            action="shipment.proactive_wismo_outreach",
            resource_type="shipment",
            resource_id=row.id,
            after_state=payload_dict,
        )

    return events


@broker.task
async def process_17track_webhook_task(payload: dict[str, Any]) -> int:
    from erp_core.shared.config import get_settings
    from erp_core.shared.db import create_runtime_engine, create_session_factory

    engine = create_runtime_engine(get_settings())
    session_factory = create_session_factory(engine)
    try:
        return await process_17track_webhook_event(session_factory, payload)
    finally:
        await engine.dispose()


@broker.task(schedule=[{"cron": "0 * * * *"}])
async def monitor_stalled_shipments_cron() -> int:
    from erp_core.shared.config import get_settings
    from erp_core.shared.db import create_runtime_engine, create_session_factory
    from erp_core.shared.redis_client import create_redis_client

    settings = get_settings()
    engine = create_runtime_engine(settings)
    session_factory = create_session_factory(engine)
    redis_client = create_redis_client(settings)
    try:
        async with system_session(session_factory) as sys_sess:
            tenant_ids = [
                row.tenant_id
                for row in (
                    await sys_sess.execute(
                        text("SELECT DISTINCT tenant_id FROM shipments WHERE exception_alert_sent = false")
                    )
                ).all()
            ]

        total_alerts = 0
        for tid in tenant_ids:
            async with tenant_session(session_factory, tid) as t_sess:
                evts = await detect_stalled_shipments_for_tenant(t_sess, tid, redis_client=redis_client)
                total_alerts += len(evts)
        return total_alerts
    finally:
        await redis_client.aclose()
        await engine.dispose()
```

Create `apps/backend/src/erp_core/orders/tracking_router.py`:

```python
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel
from sqlalchemy import select, text

from erp_core.orders.models import Shipment
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import ShipOrderRequest, ShipmentRead
from erp_core.orders.tracking_adapter import Tracking17TrackAdapter, map_17track_status, verify_17track_signature
from erp_core.orders.tracking_worker import process_17track_webhook_task
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(tags=["logistics"])
_order_service = OrderService()


def _get_session_factory(request: Request):
    factory = getattr(request.app.state, "session_factory", None) or getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


@router.post("/webhooks/17track")
async def receive_17track_webhook(
    request: Request,
    sign: str | None = Header(default=None, alias="sign"),
) -> dict[str, object]:
    raw_body = await request.body()
    secret = os.getenv("TRACK17_WEBHOOK_SECRET", "")
    if not sign or not verify_17track_signature(raw_body, sign, secret):
        raise AppError(
            slug="invalid-webhook-signature",
            status_code=401,
            code="ERR_INVALID_WEBHOOK_SIGNATURE",
            title="Invalid 17TRACK Signature",
            detail="Webhook signature verification failed.",
        )

    event_hash = hashlib.sha256(raw_body).hexdigest()
    payload = json.loads(raw_body.decode("utf-8"))

    async with system_session(_get_session_factory(request)) as sys_sess:
        inserted = (
            await sys_sess.execute(
                text(
                    """
                    INSERT INTO raw_webhook_events (channel_type, external_event_id, payload, status)
                    VALUES ('17track', :eid, CAST(:payload AS jsonb), 'received')
                    ON CONFLICT (channel_type, external_event_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {"eid": event_hash, "payload": raw_body.decode("utf-8")},
            )
        ).scalar_one_or_none()

    if inserted is not None:
        redis_client = getattr(request.app.state, "redis", None)
        if redis_client is not None:
            await redis_client.xadd(
                "stream:webhooks:17track",
                {"raw_event_id": str(inserted), "external_event_id": event_hash},
            )
        await process_17track_webhook_task.kiq(payload)

    return {"code": 200, "status": "accepted"}


@router.get("/orders/{order_id}/shipments", response_model=list[ShipmentRead])
async def list_order_shipments(
    request: Request,
    order_id: UUID,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE)
    ),
) -> list[ShipmentRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        rows = (
            await session.execute(
                select(Shipment)
                .where(Shipment.tenant_id == principal.tenant_id, Shipment.order_id == order_id)
                .order_by(Shipment.created_at.desc())
            )
        ).scalars().all()
        return [ShipmentRead.model_validate(r) for r in rows]


@router.post("/orders/{order_id}/shipments", response_model=ShipmentRead, status_code=status.HTTP_201_CREATED)
async def create_order_shipment(
    request: Request,
    order_id: UUID,
    payload: ShipOrderRequest,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.FINANCE)
    ),
) -> ShipmentRead:
    adapter = getattr(request.app.state, "tracking_adapter", None) or Tracking17TrackAdapter()
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _order_service.ship_order(
            session,
            tenant_id=principal.tenant_id,
            principal=principal,
            order_id=order_id,
            payload=payload,
            tracking_adapter=adapter,
        )


class RefreshTrackingRequest(BaseModel):
    cache_level: int = 0


@router.post("/orders/{order_id}/shipments/{shipment_id}/refresh", response_model=ShipmentRead)
@router.post("/orders/{order_id}/shipments/{shipment_id}/refresh-tracking", response_model=ShipmentRead)
async def refresh_shipment_status(
    request: Request,
    order_id: UUID,
    shipment_id: UUID,
    payload: RefreshTrackingRequest | None = None,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
    ),
) -> ShipmentRead:
    adapter = getattr(request.app.state, "tracking_adapter", None) or Tracking17TrackAdapter()
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        shipment = (
            await session.execute(
                select(Shipment)
                .where(Shipment.tenant_id == principal.tenant_id, Shipment.order_id == order_id, Shipment.id == shipment_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if shipment is None:
            raise AppError(slug="shipment-not-found", status_code=404, code="ERR_SHIPMENT_NOT_FOUND", title="Shipment Not Found", detail="Shipment not found.")

        now = datetime.now(timezone.utc)
        if shipment.last_synced_at is not None and (now - shipment.last_synced_at) < timedelta(hours=6):
            raise AppError(
                slug="tracking-cooldown",
                status_code=429,
                code="ERR_TRACKING_COOLDOWN",
                title="17TRACK Refresh Cooldown",
                detail="Manual tracking refresh is rate-limited to once every 6 hours per shipment.",
            )

        # Explicitly pass cache_level=0
        resp = await adapter.fetch_status(shipment.tracking_number, cache_level=0)
        accepted = ((resp.get("data") or {}).get("accepted") or [{}])[0]
        track_info = accepted.get("track_info") or {}
        latest_status = track_info.get("latest_status") or {}
        std_status, sub_status = map_17track_status(latest_status.get("status"), latest_status.get("sub_status"))
        shipment.standardized_status = std_status
        shipment.sub_status = sub_status
        shipment.last_synced_at = now
        await session.flush()
        return ShipmentRead.model_validate(shipment)
```

Modify `apps/backend/src/erp_core/api_main.py` and `apps/backend/src/erp_core/worker_main.py`:

```python
# In apps/backend/src/erp_core/api_main.py
from erp_core.orders.tracking_router import router as tracking_router

app.include_router(tracking_router, prefix="/api/v1")

# In apps/backend/src/erp_core/worker_main.py (import AFTER `broker` is defined to avoid circular import)
import erp_core.orders.tracking_worker  # noqa: F401
```

- [ ] **Step 4: Run 17TRACK tests, export OpenAPI, and check import-linter**

Run:
```bash
cd apps/backend && uv run pytest tests/test_17track_adapter.py -v
cd apps/backend && uv run python scripts/export_openapi.py > ../../contracts/openapi.json
cd apps/backend && uv run lint-imports --config ../../infra/importlinter.ini
```
Expected: `test_17track_adapter.py` PASSES; `openapi.json` includes `/api/v1/webhooks/17track` and `/api/v1/orders/{order_id}/shipments*`; `lint-imports` passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/orders/tracking_adapter.py apps/backend/src/erp_core/orders/tracking_worker.py apps/backend/src/erp_core/orders/tracking_router.py apps/backend/src/erp_core/api_main.py apps/backend/src/erp_core/worker_main.py apps/backend/tests/test_17track_adapter.py contracts/openapi.json
git commit -m "feat(logistics): add 17TRACK cacheLevel=0 adapter, webhook ingress & 72h stalled WISMO monitor"
```

---

### Task 3.6: Lightweight Finance Milestones (30%/70% Gate) & `OrderProfitService` (Decimal CVP)

**Files:**
- Create: `apps/backend/src/erp_core/finance/schemas.py`
- Create: `apps/backend/src/erp_core/finance/profit_service.py`
- Create: `apps/backend/src/erp_core/finance/payment_service.py`
- Create: `apps/backend/src/erp_core/finance/router.py`
- Modify: `apps/backend/src/erp_core/api_main.py` (include `finance_router` on `/api/v1`)
- Test: `apps/backend/tests/test_finance_cvp.py`

**Interfaces:**
- Consumes:
  - `erp_core.orders.order_service`: `OrderService` (`apply_payment_milestone_state`, `ship_order`)
  - `erp_core.tenancy.audit`: `record_audit_event`
  - `erp_core.tenancy.dependencies`: `CurrentPrincipal`, `require_roles`
  - `erp_core.shared.errors`: `AppError`
  - `erp_core.shared.pagination`: `PaginatedResponse`
- Produces:
  - `OrderProfitService`:
    - `async def recalculate_order_profit(self, session: AsyncSession, tenant_id: UUID, order_id: UUID, platform_commission_base: Decimal | None = None, channel_msg_cost_base: Decimal | None = None, ad_attribution_cost_base: Decimal | None = None) -> OrderProfitSnapshotRead`
    - `get_order_profit`, `get_profit_summary`
  - `PaymentService` (`create_b2b_milestone_receivables`, `create_single_receivable`, `record_and_verify_payment`, `list_receivables`)
  - HTTP endpoints: `/api/v1/finance/receivables`, `/api/v1/finance/payments`, `/api/v1/finance/orders/{order_id}/profit`, `/api/v1/finance/profit-summary`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_finance_cvp.py`:

```python
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.catalog.schemas import MoqTierInput, ProductCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.crm.pdf_renderer import InMemoryPdfStorage, ProformaInvoicePdfRenderer
from erp_core.crm.quote_service import QuoteService
from erp_core.crm.schemas import ConvertQuoteRequest, OpportunityCreate, QuoteCreate, QuoteItemInput
from erp_core.finance.payment_service import PaymentService
from erp_core.finance.profit_service import OrderProfitService
from erp_core.finance.schemas import PaymentCreate
from erp_core.orders.inventory_service import InventoryLedgerService
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import ShipOrderRequest
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, Incoterm, OrderMode, PaymentStatus, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal


def _migrator_url() -> str:
    return os.getenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_secret@localhost:5432/erp_saas",
    )


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )


@pytest.mark.asyncio
async def test_30_70_milestone_gate_fx_gain_loss_and_decimal_cvp() -> None:
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    rt_engine = create_async_engine(_runtime_url(), echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    owner_id = uuid4()
    finance_id = uuid4()
    manager_id = uuid4()
    agent_id = uuid4()
    customer_id = uuid4()
    warehouse_id = uuid4()

    owner = CurrentPrincipal(user_id=owner_id, tenant_id=tenant_id, role=UserRole.OWNER, email="owner@qz.cn")
    finance = CurrentPrincipal(user_id=finance_id, tenant_id=tenant_id, role=UserRole.FINANCE, email="fin@qz.cn")
    manager = CurrentPrincipal(user_id=manager_id, tenant_id=tenant_id, role=UserRole.MANAGER, email="mgr@qz.cn")
    agent = CurrentPrincipal(user_id=agent_id, tenant_id=tenant_id, role=UserRole.AGENT, email="agent@qz.cn")

    try:
        async with mig_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tenant_id, "name": "QZ Finance Test", "slug": f"fin-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, customer_type, display_name) VALUES (:id, :tid, 'b2b_contact', 'Global LED Buyer')"
                ),
                {"id": customer_id, "tid": tenant_id},
            )

        catalog_svc = CatalogService()
        inv_svc = InventoryLedgerService()
        order_svc = OrderService(inventory_service=inv_svc)
        profit_svc = OrderProfitService()
        payment_svc = PaymentService(order_service=order_svc, profit_service=profit_svc)
        quote_svc = QuoteService(
            catalog_service=catalog_svc,
            pdf_renderer=ProformaInvoicePdfRenderer(storage=InMemoryPdfStorage()),
        )

        async with tenant_session(rt_factory, tenant_id) as session:
            prod = await catalog_svc.create_product(
                session,
                tenant_id,
                actor_id=str(manager_id),
                payload=ProductCreate(spu_code="QZ-FIN-5M", category="lighting"),
            )
            variant = await catalog_svc.create_variant(
                session,
                tenant_id,
                actor_id=str(manager_id),
                role=UserRole.MANAGER,
                payload=VariantCreate(
                    product_id=prod.id,
                    sku="LED-FIN-5M",
                    cost_price_base=Decimal("40.0000"),
                    base_currency="CNY",
                    min_margin_floor_pct=Decimal("15.00"),
                    retail_price_usd=Decimal("15.00"),
                    b2b_moq_tiers=[MoqTierInput(min_qty=100, unit_price_usd=Decimal("10.00"))],
                    weight_grams=250,
                ),
            )
            await inv_svc.inbound_stock(
                session,
                tenant_id=tenant_id,
                warehouse_id=warehouse_id,
                variant_id=variant.id,
                qty=200,
                reference_id=uuid4(),
                idempotency_key=f"fin-seed-{variant.id}",
            )
            opp = await quote_svc.create_opportunity(
                session,
                tenant_id,
                OpportunityCreate(
                    customer_id=customer_id,
                    title="100 pcs LED Strip Wholesale",
                    expected_amount=Decimal("1000.00"),
                    currency="USD",
                    owner_user_id=manager_id,
                ),
            )
            quote = await quote_svc.create_quote(
                session,
                tenant_id,
                principal=manager,
                payload=QuoteCreate(
                    opportunity_id=opp.id,
                    incoterm=Incoterm.FOB,
                    payment_terms="30% T/T deposit, 70% before shipment",
                    base_currency="CNY",
                    quote_currency="USD",
                    fx_rate_snapshot=Decimal("7.200000"),
                    fx_locked_until=datetime.now(timezone.utc) + timedelta(days=5),
                    items=[QuoteItemInput(variant_id=variant.id, quantity=100)],
                ),
            )
            await quote_svc.send_quote(session, tenant_id, manager, quote.id)
            await quote_svc.accept_quote(session, tenant_id, manager, quote.id)

            # End-to-end QuoteService using full runtime Task 3.4 & 3.6 services
            conv = await quote_svc.convert_quote_to_order(
                session,
                tenant_id,
                principal=manager,
                quote_id=quote.id,
                payload=ConvertQuoteRequest(warehouse_id=warehouse_id, order_mode=OrderMode.B2B_WHOLESALE),
            )
            order_id = conv.order.id
            dep_rp = next(r for r in conv.receivables if r.milestone_label == "b2b_30_deposit")
            bal_rp = next(r for r in conv.receivables if r.milestone_label == "b2b_70_balance")
            assert dep_rp.due_amount == Decimal("300.00")
            assert bal_rp.due_amount == Decimal("700.00")

            # Agent CANNOT verify T/T bank slips
            with pytest.raises(AppError) as exc_agent:
                await payment_svc.record_and_verify_payment(
                    session,
                    tenant_id,
                    principal=agent,
                    payload=PaymentCreate(
                        rp_id=dep_rp.id,
                        payment_method="tt_wire",
                        external_tx_ref="SWIFT-001",
                        amount_received=Decimal("300.00"),
                        currency="USD",
                        actual_fx_rate=Decimal("7.250000"),
                        gateway_fee=Decimal("15.00"),
                    ),
                )
            assert exc_agent.value.status_code == 403

            # Finance verifies 30% deposit -> unlocks IN_PRODUCTION
            await payment_svc.record_and_verify_payment(
                session,
                tenant_id,
                principal=finance,
                payload=PaymentCreate(
                    rp_id=dep_rp.id,
                    payment_method="tt_wire",
                    external_tx_ref="SWIFT-DEPOSIT-001",
                    amount_received=Decimal("300.00"),
                    currency="USD",
                    actual_fx_rate=Decimal("7.250000"),
                    gateway_fee=Decimal("15.00"),
                    receipt_r2_key="tenants/demo/slips/deposit.png",
                ),
            )
            ord_after_dep = await order_svc.get_order(session, tenant_id, order_id)
            assert ord_after_dep.payment_status == PaymentStatus.DEPOSIT_PAID
            assert ord_after_dep.fulfillment_status == FulfillmentStatus.IN_PRODUCTION

            # Attempting to ship before 70% balance MUST block
            with pytest.raises(AppError) as exc_block:
                await order_svc.ship_order(
                    session,
                    tenant_id,
                    principal=manager,
                    order_id=order_id,
                    payload=ShipOrderRequest(warehouse_id=warehouse_id, tracking_number="DHL-PREMATURE-001"),
                )
            assert exc_block.value.code == "ERR_BALANCE_PAYMENT_REQUIRED"

            # Manager CANNOT override unpaid balance gate (only Owner/Finance)
            with pytest.raises(AppError) as exc_mgr_ovr:
                await order_svc.ship_order(
                    session,
                    tenant_id,
                    principal=manager,
                    order_id=order_id,
                    payload=ShipOrderRequest(
                        warehouse_id=warehouse_id,
                        tracking_number="DHL-MGR-OVERRIDE",
                        override_unpaid_balance=True,
                        override_reason="Trusting VIP",
                    ),
                )
            assert exc_mgr_ovr.value.status_code == 403

            # Finance verifies 70% balance at 7.18 FX rate with $20 wire fee
            await payment_svc.record_and_verify_payment(
                session,
                tenant_id,
                principal=owner,
                payload=PaymentCreate(
                    rp_id=bal_rp.id,
                    payment_method="tt_wire",
                    external_tx_ref="SWIFT-BALANCE-002",
                    amount_received=Decimal("700.00"),
                    currency="USD",
                    actual_fx_rate=Decimal("7.180000"),
                    gateway_fee=Decimal("20.00"),
                ),
            )
            ord_paid = await order_svc.get_order(session, tenant_id, order_id)
            assert ord_paid.payment_status == PaymentStatus.PAID

            # Now shipment succeeds with actual shipping cost 350.00 CNY
            await order_svc.ship_order(
                session,
                tenant_id,
                principal=manager,
                order_id=order_id,
                payload=ShipOrderRequest(
                    warehouse_id=warehouse_id,
                    tracking_number="DHL-PAID-001",
                    actual_shipping_cost_base=Decimal("350.00"),
                ),
            )

            # Recompute CVP profit with WhatsApp template msg cost = 2.50 CNY
            snap = await profit_svc.recalculate_order_profit(
                session,
                tenant_id=tenant_id,
                order_id=order_id,
                channel_msg_cost_base=Decimal("2.50"),
            )
            # Math check (pure Decimal):
            # gross_revenue_base = 1000.00 * 7.20 = 7200.00
            # cogs_base = 100 * 40.0000 = 4000.00
            # gateway_fee_base = 15.00 * 7.25 + 20.00 * 7.18 = 108.75 + 143.60 = 252.35
            # shipping_and_duty_base = 350.00
            # channel_msg_cost_base = 2.50
            # fx_gain_loss_base = 300.00*(7.25-7.20) + 700.00*(7.18-7.20) = 15.00 - 14.00 = +1.00
            # net_contribution_margin_base = 7200.00 - 4000.00 - 0 - 252.35 - 350.00 - 2.50 - 0 + 1.00 = 2596.15
            assert snap.gross_revenue_base == Decimal("7200.00")
            assert snap.cogs_base == Decimal("4000.00")
            assert snap.payment_gateway_fee_base == Decimal("252.35")
            assert snap.shipping_and_duty_base == Decimal("350.00")
            assert snap.channel_msg_cost_base == Decimal("2.50")
            assert snap.fx_gain_loss_base == Decimal("1.00")
            assert snap.net_contribution_margin_base == Decimal("2596.15")

            summary = await profit_svc.get_profit_summary(session, tenant_id)
            assert summary.total_orders == 1
            assert summary.net_contribution_margin_base == Decimal("2596.15")
    finally:
        async with mig_engine.begin() as conn:
            await conn.execute(text("DELETE FROM order_profit_snapshots WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM payment_records WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM receivables_payables WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM shipments WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM inventory_ledger WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM inventory_balances WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM order_items WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM orders WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM quote_items WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM quotes WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM opportunities WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM product_variants WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM products WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_finance_cvp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.finance.schemas'`

- [ ] **Step 3: Write `finance/schemas.py`, `finance/profit_service.py`, `finance/payment_service.py`, `finance/router.py`, and mount router**

Create `apps/backend/src/erp_core/finance/schemas.py`:

```python
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.shared.enums import FulfillmentStatus, OrderMode


class ReceivableRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    order_id: UUID
    direction: str
    milestone_label: str
    due_amount: Decimal
    paid_amount: Decimal
    currency: str
    due_date: date | None
    status: str
    created_at: datetime


class PaymentCreate(BaseModel):
    rp_id: UUID
    payment_method: str = Field(min_length=1, max_length=64)
    external_tx_ref: str | None = Field(default=None, max_length=128)
    amount_received: Decimal = Field(gt=Decimal("0"))
    currency: str = Field(min_length=3, max_length=3)
    actual_fx_rate: Decimal = Field(gt=Decimal("0"))
    gateway_fee: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    receipt_r2_key: str | None = None
    paid_at: datetime | None = None


class PaymentRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    rp_id: UUID
    payment_method: str
    external_tx_ref: str | None
    amount_received: Decimal
    currency: str
    actual_fx_rate: Decimal
    gateway_fee: Decimal
    receipt_r2_key: str | None
    verified_by_user_id: UUID | None
    paid_at: datetime
    created_at: datetime


class OrderProfitSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: UUID
    tenant_id: UUID
    base_currency: str
    gross_revenue_base: Decimal
    cogs_base: Decimal
    platform_commission_base: Decimal
    payment_gateway_fee_base: Decimal
    shipping_and_duty_base: Decimal
    channel_msg_cost_base: Decimal
    ad_attribution_cost_base: Decimal
    fx_gain_loss_base: Decimal
    net_contribution_margin_base: Decimal
    contribution_margin_pct: Decimal
    recalculated_at: datetime


class ProfitSummaryRead(BaseModel):
    base_currency: str
    total_orders: int
    gross_revenue_base: Decimal
    cogs_base: Decimal
    total_variable_expenses_base: Decimal
    fx_gain_loss_base: Decimal
    net_contribution_margin_base: Decimal
    contribution_margin_pct: Decimal
    order_mode: OrderMode | None = None


class FinanceOverviewSummaryRead(BaseModel):
    total_gross_revenue_base: Decimal
    total_net_margin_base: Decimal
    avg_margin_pct: Decimal
    base_currency: str


class FinanceOverviewReceivableItem(BaseModel):
    id: UUID
    order_id: UUID
    order_no: str
    order_mode: OrderMode
    milestone_label: str
    due_amount: Decimal
    paid_amount: Decimal
    currency: str
    status: str
    shipment_blocked_70_unpaid: bool
    pending_payment_id: UUID | None = None
    pending_tx_ref: str | None = None


class FinanceOverviewSnapshotItem(BaseModel):
    order_id: UUID
    order_no: str
    base_currency: str
    gross_revenue_base: Decimal
    cogs_base: Decimal
    platform_commission_base: Decimal
    payment_gateway_fee_base: Decimal
    shipping_and_duty_base: Decimal
    channel_msg_cost_base: Decimal
    ad_attribution_cost_base: Decimal
    fx_gain_loss_base: Decimal
    net_contribution_margin_base: Decimal


class FinanceOverviewRead(BaseModel):
    summary: FinanceOverviewSummaryRead
    receivables: list[FinanceOverviewReceivableItem]
    cvp_snapshots: list[FinanceOverviewSnapshotItem]


class PaymentVerifyResponse(BaseModel):
    ok: bool = True
    payment: PaymentRecordRead
    order_fulfillment_status: FulfillmentStatus
```

Create `apps/backend/src/erp_core/finance/profit_service.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.models import OrderProfitSnapshot, PaymentRecord, ReceivablePayable
from erp_core.finance.schemas import OrderProfitSnapshotRead, ProfitSummaryRead
from erp_core.shared.enums import FulfillmentStatus, OrderMode
from erp_core.shared.errors import AppError


def _q2(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class OrderProfitService:
    """Decimal-exact CVP contribution margin snapshot calculator and aggregator."""

    @staticmethod
    def _to_read(snap: OrderProfitSnapshot) -> OrderProfitSnapshotRead:
        gross = Decimal(str(snap.gross_revenue_base))
        net = Decimal(str(snap.net_contribution_margin_base))
        margin_pct = Decimal("0.00") if gross <= Decimal("0") else _q2(net / gross * Decimal("100"))
        return OrderProfitSnapshotRead(
            order_id=snap.order_id,
            tenant_id=snap.tenant_id,
            base_currency=snap.base_currency,
            gross_revenue_base=gross,
            cogs_base=Decimal(str(snap.cogs_base)),
            platform_commission_base=Decimal(str(snap.platform_commission_base)),
            payment_gateway_fee_base=Decimal(str(snap.payment_gateway_fee_base)),
            shipping_and_duty_base=Decimal(str(snap.shipping_and_duty_base)),
            channel_msg_cost_base=Decimal(str(snap.channel_msg_cost_base)),
            ad_attribution_cost_base=Decimal(str(snap.ad_attribution_cost_base)),
            fx_gain_loss_base=Decimal(str(snap.fx_gain_loss_base)),
            net_contribution_margin_base=net,
            contribution_margin_pct=margin_pct,
            recalculated_at=snap.recalculated_at,
        )

    async def recalculate_order_profit(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        platform_commission_base: Decimal | None = None,
        channel_msg_cost_base: Decimal | None = None,
        ad_attribution_cost_base: Decimal | None = None,
    ) -> OrderProfitSnapshotRead:
        order_row = (
            await session.execute(
                text(
                    """
                    SELECT
                        o.id, o.total_amount, o.fx_rate_to_base, o.fulfillment_status,
                        o.payment_status, o.channel_metadata, t.base_currency
                    FROM orders o
                    JOIN tenants t ON t.id = o.tenant_id
                    WHERE o.tenant_id = :tid AND o.id = :oid
                    """
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).one_or_none()
        if order_row is None:
            raise AppError(slug="order-not-found", status_code=404, code="ERR_ORDER_NOT_FOUND", title="Order Not Found", detail=f"Order '{order_id}' not found.")

        existing = (
            await session.execute(
                select(OrderProfitSnapshot).where(
                    OrderProfitSnapshot.tenant_id == tenant_id, OrderProfitSnapshot.order_id == order_id
                )
            )
        ).scalar_one_or_none()

        locked_fx = Decimal(str(order_row.fx_rate_to_base))
        order_total = Decimal(str(order_row.total_amount))
        metadata = dict(order_row.channel_metadata or {})
        refunded_ccy = Decimal(str(metadata.get("refunded_amount", "0")))

        if order_row.fulfillment_status == "cancelled":
            gross_revenue_base = Decimal("0.00")
            cogs_base = Decimal("0.00")
        else:
            net_ccy = max(Decimal("0.00"), order_total - refunded_ccy)
            gross_revenue_base = _q2(net_ccy * locked_fx)
            cogs_raw = (
                await session.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(quantity * unit_cost_base_snapshot), 0)
                        FROM order_items
                        WHERE tenant_id = :tid AND order_id = :oid
                        """
                    ),
                    {"tid": tenant_id, "oid": order_id},
                )
            ).scalar_one()
            cogs_base = _q2(Decimal(str(cogs_raw)))

        ship_raw = (
            await session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(actual_shipping_cost_base), 0)
                    FROM shipments
                    WHERE tenant_id = :tid AND order_id = :oid
                    """
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).scalar_one()
        shipping_and_duty_base = _q2(Decimal(str(ship_raw)))

        payment_rows = (
            await session.execute(
                select(PaymentRecord)
                .join(ReceivablePayable, ReceivablePayable.id == PaymentRecord.rp_id)
                .where(
                    PaymentRecord.tenant_id == tenant_id,
                    ReceivablePayable.tenant_id == tenant_id,
                    ReceivablePayable.order_id == order_id,
                    ReceivablePayable.direction == "receivable",
                )
            )
        ).scalars().all()

        gateway_fee_base = Decimal("0.00")
        fx_gain_loss_base = Decimal("0.00")
        for pr in payment_rows:
            amt = Decimal(str(pr.amount_received))
            actual_fx = Decimal(str(pr.actual_fx_rate))
            fee = Decimal(str(pr.gateway_fee))
            gateway_fee_base += fee * actual_fx
            fx_gain_loss_base += amt * (actual_fx - locked_fx)

        gateway_fee_base = _q2(gateway_fee_base)
        fx_gain_loss_base = _q2(fx_gain_loss_base)

        comm_base = _q2(
            platform_commission_base
            if platform_commission_base is not None
            else (
                Decimal(str(existing.platform_commission_base))
                if existing is not None
                else Decimal(str(metadata.get("platform_commission_base", "0.00")))
            )
        )
        msg_base = _q2(
            channel_msg_cost_base
            if channel_msg_cost_base is not None
            else (Decimal(str(existing.channel_msg_cost_base)) if existing is not None else Decimal("0.00"))
        )
        ad_base = _q2(
            ad_attribution_cost_base
            if ad_attribution_cost_base is not None
            else (
                Decimal(str(existing.ad_attribution_cost_base))
                if existing is not None
                else Decimal(str(metadata.get("ad_attribution_cost_base", "0.00")))
            )
        )

        now = datetime.now(timezone.utc)
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO order_profit_snapshots (
                        order_id, tenant_id, base_currency, gross_revenue_base, cogs_base,
                        platform_commission_base, payment_gateway_fee_base, shipping_and_duty_base,
                        channel_msg_cost_base, ad_attribution_cost_base, fx_gain_loss_base, recalculated_at
                    ) VALUES (
                        :oid, :tid, :bccy, :gross, :cogs, :comm, :gw, :ship, :msg, :ad, :fx, :ts
                    )
                    ON CONFLICT (order_id) DO UPDATE SET
                        base_currency = EXCLUDED.base_currency,
                        gross_revenue_base = EXCLUDED.gross_revenue_base,
                        cogs_base = EXCLUDED.cogs_base,
                        platform_commission_base = EXCLUDED.platform_commission_base,
                        payment_gateway_fee_base = EXCLUDED.payment_gateway_fee_base,
                        shipping_and_duty_base = EXCLUDED.shipping_and_duty_base,
                        channel_msg_cost_base = EXCLUDED.channel_msg_cost_base,
                        ad_attribution_cost_base = EXCLUDED.ad_attribution_cost_base,
                        fx_gain_loss_base = EXCLUDED.fx_gain_loss_base,
                        recalculated_at = EXCLUDED.recalculated_at
                    RETURNING order_id
                    """
                ),
                {
                    "oid": order_id,
                    "tid": tenant_id,
                    "bccy": order_row.base_currency or "CNY",
                    "gross": gross_revenue_base,
                    "cogs": cogs_base,
                    "comm": comm_base,
                    "gw": gateway_fee_base,
                    "ship": shipping_and_duty_base,
                    "msg": msg_base,
                    "ad": ad_base,
                    "fx": fx_gain_loss_base,
                    "ts": now,
                },
            )
        ).one()
        await session.flush()

        refreshed = (
            await session.execute(
                select(OrderProfitSnapshot).where(
                    OrderProfitSnapshot.tenant_id == tenant_id, OrderProfitSnapshot.order_id == row.order_id
                )
            )
        ).scalar_one()
        await session.refresh(refreshed)
        return self._to_read(refreshed)

    async def get_order_profit(
        self, session: AsyncSession, tenant_id: UUID, order_id: UUID
    ) -> OrderProfitSnapshotRead:
        snap = (
            await session.execute(
                select(OrderProfitSnapshot).where(
                    OrderProfitSnapshot.tenant_id == tenant_id, OrderProfitSnapshot.order_id == order_id
                )
            )
        ).scalar_one_or_none()
        if snap is None:
            return await self.recalculate_order_profit(session, tenant_id, order_id)
        return self._to_read(snap)

    async def get_profit_summary(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_mode: OrderMode | None = None,
        channel_account_id: UUID | None = None,
    ) -> ProfitSummaryRead:
        clauses = ["s.tenant_id = :tid"]
        params: dict[str, object] = {"tid": tenant_id}
        if order_mode is not None:
            clauses.append("o.order_mode = :omode")
            params["omode"] = order_mode.value
        if channel_account_id is not None:
            clauses.append("o.channel_account_id = :caid")
            params["caid"] = channel_account_id

        where_sql = " AND ".join(clauses)
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        COALESCE(MAX(s.base_currency), 'CNY') AS base_currency,
                        COUNT(*) AS total_orders,
                        COALESCE(SUM(s.gross_revenue_base), 0) AS gross,
                        COALESCE(SUM(s.cogs_base), 0) AS cogs,
                        COALESCE(SUM(
                            s.platform_commission_base + s.payment_gateway_fee_base
                            + s.shipping_and_duty_base + s.channel_msg_cost_base
                            + s.ad_attribution_cost_base
                        ), 0) AS variable_expenses,
                        COALESCE(SUM(s.fx_gain_loss_base), 0) AS fx_gain_loss,
                        COALESCE(SUM(s.net_contribution_margin_base), 0) AS net_margin
                    FROM order_profit_snapshots s
                    JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                    WHERE {where_sql}
                    """
                ),
                params,
            )
        ).one()

        gross = _q2(Decimal(str(row.gross)))
        net = _q2(Decimal(str(row.net_margin)))
        pct = Decimal("0.00") if gross <= Decimal("0") else _q2(net / gross * Decimal("100"))
        return ProfitSummaryRead(
            base_currency=str(row.base_currency),
            total_orders=int(row.total_orders),
            gross_revenue_base=gross,
            cogs_base=_q2(Decimal(str(row.cogs))),
            total_variable_expenses_base=_q2(Decimal(str(row.variable_expenses))),
            fx_gain_loss_base=_q2(Decimal(str(row.fx_gain_loss))),
            net_contribution_margin_base=net,
            contribution_margin_pct=pct,
            order_mode=order_mode,
        )
```

Create `apps/backend/src/erp_core/finance/payment_service.py`:

```python
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.finance.models import PaymentRecord, ReceivablePayable
from erp_core.finance.profit_service import OrderProfitService
from erp_core.finance.schemas import (
    FinanceOverviewRead,
    FinanceOverviewReceivableItem,
    FinanceOverviewSnapshotItem,
    FinanceOverviewSummaryRead,
    PaymentCreate,
    PaymentRecordRead,
    PaymentVerifyResponse,
    ReceivableRead,
)
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import OrderRead
from erp_core.shared.enums import OrderMode, PaymentStatus, UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal


class PaymentService:
    """B2B/B2C receivables, T/T bank slip L0 verification, and milestone order unlocking."""

    def __init__(
        self,
        order_service: OrderService | None = None,
        profit_service: OrderProfitService | None = None,
    ) -> None:
        self.orders = order_service or OrderService()
        self.profits = profit_service or OrderProfitService()

    async def create_b2b_milestone_receivables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        total_amount: Decimal,
        currency: str,
        deposit_ratio: Decimal = Decimal("0.30"),
        due_date_deposit: date | None = None,
        due_date_balance: date | None = None,
    ) -> list[ReceivableRead]:
        total_q = total_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        deposit_amt = (total_q * deposit_ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        balance_amt = (total_q - deposit_amt).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        dep_rp = ReceivablePayable(
            tenant_id=tenant_id,
            order_id=order_id,
            direction="receivable",
            milestone_label="b2b_30_deposit",
            due_amount=deposit_amt,
            paid_amount=Decimal("0.00"),
            currency=currency.upper(),
            due_date=due_date_deposit,
            status="open",
        )
        bal_rp = ReceivablePayable(
            tenant_id=tenant_id,
            order_id=order_id,
            direction="receivable",
            milestone_label="b2b_70_balance",
            due_amount=balance_amt,
            paid_amount=Decimal("0.00"),
            currency=currency.upper(),
            due_date=due_date_balance,
            status="open",
        )
        session.add(dep_rp)
        session.add(bal_rp)
        await session.flush()
        return [ReceivableRead.model_validate(dep_rp), ReceivableRead.model_validate(bal_rp)]

    async def create_single_receivable(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID,
        milestone_label: str,
        due_amount: Decimal,
        currency: str,
        status: str = "open",
        paid_amount: Decimal = Decimal("0.00"),
    ) -> ReceivableRead:
        rp = ReceivablePayable(
            tenant_id=tenant_id,
            order_id=order_id,
            direction="receivable",
            milestone_label=milestone_label,
            due_amount=due_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            paid_amount=paid_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            currency=currency.upper(),
            status=status,
        )
        session.add(rp)
        await session.flush()
        return ReceivableRead.model_validate(rp)

    async def list_receivables(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        order_id: UUID | None = None,
        status_filter: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[ReceivableRead]:
        stmt = select(ReceivablePayable).where(ReceivablePayable.tenant_id == tenant_id)
        cnt = select(func.count()).select_from(ReceivablePayable).where(ReceivablePayable.tenant_id == tenant_id)
        if order_id is not None:
            stmt = stmt.where(ReceivablePayable.order_id == order_id)
            cnt = cnt.where(ReceivablePayable.order_id == order_id)
        if status_filter is not None:
            stmt = stmt.where(ReceivablePayable.status == status_filter)
            cnt = cnt.where(ReceivablePayable.status == status_filter)

        total = int((await session.execute(cnt)).scalar_one())
        rows = (
            await session.execute(stmt.order_by(ReceivablePayable.created_at.desc()).limit(limit).offset(offset))
        ).scalars().all()
        items = [ReceivableRead.model_validate(r) for r in rows]
        return PaginatedResponse(items=items, total=total, limit=limit, offset=offset, has_more=(offset + len(items)) < total)

    async def _apply_verified_settlement(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        rp: ReceivablePayable,
        pr: PaymentRecord,
    ) -> OrderRead:
        amt = Decimal(str(pr.amount_received)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        new_paid = (Decimal(str(rp.paid_amount)) + amt).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rp.paid_amount = new_paid
        rp.status = "settled" if new_paid >= Decimal(str(rp.due_amount)) else "partial"
        await session.flush()

        order_rps = (
            await session.execute(
                select(ReceivablePayable)
                .where(
                    ReceivablePayable.tenant_id == tenant_id,
                    ReceivablePayable.order_id == rp.order_id,
                    ReceivablePayable.direction == "receivable",
                    ReceivablePayable.status != "void",
                )
                .with_for_update()
            )
        ).scalars().all()

        all_settled = bool(order_rps) and all(item.status == "settled" for item in order_rps)
        deposit_settled = any(
            item.milestone_label == "b2b_30_deposit" and item.status == "settled" for item in order_rps
        )
        if all_settled:
            order_read = await self.orders.apply_payment_milestone_state(
                session, tenant_id, rp.order_id, PaymentStatus.PAID, unlock_production=True
            )
        elif deposit_settled:
            order_read = await self.orders.apply_payment_milestone_state(
                session, tenant_id, rp.order_id, PaymentStatus.DEPOSIT_PAID, unlock_production=True
            )
        else:
            order_read = await self.orders.apply_payment_milestone_state(
                session, tenant_id, rp.order_id, PaymentStatus.PARTIALLY_PAID, unlock_production=False
            )

        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="payment.verified",
            resource_type="payment_record",
            resource_id=pr.id,
            after_state={
                "rp_id": str(rp.id),
                "order_id": str(rp.order_id),
                "amount_received": str(pr.amount_received),
                "actual_fx_rate": str(pr.actual_fx_rate),
                "rp_status": rp.status,
            },
        )
        await self.profits.recalculate_order_profit(session, tenant_id, rp.order_id)
        return order_read

    @staticmethod
    def _ensure_verifier_role(principal: CurrentPrincipal) -> None:
        if principal.role not in {UserRole.OWNER, UserRole.FINANCE}:
            raise AppError(
                slug="insufficient-role",
                status_code=403,
                code="ERR_INSUFFICIENT_ROLE",
                title="Payment Verification Forbidden",
                detail="Only Owner or Finance role may verify and record bank slip payments (L0 control).",
            )

    async def _lock_open_receivable(
        self, session: AsyncSession, tenant_id: UUID, rp_id: UUID
    ) -> ReceivablePayable:
        rp = (
            await session.execute(
                select(ReceivablePayable)
                .where(ReceivablePayable.tenant_id == tenant_id, ReceivablePayable.id == rp_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if rp is None:
            raise AppError(
                slug="receivable-not-found",
                status_code=404,
                code="ERR_RECEIVABLE_NOT_FOUND",
                title="Receivable Not Found",
                detail="Receivable not found.",
            )
        if rp.status in ("settled", "void"):
            raise AppError(
                slug="receivable-already-settled",
                status_code=409,
                code="ERR_RECEIVABLE_ALREADY_SETTLED",
                title="Receivable Settled or Void",
                detail=f"Receivable '{rp.id}' is already in status '{rp.status}'.",
            )
        return rp

    async def record_and_verify_payment(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: PaymentCreate,
    ) -> PaymentRecordRead:
        self._ensure_verifier_role(principal)
        rp = await self._lock_open_receivable(session, tenant_id, payload.rp_id)

        amt = payload.amount_received.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pr = PaymentRecord(
            tenant_id=tenant_id,
            rp_id=rp.id,
            payment_method=payload.payment_method,
            external_tx_ref=payload.external_tx_ref,
            amount_received=amt,
            currency=payload.currency.upper(),
            actual_fx_rate=payload.actual_fx_rate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            gateway_fee=payload.gateway_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            receipt_r2_key=payload.receipt_r2_key,
            verified_by_user_id=principal.user_id,
            paid_at=payload.paid_at or datetime.now(timezone.utc),
        )
        session.add(pr)
        await session.flush()
        await self._apply_verified_settlement(session, tenant_id, principal, rp, pr)
        return PaymentRecordRead.model_validate(pr)

    async def submit_payment_receipt(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payload: PaymentCreate,
    ) -> PaymentRecordRead:
        rp = await self._lock_open_receivable(session, tenant_id, payload.rp_id)
        pr = PaymentRecord(
            tenant_id=tenant_id,
            rp_id=rp.id,
            payment_method=payload.payment_method,
            external_tx_ref=payload.external_tx_ref,
            amount_received=payload.amount_received.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            currency=payload.currency.upper(),
            actual_fx_rate=payload.actual_fx_rate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            gateway_fee=payload.gateway_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            receipt_r2_key=payload.receipt_r2_key,
            verified_by_user_id=None,
            paid_at=payload.paid_at or datetime.now(timezone.utc),
        )
        session.add(pr)
        await session.flush()
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="payment.submitted",
            resource_type="payment_record",
            resource_id=pr.id,
            after_state={"rp_id": str(rp.id), "external_tx_ref": pr.external_tx_ref},
        )
        return PaymentRecordRead.model_validate(pr)

    async def verify_payment(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        principal: CurrentPrincipal,
        payment_id: UUID,
    ) -> PaymentVerifyResponse:
        self._ensure_verifier_role(principal)
        pr = (
            await session.execute(
                select(PaymentRecord)
                .where(PaymentRecord.tenant_id == tenant_id, PaymentRecord.id == payment_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if pr is None:
            raise AppError(
                slug="payment-not-found",
                status_code=404,
                code="ERR_PAYMENT_NOT_FOUND",
                title="Payment Record Not Found",
                detail=f"Payment record '{payment_id}' not found.",
            )
        if pr.verified_by_user_id is not None:
            raise AppError(
                slug="payment-already-verified",
                status_code=409,
                code="ERR_PAYMENT_ALREADY_VERIFIED",
                title="Payment Already Verified",
                detail=f"Payment record '{payment_id}' has already been verified.",
            )
        rp = await self._lock_open_receivable(session, tenant_id, pr.rp_id)
        pr.verified_by_user_id = principal.user_id
        order_read = await self._apply_verified_settlement(session, tenant_id, principal, rp, pr)
        return PaymentVerifyResponse(
            ok=True,
            payment=PaymentRecordRead.model_validate(pr),
            order_fulfillment_status=order_read.fulfillment_status,
        )

    async def get_overview(self, session: AsyncSession, tenant_id: UUID) -> FinanceOverviewRead:
        summary = await self.profits.get_profit_summary(session, tenant_id)
        rp_rows = (
            await session.execute(
                text(
                    """
                    SELECT rp.id, rp.order_id, o.order_no, o.order_mode, rp.milestone_label,
                           rp.due_amount, rp.paid_amount, rp.currency, rp.status,
                           CASE
                               WHEN o.order_mode IN ('b2b_wholesale', 'b2b_sample')
                                    AND EXISTS (
                                        SELECT 1 FROM receivables_payables r70
                                        WHERE r70.tenant_id = rp.tenant_id
                                          AND r70.order_id = rp.order_id
                                          AND r70.milestone_label = 'b2b_70_balance'
                                          AND r70.status <> 'settled'
                                    )
                               THEN true
                               ELSE false
                           END AS shipment_blocked_70_unpaid,
                           pend.id AS pending_payment_id,
                           pend.external_tx_ref AS pending_tx_ref
                    FROM receivables_payables rp
                    JOIN orders o ON o.id = rp.order_id AND o.tenant_id = rp.tenant_id
                    LEFT JOIN LATERAL (
                        SELECT pr.id, pr.external_tx_ref
                        FROM payment_records pr
                        WHERE pr.tenant_id = rp.tenant_id
                          AND pr.rp_id = rp.id
                          AND pr.verified_by_user_id IS NULL
                        ORDER BY pr.created_at DESC
                        LIMIT 1
                    ) pend ON true
                    WHERE rp.tenant_id = :tid AND rp.direction = 'receivable'
                    ORDER BY rp.created_at DESC
                    LIMIT 100
                    """
                ),
                {"tid": tenant_id},
            )
        ).all()

        snap_rows = (
            await session.execute(
                text(
                    """
                    SELECT s.order_id, o.order_no, s.base_currency, s.gross_revenue_base, s.cogs_base,
                           s.platform_commission_base, s.payment_gateway_fee_base,
                           s.shipping_and_duty_base, s.channel_msg_cost_base,
                           s.ad_attribution_cost_base, s.fx_gain_loss_base,
                           s.net_contribution_margin_base
                    FROM order_profit_snapshots s
                    JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                    WHERE s.tenant_id = :tid
                    ORDER BY s.recalculated_at DESC
                    LIMIT 100
                    """
                ),
                {"tid": tenant_id},
            )
        ).all()

        return FinanceOverviewRead(
            summary=FinanceOverviewSummaryRead(
                total_gross_revenue_base=summary.gross_revenue_base,
                total_net_margin_base=summary.net_contribution_margin_base,
                avg_margin_pct=summary.contribution_margin_pct,
                base_currency=summary.base_currency,
            ),
            receivables=[
                FinanceOverviewReceivableItem(
                    id=r.id,
                    order_id=r.order_id,
                    order_no=str(r.order_no),
                    order_mode=OrderMode(str(r.order_mode)),
                    milestone_label=str(r.milestone_label),
                    due_amount=Decimal(str(r.due_amount)),
                    paid_amount=Decimal(str(r.paid_amount)),
                    currency=str(r.currency),
                    status=str(r.status),
                    shipment_blocked_70_unpaid=bool(r.shipment_blocked_70_unpaid),
                    pending_payment_id=r.pending_payment_id,
                    pending_tx_ref=r.pending_tx_ref,
                )
                for r in rp_rows
            ],
            cvp_snapshots=[
                FinanceOverviewSnapshotItem(
                    order_id=s.order_id,
                    order_no=str(s.order_no),
                    base_currency=str(s.base_currency),
                    gross_revenue_base=Decimal(str(s.gross_revenue_base)),
                    cogs_base=Decimal(str(s.cogs_base)),
                    platform_commission_base=Decimal(str(s.platform_commission_base)),
                    payment_gateway_fee_base=Decimal(str(s.payment_gateway_fee_base)),
                    shipping_and_duty_base=Decimal(str(s.shipping_and_duty_base)),
                    channel_msg_cost_base=Decimal(str(s.channel_msg_cost_base)),
                    ad_attribution_cost_base=Decimal(str(s.ad_attribution_cost_base)),
                    fx_gain_loss_base=Decimal(str(s.fx_gain_loss_base)),
                    net_contribution_margin_base=Decimal(str(s.net_contribution_margin_base)),
                )
                for s in snap_rows
            ],
        )

    async def export_cvp_csv(self, session: AsyncSession, tenant_id: UUID) -> str:
        overview = await self.get_overview(session, tenant_id)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "order_id",
                "order_no",
                "base_currency",
                "gross_revenue_base",
                "cogs_base",
                "platform_commission_base",
                "payment_gateway_fee_base",
                "shipping_and_duty_base",
                "channel_msg_cost_base",
                "ad_attribution_cost_base",
                "fx_gain_loss_base",
                "net_contribution_margin_base",
            ]
        )
        for snap in overview.cvp_snapshots:
            writer.writerow(
                [
                    str(snap.order_id),
                    snap.order_no,
                    snap.base_currency,
                    str(snap.gross_revenue_base),
                    str(snap.cogs_base),
                    str(snap.platform_commission_base),
                    str(snap.payment_gateway_fee_base),
                    str(snap.shipping_and_duty_base),
                    str(snap.channel_msg_cost_base),
                    str(snap.ad_attribution_cost_base),
                    str(snap.fx_gain_loss_base),
                    str(snap.net_contribution_margin_base),
                ]
            )
        return buf.getvalue()
```

Create `apps/backend/src/erp_core/finance/router.py`:

```python
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status

from erp_core.finance.payment_service import PaymentService
from erp_core.finance.profit_service import OrderProfitService
from erp_core.finance.schemas import (
    FinanceOverviewRead,
    OrderProfitSnapshotRead,
    PaymentCreate,
    PaymentRecordRead,
    PaymentVerifyResponse,
    ProfitSummaryRead,
    ReceivableRead,
)
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import OrderMode, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/finance", tags=["finance"])
_payment_service = PaymentService()
_profit_service = OrderProfitService()


def _get_session_factory(request: Request):
    factory = getattr(request.app.state, "session_factory", None) or getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


@router.get("/overview", response_model=FinanceOverviewRead)
async def get_finance_overview(
    request: Request,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.FINANCE, UserRole.ADMIN, UserRole.MANAGER)
    ),
) -> FinanceOverviewRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _payment_service.get_overview(session, principal.tenant_id)


@router.get("/cvp/export.csv")
async def export_cvp_csv(
    request: Request,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.FINANCE, UserRole.ADMIN, UserRole.MANAGER)
    ),
) -> Response:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        csv_body = await _payment_service.export_cvp_csv(session, principal.tenant_id)
        return Response(
            content=csv_body,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="cvp-snapshots.csv"'},
        )


@router.get("/receivables", response_model=PaginatedResponse[ReceivableRead])
async def list_receivables(
    request: Request,
    order_id: UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.FINANCE, UserRole.ADMIN, UserRole.MANAGER)
    ),
) -> PaginatedResponse[ReceivableRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _payment_service.list_receivables(
            session,
            principal.tenant_id,
            order_id=order_id,
            status_filter=status_filter,
            limit=limit,
            offset=offset,
        )


@router.post("/receivables/{rp_id}/payments", response_model=PaymentRecordRead, status_code=status.HTTP_201_CREATED)
async def submit_receivable_payment(
    request: Request,
    rp_id: UUID,
    payment_method: str = Form(...),
    external_tx_ref: str | None = Form(default=None),
    amount_received: Decimal = Form(...),
    currency: str = Form(...),
    actual_fx_rate: Decimal = Form(...),
    gateway_fee: Decimal = Form(default=Decimal("0.00")),
    receipt_file: UploadFile | None = File(default=None),
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.FINANCE, UserRole.ADMIN, UserRole.MANAGER)
    ),
) -> PaymentRecordRead:
    receipt_key: str | None = None
    if receipt_file is not None and receipt_file.filename:
        raw = await receipt_file.read()
        storage = getattr(request.app.state, "storage", None) or ObjectStorageService(get_settings())
        receipt_key = await storage.upload_bytes(
            key=f"receipts/{principal.tenant_id}/{rp_id}/{uuid4().hex}",
            data=raw,
            content_type=receipt_file.content_type or "application/octet-stream",
        )
    payload = PaymentCreate(
        rp_id=rp_id,
        payment_method=payment_method,
        external_tx_ref=external_tx_ref,
        amount_received=amount_received,
        currency=currency,
        actual_fx_rate=actual_fx_rate,
        gateway_fee=gateway_fee,
        receipt_r2_key=receipt_key,
    )
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _payment_service.submit_payment_receipt(session, principal.tenant_id, principal, payload)


@router.post("/payments", response_model=PaymentRecordRead, status_code=status.HTTP_201_CREATED)
async def record_payment(
    request: Request,
    payload: PaymentCreate,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.FINANCE)),
) -> PaymentRecordRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _payment_service.record_and_verify_payment(session, principal.tenant_id, principal, payload)


@router.post("/payments/{payment_id}/verify", response_model=PaymentVerifyResponse)
async def verify_payment_record(
    request: Request,
    payment_id: UUID,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.FINANCE)),
) -> PaymentVerifyResponse:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _payment_service.verify_payment(session, principal.tenant_id, principal, payment_id)


@router.get("/orders/{order_id}/profit", response_model=OrderProfitSnapshotRead)
async def get_order_profit(
    request: Request,
    order_id: UUID,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.FINANCE, UserRole.ADMIN, UserRole.MANAGER)
    ),
) -> OrderProfitSnapshotRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _profit_service.get_order_profit(session, principal.tenant_id, order_id)


@router.get("/profit-summary", response_model=ProfitSummaryRead)
async def get_profit_summary(
    request: Request,
    order_mode: OrderMode | None = None,
    channel_account_id: UUID | None = None,
    principal: CurrentPrincipal = Depends(
        require_roles(UserRole.OWNER, UserRole.FINANCE, UserRole.ADMIN, UserRole.MANAGER)
    ),
) -> ProfitSummaryRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _profit_service.get_profit_summary(
            session, principal.tenant_id, order_mode=order_mode, channel_account_id=channel_account_id
        )
```

Modify `apps/backend/src/erp_core/api_main.py` to mount `finance_router`:

```python
from erp_core.finance.router import router as finance_router

app.include_router(finance_router, prefix="/api/v1")
```

- [ ] **Step 4: Run finance/CVP & B2B full-integration tests, export OpenAPI, and check import-linter**

Run:
```bash
cd apps/backend && uv run pytest tests/test_finance_cvp.py tests/test_b2b_quotes_and_pi.py -v
cd apps/backend && uv run python scripts/export_openapi.py > ../../contracts/openapi.json
cd apps/backend && uv run lint-imports --config ../../infra/importlinter.ini
```
Expected: All tests in `test_finance_cvp.py` and `test_b2b_quotes_and_pi.py` PASS; `openapi.json` includes `/api/v1/finance/*`; `lint-imports` passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/finance/ apps/backend/src/erp_core/api_main.py apps/backend/tests/test_finance_cvp.py contracts/openapi.json
git commit -m "feat(finance): add 30%/70% milestone gates, bank slip verification & Decimal CVP snapshots"
```

---

### Task 3.7: Shopify GraphQL Admin API Connector (`2025-04+` GraphQL-Only + Plus B2B + GDPR)

**Files:**
- Create: `apps/backend/src/erp_core/channels/ecommerce/__init__.py`
- Create: `apps/backend/src/erp_core/channels/ecommerce/base.py`
- Create: `apps/backend/src/erp_core/channels/ecommerce/shopify.py`
- Create: `apps/backend/src/erp_core/channels/ecommerce/shopify_router.py`
- Modify: `apps/backend/src/erp_core/api_main.py` (include `shopify_router` on `/api/v1`)
- Modify: `apps/backend/src/erp_core/worker_main.py` (import Shopify webhook task module)
- Test: `apps/backend/tests/test_shopify_connector.py`

**Interfaces:**
- Consumes:
  - `erp_core.shared.crypto`: `EnvelopeCryptoService`
  - `erp_core.shared.db`: `tenant_session`, `system_session`
  - `erp_core.identity.service`: `IdentityResolverService`
  - `erp_core.catalog.service`: `CatalogService`
  - `erp_core.orders.order_service`: `OrderService`
  - `erp_core.finance.payment_service`: `PaymentService`
  - `erp_core.finance.profit_service`: `OrderProfitService`
  - `erp_core.tenancy.audit`: `record_audit_event`
  - `erp_core.tenancy.dependencies`: `CurrentPrincipal`, `require_roles`
  - Plan 01 GDPR service in `erp_core.tenancy` (`ShopifyGdprPort` -> `TenancyGdprBridge`)
- Produces:
  - `CommercePlatformAdapter`, `EcommerceOrderSyncService`, `CredentialVaultHelper`, `NormalizedOrder`, `NormalizedProduct`
  - `ShopifyGraphqlAdapter`, `ShopifyGdprPort`, `TenancyGdprBridge`, `process_shopify_webhook_event`
  - HTTP endpoints: `/api/v1/connectors/shopify/oauth/install`, `/api/v1/connectors/shopify/oauth/callback`, `/api/v1/connectors/shopify/custom-app`, `/api/v1/webhooks/shopify`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_shopify_connector.py`:

```python
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.catalog.schemas import ProductCreate, SkuMappingCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.channels.ecommerce.base import CredentialVaultHelper, EcommerceOrderSyncService
from erp_core.channels.ecommerce.shopify import (
    ShopifyGraphqlAdapter,
    process_shopify_webhook_event,
    verify_shopify_webhook_hmac,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus, UserRole


def _migrator_url() -> str:
    return os.getenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_secret@localhost:5432/erp_saas",
    )


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )


@pytest.mark.asyncio
async def test_shopify_graphql_b2b_bundle_token_refresh_and_gdpr() -> None:
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    rt_engine = create_async_engine(_runtime_url(), echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    channel_account_id = uuid4()
    warehouse_id = uuid4()
    shop_domain = f"qz-led-{tenant_id.hex[:6]}.myshopify.com"
    client_secret = "shpss_test_secret_123"
    token_refreshes: list[dict] = []
    graphql_queries: list[str] = []

    def _mock_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/oauth/access_token":
            token_refreshes.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"access_token": "shpat_refreshed_24h_token", "expires_in": 86399},
            )
        if request.url.path.endswith("/graphql.json"):
            assert request.headers.get("X-Shopify-Access-Token") == "shpat_refreshed_24h_token"
            payload = json.loads(request.content.decode("utf-8"))
            query_text = payload["query"]
            graphql_queries.append(query_text)
            if "fulfillmentCreate" in query_text:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "fulfillmentCreate": {
                                "fulfillment": {"id": "gid://shopify/Fulfillment/777", "status": "SUCCESS"},
                                "userErrors": [],
                            }
                        },
                        "extensions": {
                            "cost": {"throttleStatus": {"maximumAvailable": 2000, "currentlyAvailable": 1950, "restoreRate": 100}}
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "orders": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "gid://shopify/Order/55001",
                                    "name": "#1001-B2B",
                                    "createdAt": "2026-11-19T01:00:00Z",
                                    "updatedAt": "2026-11-19T01:10:00Z",
                                    "displayFinancialStatus": "PAID",
                                    "displayFulfillmentStatus": "UNFULFILLED",
                                    "currencyCode": "USD",
                                    "poNumber": "PO-IBERIA-2026-88",
                                    "currentSubtotalPriceSet": {"shopMoney": {"amount": "150.00", "currencyCode": "USD"}},
                                    "totalShippingPriceSet": {"shopMoney": {"amount": "20.00", "currencyCode": "USD"}},
                                    "totalTaxSet": {"shopMoney": {"amount": "0.00", "currencyCode": "USD"}},
                                    "currentTotalPriceSet": {"shopMoney": {"amount": "170.00", "currencyCode": "USD"}},
                                    "customer": {
                                        "id": "gid://shopify/Customer/8801",
                                        "email": "b2b.buyer@nordiclights.se",
                                        "phone": "+46701234567",
                                        "firstName": "Sven",
                                        "lastName": "Lindqvist",
                                    },
                                    "shippingAddress": {"countryCodeV2": "SE"},
                                    "purchasingEntity": {
                                        "__typename": "PurchasingCompany",
                                        "company": {
                                            "id": "gid://shopify/Company/901",
                                            "name": "Nordic Architectural Lighting AB",
                                            "externalId": "NAL-SE",
                                        },
                                        "location": {
                                            "id": "gid://shopify/CompanyLocation/902",
                                            "name": "Stockholm HQ",
                                            "externalId": "STO-01",
                                            "shippingAddress": {"countryCodeV2": "SE"},
                                        },
                                    },
                                    "lineItems": {
                                        "nodes": [
                                            {
                                                "sku": "SHOP-LED-5M-2PACK",
                                                "quantity": 3,
                                                "originalTotalSet": {"shopMoney": {"amount": "150.00", "currencyCode": "USD"}},
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    },
                    "extensions": {
                        "cost": {"throttleStatus": {"maximumAvailable": 20000, "currentlyAvailable": 19950, "restoreRate": 1000}}
                    },
                },
            )
        return httpx.Response(404)

    class _RecordingGdprPort:
        def __init__(self) -> None:
            self.calls: list[tuple[str, UUID, dict]] = []

        async def handle_customer_data_request(self, session: AsyncSession, tenant_id: UUID, payload: dict) -> None:
            self.calls.append(("customers/data_request", tenant_id, payload))

        async def handle_customer_redact(self, session: AsyncSession, tenant_id: UUID, payload: dict) -> None:
            self.calls.append(("customers/redact", tenant_id, payload))
            cust_email = (payload.get("customer") or {}).get("email")
            if cust_email:
                await session.execute(
                    text(
                        """
                        UPDATE customers
                        SET display_name = 'REDACTED', primary_email = NULL, primary_phone_e164 = NULL
                        WHERE tenant_id = :tid AND primary_email = :email
                        """
                    ),
                    {"tid": tenant_id, "email": cust_email.lower()},
                )

        async def handle_shop_redact(self, session: AsyncSession, tenant_id: UUID, payload: dict) -> None:
            self.calls.append(("shop/redact", tenant_id, payload))

    try:
        crypto = EnvelopeCryptoService(get_settings())
        async with mig_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tenant_id, "name": "QZ Shopify Tenant", "slug": f"shp-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO channel_accounts (
                        id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile
                    ) VALUES (
                        :id, :tid, 'shopify', :ext, 'QZ Shopify Plus', 'active', '{"warehouse_id": ""}'::jsonb
                    )
                    """
                ),
                {"id": channel_account_id, "tid": tenant_id, "ext": shop_domain},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO channel_routing_directory (channel_type, external_account_id, tenant_id, channel_account_id)
                    VALUES ('shopify', :ext, :tid, :caid)
                    """
                ),
                {"ext": shop_domain, "tid": tenant_id, "caid": channel_account_id},
            )

        async with tenant_session(rt_factory, tenant_id) as session:
            await crypto.provision_tenant_dek(session, tenant_id)
            vault = CredentialVaultHelper(crypto)
            # Store expired 24h client_credentials token to verify auto-refresh
            await vault.save_secret_json(
                session,
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                credential_type="shopify_custom_app",
                payload={
                    "shop_domain": shop_domain,
                    "auth_mode": "client_credentials",
                    "client_id": "cli_test_123",
                    "client_secret": client_secret,
                    "access_token": "shpat_expired_token",
                },
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            )

            catalog_svc = CatalogService()
            prod = await catalog_svc.create_product(
                session, tenant_id, actor_id="seed", payload=ProductCreate(spu_code="SPU-SHP-5M", category="led")
            )
            variant = await catalog_svc.create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=prod.id,
                    sku="LED-STRIP-5M-IP65",
                    cost_price_base=Decimal("30.0000"),
                    retail_price_usd=Decimal("25.00"),
                    weight_grams=300,
                ),
            )
            await catalog_svc.create_sku_mapping(
                session,
                tenant_id,
                actor_id="seed",
                payload=SkuMappingCreate(
                    variant_id=variant.id,
                    channel_account_id=channel_account_id,
                    external_product_id="gid://shopify/Product/1",
                    external_sku="SHOP-LED-5M-2PACK",
                    bundle_multiplier=2,
                ),
            )
            await session.execute(
                text(
                    """
                    INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty)
                    VALUES (:tid, :wid, :vid, 20, 0)
                    """
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_transport)) as http_client:
            async with tenant_session(rt_factory, tenant_id) as session:
                adapter = await ShopifyGraphqlAdapter.from_channel_account(
                    session, tenant_id, channel_account_id, crypto=crypto, http_client=http_client
                )
                orders, _cursor = await adapter.fetch_orders_updated_since(datetime.now(timezone.utc) - timedelta(hours=1))
                assert len(token_refreshes) == 1
                assert token_refreshes[0]["grant_type"] == "client_credentials"
                assert len(orders) == 1
                assert orders[0].order_mode == OrderMode.B2B_WHOLESALE
                assert orders[0].b2b_company is not None
                assert orders[0].b2b_company.legal_name == "Nordic Architectural Lighting AB"

                sync_svc = EcommerceOrderSyncService()
                order_id, created = await sync_svc.ingest_normalized_order(
                    session,
                    tenant_id=tenant_id,
                    channel_account_id=channel_account_id,
                    default_warehouse_id=warehouse_id,
                    order_dto=orders[0],
                )
                assert created is True

                # 3 units of 2-pack MUST reserve 3 * 2 = 6 units of LED-STRIP-5M-IP65
                bal_row = (
                    await session.execute(
                        text(
                            "SELECT reserved_qty FROM inventory_balances WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid"
                        ),
                        {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
                    )
                ).one()
                assert bal_row.reserved_qty == 6

                ord_row = (
                    await session.execute(
                        text("SELECT order_mode, payment_status, channel_metadata FROM orders WHERE id = :oid"),
                        {"oid": order_id},
                    )
                ).one()
                assert ord_row.order_mode == OrderMode.B2B_WHOLESALE.value
                assert ord_row.payment_status == PaymentStatus.PAID.value
                assert ord_row.channel_metadata["po_number"] == "PO-IBERIA-2026-88"

                ful = await adapter.create_fulfillment(
                    external_order_id="gid://shopify/Order/55001",
                    fulfillment_order_id="gid://shopify/FulfillmentOrder/77",
                    tracking_number="DHL-SE-9988",
                    carrier_company="DHL",
                )
                assert ful["fulfillment"]["status"] == "SUCCESS"
                assert any("fulfillmentCreate" in q for q in graphql_queries)

        # Verify HMAC and mandatory GDPR customers/redact webhook calling GDPR port
        redact_body = json.dumps(
            {
                "shop_id": 12345,
                "shop_domain": shop_domain,
                "customer": {"id": 8801, "email": "b2b.buyer@nordiclights.se"},
                "orders_to_redact": [55001],
            }
        ).encode("utf-8")
        sig = base64.b64encode(hmac.new(client_secret.encode("utf-8"), redact_body, hashlib.sha256).digest()).decode(
            "utf-8"
        )
        assert verify_shopify_webhook_hmac(redact_body, sig, client_secret) is True

        gdpr_port = _RecordingGdprPort()
        await process_shopify_webhook_event(
            rt_factory,
            topic="customers/redact",
            shop_domain=shop_domain,
            payload=json.loads(redact_body.decode("utf-8")),
            gdpr_port=gdpr_port,
        )
        assert len(gdpr_port.calls) == 1
        assert gdpr_port.calls[0][0] == "customers/redact"

        async with tenant_session(rt_factory, tenant_id) as session:
            cust = (
                await session.execute(
                    text("SELECT display_name, primary_email FROM customers WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
            ).one()
            assert cust.display_name == "REDACTED"
            assert cust.primary_email is None
    finally:
        async with mig_engine.begin() as conn:
            await conn.execute(text("DELETE FROM order_profit_snapshots WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM receivables_payables WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM inventory_ledger WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM inventory_balances WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM order_items WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM orders WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM platform_sku_mappings WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM product_variants WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM products WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM customer_identities WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM b2b_companies WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM channel_credentials WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM channel_routing_directory WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM channel_accounts WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM tenant_keys WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_shopify_connector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.channels.ecommerce'`

- [ ] **Step 3: Write `channels/ecommerce/__init__.py`, `base.py`, `shopify.py`, `shopify_router.py`, and wire `api_main.py` & `worker_main.py`**

Create `apps/backend/src/erp_core/channels/ecommerce/__init__.py`:

```python
"""P0 e-commerce platform connectors (Shopify GraphQL, WooCommerce, SHOPLINE, Shoplazza, TikTok Shop)."""
```

Create `apps/backend/src/erp_core/channels/ecommerce/base.py`:

```python
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.service import CatalogService
from erp_core.finance.payment_service import PaymentService
from erp_core.finance.profit_service import OrderProfitService
from erp_core.identity.service import IdentityResolverService
from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import OrderCreateInternal, OrderItemInternal
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event


class NormalizedLineItem(BaseModel):
    external_sku: str
    title: str = ""
    quantity: int = Field(gt=0)
    line_total: Decimal


class NormalizedCustomer(BaseModel):
    email: str | None = None
    phone_e164: str | None = None
    external_customer_id: str | None = None
    external_identity_type: IdentityType = IdentityType.EMAIL
    display_name: str = "Unknown Buyer"
    country_code: str | None = None


class NormalizedB2BCompany(BaseModel):
    legal_name: str
    country_code: str = "US"
    external_company_id: str | None = None
    location_id: str | None = None


class NormalizedOrder(BaseModel):
    external_order_id: str
    external_order_number: str | None = None
    order_mode: OrderMode = OrderMode.B2C_RETAIL
    currency: str = "USD"
    fx_rate_to_base: Decimal = Decimal("7.200000")
    subtotal_amount: Decimal
    shipping_charged: Decimal = Decimal("0.00")
    discount_amount: Decimal = Decimal("0.00")
    tax_collected: Decimal = Decimal("0.00")
    total_amount: Decimal
    platform_commission_base: Decimal = Decimal("0.00")
    fulfillment_status: FulfillmentStatus = FulfillmentStatus.CONFIRMED_RESERVED
    payment_status: PaymentStatus = PaymentStatus.PAID
    customer: NormalizedCustomer
    b2b_company: NormalizedB2BCompany | None = None
    line_items: list[NormalizedLineItem] = Field(min_length=1)
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime


class NormalizedProduct(BaseModel):
    external_product_id: str
    title: str
    variants: list[dict[str, Any]] = Field(default_factory=list)


class CommercePlatformAdapter(ABC):
    """Unified interface for all P0-P2 e-commerce platform connectors."""

    @abstractmethod
    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]: ...

    @abstractmethod
    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]: ...

    @abstractmethod
    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool: ...


class CredentialVaultHelper:
    """Envelope-encrypted JSON credential persistence bound to AAD(tenant:account:type)."""

    def __init__(self, crypto: EnvelopeCryptoService | None = None) -> None:
        self.crypto = crypto or EnvelopeCryptoService(get_settings())

    async def save_secret_json(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        credential_type: str,
        payload: dict[str, Any],
        expires_at: datetime | None = None,
    ) -> None:
        plaintext = json.dumps(payload, separators=(",", ":"))
        ciphertext, nonce, auth_tag, key_version = await self.crypto.encrypt_secret(
            session, tenant_id, channel_account_id, credential_type, plaintext
        )
        await session.execute(
            text(
                """
                INSERT INTO channel_credentials (
                    id, tenant_id, channel_account_id, credential_type,
                    ciphertext, nonce, auth_tag, key_version, expires_at, updated_at
                ) VALUES (
                    :id, :tid, :caid, :ctype, :ct, :nonce, :tag, :kver, :exp, now()
                )
                ON CONFLICT (tenant_id, channel_account_id, credential_type)
                DO UPDATE SET
                    ciphertext = EXCLUDED.ciphertext,
                    nonce = EXCLUDED.nonce,
                    auth_tag = EXCLUDED.auth_tag,
                    key_version = EXCLUDED.key_version,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = now()
                """
            ),
            {
                "id": uuid4(),
                "tid": tenant_id,
                "caid": channel_account_id,
                "ctype": credential_type,
                "ct": ciphertext,
                "nonce": nonce,
                "tag": auth_tag,
                "kver": key_version,
                "exp": expires_at,
            },
        )

    async def load_secret_json(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        credential_type: str,
    ) -> tuple[dict[str, Any], datetime | None]:
        row = (
            await session.execute(
                text(
                    """
                    SELECT ciphertext, nonce, auth_tag, key_version, expires_at
                    FROM channel_credentials
                    WHERE tenant_id = :tid AND channel_account_id = :caid AND credential_type = :ctype
                    """
                ),
                {"tid": tenant_id, "caid": channel_account_id, "ctype": credential_type},
            )
        ).one_or_none()
        if row is None:
            raise AppError(
                slug="credential-not-found",
                status_code=404,
                code="ERR_CREDENTIAL_NOT_FOUND",
                title="Credential Not Found",
                detail=f"Missing '{credential_type}' credential for channel account '{channel_account_id}'.",
            )
        plaintext = await self.crypto.decrypt_secret(
            session,
            tenant_id,
            channel_account_id,
            credential_type,
            bytes(row.ciphertext),
            bytes(row.nonce),
            bytes(row.auth_tag),
            int(row.key_version),
        )
        return json.loads(plaintext), row.expires_at


class EcommerceOrderSyncService:
    """Idempotent ingestion of normalized e-commerce orders into ERP core."""

    def __init__(
        self,
        identity_service: IdentityResolverService | None = None,
        catalog_service: CatalogService | None = None,
        order_service: OrderService | None = None,
        payment_service: PaymentService | None = None,
        profit_service: OrderProfitService | None = None,
    ) -> None:
        self.identity = identity_service or IdentityResolverService()
        self.catalog = catalog_service or CatalogService()
        self.orders = order_service or OrderService(catalog_service=self.catalog)
        self.payments = payment_service or PaymentService(order_service=self.orders)
        self.profits = profit_service or OrderProfitService()

    async def ingest_normalized_order(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        default_warehouse_id: UUID,
        order_dto: NormalizedOrder,
        actor_id: str = "ecommerce_sync",
    ) -> tuple[UUID, bool]:
        existing = await self.orders.find_by_external_order(
            session, tenant_id, channel_account_id, order_dto.external_order_id
        )
        if existing is not None:
            await self.orders.update_external_order_status(
                session,
                tenant_id=tenant_id,
                order_id=existing.id,
                fulfillment_status=order_dto.fulfillment_status,
                payment_status=order_dto.payment_status,
                channel_metadata_patch=order_dto.channel_metadata,
            )
            await self.profits.recalculate_order_profit(
                session,
                tenant_id=tenant_id,
                order_id=existing.id,
                platform_commission_base=order_dto.platform_commission_base,
            )
            return existing.id, False

        # Resolve identity deterministically
        if order_dto.customer.email:
            id_type = IdentityType.EMAIL
            id_val = order_dto.customer.email
        elif order_dto.customer.phone_e164:
            id_type = IdentityType.PHONE_E164
            id_val = order_dto.customer.phone_e164
        elif order_dto.customer.external_customer_id:
            id_type = order_dto.customer.external_identity_type
            id_val = order_dto.customer.external_customer_id
        else:
            id_type = IdentityType.WEB_VISITOR_ID
            id_val = f"ext-order:{order_dto.external_order_id}"

        customer_id, _ = await self.identity.resolve_or_create_customer(
            session,
            tenant_id=tenant_id,
            identity_type=id_type,
            raw_value=id_val,
            display_name=order_dto.customer.display_name,
            country_hint=order_dto.customer.country_code,
        )

        # Link B2B company if present (e.g. Shopify Plus PurchasingCompany or SHOPLINE B2B)
        if order_dto.b2b_company is not None:
            comp_row = (
                await session.execute(
                    text(
                        """
                        SELECT id FROM b2b_companies
                        WHERE tenant_id = :tid AND legal_name = :lname
                        LIMIT 1
                        """
                    ),
                    {"tid": tenant_id, "lname": order_dto.b2b_company.legal_name},
                )
            ).one_or_none()
            if comp_row is None:
                company_id = uuid4()
                await session.execute(
                    text(
                        """
                        INSERT INTO b2b_companies (id, tenant_id, legal_name, country_code, default_incoterm)
                        VALUES (:id, :tid, :lname, :cc, 'FOB')
                        """
                    ),
                    {
                        "id": company_id,
                        "tid": tenant_id,
                        "lname": order_dto.b2b_company.legal_name,
                        "cc": order_dto.b2b_company.country_code[:2].upper(),
                    },
                )
            else:
                company_id = comp_row.id

            await session.execute(
                text(
                    """
                    UPDATE customers
                    SET company_id = COALESCE(company_id, :cid),
                        customer_type = 'b2b_company_rep'
                    WHERE tenant_id = :tid AND id = :cust_id
                    """
                ),
                {"cid": company_id, "tid": tenant_id, "cust_id": customer_id},
            )

        # Resolve SKUs and apply bundle_multiplier
        order_items: list[OrderItemInternal] = []
        for line in order_dto.line_items:
            resolved = await self.catalog.resolve_sku_for_channel(
                session,
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                external_sku=line.external_sku,
                external_qty=line.quantity,
            )
            if resolved is None:
                raise AppError(
                    slug="unmapped-channel-sku",
                    status_code=422,
                    code="ERR_UNMAPPED_CHANNEL_SKU",
                    title="Unmapped Channel SKU",
                    detail=f"External SKU '{line.external_sku}' is not mapped for channel account '{channel_account_id}'.",
                )
            unit_price_base_unit = (line.line_total / Decimal(resolved.deduct_qty)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            order_items.append(
                OrderItemInternal(
                    variant_id=resolved.variant_id,
                    sku_snapshot=resolved.internal_sku,
                    quantity=resolved.deduct_qty,
                    unit_price=unit_price_base_unit,
                    unit_cost_base_snapshot=resolved.cost_price_base,
                    line_total=line.line_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                )
            )

        created_order = await self.orders.create_order_atomic(
            session,
            tenant_id=tenant_id,
            actor_type="webhook",
            actor_id=actor_id,
            payload=OrderCreateInternal(
                order_mode=order_dto.order_mode,
                channel_account_id=channel_account_id,
                external_order_id=order_dto.external_order_id,
                customer_id=customer_id,
                warehouse_id=default_warehouse_id,
                currency=order_dto.currency.upper(),
                fx_rate_to_base=order_dto.fx_rate_to_base,
                subtotal_amount=order_dto.subtotal_amount,
                shipping_charged=order_dto.shipping_charged,
                discount_amount=order_dto.discount_amount,
                tax_collected=order_dto.tax_collected,
                total_amount=order_dto.total_amount,
                initial_fulfillment_status=order_dto.fulfillment_status,
                initial_payment_status=order_dto.payment_status,
                channel_metadata=order_dto.channel_metadata,
                items=order_items,
            ),
        )

        is_paid = order_dto.payment_status == PaymentStatus.PAID
        await self.payments.create_single_receivable(
            session,
            tenant_id=tenant_id,
            order_id=created_order.id,
            milestone_label="full_prepaid",
            due_amount=created_order.total_amount,
            currency=created_order.currency,
            status="settled" if is_paid else "open",
            paid_amount=created_order.total_amount if is_paid else Decimal("0.00"),
        )
        await self.profits.recalculate_order_profit(
            session,
            tenant_id=tenant_id,
            order_id=created_order.id,
            platform_commission_base=order_dto.platform_commission_base,
        )
        await record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_type="webhook",
            actor_id=actor_id,
            action="ecommerce.order_imported",
            resource_type="order",
            resource_id=created_order.id,
            after_state={"external_order_id": order_dto.external_order_id, "order_no": created_order.order_no},
        )
        return created_order.id, True
```

Create `apps/backend/src/erp_core/channels/ecommerce/shopify.py`:

```python
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    CredentialVaultHelper,
    EcommerceOrderSyncService,
    NormalizedB2BCompany,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
)
from erp_core.shared.config import get_settings
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.gdpr_service import GdprService

SHOPIFY_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")
SHOPIFY_API_VERSION = "2026-01"

ORDERS_GRAPHQL_QUERY = """
query SyncOrders($first: Int!, $query: String, $after: String) {
  orders(first: $first, query: $query, after: $after, sortKey: UPDATED_AT) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      name
      createdAt
      updatedAt
      displayFinancialStatus
      displayFulfillmentStatus
      currencyCode
      poNumber
      currentSubtotalPriceSet { shopMoney { amount currencyCode } }
      totalShippingPriceSet { shopMoney { amount currencyCode } }
      totalTaxSet { shopMoney { amount currencyCode } }
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      customer {
        id
        email
        phone
        firstName
        lastName
      }
      shippingAddress { countryCodeV2 }
      purchasingEntity {
        __typename
        ... on PurchasingCompany {
          company { id name externalId }
          location {
            id
            name
            externalId
            shippingAddress { countryCodeV2 }
          }
        }
      }
      lineItems(first: 100) {
        nodes {
          sku
          title
          quantity
          originalTotalSet { shopMoney { amount currencyCode } }
        }
      }
    }
  }
}
"""

PRODUCTS_GRAPHQL_QUERY = """
query SyncProducts($first: Int!, $query: String, $after: String) {
  products(first: $first, query: $query, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      title
      variants(first: 100) {
        nodes {
          id
          sku
          price
          inventoryItem { id }
        }
      }
    }
  }
}
"""

FULFILLMENT_CREATE_MUTATION = """
mutation CreateFulfillment($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment {
      id
      status
    }
    userErrors {
      field
      message
    }
  }
}
"""


def validate_shop_domain(shop: str) -> str:
    cleaned = shop.strip().lower()
    if not SHOPIFY_DOMAIN_RE.match(cleaned):
        raise AppError(
            slug="invalid-shopify-domain",
            status_code=422,
            code="ERR_INVALID_SHOPIFY_DOMAIN",
            title="Invalid Shopify Domain",
            detail="Shop domain must match '<store>.myshopify.com'.",
        )
    return cleaned


def build_shopify_oauth_install_url(
    shop: str,
    redirect_uri: str,
    state: str,
    scopes: str = "read_orders,write_orders,read_products,read_inventory,write_fulfillments,read_customers,read_companies",
) -> str:
    domain = validate_shop_domain(shop)
    client_id = os.getenv("SHOPIFY_CLIENT_ID", "")
    qs = urlencode(
        {
            "client_id": client_id,
            "scope": scopes,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"https://{domain}/admin/oauth/authorize?{qs}"


def verify_shopify_oauth_hmac(query_params: Mapping[str, str], client_secret: str) -> bool:
    received_hmac = query_params.get("hmac", "")
    if not received_hmac or not client_secret:
        return False
    pairs = sorted((k, v) for k, v in query_params.items() if k not in {"hmac", "signature"})
    message = "&".join(f"{k}={v}" for k, v in pairs).encode("utf-8")
    expected = hmac.new(client_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received_hmac.lower())


def verify_shopify_webhook_hmac(raw_body: bytes, hmac_header: str, secret: str) -> bool:
    if not hmac_header or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, hmac_header.strip())


class ShopifyGdprPort(Protocol):
    async def handle_customer_data_request(self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]) -> None: ...
    async def handle_customer_redact(self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]) -> None: ...
    async def handle_shop_redact(self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]) -> None: ...


class TenancyGdprBridge:
    """Delegates mandatory Shopify GDPR webhooks to Plan 01's tenancy GdprService."""

    def __init__(
        self,
        crypto: EnvelopeCryptoService | None = None,
        storage: ObjectStorageService | None = None,
        gdpr_service: GdprService | None = None,
    ) -> None:
        settings = get_settings()
        self.crypto = crypto or EnvelopeCryptoService(settings)
        self.storage = storage or ObjectStorageService(settings)
        self.gdpr = gdpr_service or GdprService()

    @staticmethod
    async def _resolve_customer_id(session: AsyncSession, tenant_id: UUID, customer_payload: dict[str, Any]) -> UUID | None:
        ext_id = str(customer_payload.get("id") or "").strip()
        email = (customer_payload.get("email") or "").strip().lower()
        if ext_id:
            cid = (
                await session.execute(
                    text(
                        """
                        SELECT customer_id FROM customer_identities
                        WHERE tenant_id = :tid AND identity_type = 'shopify_customer_id' AND identity_value = :val
                        LIMIT 1
                        """
                    ),
                    {"tid": tenant_id, "val": ext_id},
                )
            ).scalar_one_or_none()
            if cid is not None:
                return cid
        if email:
            return (
                await session.execute(
                    text(
                        """
                        SELECT id FROM customers
                        WHERE tenant_id = :tid AND lower(primary_email) = :email
                        LIMIT 1
                        """
                    ),
                    {"tid": tenant_id, "email": email},
                )
            ).scalar_one_or_none()
        return None

    async def handle_customer_data_request(
        self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
    ) -> None:
        customer_id = await self._resolve_customer_id(session, tenant_id, payload.get("customer") or {})
        if customer_id is None:
            return
        await self.gdpr.export_customer_data(
            session,
            self.storage,
            tenant_id=tenant_id,
            customer_id=customer_id,
            actor_id="shopify_gdpr_webhook",
        )

    async def handle_customer_redact(
        self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]
    ) -> None:
        customer_id = await self._resolve_customer_id(session, tenant_id, payload.get("customer") or {})
        if customer_id is None:
            return
        await self.gdpr.anonymize_customer_pii(
            session,
            tenant_id=tenant_id,
            customer_id=customer_id,
            actor_id="shopify_gdpr_webhook",
        )

    async def handle_shop_redact(self, session: AsyncSession, tenant_id: UUID, payload: dict[str, Any]) -> None:
        await self.gdpr.redact_tenant_crypto_shred(
            session,
            self.storage,
            self.crypto,
            tenant_id=tenant_id,
            actor_id="shopify_gdpr_webhook",
        )


class ShopifyGraphqlAdapter(CommercePlatformAdapter):
    """Shopify 2026-01 GraphQL-Only connector with 24h client_credentials auto-refresh & cost guard."""

    def __init__(
        self,
        shop_domain: str,
        access_token: str,
        webhook_secret: str = "",
        auth_mode: str = "oauth",
        client_id: str | None = None,
        client_secret: str | None = None,
        expires_at: datetime | None = None,
        http_client: httpx.AsyncClient | None = None,
        tenant_id: UUID | None = None,
        channel_account_id: UUID | None = None,
        vault: CredentialVaultHelper | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self.shop_domain = validate_shop_domain(shop_domain)
        self.access_token = access_token
        self.webhook_secret = webhook_secret or os.getenv("SHOPIFY_CLIENT_SECRET", "")
        self.auth_mode = auth_mode
        self.client_id = client_id
        self.client_secret = client_secret
        self.expires_at = expires_at
        self._http_client = http_client
        self._tenant_id = tenant_id
        self._channel_account_id = channel_account_id
        self._vault = vault
        self._session = session

    @classmethod
    async def from_channel_account(
        cls,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        crypto: EnvelopeCryptoService | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> ShopifyGraphqlAdapter:
        vault = CredentialVaultHelper(crypto)
        try:
            data, expires_at = await vault.load_secret_json(session, tenant_id, channel_account_id, "shopify_custom_app")
        except AppError:
            data, expires_at = await vault.load_secret_json(session, tenant_id, channel_account_id, "oauth_token")
        return cls(
            shop_domain=data["shop_domain"],
            access_token=data.get("access_token", ""),
            webhook_secret=data.get("client_secret") or os.getenv("SHOPIFY_CLIENT_SECRET", ""),
            auth_mode=data.get("auth_mode", "oauth"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            expires_at=expires_at,
            http_client=http_client,
            tenant_id=tenant_id,
            channel_account_id=channel_account_id,
            vault=vault,
            session=session,
        )

    async def _ensure_valid_access_token(self) -> None:
        if self.auth_mode != "client_credentials":
            return
        now = datetime.now(timezone.utc)
        if self.access_token and self.expires_at and self.expires_at > now + timedelta(minutes=5):
            return
        if not self.client_id or not self.client_secret:
            raise AppError(
                slug="shopify-credentials-missing",
                status_code=401,
                code="ERR_SHOPIFY_CREDENTIALS_MISSING",
                title="Shopify Credentials Missing",
                detail="client_id and client_secret are required for 2026-01 Custom App token refresh.",
            )
        token_url = f"https://{self.shop_domain}/admin/oauth/access_token"
        body = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        if self._http_client is not None:
            resp = await self._http_client.post(token_url, json=body)
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(token_url, json=body)
        resp.raise_for_status()
        tok = resp.json()
        self.access_token = tok["access_token"]
        expires_in = int(tok.get("expires_in", 86399))
        self.expires_at = now + timedelta(seconds=expires_in)

        if self._vault and self._session and self._tenant_id and self._channel_account_id:
            await self._vault.save_secret_json(
                self._session,
                tenant_id=self._tenant_id,
                channel_account_id=self._channel_account_id,
                credential_type="shopify_custom_app",
                payload={
                    "shop_domain": self.shop_domain,
                    "auth_mode": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "access_token": self.access_token,
                },
                expires_at=self.expires_at,
            )

    async def _graphql(self, query: str, variables: dict[str, Any], min_cost_reserve: int = 80) -> dict[str, Any]:
        await self._ensure_valid_access_token()
        url = f"https://{self.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

        for attempt in range(4):
            headers = {
                "X-Shopify-Access-Token": self.access_token,
                "Content-Type": "application/json",
            }
            if self._http_client is not None:
                resp = await self._http_client.post(url, json={"query": query, "variables": variables}, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(url, json={"query": query, "variables": variables}, headers=headers)

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", str(2**attempt)))
                await asyncio.sleep(min(retry_after, 10.0))
                continue

            resp.raise_for_status()
            body = resp.json()
            errors = body.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                throttle = ((body.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
                avail = float(throttle.get("currentlyAvailable", 0))
                rate = max(float(throttle.get("restoreRate", 100)), 1.0)
                wait_s = max((min_cost_reserve - avail) / rate, 0.5)
                await asyncio.sleep(min(wait_s, 10.0))
                continue
            if errors:
                raise AppError(
                    slug="shopify-graphql-error",
                    status_code=502,
                    code="ERR_SHOPIFY_GRAPHQL",
                    title="Shopify GraphQL Error",
                    detail=json.dumps(errors),
                )

            throttle = ((body.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
            avail = float(throttle.get("currentlyAvailable", 1000))
            rate = max(float(throttle.get("restoreRate", 100)), 1.0)
            if avail < min_cost_reserve:
                await asyncio.sleep(min((min_cost_reserve - avail) / rate, 5.0))
            return body["data"]

        raise AppError(
            slug="shopify-rate-limit-exhausted",
            status_code=429,
            code="ERR_SHOPIFY_THROTTLED",
            title="Shopify Rate Limit Exhausted",
            detail="Shopify GraphQL cost bucket remained exhausted after retries.",
        )

    @staticmethod
    def _normalize_order_node(node: dict[str, Any]) -> NormalizedOrder:
        cust_node = node.get("customer") or {}
        ship_addr = node.get("shippingAddress") or {}
        purchasing = node.get("purchasingEntity") or {}
        b2b_company: NormalizedB2BCompany | None = None
        order_mode = OrderMode.B2C_RETAIL
        channel_meta: dict[str, Any] = {
            "shopify_order_name": node.get("name"),
            "financial_status": node.get("displayFinancialStatus"),
            "fulfillment_status": node.get("displayFulfillmentStatus"),
        }
        if node.get("poNumber"):
            channel_meta["po_number"] = node["poNumber"]

        if purchasing.get("__typename") == "PurchasingCompany" and purchasing.get("company"):
            order_mode = OrderMode.B2B_WHOLESALE
            comp = purchasing["company"]
            loc = purchasing.get("location") or {}
            loc_cc = (loc.get("shippingAddress") or {}).get("countryCodeV2") or ship_addr.get("countryCodeV2") or "US"
            b2b_company = NormalizedB2BCompany(
                legal_name=comp["name"],
                country_code=loc_cc,
                external_company_id=comp.get("id"),
                location_id=loc.get("id"),
            )
            channel_meta["shopify_company_id"] = comp.get("id")
            channel_meta["shopify_company_location_id"] = loc.get("id")

        fin_status = str(node.get("displayFinancialStatus") or "").upper()
        if fin_status == "PAID":
            pay_status = PaymentStatus.PAID
        elif fin_status == "PARTIALLY_PAID":
            pay_status = PaymentStatus.PARTIALLY_PAID
        elif fin_status == "REFUNDED":
            pay_status = PaymentStatus.REFUNDED
        else:
            pay_status = PaymentStatus.UNPAID

        ful_str = str(node.get("displayFulfillmentStatus") or "").upper()
        if ful_str == "FULFILLED":
            ful_status = FulfillmentStatus.SHIPPED
        elif ful_str == "PARTIALLY_FULFILLED":
            ful_status = FulfillmentStatus.PARTIALLY_SHIPPED
        else:
            ful_status = FulfillmentStatus.CONFIRMED_RESERVED

        subtotal = Decimal(str(((node.get("currentSubtotalPriceSet") or {}).get("shopMoney") or {}).get("amount", "0.00")))
        shipping = Decimal(str(((node.get("totalShippingPriceSet") or {}).get("shopMoney") or {}).get("amount", "0.00")))
        tax = Decimal(str(((node.get("totalTaxSet") or {}).get("shopMoney") or {}).get("amount", "0.00")))
        total = Decimal(str(((node.get("currentTotalPriceSet") or {}).get("shopMoney") or {}).get("amount", "0.00")))
        ccy = node.get("currencyCode") or "USD"

        first = cust_node.get("firstName") or ""
        last = cust_node.get("lastName") or ""
        display_name = f"{first} {last}".strip() or cust_node.get("email") or "Shopify Buyer"

        items: list[NormalizedLineItem] = []
        for li in (node.get("lineItems") or {}).get("nodes") or []:
            sku = (li.get("sku") or "").strip()
            if not sku:
                continue
            line_amt = Decimal(str(((li.get("originalTotalSet") or {}).get("shopMoney") or {}).get("amount", "0.00")))
            items.append(
                NormalizedLineItem(
                    external_sku=sku,
                    title=li.get("title") or sku,
                    quantity=int(li["quantity"]),
                    line_total=line_amt,
                )
            )

        updated_dt = datetime.fromisoformat(str(node["updatedAt"]).replace("Z", "+00:00"))
        return NormalizedOrder(
            external_order_id=node["id"],
            external_order_number=node.get("name"),
            order_mode=order_mode,
            currency=ccy,
            subtotal_amount=subtotal,
            shipping_charged=shipping,
            tax_collected=tax,
            total_amount=total,
            fulfillment_status=ful_status,
            payment_status=pay_status,
            customer=NormalizedCustomer(
                email=cust_node.get("email"),
                phone_e164=cust_node.get("phone"),
                external_customer_id=cust_node.get("id"),
                external_identity_type=IdentityType.SHOPIFY_CUSTOMER_ID,
                display_name=display_name,
                country_code=ship_addr.get("countryCodeV2"),
            ),
            b2b_company=b2b_company,
            line_items=items,
            channel_metadata=channel_meta,
            updated_at=updated_dt,
        )

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = await self._graphql(
            ORDERS_GRAPHQL_QUERY,
            {"first": 50, "query": f"updated_at:>={iso}", "after": cursor},
        )
        orders_conn = data["orders"]
        normalized = [self._normalize_order_node(n) for n in orders_conn["nodes"] if (n.get("lineItems") or {}).get("nodes")]
        page_info = orders_conn["pageInfo"]
        next_cursor = page_info["endCursor"] if page_info.get("hasNextPage") else None
        return normalized, next_cursor

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = await self._graphql(
            PRODUCTS_GRAPHQL_QUERY,
            {"first": 50, "query": f"updated_at:>={iso}", "after": cursor},
        )
        prods_conn = data["products"]
        items = [
            NormalizedProduct(
                external_product_id=p["id"],
                title=p["title"],
                variants=(p.get("variants") or {}).get("nodes") or [],
            )
            for p in prods_conn["nodes"]
        ]
        page_info = prods_conn["pageInfo"]
        next_cursor = page_info["endCursor"] if page_info.get("hasNextPage") else None
        return items, next_cursor

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        fo_id = fulfillment_order_id or external_order_id
        tracking_info: dict[str, Any] = {"number": tracking_number, "company": carrier_company}
        if tracking_url:
            tracking_info["url"] = tracking_url
        data = await self._graphql(
            FULFILLMENT_CREATE_MUTATION,
            {
                "fulfillment": {
                    "notifyCustomer": notify_customer,
                    "trackingInfo": tracking_info,
                    "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fo_id}],
                }
            },
        )
        result = data["fulfillmentCreate"]
        if result.get("userErrors"):
            raise AppError(
                slug="shopify-fulfillment-error",
                status_code=422,
                code="ERR_SHOPIFY_FULFILLMENT",
                title="Shopify Fulfillment Error",
                detail=json.dumps(result["userErrors"]),
            )
        return result

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        sig = headers.get("X-Shopify-Hmac-SHA256") or headers.get("x-shopify-hmac-sha256") or ""
        return verify_shopify_webhook_hmac(raw_body, sig, secret)


async def process_shopify_webhook_event(
    session_factory: async_sessionmaker[AsyncSession],
    topic: str,
    shop_domain: str,
    payload: dict[str, Any],
    gdpr_port: ShopifyGdprPort | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    async with system_session(session_factory) as sys_sess:
        route = (
            await sys_sess.execute(
                text(
                    """
                    SELECT tenant_id, channel_account_id
                    FROM channel_routing_directory
                    WHERE channel_type = 'shopify' AND external_account_id = :ext
                    """
                ),
                {"ext": shop_domain.strip().lower()},
            )
        ).one_or_none()
    if route is None:
        return

    gdpr = gdpr_port or TenancyGdprBridge()
    async with tenant_session(session_factory, route.tenant_id) as t_sess:
        if topic == "customers/data_request":
            await gdpr.handle_customer_data_request(t_sess, route.tenant_id, payload)
            return
        if topic == "customers/redact":
            await gdpr.handle_customer_redact(t_sess, route.tenant_id, payload)
            return
        if topic == "shop/redact":
            await gdpr.handle_shop_redact(t_sess, route.tenant_id, payload)
            return

        if topic in {"orders/create", "orders/updated"}:
            acct_row = (
                await t_sess.execute(
                    text("SELECT rate_limit_profile FROM channel_accounts WHERE tenant_id = :tid AND id = :caid"),
                    {"tid": route.tenant_id, "caid": route.channel_account_id},
                )
            ).one()
            wh_raw = (acct_row.rate_limit_profile or {}).get("warehouse_id")
            warehouse_id = UUID(wh_raw) if wh_raw else route.channel_account_id
            adapter = await ShopifyGraphqlAdapter.from_channel_account(
                t_sess, route.tenant_id, route.channel_account_id, http_client=http_client
            )
            orders, _ = await adapter.fetch_orders_updated_since(datetime.now(timezone.utc) - timedelta(hours=1))
            target_gid = payload.get("admin_graphql_api_id") or f"gid://shopify/Order/{payload.get('id')}"
            sync_svc = EcommerceOrderSyncService()
            for o in orders:
                if o.external_order_id == target_gid:
                    await sync_svc.ingest_normalized_order(
                        t_sess,
                        tenant_id=route.tenant_id,
                        channel_account_id=route.channel_account_id,
                        default_warehouse_id=warehouse_id,
                        order_dto=o,
                    )
                    break
            await record_audit_event(
                t_sess,
                tenant_id=route.tenant_id,
                actor_type="webhook",
                actor_id=f"shopify:{shop_domain}",
                action=f"shopify.webhook.{topic}",
                resource_type="channel_account",
                resource_id=route.channel_account_id,
            )
```

Create `apps/backend/src/erp_core/channels/ecommerce/shopify_router.py`:

```python
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.channels.ecommerce.shopify import (
    build_shopify_oauth_install_url,
    process_shopify_webhook_event,
    validate_shop_domain,
    verify_shopify_oauth_hmac,
    verify_shopify_webhook_hmac,
)
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles
from erp_core.worker_main import broker

router = APIRouter(tags=["shopify"])


def _get_session_factory(request: Request):
    factory = getattr(request.app.state, "session_factory", None) or getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


class ShopifyCustomAppConnectRequest(BaseModel):
    shop_domain: str
    client_id: str = Field(min_length=4)
    client_secret: str = Field(min_length=4)
    warehouse_id: UUID | None = None
    display_name: str | None = None


class ShopifyConnectResponse(BaseModel):
    channel_account_id: UUID
    shop_domain: str
    auth_mode: str


@broker.task
async def process_shopify_webhook_task(topic: str, shop_domain: str, payload: dict) -> None:
    from erp_core.shared.config import get_settings
    from erp_core.shared.db import create_runtime_engine, create_session_factory

    settings = get_settings()
    engine = create_runtime_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await process_shopify_webhook_event(session_factory, topic=topic, shop_domain=shop_domain, payload=payload)
    finally:
        await engine.dispose()


@router.get("/connectors/shopify/oauth/install")
async def shopify_oauth_install(
    shop: str = Query(...),
    redirect_uri: str = Query(...),
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN)),
) -> dict[str, str]:
    state = f"{principal.tenant_id}:{uuid4().hex}"
    return {"install_url": build_shopify_oauth_install_url(shop=shop, redirect_uri=redirect_uri, state=state), "state": state}


@router.get("/connectors/shopify/oauth/callback", response_model=ShopifyConnectResponse)
async def shopify_oauth_callback(
    request: Request,
    shop: str = Query(...),
    code: str = Query(...),
    state: str = Query(...),
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN)),
) -> ShopifyConnectResponse:
    params = dict(request.query_params)
    secret = os.getenv("SHOPIFY_CLIENT_SECRET", "")
    if not verify_shopify_oauth_hmac(params, secret):
        raise AppError(
            slug="invalid-oauth-hmac",
            status_code=401,
            code="ERR_INVALID_OAUTH_HMAC",
            title="Invalid Shopify HMAC",
            detail="OAuth HMAC failed.",
        )
    domain = validate_shop_domain(shop)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"https://{domain}/admin/oauth/access_token",
            json={"client_id": os.getenv("SHOPIFY_CLIENT_ID", ""), "client_secret": secret, "code": code},
        )
        resp.raise_for_status()
        tok = resp.json()

    account_id = uuid4()
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO channel_accounts (
                    id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile
                ) VALUES (
                    :id, :tid, 'shopify', :ext, :dname, 'active', '{}'::jsonb
                )
                ON CONFLICT (tenant_id, channel_type, external_account_id)
                DO UPDATE SET status = 'active'
                RETURNING id
                """
            ),
            {"id": account_id, "tid": principal.tenant_id, "ext": domain, "dname": domain},
        )
        vault = CredentialVaultHelper()
        await vault.save_secret_json(
            session,
            tenant_id=principal.tenant_id,
            channel_account_id=account_id,
            credential_type="oauth_token",
            payload={"shop_domain": domain, "auth_mode": "oauth", "access_token": tok["access_token"]},
        )

    async with system_session(_get_session_factory(request)) as sys_sess:
        await sys_sess.execute(
            text(
                """
                INSERT INTO channel_routing_directory (channel_type, external_account_id, tenant_id, channel_account_id)
                VALUES ('shopify', :ext, :tid, :caid)
                ON CONFLICT (channel_type, external_account_id)
                DO UPDATE SET tenant_id = EXCLUDED.tenant_id, channel_account_id = EXCLUDED.channel_account_id
                """
            ),
            {"ext": domain, "tid": principal.tenant_id, "caid": account_id},
        )
    return ShopifyConnectResponse(channel_account_id=account_id, shop_domain=domain, auth_mode="oauth")


@router.post("/connectors/shopify/custom-app", response_model=ShopifyConnectResponse, status_code=status.HTTP_201_CREATED)
async def connect_shopify_custom_app(
    request: Request,
    payload: ShopifyCustomAppConnectRequest,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN)),
) -> ShopifyConnectResponse:
    """Connect merchant-created 2026-01 Dev Dashboard Custom App using client_credentials (24h auto-refresh)."""
    domain = validate_shop_domain(payload.shop_domain)
    account_id = uuid4()
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO channel_accounts (
                    id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile
                ) VALUES (
                    :id, :tid, 'shopify', :ext, :dname, 'active', CAST(:rlp AS jsonb)
                )
                """
            ),
            {
                "id": account_id,
                "tid": principal.tenant_id,
                "ext": domain,
                "dname": payload.display_name or domain,
                "rlp": json.dumps({"warehouse_id": str(payload.warehouse_id) if payload.warehouse_id else ""}),
            },
        )
        vault = CredentialVaultHelper()
        await vault.save_secret_json(
            session,
            tenant_id=principal.tenant_id,
            channel_account_id=account_id,
            credential_type="shopify_custom_app",
            payload={
                "shop_domain": domain,
                "auth_mode": "client_credentials",
                "client_id": payload.client_id,
                "client_secret": payload.client_secret,
                "access_token": "",
            },
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )

    async with system_session(_get_session_factory(request)) as sys_sess:
        await sys_sess.execute(
            text(
                """
                INSERT INTO channel_routing_directory (channel_type, external_account_id, tenant_id, channel_account_id)
                VALUES ('shopify', :ext, :tid, :caid)
                ON CONFLICT (channel_type, external_account_id)
                DO UPDATE SET tenant_id = EXCLUDED.tenant_id, channel_account_id = EXCLUDED.channel_account_id
                """
            ),
            {"ext": domain, "tid": principal.tenant_id, "caid": account_id},
        )
    return ShopifyConnectResponse(channel_account_id=account_id, shop_domain=domain, auth_mode="client_credentials")


@router.post("/webhooks/shopify")
async def receive_shopify_webhook(
    request: Request,
    x_shopify_hmac_sha256: str | None = Header(default=None, alias="X-Shopify-Hmac-SHA256"),
    x_shopify_topic: str = Header(default="", alias="X-Shopify-Topic"),
    x_shopify_shop_domain: str = Header(default="", alias="X-Shopify-Shop-Domain"),
    x_shopify_webhook_id: str | None = Header(default=None, alias="X-Shopify-Webhook-Id"),
) -> dict[str, str]:
    raw_body = await request.body()
    secret = os.getenv("SHOPIFY_CLIENT_SECRET", "")
    if not x_shopify_hmac_sha256 or not verify_shopify_webhook_hmac(raw_body, x_shopify_hmac_sha256, secret):
        raise AppError(
            slug="invalid-webhook-signature",
            status_code=401,
            code="ERR_INVALID_WEBHOOK_SIGNATURE",
            title="Invalid Shopify Webhook HMAC",
            detail="Shopify webhook HMAC verification failed.",
        )

    event_id = x_shopify_webhook_id or hashlib.sha256(raw_body).hexdigest()
    payload = json.loads(raw_body.decode("utf-8"))

    async with system_session(_get_session_factory(request)) as sys_sess:
        inserted = (
            await sys_sess.execute(
                text(
                    """
                    INSERT INTO raw_webhook_events (channel_type, external_event_id, payload, status)
                    VALUES ('shopify', :eid, CAST(:payload AS jsonb), 'received')
                    ON CONFLICT (channel_type, external_event_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {"eid": event_id, "payload": raw_body.decode("utf-8")},
            )
        ).scalar_one_or_none()

    if inserted is not None:
        redis_client = getattr(request.app.state, "redis", None)
        if redis_client is not None:
            await redis_client.xadd(
                "stream:webhooks:shopify",
                {"raw_event_id": str(inserted), "topic": x_shopify_topic, "shop_domain": x_shopify_shop_domain},
            )
        await process_shopify_webhook_task.kiq(x_shopify_topic, x_shopify_shop_domain, payload)

    return {"status": "accepted"}
```

Modify `apps/backend/src/erp_core/api_main.py` and `apps/backend/src/erp_core/worker_main.py`:

```python
# In apps/backend/src/erp_core/api_main.py
from erp_core.channels.ecommerce.shopify_router import router as shopify_router

app.include_router(shopify_router, prefix="/api/v1")

# In apps/backend/src/erp_core/worker_main.py
import erp_core.channels.ecommerce.shopify_router  # noqa: F401
```

- [ ] **Step 4: Run Shopify tests, export OpenAPI, and check import-linter**

Run:
```bash
cd apps/backend && uv run pytest tests/test_shopify_connector.py -v
cd apps/backend && uv run python scripts/export_openapi.py > ../../contracts/openapi.json
cd apps/backend && uv run lint-imports --config ../../infra/importlinter.ini
```
Expected: `test_shopify_connector.py` PASSES; `openapi.json` includes `/api/v1/connectors/shopify/*` and `/api/v1/webhooks/shopify`; `lint-imports` passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/channels/ecommerce/ apps/backend/src/erp_core/api_main.py apps/backend/src/erp_core/worker_main.py apps/backend/tests/test_shopify_connector.py contracts/openapi.json
git commit -m "feat(shopify): add GraphQL-only 2026-01 connector, Plus B2B, 24h token refresh & GDPR hooks"
```

---

### Task 3.8: WooCommerce REST v3 + SHOPLINE + Shoplazza Connectors

**Files:**
- Create: `apps/backend/src/erp_core/channels/ecommerce/woocommerce.py`
- Create: `apps/backend/src/erp_core/channels/ecommerce/shopline.py`
- Create: `apps/backend/src/erp_core/channels/ecommerce/shoplazza.py`
- Create: `apps/backend/src/erp_core/channels/ecommerce/connectors_router.py`
- Modify: `apps/backend/src/erp_core/api_main.py` (include `connectors_router` on `/api/v1`)
- Modify: `apps/backend/src/erp_core/worker_main.py` (import `connectors_router` tasks)
- Test: `apps/backend/tests/test_woo_shopline_shoplazza.py`

**Interfaces:**
- Consumes:
  - `erp_core.channels.ecommerce.base`: `CommercePlatformAdapter`, `CredentialVaultHelper`, `EcommerceOrderSyncService`, `NormalizedOrder`, `NormalizedProduct`
  - `erp_core.shared.db`: `tenant_session`, `system_session`
  - `erp_core.tenancy.dependencies`: `CurrentPrincipal`, `require_roles`
- Produces:
  - `WooCommerceAdapter`, `build_wc_authorize_url`, `verify_woocommerce_webhook`
  - `ShoplineAdapter`, `LeakyBucketLimiter`, `verify_shopline_webhook`
  - `ShoplazzaAdapter`, `verify_shoplazza_oauth_hmac`, `verify_shoplazza_webhook`
  - HTTP endpoints: `/api/v1/connectors/woocommerce/*`, `/api/v1/webhooks/woocommerce`, `/api/v1/connectors/shopline/*`, `/api/v1/webhooks/shopline`, `/api/v1/connectors/shoplazza/*`, `/api/v1/webhooks/shoplazza`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_woo_shopline_shoplazza.py`:

```python
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.ecommerce.shoplazza import (
    ShoplazzaAdapter,
    verify_shoplazza_oauth_hmac,
    verify_shoplazza_webhook,
)
from erp_core.channels.ecommerce.shopline import LeakyBucketLimiter, ShoplineAdapter, verify_shopline_webhook
from erp_core.channels.ecommerce.woocommerce import (
    WooCommerceAdapter,
    build_wc_authorize_url,
    verify_woocommerce_webhook,
)
from erp_core.shared.enums import FulfillmentStatus, OrderMode


@pytest.mark.asyncio
async def test_woocommerce_meta_data_order_notes_and_webhook_hmac() -> None:
    url = build_wc_authorize_url(
        store_url="https://qz-lighting.com",
        app_name="OmniERP",
        user_id="tenant-1",
        return_url="https://erp.local/done",
        callback_url="https://erp.local/api/v1/connectors/woocommerce/auth-callback",
    )
    assert url.startswith("https://qz-lighting.com/wc-auth/v1/authorize?")
    assert "scope=read_write" in url

    note_requests: list[dict] = []

    def _woo_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/wc/v3/orders" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9021,
                        "number": "WC-9021",
                        "status": "processing",
                        "currency": "USD",
                        "date_modified_gmt": "2026-11-19T01:00:00",
                        "shipping_total": "15.00",
                        "discount_total": "0.00",
                        "total_tax": "5.00",
                        "total": "220.00",
                        "billing": {
                            "email": "wholesale@euroled.de",
                            "phone": "+49301234567",
                            "first_name": "Lukas",
                            "last_name": "Weber",
                            "country": "DE",
                            "company": "EuroLED GmbH",
                        },
                        "meta_data": [
                            {"id": 1, "key": "_wholesale_po_number", "value": "PO-DE-2026-77"},
                            {"id": 2, "key": "_tracking_number", "value": "DHL-DE-4455"},
                        ],
                        "line_items": [
                            {"sku": "LED-STRIP-5M-IP65", "name": "5M LED Strip", "quantity": 10, "total": "200.00"}
                        ],
                    }
                ],
            )
        if request.url.path == "/wp-json/wc/v3/orders/9021/notes" and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            note_requests.append(body)
            return httpx.Response(201, json={"id": 44, **body})
        if request.url.path == "/wp-json/wc/v3/orders/9021/fulfillments" and request.method == "POST":
            return httpx.Response(201, json={"id": 55})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_woo_handler)) as client:
        adapter = WooCommerceAdapter(
            store_url="https://qz-lighting.com",
            consumer_key="ck_test",
            consumer_secret="cs_test",
            meta_field_map={"po_number": "_wholesale_po_number", "tracking_number": "_tracking_number"},
            http_client=client,
        )
        orders, _ = await adapter.fetch_orders_updated_since(datetime.now(timezone.utc) - timedelta(hours=1))
        assert len(orders) == 1
        assert orders[0].order_mode == OrderMode.B2B_WHOLESALE
        assert orders[0].b2b_company is not None
        assert orders[0].b2b_company.legal_name == "EuroLED GmbH"
        assert orders[0].channel_metadata["po_number"] == "PO-DE-2026-77"

        await adapter.create_fulfillment("9021", "DHL-DE-9999", "DHL", notify_customer=True)
        assert len(note_requests) == 1
        assert note_requests[0]["customer_note"] is True
        assert "DHL-DE-9999" in note_requests[0]["note"]

    raw = b'{"id": 9021, "status": "processing"}'
    secret = "wc-wh-secret"
    sig = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    assert verify_woocommerce_webhook(raw, {"X-WC-Webhook-Signature": sig}, secret) is True
    assert verify_woocommerce_webhook(raw, {"X-WC-Webhook-Signature": "bad"}, secret) is False


@pytest.mark.asyncio
async def test_shopline_b2b_po_and_leaky_bucket() -> None:
    def _sl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orders.json"):
            return httpx.Response(
                200,
                json={
                    "orders": [
                        {
                            "id": "sl_ord_301",
                            "order_number": "SL-301",
                            "financial_status": "paid",
                            "fulfillment_status": "unfulfilled",
                            "currency": "USD",
                            "subtotal_price": "500.00",
                            "total_shipping_price": "30.00",
                            "total_tax": "0.00",
                            "total_price": "530.00",
                            "purchase_order_number": "PO-SL-B2B-09",
                            "b2b_company_location": {"company_name": "Pacific Lighting Pty", "country_code": "AU"},
                            "updated_at": "2026-11-19T01:05:00Z",
                            "customer": {"email": "ops@pacificled.au", "first_name": "Liam", "last_name": "Chen"},
                            "line_items": [{"sku": "LED-STRIP-5M-IP65", "title": "5M Strip", "quantity": 25, "price": "20.00"}],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    bucket = LeakyBucketLimiter(redis_client=None)
    allowed, wait_ms = await bucket.acquire("shopline:store1", rate_per_sec=4.0, capacity=40)
    assert allowed is True and wait_ms == 0

    async with httpx.AsyncClient(transport=httpx.MockTransport(_sl_handler)) as client:
        adapter = ShoplineAdapter(
            store_domain="qz-led.myshopline.com",
            access_token="sl_tok_3yr",
            leaky_bucket=bucket,
            http_client=client,
        )
        orders, _ = await adapter.fetch_orders_updated_since(datetime.now(timezone.utc) - timedelta(hours=1))
        assert len(orders) == 1
        assert orders[0].order_mode == OrderMode.B2B_WHOLESALE
        assert orders[0].channel_metadata["po_number"] == "PO-SL-B2B-09"
        assert orders[0].b2b_company is not None
        assert orders[0].b2b_company.legal_name == "Pacific Lighting Pty"

    raw = b'{"id":"sl_ord_301"}'
    secret = "sl_app_secret"
    sig = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    assert verify_shopline_webhook(raw, {"X-Shopline-Hmac-Sha256": sig}, secret) is True


@pytest.mark.asyncio
async def test_shoplazza_dual_hmac_and_deduplication_idempotency() -> None:
    secret = "shoplazza_client_secret_99"
    # 1. OAuth Query String Hex HMAC
    query_params = {"code": "authcode1", "shop": "qz.myshoplaza.com", "timestamp": "1763510000"}
    canonical = "code=authcode1&shop=qz.myshoplaza.com&timestamp=1763510000"
    oauth_hex = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert verify_shoplazza_oauth_hmac({**query_params, "hmac": oauth_hex}, secret) is True
    assert verify_shoplazza_oauth_hmac({**query_params, "hmac": "wrong"}, secret) is False

    # 2. Webhook Body Base64 HMAC
    raw = b'{"id":"spz_991","total_price":"99.00"}'
    wh_b64 = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    assert verify_shoplazza_webhook(raw, {"X-Shoplazza-Hmac-Sha256": wh_b64}, secret) is True

    # 3. Verify X-Shoplazza-Deduplication-ID idempotency against raw_webhook_events
    mig_url = os.getenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_secret@localhost:5432/erp_saas",
    )
    rt_url = os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )
    mig_engine = create_async_engine(mig_url, echo=False)
    rt_engine = create_async_engine(rt_url, echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    os.environ["SHOPLAZZA_CLIENT_SECRET"] = secret
    from erp_core.channels.ecommerce.connectors_router import router as connectors_router

    test_app = FastAPI()
    test_app.include_router(connectors_router, prefix="/api/v1")
    test_app.state.session_factory = rt_factory
    dedup_id = f"spz-dedup-{uuid4().hex}"

    try:
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            headers = {
                "X-Shoplazza-Hmac-Sha256": wh_b64,
                "X-Shoplazza-Deduplication-ID": dedup_id,
                "X-Shoplazza-Topic": "orders/create",
                "X-Shoplazza-Shop-Domain": "qz.myshoplaza.com",
            }
            r1 = await client.post("/api/v1/webhooks/shoplazza", content=raw, headers=headers)
            r2 = await client.post("/api/v1/webhooks/shoplazza", content=raw, headers=headers)
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r1.json()["deduplicated"] is False
            assert r2.json()["deduplicated"] is True

        async with mig_engine.connect() as conn:
            cnt = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM raw_webhook_events WHERE channel_type = 'shoplazza' AND external_event_id = :eid"
                    ),
                    {"eid": dedup_id},
                )
            ).scalar_one()
            assert cnt == 1
    finally:
        async with mig_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM raw_webhook_events WHERE channel_type = 'shoplazza' AND external_event_id = :eid"),
                {"eid": dedup_id},
            )
        await mig_engine.dispose()
        await rt_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_woo_shopline_shoplazza.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.channels.ecommerce.woocommerce'`

- [ ] **Step 3: Write `woocommerce.py`, `shopline.py`, `shoplazza.py`, `connectors_router.py`, and wire `api_main.py` & `worker_main.py`**

Create `apps/backend/src/erp_core/channels/ecommerce/woocommerce.py`:

```python
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    CredentialVaultHelper,
    NormalizedB2BCompany,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus

_STORE_SEMAPHORES: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(3))


def build_wc_authorize_url(
    store_url: str,
    app_name: str,
    user_id: str,
    return_url: str,
    callback_url: str,
    scope: str = "read_write",
) -> str:
    base = store_url.rstrip("/")
    qs = urlencode(
        {
            "app_name": app_name,
            "scope": scope,
            "user_id": user_id,
            "return_url": return_url,
            "callback_url": callback_url,
        }
    )
    return f"{base}/wc-auth/v1/authorize?{qs}"


def verify_woocommerce_webhook(raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
    sig = headers.get("X-WC-Webhook-Signature") or headers.get("x-wc-webhook-signature") or ""
    if not sig or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, sig.strip())


class WooCommerceAdapter(CommercePlatformAdapter):
    """WooCommerce REST v3 adapter with per-site concurrency cap, meta_data mapper & Order Notes."""

    def __init__(
        self,
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        webhook_secret: str = "",
        meta_field_map: dict[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store_url = store_url.rstrip("/")
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.webhook_secret = webhook_secret
        self.meta_field_map = meta_field_map or {
            "po_number": "_wholesale_po_number",
            "tracking_number": "_tracking_number",
            "carrier": "_shipment_provider",
        }
        self._http_client = http_client

    @classmethod
    async def from_channel_account(
        cls,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        crypto: EnvelopeCryptoService | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> WooCommerceAdapter:
        vault = CredentialVaultHelper(crypto)
        data, _ = await vault.load_secret_json(session, tenant_id, channel_account_id, "byok_api_key")
        return cls(
            store_url=data["store_url"],
            consumer_key=data["consumer_key"],
            consumer_secret=data["consumer_secret"],
            webhook_secret=data.get("webhook_secret", ""),
            meta_field_map=data.get("meta_field_map"),
            http_client=http_client,
        )

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None, json_body: Any = None) -> Any:
        url = f"{self.store_url}/wp-json/wc/v3{path}"
        sem = _STORE_SEMAPHORES[self.store_url]
        async with sem:
            if self._http_client is not None:
                resp = await self._http_client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    auth=(self.consumer_key, self.consumer_secret),
                )
            else:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        auth=(self.consumer_key, self.consumer_secret),
                    )
            resp.raise_for_status()
            return resp.json()

    def _extract_meta(self, meta_list: list[dict[str, Any]]) -> dict[str, Any]:
        by_key = {str(m.get("key")): m.get("value") for m in meta_list if m.get("key") is not None}
        extracted: dict[str, Any] = {}
        for canonical_key, wc_key in self.meta_field_map.items():
            if wc_key in by_key and by_key[wc_key] not in (None, ""):
                extracted[canonical_key] = by_key[wc_key]
        return extracted

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        page = int(cursor) if cursor else 1
        rows = await self._request(
            "GET",
            "/orders",
            params={"modified_after": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "per_page": 50, "page": page},
        )
        orders: list[NormalizedOrder] = []
        for raw in rows:
            meta = self._extract_meta(raw.get("meta_data") or [])
            billing = raw.get("billing") or {}
            company_name = (billing.get("company") or "").strip()
            is_b2b = bool(meta.get("po_number")) or bool(company_name and "wholesale" in str(raw.get("created_via", "")).lower())
            b2b_comp = (
                NormalizedB2BCompany(legal_name=company_name or f"WC Buyer {raw['id']}", country_code=billing.get("country") or "US")
                if is_b2b
                else None
            )

            wc_status = str(raw.get("status") or "processing").lower()
            if wc_status in {"completed"}:
                ful_status = FulfillmentStatus.DELIVERED
                pay_status = PaymentStatus.PAID
            elif wc_status in {"cancelled", "failed"}:
                ful_status = FulfillmentStatus.CANCELLED
                pay_status = PaymentStatus.UNPAID
            elif wc_status == "refunded":
                ful_status = FulfillmentStatus.REFUNDED
                pay_status = PaymentStatus.REFUNDED
            elif wc_status == "pending":
                ful_status = FulfillmentStatus.PENDING_PAYMENT
                pay_status = PaymentStatus.UNPAID
            else:
                ful_status = FulfillmentStatus.CONFIRMED_RESERVED
                pay_status = PaymentStatus.PAID

            items: list[NormalizedLineItem] = []
            for li in raw.get("line_items") or []:
                sku = str(li.get("sku") or "").strip()
                if not sku:
                    continue
                items.append(
                    NormalizedLineItem(
                        external_sku=sku,
                        title=str(li.get("name") or sku),
                        quantity=int(li["quantity"]),
                        line_total=Decimal(str(li.get("total", "0.00"))),
                    )
                )
            if not items:
                continue

            total = Decimal(str(raw.get("total", "0.00")))
            shipping = Decimal(str(raw.get("shipping_total", "0.00")))
            discount = Decimal(str(raw.get("discount_total", "0.00")))
            tax = Decimal(str(raw.get("total_tax", "0.00")))
            subtotal = max(Decimal("0.00"), total - shipping - tax + discount)

            mod_raw = str(raw.get("date_modified_gmt") or datetime.now(timezone.utc).isoformat())
            mod_dt = datetime.fromisoformat(mod_raw.replace("Z", "+00:00"))
            if mod_dt.tzinfo is None:
                mod_dt = mod_dt.replace(tzinfo=timezone.utc)

            first = billing.get("first_name") or ""
            last = billing.get("last_name") or ""
            orders.append(
                NormalizedOrder(
                    external_order_id=str(raw["id"]),
                    external_order_number=str(raw.get("number") or raw["id"]),
                    order_mode=OrderMode.B2B_WHOLESALE if is_b2b else OrderMode.B2C_RETAIL,
                    currency=str(raw.get("currency") or "USD").upper(),
                    subtotal_amount=subtotal,
                    shipping_charged=shipping,
                    discount_amount=discount,
                    tax_collected=tax,
                    total_amount=total,
                    fulfillment_status=ful_status,
                    payment_status=pay_status,
                    customer=NormalizedCustomer(
                        email=billing.get("email"),
                        phone_e164=billing.get("phone"),
                        external_customer_id=str(raw.get("customer_id") or raw["id"]),
                        external_identity_type=IdentityType.EMAIL,
                        display_name=f"{first} {last}".strip() or billing.get("email") or "WooCommerce Buyer",
                        country_code=billing.get("country"),
                    ),
                    b2b_company=b2b_comp,
                    line_items=items,
                    channel_metadata={"wc_status": wc_status, **meta},
                    updated_at=mod_dt,
                )
            )
        next_cursor = str(page + 1) if len(rows) == 50 else None
        return orders, next_cursor

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        page = int(cursor) if cursor else 1
        rows = await self._request(
            "GET",
            "/products",
            params={"modified_after": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "per_page": 50, "page": page},
        )
        prods = [
            NormalizedProduct(external_product_id=str(p["id"]), title=p["name"], variants=[{"sku": p.get("sku"), "price": p.get("price")}])
            for p in rows
        ]
        return prods, (str(page + 1) if len(rows) == 50 else None)

    async def post_order_note(self, external_order_id: str, note: str, customer_note: bool = True) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/orders/{external_order_id}/notes",
            json_body={"note": note, "customer_note": customer_note},
        )

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        ful_resp = await self._request(
            "POST",
            f"/orders/{external_order_id}/fulfillments",
            json_body={
                "_tracking_number": tracking_number,
                "_shipment_provider": carrier_company,
                "_tracking_url": tracking_url or "",
            },
        )
        if notify_customer:
            msg = f"Your order has shipped via {carrier_company}. Tracking number: {tracking_number}"
            if tracking_url:
                msg += f" ({tracking_url})"
            await self.post_order_note(external_order_id, note=msg, customer_note=True)
        return ful_resp

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_woocommerce_webhook(raw_body, headers, secret)
```

Create `apps/backend/src/erp_core/channels/ecommerce/shopline.py`:

```python
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    CredentialVaultHelper,
    NormalizedB2BCompany,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

LEAKY_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])

local data = redis.call('HMGET', key, 'level', 'last_ms')
local level = tonumber(data[1]) or 0
local last_ms = tonumber(data[2]) or now_ms

local elapsed = math.max(0, now_ms - last_ms) / 1000.0
level = math.max(0, level - elapsed * rate)

if level + 1.0 <= capacity then
    level = level + 1.0
    redis.call('HSET', key, 'level', tostring(level), 'last_ms', tostring(now_ms))
    redis.call('PEXPIRE', key, math.ceil((capacity / rate) * 1000) + 1000)
    return {1, 0}
else
    local wait_ms = math.ceil(((level + 1.0 - capacity) / rate) * 1000)
    return {0, wait_ms}
end
"""


class LeakyBucketLimiter:
    """Atomic Redis (with local in-memory fallback) leaky bucket rate limiter."""

    def __init__(self, redis_client: Any | None = None) -> None:
        self.redis = redis_client
        self._local: dict[str, tuple[float, float]] = {}

    async def acquire(self, bucket_key: str, rate_per_sec: float, capacity: int) -> tuple[bool, int]:
        now_ms = time.monotonic() * 1000.0
        if self.redis is not None and hasattr(self.redis, "eval"):
            res = await self.redis.eval(
                LEAKY_BUCKET_LUA, 1, bucket_key, str(rate_per_sec), str(capacity), str(int(time.time() * 1000))
            )
            return bool(int(res[0])), int(res[1])

        level, last_ms = self._local.get(bucket_key, (0.0, now_ms))
        elapsed = max(0.0, now_ms - last_ms) / 1000.0
        level = max(0.0, level - elapsed * rate_per_sec)
        if level + 1.0 <= capacity:
            self._local[bucket_key] = (level + 1.0, now_ms)
            return True, 0
        wait_ms = int(((level + 1.0 - capacity) / rate_per_sec) * 1000) + 1
        return False, wait_ms

    async def wait_and_acquire(self, bucket_key: str, rate_per_sec: float, capacity: int) -> None:
        for _ in range(5):
            ok, wait_ms = await self.acquire(bucket_key, rate_per_sec, capacity)
            if ok:
                return
            await asyncio.sleep(min(wait_ms / 1000.0, 3.0))
        raise AppError(
            slug="shopline-rate-limit",
            status_code=429,
            code="ERR_SHOPLINE_RATE_LIMIT",
            title="SHOPLINE Rate Limit",
            detail="Leaky bucket saturated.",
        )


def verify_shopline_webhook(raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
    sig = headers.get("X-Shopline-Hmac-Sha256") or headers.get("x-shopline-hmac-sha256") or ""
    if not sig or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, sig.strip())


class ShoplineAdapter(CommercePlatformAdapter):
    """SHOPLINE REST Admin API v20260301 adapter with 4 QPS leaky bucket & B2B PO extraction."""

    def __init__(
        self,
        store_domain: str,
        access_token: str,
        api_version: str = "v20260301",
        is_enterprise: bool = False,
        leaky_bucket: LeakyBucketLimiter | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store_domain = store_domain.strip().lower()
        self.access_token = access_token
        self.api_version = api_version
        self.is_enterprise = is_enterprise
        self.leaky_bucket = leaky_bucket or LeakyBucketLimiter()
        self._http_client = http_client

    @classmethod
    async def from_channel_account(
        cls,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        crypto: EnvelopeCryptoService | None = None,
        redis_client: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> ShoplineAdapter:
        vault = CredentialVaultHelper(crypto)
        data, _ = await vault.load_secret_json(session, tenant_id, channel_account_id, "oauth_token")
        return cls(
            store_domain=data["store_domain"],
            access_token=data["access_token"],
            api_version=data.get("api_version", "v20260301"),
            is_enterprise=bool(data.get("is_enterprise", False)),
            leaky_bucket=LeakyBucketLimiter(redis_client=redis_client),
            http_client=http_client,
        )

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None, json_body: Any = None) -> dict[str, Any]:
        if not self.is_enterprise:
            await self.leaky_bucket.wait_and_acquire(f"leaky:shopline:{self.store_domain}", rate_per_sec=4.0, capacity=40)
        url = f"https://{self.store_domain}/admin/openapi/{self.api_version}{path}"
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json; charset=utf-8"}
        if self._http_client is not None:
            resp = await self._http_client.request(method, url, params=params, json=json_body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.request(method, url, params=params, json=json_body, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        params: dict[str, Any] = {
            "updated_at_min": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 50,
        }
        if cursor:
            params["page_info"] = cursor
        body = await self._request("GET", "/orders.json", params=params)
        orders: list[NormalizedOrder] = []
        for raw in body.get("orders") or []:
            po_num = raw.get("purchase_order_number") or raw.get("po_number")
            b2b_loc = raw.get("b2b_company_location") or {}
            is_b2b = bool(po_num or b2b_loc)
            b2b_comp = (
                NormalizedB2BCompany(
                    legal_name=str(b2b_loc.get("company_name") or f"SHOPLINE B2B {raw['id']}"),
                    country_code=str(b2b_loc.get("country_code") or "US"),
                )
                if is_b2b
                else None
            )

            cust = raw.get("customer") or {}
            first = cust.get("first_name") or ""
            last = cust.get("last_name") or ""
            items: list[NormalizedLineItem] = []
            for li in raw.get("line_items") or []:
                sku = str(li.get("sku") or "").strip()
                if not sku:
                    continue
                qty = int(li["quantity"])
                line_tot = Decimal(str(li.get("price", "0.00"))) * Decimal(qty)
                items.append(NormalizedLineItem(external_sku=sku, title=str(li.get("title") or sku), quantity=qty, line_total=line_tot))
            if not items:
                continue

            fin = str(raw.get("financial_status") or "paid").lower()
            pay_status = PaymentStatus.PAID if fin == "paid" else PaymentStatus.UNPAID
            ful = str(raw.get("fulfillment_status") or "unfulfilled").lower()
            ful_status = FulfillmentStatus.SHIPPED if ful == "fulfilled" else FulfillmentStatus.CONFIRMED_RESERVED

            orders.append(
                NormalizedOrder(
                    external_order_id=str(raw["id"]),
                    external_order_number=str(raw.get("order_number") or raw["id"]),
                    order_mode=OrderMode.B2B_WHOLESALE if is_b2b else OrderMode.B2C_RETAIL,
                    currency=str(raw.get("currency") or "USD").upper(),
                    subtotal_amount=Decimal(str(raw.get("subtotal_price", "0.00"))),
                    shipping_charged=Decimal(str(raw.get("total_shipping_price", "0.00"))),
                    tax_collected=Decimal(str(raw.get("total_tax", "0.00"))),
                    total_amount=Decimal(str(raw.get("total_price", "0.00"))),
                    fulfillment_status=ful_status,
                    payment_status=pay_status,
                    customer=NormalizedCustomer(
                        email=cust.get("email"),
                        phone_e164=cust.get("phone"),
                        external_customer_id=str(cust.get("id") or raw["id"]),
                        external_identity_type=IdentityType.EMAIL,
                        display_name=f"{first} {last}".strip() or cust.get("email") or "SHOPLINE Buyer",
                    ),
                    b2b_company=b2b_comp,
                    line_items=items,
                    channel_metadata={"po_number": po_num, "b2b_company_location": b2b_loc},
                    updated_at=datetime.fromisoformat(str(raw["updated_at"]).replace("Z", "+00:00")),
                )
            )
        return orders, body.get("next_page_info")

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        body = await self._request(
            "GET",
            "/products.json",
            params={"updated_at_min": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 50},
        )
        prods = [
            NormalizedProduct(external_product_id=str(p["id"]), title=p.get("title", ""), variants=p.get("variants") or [])
            for p in body.get("products") or []
        ]
        return prods, body.get("next_page_info")

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/orders/{external_order_id}/fulfillments.json",
            json_body={
                "fulfillment": {
                    "tracking_number": tracking_number,
                    "tracking_company": carrier_company,
                    "tracking_url": tracking_url or "",
                    "notify_customer": notify_customer,
                }
            },
        )

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_shopline_webhook(raw_body, headers, secret)
```

Create `apps/backend/src/erp_core/channels/ecommerce/shoplazza.py`:

```python
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    CredentialVaultHelper,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
)
from erp_core.channels.ecommerce.shopline import LeakyBucketLimiter
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus


def verify_shoplazza_oauth_hmac(query_params: Mapping[str, str], client_secret: str) -> bool:
    """Shoplazza OAuth callback uses Hex HMAC-SHA256 on sorted query string (different from Webhook Base64)."""
    received = query_params.get("hmac", "")
    if not received or not client_secret:
        return False
    pairs = sorted((k, v) for k, v in query_params.items() if k not in {"hmac", "signature"})
    canonical = "&".join(f"{k}={v}" for k, v in pairs).encode("utf-8")
    expected_hex = hmac.new(client_secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hex, received.lower())


def verify_shoplazza_webhook(raw_body: bytes, headers: Mapping[str, str], client_secret: str) -> bool:
    """Shoplazza webhook uses Base64 HMAC-SHA256 on raw request body."""
    sig = headers.get("X-Shoplazza-Hmac-Sha256") or headers.get("x-shoplazza-hmac-sha256") or ""
    if not sig or not client_secret:
        return False
    digest = hmac.new(client_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, sig.strip())


class ShoplazzaAdapter(CommercePlatformAdapter):
    """Shoplazza OpenAPI 2026-01 connector with dual leaky buckets (2 QPS App*Store & 20 QPS Store)."""

    def __init__(
        self,
        shop_domain: str,
        access_token: str,
        api_version: str = "2026-01",
        leaky_bucket: LeakyBucketLimiter | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.shop_domain = shop_domain.strip().lower()
        self.access_token = access_token
        self.api_version = api_version
        self.leaky_bucket = leaky_bucket or LeakyBucketLimiter()
        self._http_client = http_client

    @classmethod
    async def from_channel_account(
        cls,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        crypto: EnvelopeCryptoService | None = None,
        redis_client: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> ShoplazzaAdapter:
        vault = CredentialVaultHelper(crypto)
        data, _ = await vault.load_secret_json(session, tenant_id, channel_account_id, "oauth_token")
        return cls(
            shop_domain=data["shop_domain"],
            access_token=data["access_token"],
            api_version=data.get("api_version", "2026-01"),
            leaky_bucket=LeakyBucketLimiter(redis_client=redis_client),
            http_client=http_client,
        )

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None, json_body: Any = None) -> dict[str, Any]:
        # Dual leaky bucket enforcement: 2 QPS App*Store (cap 40) + 20 QPS Store global (cap 80)
        await self.leaky_bucket.wait_and_acquire(f"leaky:shoplazza:app_store:{self.shop_domain}", rate_per_sec=2.0, capacity=40)
        await self.leaky_bucket.wait_and_acquire(f"leaky:shoplazza:store:{self.shop_domain}", rate_per_sec=20.0, capacity=80)

        url = f"https://{self.shop_domain}/openapi/{self.api_version}{path}"
        headers = {"Access-Token": self.access_token, "Content-Type": "application/json"}
        for _ in range(3):
            if self._http_client is not None:
                resp = await self._http_client.request(method, url, params=params, json=json_body, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.request(method, url, params=params, json=json_body, headers=headers)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                await asyncio.sleep(min(retry_after, 5.0))
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return {}

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        page = int(cursor) if cursor else 1
        body = await self._request(
            "GET",
            "/orders",
            params={
                "updated_at_min": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 50,
                "page": page,
            },
        )
        rows = body.get("orders") or []
        orders: list[NormalizedOrder] = []
        for raw in rows:
            cust = raw.get("customer") or {}
            items: list[NormalizedLineItem] = []
            for li in raw.get("line_items") or []:
                sku = str(li.get("sku") or "").strip()
                if not sku:
                    continue
                qty = int(li["quantity"])
                line_tot = Decimal(str(li.get("total", li.get("price", "0.00"))))
                items.append(NormalizedLineItem(external_sku=sku, title=str(li.get("title") or sku), quantity=qty, line_total=line_tot))
            if not items:
                continue

            fin = str(raw.get("financial_status") or "paid").lower()
            pay_status = PaymentStatus.PAID if fin == "paid" else PaymentStatus.UNPAID
            ful = str(raw.get("fulfillment_status") or "unfulfilled").lower()
            ful_status = FulfillmentStatus.SHIPPED if ful == "fulfilled" else FulfillmentStatus.CONFIRMED_RESERVED

            orders.append(
                NormalizedOrder(
                    external_order_id=str(raw["id"]),
                    external_order_number=str(raw.get("number") or raw["id"]),
                    order_mode=OrderMode.B2C_RETAIL,
                    currency=str(raw.get("currency") or "USD").upper(),
                    subtotal_amount=Decimal(str(raw.get("sub_total", "0.00"))),
                    shipping_charged=Decimal(str(raw.get("shipping_line_price", "0.00"))),
                    tax_collected=Decimal(str(raw.get("total_tax", "0.00"))),
                    total_amount=Decimal(str(raw.get("total_price", "0.00"))),
                    fulfillment_status=ful_status,
                    payment_status=pay_status,
                    customer=NormalizedCustomer(
                        email=cust.get("email"),
                        phone_e164=cust.get("phone"),
                        external_customer_id=str(cust.get("id") or raw["id"]),
                        external_identity_type=IdentityType.EMAIL,
                        display_name=f"{cust.get('first_name', '')} {cust.get('last_name', '')}".strip() or cust.get("email") or "Shoplazza Buyer",
                    ),
                    line_items=items,
                    channel_metadata={"shoplazza_number": raw.get("number")},
                    updated_at=datetime.fromisoformat(str(raw["updated_at"]).replace("Z", "+00:00")),
                )
            )
        return orders, (str(page + 1) if len(rows) == 50 else None)

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        page = int(cursor) if cursor else 1
        body = await self._request("GET", "/products", params={"limit": 50, "page": page})
        rows = body.get("products") or []
        prods = [
            NormalizedProduct(external_product_id=str(p["id"]), title=p.get("title", ""), variants=p.get("variants") or [])
            for p in rows
        ]
        return prods, (str(page + 1) if len(rows) == 50 else None)

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/orders/{external_order_id}/fulfillments",
            json_body={
                "tracking_number": tracking_number,
                "tracking_company": carrier_company,
                "tracking_url": tracking_url or "",
                "notify_customer": notify_customer,
            },
        )

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_shoplazza_webhook(raw_body, headers, secret)
```

Create `apps/backend/src/erp_core/channels/ecommerce/connectors_router.py`:

```python
from __future__ import annotations

import hashlib
import json
import os
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.channels.ecommerce.shoplazza import verify_shoplazza_oauth_hmac, verify_shoplazza_webhook
from erp_core.channels.ecommerce.shopline import verify_shopline_webhook
from erp_core.channels.ecommerce.woocommerce import build_wc_authorize_url, verify_woocommerce_webhook
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(tags=["ecommerce-connectors"])


def _get_session_factory(request: Request):
    factory = getattr(request.app.state, "session_factory", None) or getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


class WooAuthUrlRequest(BaseModel):
    store_url: str
    return_url: str
    callback_url: str


class WooAuthCallbackPayload(BaseModel):
    key_id: int
    user_id: str
    consumer_key: str
    consumer_secret: str
    key_permissions: str


class ShoplineConnectRequest(BaseModel):
    store_domain: str
    access_token: str = Field(min_length=8)
    is_enterprise: bool = False
    warehouse_id: UUID | None = None


class ShoplazzaConnectRequest(BaseModel):
    shop_domain: str
    access_token: str = Field(min_length=8)
    warehouse_id: UUID | None = None


async def _register_channel_account(
    request: Request,
    tenant_id: UUID,
    channel_type: str,
    external_account_id: str,
    display_name: str,
    cred_type: str,
    cred_payload: dict,
    rate_limit_profile: dict,
) -> UUID:
    account_id = uuid4()
    async with tenant_session(_get_session_factory(request), tenant_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO channel_accounts (
                        id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile
                    ) VALUES (
                        :id, :tid, :ctype, :ext, :dname, 'active', CAST(:rlp AS jsonb)
                    )
                    ON CONFLICT (tenant_id, channel_type, external_account_id)
                    DO UPDATE SET status = 'active', rate_limit_profile = EXCLUDED.rate_limit_profile
                    RETURNING id
                    """
                ),
                {
                    "id": account_id,
                    "tid": tenant_id,
                    "ctype": channel_type,
                    "ext": external_account_id,
                    "dname": display_name,
                    "rlp": json.dumps(rate_limit_profile),
                },
            )
        ).one()
        final_id = row.id
        vault = CredentialVaultHelper()
        await vault.save_secret_json(session, tenant_id, final_id, cred_type, cred_payload)

    async with system_session(_get_session_factory(request)) as sys_sess:
        await sys_sess.execute(
            text(
                """
                INSERT INTO channel_routing_directory (channel_type, external_account_id, tenant_id, channel_account_id)
                VALUES (:ctype, :ext, :tid, :caid)
                ON CONFLICT (channel_type, external_account_id)
                DO UPDATE SET tenant_id = EXCLUDED.tenant_id, channel_account_id = EXCLUDED.channel_account_id
                """
            ),
            {"ctype": channel_type, "ext": external_account_id, "tid": tenant_id, "caid": final_id},
        )
    return final_id


async def _persist_webhook_event(
    request: Request, channel_type: str, event_id: str, raw_body: bytes, stream_meta: dict[str, str]
) -> bool:
    async with system_session(_get_session_factory(request)) as sys_sess:
        inserted = (
            await sys_sess.execute(
                text(
                    """
                    INSERT INTO raw_webhook_events (channel_type, external_event_id, payload, status)
                    VALUES (:ctype, :eid, CAST(:payload AS jsonb), 'received')
                    ON CONFLICT (channel_type, external_event_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {"ctype": channel_type, "eid": event_id, "payload": raw_body.decode("utf-8")},
            )
        ).scalar_one_or_none()
    if inserted is None:
        return False
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is not None:
        await redis_client.xadd(f"stream:webhooks:{channel_type}", {"raw_event_id": str(inserted), **stream_meta})
    return True


@router.post("/connectors/woocommerce/auth-url")
async def get_woocommerce_auth_url(
    payload: WooAuthUrlRequest,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN)),
) -> dict[str, str]:
    return {
        "authorize_url": build_wc_authorize_url(
            store_url=payload.store_url,
            app_name="OmniERP",
            user_id=f"{principal.tenant_id}|{payload.store_url.rstrip('/')}",
            return_url=payload.return_url,
            callback_url=payload.callback_url,
        )
    }


@router.post("/connectors/woocommerce/auth-callback")
async def woocommerce_auth_callback(request: Request, payload: WooAuthCallbackPayload) -> dict[str, str]:
    tenant_part, store_url = payload.user_id.split("|", 1)
    tenant_id = UUID(tenant_part)
    await _register_channel_account(
        request,
        tenant_id=tenant_id,
        channel_type="woocommerce",
        external_account_id=store_url.lower(),
        display_name=store_url,
        cred_type="byok_api_key",
        cred_payload={
            "store_url": store_url,
            "consumer_key": payload.consumer_key,
            "consumer_secret": payload.consumer_secret,
        },
        rate_limit_profile={"max_concurrency": 3},
    )
    return {"status": "connected"}


@router.post("/webhooks/woocommerce")
async def receive_woocommerce_webhook(
    request: Request,
    x_wc_webhook_signature: str | None = Header(default=None, alias="X-WC-Webhook-Signature"),
    x_wc_webhook_delivery_id: str | None = Header(default=None, alias="X-WC-Webhook-Delivery-ID"),
    x_wc_webhook_source: str = Header(default="", alias="X-WC-Webhook-Source"),
) -> dict[str, object]:
    raw_body = await request.body()
    secret = os.getenv("WOOCOMMERCE_WEBHOOK_SECRET", "")
    if not x_wc_webhook_signature or not verify_woocommerce_webhook(
        raw_body, {"X-WC-Webhook-Signature": x_wc_webhook_signature}, secret
    ):
        raise AppError(
            slug="invalid-webhook-signature",
            status_code=401,
            code="ERR_INVALID_WEBHOOK_SIGNATURE",
            title="Invalid Woo Signature",
            detail="HMAC failed.",
        )
    event_id = x_wc_webhook_delivery_id or hashlib.sha256(raw_body).hexdigest()
    fresh = await _persist_webhook_event(request, "woocommerce", event_id, raw_body, {"source": x_wc_webhook_source})
    return {"status": "accepted", "deduplicated": not fresh}


@router.post("/connectors/shopline/connect", status_code=status.HTTP_201_CREATED)
async def connect_shopline(
    request: Request,
    payload: ShoplineConnectRequest,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN)),
) -> dict[str, str]:
    domain = payload.store_domain.strip().lower()
    caid = await _register_channel_account(
        request,
        tenant_id=principal.tenant_id,
        channel_type="shopline",
        external_account_id=domain,
        display_name=domain,
        cred_type="oauth_token",
        cred_payload={"store_domain": domain, "access_token": payload.access_token, "is_enterprise": payload.is_enterprise},
        rate_limit_profile={"qps": 4, "capacity": 40, "warehouse_id": str(payload.warehouse_id or "")},
    )
    return {"channel_account_id": str(caid), "store_domain": domain}


@router.post("/webhooks/shopline")
async def receive_shopline_webhook(
    request: Request,
    x_shopline_hmac_sha256: str | None = Header(default=None, alias="X-Shopline-Hmac-Sha256"),
    x_shopline_webhook_id: str | None = Header(default=None, alias="X-Shopline-Webhook-Id"),
    x_shopline_topic: str = Header(default="", alias="X-Shopline-Topic"),
) -> dict[str, object]:
    raw_body = await request.body()
    secret = os.getenv("SHOPLINE_APP_SECRET", "")
    if not x_shopline_hmac_sha256 or not verify_shopline_webhook(
        raw_body, {"X-Shopline-Hmac-Sha256": x_shopline_hmac_sha256}, secret
    ):
        raise AppError(
            slug="invalid-webhook-signature",
            status_code=401,
            code="ERR_INVALID_WEBHOOK_SIGNATURE",
            title="Invalid SHOPLINE Signature",
            detail="HMAC failed.",
        )
    event_id = x_shopline_webhook_id or hashlib.sha256(raw_body).hexdigest()
    fresh = await _persist_webhook_event(request, "shopline", event_id, raw_body, {"topic": x_shopline_topic})
    return {"status": "accepted", "deduplicated": not fresh}


@router.get("/connectors/shoplazza/oauth/callback")
async def shoplazza_oauth_callback(
    request: Request,
    shop: str = Query(...),
    code: str = Query(...),
) -> dict[str, str]:
    secret = os.getenv("SHOPLAZZA_CLIENT_SECRET", "")
    if not verify_shoplazza_oauth_hmac(dict(request.query_params), secret):
        raise AppError(
            slug="invalid-oauth-hmac",
            status_code=401,
            code="ERR_INVALID_OAUTH_HMAC",
            title="Invalid Shoplazza OAuth HMAC",
            detail="Query HMAC failed.",
        )
    return {"status": "verified", "shop": shop, "code": code}


@router.post("/connectors/shoplazza/connect", status_code=status.HTTP_201_CREATED)
async def connect_shoplazza(
    request: Request,
    payload: ShoplazzaConnectRequest,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN)),
) -> dict[str, str]:
    domain = payload.shop_domain.strip().lower()
    caid = await _register_channel_account(
        request,
        tenant_id=principal.tenant_id,
        channel_type="shoplazza",
        external_account_id=domain,
        display_name=domain,
        cred_type="oauth_token",
        cred_payload={"shop_domain": domain, "access_token": payload.access_token},
        rate_limit_profile={"app_store_qps": 2, "store_qps": 20, "warehouse_id": str(payload.warehouse_id or "")},
    )
    return {"channel_account_id": str(caid), "shop_domain": domain}


@router.post("/webhooks/shoplazza")
async def receive_shoplazza_webhook(
    request: Request,
    x_shoplazza_hmac_sha256: str | None = Header(default=None, alias="X-Shoplazza-Hmac-Sha256"),
    x_shoplazza_dedup_id: str | None = Header(default=None, alias="X-Shoplazza-Deduplication-ID"),
    x_shoplazza_topic: str = Header(default="", alias="X-Shoplazza-Topic"),
    x_shoplazza_shop_domain: str = Header(default="", alias="X-Shoplazza-Shop-Domain"),
) -> dict[str, object]:
    raw_body = await request.body()
    secret = os.getenv("SHOPLAZZA_CLIENT_SECRET", "")
    if not x_shoplazza_hmac_sha256 or not verify_shoplazza_webhook(
        raw_body, {"X-Shoplazza-Hmac-Sha256": x_shoplazza_hmac_sha256}, secret
    ):
        raise AppError(
            slug="invalid-webhook-signature",
            status_code=401,
            code="ERR_INVALID_WEBHOOK_SIGNATURE",
            title="Invalid Shoplazza Signature",
            detail="Webhook HMAC failed.",
        )
    event_id = x_shoplazza_dedup_id or hashlib.sha256(raw_body).hexdigest()
    fresh = await _persist_webhook_event(
        request,
        "shoplazza",
        event_id,
        raw_body,
        {"topic": x_shoplazza_topic, "shop_domain": x_shoplazza_shop_domain},
    )
    return {"status": "accepted", "deduplicated": not fresh}
```

Modify `apps/backend/src/erp_core/api_main.py` and `apps/backend/src/erp_core/worker_main.py`:

```python
# In apps/backend/src/erp_core/api_main.py
from erp_core.channels.ecommerce.connectors_router import router as ecommerce_connectors_router

app.include_router(ecommerce_connectors_router, prefix="/api/v1")

# In apps/backend/src/erp_core/worker_main.py
import erp_core.channels.ecommerce.connectors_router  # noqa: F401
```

- [ ] **Step 4: Run tests, export OpenAPI, and check import-linter**

Run:
```bash
cd apps/backend && uv run pytest tests/test_woo_shopline_shoplazza.py -v
cd apps/backend && uv run python scripts/export_openapi.py > ../../contracts/openapi.json
cd apps/backend && uv run lint-imports --config ../../infra/importlinter.ini
```
Expected: `test_woo_shopline_shoplazza.py` PASSES; `openapi.json` updated; `lint-imports` passes.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/channels/ecommerce/woocommerce.py apps/backend/src/erp_core/channels/ecommerce/shopline.py apps/backend/src/erp_core/channels/ecommerce/shoplazza.py apps/backend/src/erp_core/channels/ecommerce/connectors_router.py apps/backend/src/erp_core/api_main.py apps/backend/src/erp_core/worker_main.py apps/backend/tests/test_woo_shopline_shoplazza.py contracts/openapi.json
git commit -m "feat(connectors): add WooCommerce REST v3, SHOPLINE B2B & Shoplazza dual-HMAC connectors"
```

---

### Task 3.9: TikTok Shop Core Connector (Custom App / BYOK) & 15-Minute Reconciliation Cron

**Files:**
- Create: `apps/backend/src/erp_core/channels/ecommerce/tiktok_shop.py`
- Create: `apps/backend/src/erp_core/orders/reconciliation_worker.py`
- Modify: `apps/backend/src/erp_core/channels/ecommerce/connectors_router.py` (add `/connectors/tiktok/connect` and `/webhooks/tiktok`)
- Modify: `apps/backend/src/erp_core/worker_main.py` (import `erp_core.orders.reconciliation_worker`)
- Test: `apps/backend/tests/test_tiktok_and_reconciliation.py`

**Interfaces:**
- Consumes:
  - `erp_core.channels.ecommerce.base`: `CommercePlatformAdapter`, `CredentialVaultHelper`, `EcommerceOrderSyncService`, `NormalizedOrder`, `NormalizedProduct`
  - `erp_core.channels.ecommerce.shopify`: `ShopifyGraphqlAdapter`
  - `erp_core.channels.ecommerce.woocommerce`: `WooCommerceAdapter`
  - `erp_core.channels.ecommerce.shopline`: `ShoplineAdapter`
  - `erp_core.channels.ecommerce.shoplazza`: `ShoplazzaAdapter`
  - `erp_core.shared.db`: `tenant_session`, `system_session`
  - `erp_core.tenancy.audit`: `record_audit_event`
- Produces:
  - `TikTokShopCoreAdapter`, `sign_tiktok_request`, `verify_tiktok_webhook_signature`
  - `reconcile_channel_account_orders`, `reconcile_ecommerce_orders_cron`
  - HTTP endpoints: `/api/v1/connectors/tiktok/connect`, `/api/v1/webhooks/tiktok`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_tiktok_and_reconciliation.py`:

```python
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.catalog.schemas import ProductCreate, SkuMappingCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.channels.ecommerce.tiktok_shop import (
    TikTokShopCoreAdapter,
    sign_tiktok_request,
    verify_tiktok_webhook_signature,
)
from erp_core.orders.reconciliation_worker import reconcile_channel_account_orders
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, OrderMode, UserRole


def _migrator_url() -> str:
    return os.getenv(
        "DATABASE_MIGRATOR_URL",
        "postgresql+asyncpg://app_migrator:migrator_secret@localhost:5432/erp_saas",
    )


def _runtime_url() -> str:
    return os.getenv(
        "DATABASE_RUNTIME_URL",
        "postgresql+asyncpg://app_runtime:runtime_secret@localhost:6432/erp_saas",
    )


@pytest.mark.asyncio
async def test_tiktok_signing_webhook_order_import_and_15min_reconciliation_cron() -> None:
    app_key = "tt_app_key_001"
    app_secret = "tt_app_secret_xyz"
    shop_cipher = "GCP_CIPHER_01"

    # 1. Verify TTSPC API request signature (app_secret + path + sorted_query_kv + body + app_secret)
    computed = sign_tiktok_request(
        app_secret=app_secret,
        path="/order/202309/orders/search",
        query_params={"timestamp": "1763510000", "app_key": app_key, "shop_cipher": shop_cipher, "access_token": "ignored"},
        body_str='{"page_size":50}',
    )
    expected_msg = (
        f"{app_secret}/order/202309/orders/searchapp_key{app_key}shop_cipher{shop_cipher}timestamp1763510000"
        '{"page_size":50}'
        f"{app_secret}"
    )
    assert computed == hmac.new(app_secret.encode(), expected_msg.encode(), hashlib.sha256).hexdigest()

    # 2. Verify TTSPC webhook Authorization: HMAC-SHA256(app_key + raw_body, app_secret)
    wh_raw = b'{"type":1,"tts_notification_id":"tts_evt_771","shop_id":"7499911","data":{"order_id":"577001"}}'
    wh_sig = hmac.new(app_secret.encode(), app_key.encode() + wh_raw, hashlib.sha256).hexdigest()
    assert verify_tiktok_webhook_signature(wh_raw, {"Authorization": wh_sig}, app_key=app_key, app_secret=app_secret) is True
    assert verify_tiktok_webhook_signature(wh_raw, {"Authorization": "bad_sig"}, app_key=app_key, app_secret=app_secret) is False

    # 3. Full DB test: missed webhook order recovered idempotently by reconciliation cron
    mig_engine = create_async_engine(_migrator_url(), echo=False)
    rt_engine = create_async_engine(_runtime_url(), echo=False)
    rt_factory = async_sessionmaker(rt_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_id = uuid4()
    channel_account_id = uuid4()
    warehouse_id = uuid4()
    shipped_calls: list[dict] = []

    def _tt_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-tts-access-token") == "tts_live_tok"
        assert "sign" in request.url.params
        if request.url.path == "/order/202309/orders/search":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "orders": [{"id": "TT-ORD-88001"}],
                        "next_page_token": "",
                    },
                },
            )
        if request.url.path == "/order/202309/orders":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "orders": [
                            {
                                "id": "TT-ORD-88001",
                                "status": "AWAITING_SHIPMENT",
                                "update_time": 1763510500,
                                "buyer_email": "tt.shopper@example.com",
                                "user_id": "tt_buyer_991",
                                "recipient_address": {"name": "Mia Torres", "phone_number": "+14155550199", "region_code": "US"},
                                "payment": {
                                    "currency": "USD",
                                    "sub_total": "45.00",
                                    "shipping_fee": "5.00",
                                    "tax": "0.00",
                                    "total_amount": "50.00",
                                    "platform_discount": "0.00",
                                },
                                "line_items": [
                                    {"id": "li_1", "seller_sku": "TT-LED-5M-1PK", "product_name": "LED Strip", "sale_price": "45.00"}
                                ],
                                "packages": [{"id": "pkg_551"}],
                            }
                        ]
                    },
                },
            )
        if request.url.path == "/fulfillment/202309/packages/pkg_551/ship":
            shipped_calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    try:
        async with mig_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tenant_id, "name": "QZ TikTok Shop", "slug": f"tt-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO channel_accounts (
                        id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile
                    ) VALUES (
                        :id, :tid, 'tiktok_shop', '7499911', 'QZ US TikTok Shop', 'active', '{}'::jsonb
                    )
                    """
                ),
                {"id": channel_account_id, "tid": tenant_id},
            )

        catalog_svc = CatalogService()
        async with tenant_session(rt_factory, tenant_id) as session:
            prod = await catalog_svc.create_product(
                session, tenant_id, actor_id="seed", payload=ProductCreate(spu_code="SPU-TT-5M", category="led")
            )
            variant = await catalog_svc.create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=prod.id,
                    sku="LED-STRIP-5M-IP65",
                    cost_price_base=Decimal("32.0000"),
                    retail_price_usd=Decimal("45.00"),
                    weight_grams=300,
                ),
            )
            await catalog_svc.create_sku_mapping(
                session,
                tenant_id,
                actor_id="seed",
                payload=SkuMappingCreate(
                    variant_id=variant.id,
                    channel_account_id=channel_account_id,
                    external_product_id="tt_prod_1",
                    external_sku="TT-LED-5M-1PK",
                    bundle_multiplier=1,
                ),
            )
            await session.execute(
                text(
                    """
                    INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty)
                    VALUES (:tid, :wid, :vid, 15, 0)
                    """
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(_tt_handler)) as client:
            adapter = TikTokShopCoreAdapter(
                app_key=app_key,
                app_secret=app_secret,
                access_token="tts_live_tok",
                shop_cipher=shop_cipher,
                app_mode="seller_byok",
                http_client=client,
            )

            async with tenant_session(rt_factory, tenant_id) as session:
                # 1st reconciliation run catches the missed webhook order
                stats_1 = await reconcile_channel_account_orders(
                    session,
                    tenant_id=tenant_id,
                    channel_account_id=channel_account_id,
                    channel_type=ChannelType.TIKTOK_SHOP,
                    adapter=adapter,
                    default_warehouse_id=warehouse_id,
                    since=datetime.now(timezone.utc) - timedelta(minutes=30),
                )
                assert stats_1["recovered"] == 1
                assert stats_1["updated"] == 0

                # 2nd reconciliation run is 100% idempotent (0 new reservations!)
                stats_2 = await reconcile_channel_account_orders(
                    session,
                    tenant_id=tenant_id,
                    channel_account_id=channel_account_id,
                    channel_type=ChannelType.TIKTOK_SHOP,
                    adapter=adapter,
                    default_warehouse_id=warehouse_id,
                    since=datetime.now(timezone.utc) - timedelta(minutes=30),
                )
                assert stats_2["recovered"] == 0
                assert stats_2["updated"] == 1

                bal = (
                    await session.execute(
                        text(
                            "SELECT reserved_qty FROM inventory_balances WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid"
                        ),
                        {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
                    )
                ).one()
                assert bal.reserved_qty == 1

                ord_row = (
                    await session.execute(
                        text("SELECT order_mode, total_amount FROM orders WHERE tenant_id = :tid AND external_order_id = 'TT-ORD-88001'"),
                        {"tid": tenant_id},
                    )
                ).one()
                assert ord_row.order_mode == OrderMode.B2C_RETAIL.value
                assert Decimal(str(ord_row.total_amount)) == Decimal("50.00")

                await adapter.create_fulfillment(
                    external_order_id="TT-ORD-88001",
                    fulfillment_order_id="pkg_551",
                    tracking_number="USPS-TT-9900",
                    carrier_company="7117021",
                )
                assert len(shipped_calls) == 1
                assert shipped_calls[0]["tracking_number"] == "USPS-TT-9900"
    finally:
        async with mig_engine.begin() as conn:
            await conn.execute(text("DELETE FROM order_profit_snapshots WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM receivables_payables WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM inventory_ledger WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM inventory_balances WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM order_items WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM orders WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM platform_sku_mappings WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM product_variants WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM products WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM customer_identities WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM channel_accounts WHERE tenant_id = :tid"), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await mig_engine.dispose()
        await rt_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_tiktok_and_reconciliation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.channels.ecommerce.tiktok_shop'`

- [ ] **Step 3: Write `tiktok_shop.py`, `reconciliation_worker.py`, update `connectors_router.py` & `worker_main.py`**

Create `apps/backend/src/erp_core/channels/ecommerce/tiktok_shop.py`:

```python
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    CredentialVaultHelper,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    NormalizedProduct,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

TIKTOK_API_BASE = "https://open-api.tiktokglobalshop.com"
TIKTOK_AUTH_BASE = "https://auth.tiktok-shops.com"


def sign_tiktok_request(
    app_secret: str,
    path: str,
    query_params: Mapping[str, str],
    body_str: str = "",
) -> str:
    """TTSPC v202309+ request signature: HMAC-SHA256(app_secret, app_secret + path + sorted_kv + body + app_secret)."""
    filtered = sorted((k, str(v)) for k, v in query_params.items() if k not in {"sign", "access_token"})
    kv_str = "".join(f"{k}{v}" for k, v in filtered)
    message = f"{app_secret}{path}{kv_str}{body_str}{app_secret}"
    return hmac.new(app_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_tiktok_webhook_signature(
    raw_body: bytes,
    headers: Mapping[str, str],
    app_key: str,
    app_secret: str,
) -> bool:
    """Verify TTSPC webhook Authorization header = HMAC-SHA256(app_key + raw_body, app_secret) hex."""
    auth_header = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth_header or not app_key or not app_secret:
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"),
        app_key.encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, auth_header.strip().lower())


async def enforce_custom_app_shop_limit(session: AsyncSession, app_mode: str, app_key: str) -> None:
    """Enforce TikTok Shop <25 authorized shops threshold for ISV Custom Apps (seller_byok is exempt)."""
    if app_mode == "seller_byok":
        return
    count = int(
        (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM channel_accounts
                    WHERE channel_type = 'tiktok_shop'
                      AND status = 'active'
                      AND rate_limit_profile->>'app_key' = :akey
                      AND rate_limit_profile->>'app_mode' = 'custom_isv'
                    """
                ),
                {"akey": app_key},
            )
        ).scalar_one()
    )
    if count >= 25:
        raise AppError(
            slug="tiktok-custom-app-shop-limit",
            status_code=403,
            code="ERR_TIKTOK_CUSTOM_APP_SHOP_LIMIT",
            title="TikTok Custom App Shop Cap Reached",
            detail="ISV Custom App is capped at <25 authorized shops; use Public App or merchant TikTok Shop Seller BYOK.",
        )


class TikTokShopCoreAdapter(CommercePlatformAdapter):
    """TikTok Shop Core Orders & Fulfillment connector supporting Custom App (<25 shops) & Seller BYOK."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        access_token: str,
        shop_cipher: str,
        refresh_token: str = "",
        expires_at: datetime | None = None,
        app_mode: str = "custom_isv",
        http_client: httpx.AsyncClient | None = None,
        tenant_id: UUID | None = None,
        channel_account_id: UUID | None = None,
        vault: CredentialVaultHelper | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token
        self.shop_cipher = shop_cipher
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.app_mode = app_mode
        self._http_client = http_client
        self._tenant_id = tenant_id
        self._channel_account_id = channel_account_id
        self._vault = vault
        self._session = session

    @classmethod
    async def from_channel_account(
        cls,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        crypto: EnvelopeCryptoService | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> TikTokShopCoreAdapter:
        vault = CredentialVaultHelper(crypto)
        data, expires_at = await vault.load_secret_json(session, tenant_id, channel_account_id, "oauth_token")
        return cls(
            app_key=data.get("app_key") or os.getenv("TIKTOK_SHOP_APP_KEY", ""),
            app_secret=data.get("app_secret") or os.getenv("TIKTOK_SHOP_APP_SECRET", ""),
            access_token=data["access_token"],
            shop_cipher=data["shop_cipher"],
            refresh_token=data.get("refresh_token", ""),
            expires_at=expires_at,
            app_mode=data.get("app_mode", "custom_isv"),
            http_client=http_client,
            tenant_id=tenant_id,
            channel_account_id=channel_account_id,
            vault=vault,
            session=session,
        )

    async def _signed_request(
        self,
        method: str,
        path: str,
        extra_params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(4):
            ts = str(int(time.time()))
            query_params: dict[str, str] = {
                "app_key": self.app_key,
                "shop_cipher": self.shop_cipher,
                "timestamp": ts,
            }
            if extra_params:
                query_params.update(extra_params)
            body_str = json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""
            query_params["sign"] = sign_tiktok_request(self.app_secret, path, query_params, body_str)

            url = f"{TIKTOK_API_BASE}{path}"
            headers = {"x-tts-access-token": self.access_token, "Content-Type": "application/json"}
            if self._http_client is not None:
                resp = await self._http_client.request(
                    method, url, params=query_params, content=body_str or None, headers=headers
                )
            else:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.request(
                        method, url, params=query_params, content=body_str or None, headers=headers
                    )

            if resp.status_code == 429:
                await asyncio.sleep(min(2.0**attempt, 8.0))
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 36009002:
                await asyncio.sleep(min(2.0**attempt, 8.0))
                continue
            if data.get("code", 0) != 0:
                raise AppError(
                    slug="tiktok-api-error",
                    status_code=502,
                    code="ERR_TIKTOK_API",
                    title="TikTok Shop API Error",
                    detail=f"TikTok code={data.get('code')}: {data.get('message')}",
                )
            return data.get("data") or {}

        raise AppError(
            slug="tiktok-rate-limit",
            status_code=429,
            code="ERR_TIKTOK_RATE_LIMIT",
            title="TikTok Rate Limited",
            detail="Exceeded TikTok QPS limit.",
        )

    async def fetch_orders_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedOrder], str | None]:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        since_ts = int(since.timestamp())
        extra = {"page_size": "50"}
        if cursor:
            extra["page_token"] = cursor
        search_data = await self._signed_request(
            "POST",
            "/order/202309/orders/search",
            extra_params=extra,
            json_body={"update_time_ge": since_ts, "update_time_lt": now_ts},
        )
        summaries = search_data.get("orders") or []
        order_ids = [str(o["id"]) for o in summaries if o.get("id")]
        if not order_ids:
            return [], None

        detail_data = await self._signed_request(
            "GET",
            "/order/202309/orders",
            extra_params={"ids": ",".join(order_ids)},
        )
        normalized: list[NormalizedOrder] = []
        for raw in detail_data.get("orders") or []:
            pay = raw.get("payment") or {}
            recipient = raw.get("recipient_address") or {}
            sku_counts: Counter[str] = Counter()
            sku_totals: dict[str, Decimal] = {}
            sku_names: dict[str, str] = {}
            for li in raw.get("line_items") or []:
                sku = str(li.get("seller_sku") or "").strip()
                if not sku:
                    continue
                sku_counts[sku] += int(li.get("quantity", 1))
                sku_totals[sku] = sku_totals.get(sku, Decimal("0.00")) + Decimal(str(li.get("sale_price", "0.00")))
                sku_names[sku] = str(li.get("product_name") or sku)

            line_items = [
                NormalizedLineItem(
                    external_sku=sku,
                    title=sku_names[sku],
                    quantity=qty,
                    line_total=sku_totals[sku],
                )
                for sku, qty in sku_counts.items()
            ]
            if not line_items:
                continue

            tt_status = str(raw.get("status") or "").upper()
            if tt_status in {"IN_TRANSIT", "DELIVERED", "COMPLETED"}:
                ful_status = FulfillmentStatus.DELIVERED if tt_status in {"DELIVERED", "COMPLETED"} else FulfillmentStatus.SHIPPED
                pay_status = PaymentStatus.PAID
            elif tt_status == "CANCELLED":
                ful_status = FulfillmentStatus.CANCELLED
                pay_status = PaymentStatus.REFUNDED
            elif tt_status == "UNPAID":
                ful_status = FulfillmentStatus.PENDING_PAYMENT
                pay_status = PaymentStatus.UNPAID
            else:
                ful_status = FulfillmentStatus.CONFIRMED_RESERVED
                pay_status = PaymentStatus.PAID

            upd_ts = int(raw.get("update_time") or now_ts)
            package_ids = [str(p["id"]) for p in (raw.get("packages") or []) if p.get("id")]
            normalized.append(
                NormalizedOrder(
                    external_order_id=str(raw["id"]),
                    external_order_number=str(raw["id"]),
                    order_mode=OrderMode.B2C_RETAIL,
                    currency=str(pay.get("currency") or "USD").upper(),
                    subtotal_amount=Decimal(str(pay.get("sub_total", "0.00"))),
                    shipping_charged=Decimal(str(pay.get("shipping_fee", "0.00"))),
                    discount_amount=Decimal(str(pay.get("platform_discount", "0.00"))),
                    tax_collected=Decimal(str(pay.get("tax", "0.00"))),
                    total_amount=Decimal(str(pay.get("total_amount", "0.00"))),
                    fulfillment_status=ful_status,
                    payment_status=pay_status,
                    customer=NormalizedCustomer(
                        email=raw.get("buyer_email"),
                        phone_e164=recipient.get("phone_number"),
                        external_customer_id=str(raw.get("user_id") or raw["id"]),
                        external_identity_type=IdentityType.TIKTOK_BUYER_ID,
                        display_name=str(recipient.get("name") or "TikTok Shop Buyer"),
                        country_code=recipient.get("region_code"),
                    ),
                    line_items=line_items,
                    channel_metadata={"tiktok_status": tt_status, "package_ids": package_ids, "app_mode": self.app_mode},
                    updated_at=datetime.fromtimestamp(upd_ts, tz=timezone.utc),
                )
            )
        next_token = search_data.get("next_page_token") or None
        return normalized, next_token

    async def fetch_products_updated_since(
        self, since: datetime, cursor: str | None = None
    ) -> tuple[list[NormalizedProduct], str | None]:
        extra = {"page_size": "50"}
        if cursor:
            extra["page_token"] = cursor
        data = await self._signed_request(
            "POST",
            "/product/202309/products/search",
            extra_params=extra,
            json_body={"update_time_ge": int(since.timestamp())},
        )
        prods = [
            NormalizedProduct(
                external_product_id=str(p["id"]),
                title=str(p.get("title") or ""),
                variants=p.get("skus") or [],
            )
            for p in data.get("products") or []
        ]
        return prods, (data.get("next_page_token") or None)

    async def create_fulfillment(
        self,
        external_order_id: str,
        tracking_number: str,
        carrier_company: str,
        tracking_url: str | None = None,
        fulfillment_order_id: str | None = None,
        notify_customer: bool = True,
    ) -> dict[str, Any]:
        package_id = fulfillment_order_id or external_order_id
        return await self._signed_request(
            "POST",
            f"/fulfillment/202309/packages/{package_id}/ship",
            json_body={
                "handover_method": "DROP_OFF",
                "self_shipment": {
                    "tracking_number": tracking_number,
                    "shipping_provider_id": carrier_company,
                },
            },
        )

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool:
        return verify_tiktok_webhook_signature(raw_body, headers, app_key=self.app_key, app_secret=secret)
```

Create `apps/backend/src/erp_core/orders/reconciliation_worker.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.ecommerce.base import CommercePlatformAdapter, EcommerceOrderSyncService
from erp_core.channels.ecommerce.shopify import ShopifyGraphqlAdapter
from erp_core.channels.ecommerce.shoplazza import ShoplazzaAdapter
from erp_core.channels.ecommerce.shopline import ShoplineAdapter
from erp_core.channels.ecommerce.tiktok_shop import TikTokShopCoreAdapter
from erp_core.channels.ecommerce.woocommerce import WooCommerceAdapter
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import ChannelType
from erp_core.tenancy.audit import record_audit_event
from erp_core.worker_main import broker

ECOMMERCE_CHANNEL_TYPES = (
    ChannelType.SHOPIFY.value,
    ChannelType.WOOCOMMERCE.value,
    ChannelType.SHOPLINE.value,
    ChannelType.SHOPLAZZA.value,
    ChannelType.TIKTOK_SHOP.value,
)


async def reconcile_channel_account_orders(
    session: AsyncSession,
    tenant_id: UUID,
    channel_account_id: UUID,
    channel_type: ChannelType,
    adapter: CommercePlatformAdapter,
    default_warehouse_id: UUID,
    since: datetime,
    sync_service: EcommerceOrderSyncService | None = None,
) -> dict[str, int]:
    """Poll updated_at window from platform adapter and idempotently ingest any missed webhook orders."""
    svc = sync_service or EcommerceOrderSyncService()
    cursor: str | None = None
    recovered = 0
    updated = 0

    while True:
        orders, next_cursor = await adapter.fetch_orders_updated_since(since, cursor=cursor)
        for order_dto in orders:
            _, created = await svc.ingest_normalized_order(
                session,
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                default_warehouse_id=default_warehouse_id,
                order_dto=order_dto,
                actor_id=f"reconcile_cron:{channel_type.value}",
            )
            if created:
                recovered += 1
            else:
                updated += 1
        if not next_cursor:
            break
        cursor = next_cursor

    await record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_type="system_cron",
        actor_id="ecommerce_reconciliation_15m",
        action="reconciliation.completed",
        resource_type="channel_account",
        resource_id=channel_account_id,
        after_state={"channel_type": channel_type.value, "recovered": recovered, "updated": updated},
    )
    return {"recovered": recovered, "updated": updated}


async def _instantiate_adapter(
    session: AsyncSession,
    tenant_id: UUID,
    channel_account_id: UUID,
    channel_type: ChannelType,
    crypto: EnvelopeCryptoService,
    redis_client,
) -> CommercePlatformAdapter:
    if channel_type == ChannelType.SHOPIFY:
        return await ShopifyGraphqlAdapter.from_channel_account(session, tenant_id, channel_account_id, crypto=crypto)
    if channel_type == ChannelType.WOOCOMMERCE:
        return await WooCommerceAdapter.from_channel_account(session, tenant_id, channel_account_id, crypto=crypto)
    if channel_type == ChannelType.SHOPLINE:
        return await ShoplineAdapter.from_channel_account(
            session, tenant_id, channel_account_id, crypto=crypto, redis_client=redis_client
        )
    if channel_type == ChannelType.SHOPLAZZA:
        return await ShoplazzaAdapter.from_channel_account(
            session, tenant_id, channel_account_id, crypto=crypto, redis_client=redis_client
        )
    if channel_type == ChannelType.TIKTOK_SHOP:
        return await TikTokShopCoreAdapter.from_channel_account(session, tenant_id, channel_account_id, crypto=crypto)
    raise ValueError(f"Unsupported e-commerce reconciliation channel: {channel_type}")


@broker.task(schedule=[{"cron": "*/15 * * * *"}])
async def reconcile_ecommerce_orders_cron() -> dict[str, int]:
    from erp_core.shared.db import create_runtime_engine, create_session_factory
    from erp_core.shared.redis_client import create_redis_client

    settings = get_settings()
    engine = create_runtime_engine(settings)
    session_factory = create_session_factory(engine)
    redis_client = create_redis_client(settings)
    crypto = EnvelopeCryptoService(settings)
    since = datetime.now(timezone.utc) - timedelta(minutes=30)

    try:
        async with system_session(session_factory) as sys_sess:
            accounts = (
                await sys_sess.execute(
                    text(
                        """
                        SELECT id, tenant_id, channel_type, rate_limit_profile
                        FROM channel_accounts
                        WHERE status = 'active' AND channel_type = ANY(:ctypes)
                        """
                    ),
                    {"ctypes": list(ECOMMERCE_CHANNEL_TYPES)},
                )
            ).all()

        total_recovered = 0
        total_updated = 0
        for acct in accounts:
            ctype = ChannelType(acct.channel_type)
            wh_raw = (acct.rate_limit_profile or {}).get("warehouse_id")
            warehouse_id = UUID(wh_raw) if wh_raw else acct.id
            async with tenant_session(session_factory, acct.tenant_id) as t_sess:
                adapter = await _instantiate_adapter(t_sess, acct.tenant_id, acct.id, ctype, crypto, redis_client)
                stats = await reconcile_channel_account_orders(
                    t_sess,
                    tenant_id=acct.tenant_id,
                    channel_account_id=acct.id,
                    channel_type=ctype,
                    adapter=adapter,
                    default_warehouse_id=warehouse_id,
                    since=since,
                )
                total_recovered += stats["recovered"]
                total_updated += stats["updated"]
        return {"recovered": total_recovered, "updated": total_updated}
    finally:
        await redis_client.aclose()
        await engine.dispose()
```

Modify `apps/backend/src/erp_core/channels/ecommerce/connectors_router.py` to add TikTok Shop connection & webhook routes:

```python
from erp_core.channels.ecommerce.tiktok_shop import enforce_custom_app_shop_limit, verify_tiktok_webhook_signature


class TikTokConnectRequest(BaseModel):
    shop_id: str
    shop_cipher: str
    access_token: str = Field(min_length=8)
    refresh_token: str = ""
    app_mode: str = Field(default="custom_isv", pattern="^(custom_isv|seller_byok)$")
    app_key: str | None = None
    app_secret: str | None = None
    warehouse_id: UUID | None = None


@router.post("/connectors/tiktok/connect", status_code=status.HTTP_201_CREATED)
async def connect_tiktok_shop(
    request: Request,
    payload: TikTokConnectRequest,
    principal: CurrentPrincipal = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN)),
) -> dict[str, str]:
    app_key = payload.app_key or os.getenv("TIKTOK_SHOP_APP_KEY", "")
    app_secret = payload.app_secret or os.getenv("TIKTOK_SHOP_APP_SECRET", "")
    async with system_session(_get_session_factory(request)) as sys_sess:
        await enforce_custom_app_shop_limit(sys_sess, payload.app_mode, app_key)

    caid = await _register_channel_account(
        request,
        tenant_id=principal.tenant_id,
        channel_type="tiktok_shop",
        external_account_id=payload.shop_id.strip(),
        display_name=f"TikTok Shop {payload.shop_id.strip()}",
        cred_type="oauth_token",
        cred_payload={
            "app_key": app_key,
            "app_secret": app_secret,
            "access_token": payload.access_token,
            "refresh_token": payload.refresh_token,
            "shop_cipher": payload.shop_cipher,
            "app_mode": payload.app_mode,
        },
        rate_limit_profile={
            "app_mode": payload.app_mode,
            "app_key": app_key,
            "warehouse_id": str(payload.warehouse_id or ""),
        },
    )
    return {"channel_account_id": str(caid), "shop_id": payload.shop_id.strip(), "app_mode": payload.app_mode}


@router.post("/webhooks/tiktok")
async def receive_tiktok_webhook(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, object]:
    raw_body = await request.body()
    app_key = os.getenv("TIKTOK_SHOP_APP_KEY", "")
    app_secret = os.getenv("TIKTOK_SHOP_APP_SECRET", "")
    if not authorization or not verify_tiktok_webhook_signature(
        raw_body, {"Authorization": authorization}, app_key=app_key, app_secret=app_secret
    ):
        raise AppError(
            slug="invalid-webhook-signature",
            status_code=401,
            code="ERR_INVALID_WEBHOOK_SIGNATURE",
            title="Invalid TikTok Signature",
            detail="Authorization HMAC failed.",
        )
    body = json.loads(raw_body.decode("utf-8"))
    event_id = str(body.get("tts_notification_id") or hashlib.sha256(raw_body).hexdigest())
    fresh = await _persist_webhook_event(
        request, "tiktok_shop", event_id, raw_body, {"shop_id": str(body.get("shop_id") or "")}
    )
    return {"status": "accepted", "deduplicated": not fresh}
```

Modify `apps/backend/src/erp_core/worker_main.py`:

```python
import erp_core.orders.reconciliation_worker  # noqa: F401
```

- [ ] **Step 4: Run TikTok & reconciliation tests, export OpenAPI, and run full Plan 03 verification**

Run:
```bash
cd apps/backend && uv run pytest tests/test_tiktok_and_reconciliation.py -v
cd apps/backend && uv run pytest tests/test_erp_migration.py tests/test_catalog_service.py tests/test_b2b_quotes_and_pi.py tests/test_inventory_concurrency.py tests/test_17track_adapter.py tests/test_finance_cvp.py tests/test_shopify_connector.py tests/test_woo_shopline_shoplazza.py tests/test_tiktok_and_reconciliation.py -v
cd apps/backend && uv run python scripts/export_openapi.py > ../../contracts/openapi.json
cd apps/backend && uv run lint-imports --config ../../infra/importlinter.ini
```
Expected: All 9 test suites PASS; `openapi.json` includes all Plan 03 endpoints; `lint-imports` passes with zero cross-module `.models` boundary violations.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/channels/ecommerce/tiktok_shop.py apps/backend/src/erp_core/channels/ecommerce/connectors_router.py apps/backend/src/erp_core/orders/reconciliation_worker.py apps/backend/src/erp_core/worker_main.py apps/backend/tests/test_tiktok_and_reconciliation.py contracts/openapi.json
git commit -m "feat(tiktok,reconcile): add TikTok Shop Core BYOK/Custom App connector & 15m reconciliation cron"
```
