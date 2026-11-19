# Omnichannel Inbox & P0 Channels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the zero-message-loss omnichannel webhook pipeline, Redis Lua dual token bucket rate limiter, unified customer identity graph (`bsuid`, deterministic auto-linking, `pg_trgm` L1 merge proposals, reversible splits, buyer local timezone), P0 messaging channels (WhatsApp Cloud + Coexistence, Zero-CASA Email + Made-in-China/Alibaba B2B inquiry parser, `< 35KB` Shadow DOM Web LiveChat Widget), and Unified Inbox REST & SSE API.

**Architecture:** `erp_core.channels` and `erp_core.identity` operate as strictly isolated Modular Monolith packages inside `apps/backend` using PostgreSQL 17 `FORCE ROW LEVEL SECURITY` behind PgBouncer transaction pooling (`set_config(..., is_local=true)`). Inbound webhooks authenticate raw-body HMAC signatures, deduplicate into `raw_webhook_events` (`ON CONFLICT DO NOTHING`), stream to Taskiq workers in `< 100ms`, resolve cross-channel identities without silent probabilistic merging, enforce Meta 2026-10 pricing/window/suppression guardrails, and fan out live UI updates over tenant-scoped Redis Pub/Sub SSE streams.

**Tech Stack:** `Python >=3.12`, `uv`, `FastAPI >=0.115`, `Pydantic v2`, `SQLAlchemy 2.0 (asyncpg)`, `Alembic`, `Taskiq + taskiq-redis`, `Redis 7+` (Streams, Lua scripting, Pub/Sub), `httpx`, `nh3`, `phonenumbers`, `PyJWT`, `boto3`, `pytest + pytest-asyncio`, `Node >=22`, `pnpm >=9`, `Vite` + `TypeScript` (Shadow DOM WebChat Widget).

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

### Task 2.1: Alembic Migration `0002_channels_identity_inbox.py` & SQLAlchemy Models

**Files:**
- Create: `apps/backend/alembic/versions/0002_channels_identity_inbox.py`
- Create: `apps/backend/src/erp_core/channels/__init__.py`
- Create: `apps/backend/src/erp_core/channels/models.py`
- Create: `apps/backend/src/erp_core/identity/__init__.py`
- Create: `apps/backend/src/erp_core/identity/models.py`
- Modify: `apps/backend/alembic/env.py`
- Test: `apps/backend/tests/test_channels_migration_rls.py`

**Interfaces:**
- Consumes: `erp_core.shared.db` (`Base`, `tenant_session`, `system_session`), `erp_core.shared.enums` (`ChannelType`, `ChannelStatus`, `ConversationMode`, `ConversationStatus`, `MessageDirection`, `MessageSenderType`, `MessageContentType`, `DeliveryStatus`, `IdentityType`, `ConfidenceLevel`, `CustomerType`, `AutonomyLevel`, `Incoterm`), Plan 01 `tenants` table and `app_runtime` role.
- Produces: Alembic revision `0002_channels_identity_inbox` creating 12 tables (`channel_routing_directory`, `channel_accounts`, `raw_webhook_events`, `dead_letter_events`, `b2b_companies`, `customers`, `customer_identities`, `identity_merge_events`, `conversations`, `messages`, `consent_records`, `suppression_list`), `pg_trgm` extension, `SECURITY DEFINER` function `resolve_tenant_by_channel_external_id(TEXT, TEXT)`, partial unique index `uq_messages_idempotency`, and ORM models in `erp_core.channels.models` and `erp_core.identity.models`.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_channels_migration_rls.py`:

```python
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.shared.db import tenant_session
from erp_core.shared.enums import (
    ChannelStatus,
    ChannelType,
    ConfidenceLevel,
    ConversationMode,
    ConversationStatus,
    CustomerType,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)


def _runtime_url() -> str:
    return os.environ["DATABASE_RUNTIME_URL"]


def _migrator_url() -> str:
    return os.environ["DATABASE_MIGRATOR_URL"]


@pytest.mark.asyncio
async def test_migration_0002_objects_exist() -> None:
    engine = create_async_engine(_migrator_url(), future=True)
    try:
        async with engine.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0002_channels_identity_inbox"

            tables = set(
                (
                    await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                        )
                    )
                ).scalars()
            )
            expected = {
                "channel_routing_directory",
                "channel_accounts",
                "raw_webhook_events",
                "dead_letter_events",
                "b2b_companies",
                "customers",
                "customer_identities",
                "identity_merge_events",
                "conversations",
                "messages",
                "consent_records",
                "suppression_list",
            }
            assert expected.issubset(tables)

            fn_exists = (
                await conn.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_proc WHERE proname = 'resolve_tenant_by_channel_external_id'"
                        ")"
                    )
                )
            ).scalar_one()
            assert fn_exists is True

            trgm_exists = (
                await conn.execute(
                    text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')")
                )
            ).scalar_one()
            assert trgm_exists is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_messages_idempotency_and_rls_isolation() -> None:
    migrator_engine = create_async_engine(_migrator_url(), future=True)
    runtime_engine = create_async_engine(_runtime_url(), future=True)
    runtime_factory = async_sessionmaker(runtime_engine, class_=AsyncSession, expire_on_commit=False)

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    account_a = uuid.uuid4()
    customer_a = uuid.uuid4()
    conv_a = uuid.uuid4()

    try:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) VALUES "
                    "(:ta, :sa, 'Tenant A'), (:tb, :sb, 'Tenant B')"
                ),
                {
                    "ta": tenant_a,
                    "sa": f"tenant-a-{tenant_a.hex[:8]}",
                    "tb": tenant_b,
                    "sb": f"tenant-b-{tenant_b.hex[:8]}",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_routing_directory "
                    "(channel_type, external_account_id, tenant_id, channel_account_id) "
                    "VALUES (:ctype, :ext_id, :tid, :aid)"
                ),
                {
                    "ctype": ChannelType.WHATSAPP_CLOUD.value,
                    "ext_id": f"pn_{account_a.hex[:12]}",
                    "tid": tenant_a,
                    "aid": account_a,
                },
            )

        # 1. Fail-closed check: runtime query without tenant context must see 0 rows and block writes
        async with runtime_factory() as bare_session:
            async with bare_session.begin():
                rows = (await bare_session.execute(text("SELECT id FROM customers"))).all()
                assert rows == []
                resolved = (
                    await bare_session.execute(
                        text(
                            "SELECT tenant_id, channel_account_id "
                            "FROM resolve_tenant_by_channel_external_id(:ctype, :ext_id)"
                        ),
                        {
                            "ctype": ChannelType.WHATSAPP_CLOUD.value,
                            "ext_id": f"pn_{account_a.hex[:12]}",
                        },
                    )
                ).first()
                assert resolved is not None
                assert resolved[0] == tenant_a
                assert resolved[1] == account_a

            with pytest.raises(DBAPIError):
                async with bare_session.begin():
                    await bare_session.execute(
                        text(
                            "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                            "VALUES (:id, :tid, :ctype, 'Unauthorized')"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "tid": tenant_a,
                            "ctype": CustomerType.B2C_CONSUMER.value,
                        },
                    )

        # 2. Tenant A writes channel_account, customer, identity, conversation, and message
        async with tenant_session(runtime_factory, tenant_a) as sess_a:
            await sess_a.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile) "
                    "VALUES (:id, :tid, :ctype, :ext_id, 'WA Main', :status, '{\"mps\": 80, \"coexistence\": false}'::jsonb)"
                ),
                {
                    "id": account_a,
                    "tid": tenant_a,
                    "ctype": ChannelType.WHATSAPP_CLOUD.value,
                    "ext_id": f"pn_{account_a.hex[:12]}",
                    "status": ChannelStatus.ACTIVE.value,
                },
            )
            await sess_a.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, customer_type, display_name, primary_email) "
                    "VALUES (:id, :tid, :ctype, 'Alice Buyer', 'alice@example.com')"
                ),
                {
                    "id": customer_a,
                    "tid": tenant_a,
                    "ctype": CustomerType.B2C_CONSUMER.value,
                },
            )
            await sess_a.execute(
                text(
                    "INSERT INTO customer_identities "
                    "(id, tenant_id, customer_id, identity_type, identity_value, confidence_level) "
                    "VALUES (:id, :tid, :cid, :itype, 'alice@example.com', :conf)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tenant_a,
                    "cid": customer_a,
                    "itype": IdentityType.EMAIL.value,
                    "conf": ConfidenceLevel.DETERMINISTIC.value,
                },
            )
            await sess_a.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, tenant_id, customer_id, channel_account_id, channel_type, conversation_mode, status) "
                    "VALUES (:id, :tid, :cid, :aid, :ctype, :mode, :status)"
                ),
                {
                    "id": conv_a,
                    "tid": tenant_a,
                    "cid": customer_a,
                    "aid": account_a,
                    "ctype": ChannelType.WHATSAPP_CLOUD.value,
                    "mode": ConversationMode.B2C_SUPPORT.value,
                    "status": ConversationStatus.OPEN.value,
                },
            )
            now = datetime.now(timezone.utc)
            await sess_a.execute(
                text(
                    "INSERT INTO messages "
                    "(id, tenant_id, conversation_id, customer_id, channel_type, external_message_id, "
                    "direction, sender_type, content_type, body_text, delivery_status, source_timestamp) "
                    "VALUES (:id, :tid, :conv_id, :cid, :ctype, 'wamid.dedup.001', "
                    ":dir, :stype, :cont, 'Hello', :dstat, :ts)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tenant_a,
                    "conv_id": conv_a,
                    "cid": customer_a,
                    "ctype": ChannelType.WHATSAPP_CLOUD.value,
                    "dir": MessageDirection.INBOUND.value,
                    "stype": MessageSenderType.CUSTOMER.value,
                    "cont": MessageContentType.TEXT.value,
                    "dstat": DeliveryStatus.DELIVERED.value,
                    "ts": now,
                },
            )
            # NULL external_message_id internal notes must NOT collide
            for note_idx in range(2):
                await sess_a.execute(
                    text(
                        "INSERT INTO messages "
                        "(id, tenant_id, conversation_id, customer_id, channel_type, external_message_id, "
                        "direction, sender_type, content_type, body_text, delivery_status, source_timestamp) "
                        "VALUES (:id, :tid, :conv_id, :cid, :ctype, NULL, "
                        ":dir, :stype, :cont, :body, :dstat, :ts)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": tenant_a,
                        "conv_id": conv_a,
                        "cid": customer_a,
                        "ctype": ChannelType.WHATSAPP_CLOUD.value,
                        "dir": MessageDirection.INTERNAL_NOTE.value,
                        "stype": MessageSenderType.HUMAN_AGENT.value,
                        "cont": MessageContentType.TEXT.value,
                        "body": f"Internal note {note_idx}",
                        "dstat": DeliveryStatus.DELIVERED.value,
                        "ts": now,
                    },
                )

        # 3. Duplicate external_message_id in same tenant/channel must violate uq_messages_idempotency
        with pytest.raises(IntegrityError):
            async with tenant_session(runtime_factory, tenant_a) as sess_dup:
                await sess_dup.execute(
                    text(
                        "INSERT INTO messages "
                        "(id, tenant_id, conversation_id, customer_id, channel_type, external_message_id, "
                        "direction, sender_type, content_type, body_text, delivery_status, source_timestamp) "
                        "VALUES (:id, :tid, :conv_id, :cid, :ctype, 'wamid.dedup.001', "
                        ":dir, :stype, :cont, 'Dup', :dstat, :ts)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": tenant_a,
                        "conv_id": conv_a,
                        "cid": customer_a,
                        "ctype": ChannelType.WHATSAPP_CLOUD.value,
                        "dir": MessageDirection.INBOUND.value,
                        "stype": MessageSenderType.CUSTOMER.value,
                        "cont": MessageContentType.TEXT.value,
                        "dstat": DeliveryStatus.DELIVERED.value,
                        "ts": datetime.now(timezone.utc),
                    },
                )

        # 4. Tenant B sees zero customers and zero messages from Tenant A
        async with tenant_session(runtime_factory, tenant_b) as sess_b:
            cust_rows = (await sess_b.execute(text("SELECT id FROM customers"))).all()
            msg_rows = (await sess_b.execute(text("SELECT id FROM messages"))).all()
            assert cust_rows == []
            assert msg_rows == []
    finally:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM channel_routing_directory WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM messages WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM conversations WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM customer_identities WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM customers WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM channel_accounts WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM tenants WHERE id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
        await runtime_engine.dispose()
        await migrator_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_channels_migration_rls.py -v`
Expected: FAIL because `0002_channels_identity_inbox` has not been created or migrated yet.

- [ ] **Step 3: Write Alembic migration and SQLAlchemy models**

Check `apps/backend/alembic/versions/0001_*.py` to confirm its exact `revision` string matches `"0001_tenancy_rls_and_audit"` from Plan 01.

Create `apps/backend/alembic/versions/0002_channels_identity_inbox.py`:

```python
"""Create omnichannel inbox, webhook pipeline, and identity graph tables.

Revision ID: 0002_channels_identity_inbox
Revises: 0001_tenancy_rls_and_audit
Create Date: 2026-11-19 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_channels_identity_inbox"
down_revision: str | None = "0001_tenancy_rls_and_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHANNEL_TYPE_CHECK = (
    "channel_type IN ("
    "'whatsapp_cloud', 'whatsapp_coexistence', 'email', 'web_livechat', "
    "'shopify', 'woocommerce', 'shopline', 'shoplazza', 'tiktok_shop', "
    "'made_in_china_email', 'alibaba_email', 'messenger', 'instagram_dm', "
    "'telegram', 'amazon_sp', 'ebay', 'mercado_libre', 'temu', "
    "'walmart', 'shopee', 'lazada'"
    ")"
)

TENANT_RLS_TABLES: tuple[str, ...] = (
    "channel_accounts",
    "dead_letter_events",
    "b2b_companies",
    "customers",
    "customer_identities",
    "identity_merge_events",
    "conversations",
    "messages",
    "consent_records",
    "suppression_list",
)


def _enable_force_rls(table_name: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table_name} TO app_runtime")
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY app_runtime_permissive ON {table_name} "
        "AS PERMISSIVE FOR ALL TO app_runtime USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"CREATE POLICY tenant_isolation_policy ON {table_name} "
        "AS RESTRICTIVE FOR ALL TO app_runtime "
        "USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 1. channel_routing_directory (Non-PII global routing table, accessed via SECURITY DEFINER fn)
    op.create_table(
        "channel_routing_directory",
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_account_id", sa.Text(), nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(CHANNEL_TYPE_CHECK, name="ck_channel_routing_directory_channel_type"),
        sa.PrimaryKeyConstraint("channel_type", "external_account_id"),
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE channel_routing_directory TO app_runtime"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_tenant_by_channel_external_id(
            p_channel_type TEXT,
            p_external_id TEXT
        )
        RETURNS TABLE (tenant_id UUID, channel_account_id UUID)
        LANGUAGE sql
        SECURITY DEFINER
        STABLE
        SET search_path = public, pg_temp
        AS $$
            SELECT crd.tenant_id, crd.channel_account_id
            FROM public.channel_routing_directory crd
            WHERE crd.channel_type = p_channel_type
              AND crd.external_account_id = p_external_id
              AND crd.is_active = true
            LIMIT 1;
        $$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION resolve_tenant_by_channel_external_id(TEXT, TEXT) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION resolve_tenant_by_channel_external_id(TEXT, TEXT) TO app_runtime"
    )

    # 2. channel_accounts
    op.create_table(
        "channel_accounts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_account_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "rate_limit_profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{\"mps\": 80, \"coexistence\": false}'::jsonb"),
        ),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(CHANNEL_TYPE_CHECK, name="ck_channel_accounts_channel_type"),
        sa.CheckConstraint(
            "status IN ('active', 'token_expired', 'rate_limited', 'disconnected')",
            name="ck_channel_accounts_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_type",
            "external_account_id",
            name="uq_channel_accounts_tenant_type_ext",
        ),
    )
    op.create_index(
        "idx_channel_accounts_tenant_type",
        "channel_accounts",
        ["tenant_id", "channel_type"],
    )
    _enable_force_rls("channel_accounts")

    # 3. raw_webhook_events (No RLS: tenant_id nullable until resolved at ingress)
    op.create_table(
        "raw_webhook_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_event_id", sa.Text(), nullable=False),
        sa.Column("external_account_id", sa.Text(), nullable=True),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "signature_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "processing_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'received'"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(CHANNEL_TYPE_CHECK, name="ck_raw_webhook_events_channel_type"),
        sa.CheckConstraint(
            "processing_status IN ('received', 'queued', 'processing', 'processed', 'deferred', 'dead_letter')",
            name="ck_raw_webhook_events_status",
        ),
        sa.UniqueConstraint(
            "channel_type",
            "external_event_id",
            name="uq_raw_webhook_events_channel_ext_event",
        ),
    )
    op.create_index(
        "idx_raw_webhook_events_status",
        "raw_webhook_events",
        ["processing_status", "received_at"],
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE raw_webhook_events TO app_runtime")

    # 4. dead_letter_events
    op.create_table(
        "dead_letter_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "raw_webhook_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_webhook_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column(
            "direction",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'inbound'"),
        ),
        sa.Column("external_event_id", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("direction IN ('inbound', 'outbound')", name="ck_dlq_direction"),
        sa.CheckConstraint(
            "status IN ('open', 'replayed', 'resolved', 'discarded')",
            name="ck_dlq_status",
        ),
    )
    op.create_index(
        "idx_dead_letter_events_tenant_status",
        "dead_letter_events",
        ["tenant_id", "status", sa.text("created_at DESC")],
    )
    _enable_force_rls("dead_letter_events")

    # 5. b2b_companies
    op.create_table(
        "b2b_companies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("legal_name", sa.Text(), nullable=False),
        sa.Column("country_code", sa.CHAR(2), nullable=False, server_default=sa.text("'US'")),
        sa.Column("tax_vat_number", sa.Text(), nullable=True),
        sa.Column("website_domain", sa.Text(), nullable=True),
        sa.Column(
            "credit_tier",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'prepaid'"),
        ),
        sa.Column(
            "default_incoterm",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'FOB'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "credit_tier IN ('prepaid', 'net_30', 'vip_credit')",
            name="ck_b2b_companies_credit_tier",
        ),
        sa.CheckConstraint(
            "default_incoterm IN ('EXW', 'FOB', 'CIF', 'DDP', 'DAP')",
            name="ck_b2b_companies_incoterm",
        ),
    )
    op.create_index(
        "idx_b2b_companies_tenant_domain",
        "b2b_companies",
        ["tenant_id", "website_domain"],
    )
    op.execute(
        "CREATE INDEX idx_b2b_companies_legal_name_trgm "
        "ON b2b_companies USING gin (legal_name gin_trgm_ops)"
    )
    _enable_force_rls("b2b_companies")

    # 6. customers
    op.create_table(
        "customers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'b2c_consumer'"),
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("b2b_companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("primary_email", sa.Text(), nullable=True),
        sa.Column("primary_phone_e164", sa.Text(), nullable=True),
        sa.Column("country_code", sa.CHAR(2), nullable=True),
        sa.Column("postal_code", sa.Text(), nullable=True),
        sa.Column(
            "preferred_language",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'en'"),
        ),
        sa.Column("iana_timezone", sa.Text(), nullable=True),
        sa.Column("timezone_source", sa.Text(), nullable=True),
        sa.Column(
            "lifecycle_stage",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'lead'"),
        ),
        sa.Column(
            "ltv_usd",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "merged_into_customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "customer_type IN ('b2c_consumer', 'b2b_contact', 'b2b_company_rep')",
            name="ck_customers_customer_type",
        ),
        sa.CheckConstraint(
            "timezone_source IS NULL OR timezone_source IN ('phone_prefix', 'shipping_country', 'manual')",
            name="ck_customers_timezone_source",
        ),
    )
    op.create_index(
        "idx_customers_tenant_created",
        "customers",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_customers_tenant_email",
        "customers",
        ["tenant_id", "primary_email"],
        postgresql_where=sa.text("primary_email IS NOT NULL"),
    )
    op.create_index(
        "idx_customers_tenant_phone",
        "customers",
        ["tenant_id", "primary_phone_e164"],
        postgresql_where=sa.text("primary_phone_e164 IS NOT NULL"),
    )
    op.execute(
        "CREATE INDEX idx_customers_display_name_trgm "
        "ON customers USING gin (display_name gin_trgm_ops)"
    )
    _enable_force_rls("customers")

    # 7. customer_identities
    op.create_table(
        "customer_identities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("identity_type", sa.Text(), nullable=False),
        sa.Column("identity_value", sa.Text(), nullable=False),
        sa.Column("confidence_level", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "identity_type IN ("
            "'email', 'phone_e164', 'whatsapp_waid', 'whatsapp_bsuid', "
            "'ig_psid', 'fb_psid', 'shopify_customer_id', 'tiktok_buyer_id', "
            "'amazon_buyer_email', 'web_visitor_id'"
            ")",
            name="ck_customer_identities_type",
        ),
        sa.CheckConstraint(
            "confidence_level IN ('deterministic', 'human_verified', 'probabilistic')",
            name="ck_customer_identities_confidence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "identity_type",
            "identity_value",
            name="uq_customer_identities_tenant_type_val",
        ),
    )
    op.create_index(
        "idx_customer_identities_tenant_customer",
        "customer_identities",
        ["tenant_id", "customer_id"],
    )
    _enable_force_rls("customer_identities")

    # 8. identity_merge_events
    op.create_table(
        "identity_merge_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "moved_identity_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column(
            "moved_conversation_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column(
            "reverted_merge_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("identity_merge_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("performed_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("action IN ('merge', 'split')", name="ck_identity_merge_events_action"),
    )
    op.create_index(
        "idx_identity_merge_events_tenant_created",
        "identity_merge_events",
        ["tenant_id", sa.text("created_at DESC")],
    )
    _enable_force_rls("identity_merge_events")

    # 9. conversations
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "channel_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_thread_id", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column(
            "conversation_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'b2c_support'"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("assigned_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ai_autonomy_override", sa.Text(), nullable=True),
        sa.Column("csw_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fep_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ctwa_clid", sa.Text(), nullable=True),
        sa.Column("human_agent_window_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_preview", sa.Text(), nullable=True),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "channel_policy_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(CHANNEL_TYPE_CHECK, name="ck_conversations_channel_type"),
        sa.CheckConstraint(
            "conversation_mode IN ('b2c_support', 'b2b_inquiry', 'mixed')",
            name="ck_conversations_mode",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'pending_human', 'snoozed', 'resolved')",
            name="ck_conversations_status",
        ),
        sa.CheckConstraint(
            "ai_autonomy_override IS NULL OR ai_autonomy_override IN ('L0_copilot', 'L1_proposal', 'L2_auto')",
            name="ck_conversations_autonomy",
        ),
    )
    op.create_index(
        "idx_conversations_tenant_updated",
        "conversations",
        ["tenant_id", sa.text("updated_at DESC")],
    )
    op.create_index(
        "idx_conversations_tenant_status_sla",
        "conversations",
        ["tenant_id", "status", sa.text("sla_due_at ASC NULLS LAST")],
    )
    op.create_index(
        "idx_conversations_tenant_thread",
        "conversations",
        ["tenant_id", "channel_account_id", "external_thread_id"],
        postgresql_where=sa.text("external_thread_id IS NOT NULL"),
    )
    op.create_index(
        "idx_conversations_tenant_customer",
        "conversations",
        ["tenant_id", "customer_id"],
    )
    _enable_force_rls("conversations")

    # 10. messages
    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "customer_identity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer_identities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_message_id", sa.Text(), nullable=True),
        sa.Column("external_sender_id", sa.Text(), nullable=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("sender_type", sa.Text(), nullable=False),
        sa.Column("sender_id", sa.Text(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_sanitized_html", sa.Text(), nullable=True),
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "channel_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "delivery_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "channel_msg_cost",
            sa.Numeric(12, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(CHANNEL_TYPE_CHECK, name="ck_messages_channel_type"),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound', 'internal_note')",
            name="ck_messages_direction",
        ),
        sa.CheckConstraint(
            "sender_type IN ('customer', 'human_agent', 'ai_agent', 'system')",
            name="ck_messages_sender_type",
        ),
        sa.CheckConstraint(
            "content_type IN ('text', 'rich_html', 'template', 'media', 'quote_card', 'order_card')",
            name="ck_messages_content_type",
        ),
        sa.CheckConstraint(
            "delivery_status IN ('queued', 'sent', 'delivered', 'read', 'failed')",
            name="ck_messages_delivery_status",
        ),
    )
    op.create_index(
        "uq_messages_idempotency",
        "messages",
        ["tenant_id", "channel_type", "external_message_id"],
        unique=True,
        postgresql_where=sa.text("external_message_id IS NOT NULL"),
    )
    op.create_index(
        "idx_messages_tenant_conv_ts",
        "messages",
        ["tenant_id", "conversation_id", "source_timestamp", "created_at"],
    )
    _enable_force_rls("messages")

    # 11. consent_records
    op.create_table(
        "consent_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("consent_status", sa.Text(), nullable=False),
        sa.Column(
            "opt_in_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[\"order_updates\"]'::jsonb"),
        ),
        sa.Column("disclosed_business_name", sa.Text(), nullable=True),
        sa.Column("proof_source", sa.Text(), nullable=False),
        sa.Column("proof_ip", postgresql.INET(), nullable=True),
        sa.Column("evidence_ref", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "consent_status IN ('opted_in', 'opted_out')",
            name="ck_consent_records_status",
        ),
    )
    op.create_index(
        "idx_consent_records_tenant_customer_channel",
        "consent_records",
        ["tenant_id", "customer_id", "channel_type", sa.text("recorded_at DESC")],
    )
    _enable_force_rls("consent_records")

    # 12. suppression_list
    op.create_table(
        "suppression_list",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("normalized_address", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_type",
            "normalized_address",
            name="uq_suppression_list_tenant_channel_addr",
        ),
    )
    _enable_force_rls("suppression_list")


def downgrade() -> None:
    for table_name in reversed(TENANT_RLS_TABLES):
        op.drop_table(table_name)
    op.drop_table("raw_webhook_events")
    op.execute("DROP FUNCTION IF EXISTS resolve_tenant_by_channel_external_id(TEXT, TEXT)")
    op.drop_table("channel_routing_directory")
```

Create `apps/backend/src/erp_core/channels/__init__.py`:

```python
"""Omnichannel messaging adapters, webhook pipeline, and unified inbox."""
```

Create `apps/backend/src/erp_core/channels/models.py`:

```python
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class ChannelRoutingDirectory(Base):
    """Non-PII global directory mapping external channel identifiers to tenants."""

    __tablename__ = "channel_routing_directory"
    __table_args__ = (PrimaryKeyConstraint("channel_type", "external_account_id"),)

    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_account_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ChannelAccount(Base):
    """Tenant-owned messaging channel connection metadata."""

    __tablename__ = "channel_accounts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_type",
            "external_account_id",
            name="uq_channel_accounts_tenant_type_ext",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    rate_limit_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: {"mps": 80, "coexistence": False}
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class RawWebhookEvent(Base):
    """Immutable ingress webhook store deduplicating upstream retry storms."""

    __tablename__ = "raw_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "channel_type",
            "external_event_id",
            name="uq_raw_webhook_events_channel_ext_event",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    processing_status: Mapped[str] = mapped_column(Text, nullable=False, default="received")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeadLetterEvent(Base):
    """Failed inbound or outbound webhook/delivery event queued for operator replay."""

    __tablename__ = "dead_letter_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    raw_webhook_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("raw_webhook_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False, default="inbound")
    external_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Conversation(Base):
    """Unified omnichannel conversation thread with channel compliance windows."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("channel_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_mode: Mapped[str] = mapped_column(Text, nullable=False, default="b2c_support")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    ai_autonomy_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    csw_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fep_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ctwa_clid: Mapped[str | None] = mapped_column(Text, nullable=True)
    human_agent_window_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_message_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    channel_policy_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Message(Base):
    """Normalized omnichannel message with partial unique idempotency guard."""

    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "uq_messages_idempotency",
            "tenant_id",
            "channel_type",
            "external_message_id",
            unique=True,
            postgresql_where=text("external_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    customer_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_sender_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    sender_type: Mapped[str] = mapped_column(Text, nullable=False)
    sender_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_sanitized_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    channel_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    delivery_status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    channel_msg_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ConsentRecord(Base):
    """Append-only opt-in and opt-out proof ledger."""

    __tablename__ = "consent_records"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    consent_status: Mapped[str] = mapped_column(Text, nullable=False)
    opt_in_scope: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=lambda: ["order_updates"]
    )
    disclosed_business_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_source: Mapped[str] = mapped_column(Text, nullable=False)
    proof_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SuppressionEntry(Base):
    """Hard outbound suppression list checked prior to every outbound dispatch."""

    __tablename__ = "suppression_list"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_type",
            "normalized_address",
            name="uq_suppression_list_tenant_channel_addr",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_address: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
```

Create `apps/backend/src/erp_core/identity/__init__.py`:

```python
"""Unified customer identity graph, timezone resolution, and merge/split lifecycle."""
```

Create `apps/backend/src/erp_core/identity/models.py`:

```python
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CHAR,
    DateTime,
    ForeignKey,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class B2bCompany(Base):
    """B2B buyer organization entity linked to one or more contact customers."""

    __tablename__ = "b2b_companies"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    legal_name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str] = mapped_column(CHAR(2), nullable=False, default="US")
    tax_vat_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    website_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    credit_tier: Mapped[str] = mapped_column(Text, nullable=False, default="prepaid")
    default_incoterm: Mapped[str] = mapped_column(Text, nullable=False, default="FOB")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Customer(Base):
    """Canonical unified customer record across B2C and B2B touchpoints."""

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    customer_type: Mapped[str] = mapped_column(Text, nullable=False, default="b2c_consumer")
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("b2b_companies.id", ondelete="SET NULL"),
        nullable=True,
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    primary_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_phone_e164: Mapped[str | None] = mapped_column(Text, nullable=True)
    country_code: Mapped[str | None] = mapped_column(CHAR(2), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_language: Mapped[str] = mapped_column(Text, nullable=False, default="en")
    iana_timezone: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    lifecycle_stage: Mapped[str] = mapped_column(Text, nullable=False, default="lead")
    ltv_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    merged_into_customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CustomerIdentity(Base):
    """Channel-scoped identifier linked to a customer record."""

    __tablename__ = "customer_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "identity_type",
            "identity_value",
            name="uq_customer_identities_tenant_type_val",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    identity_type: Mapped[str] = mapped_column(Text, nullable=False)
    identity_value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_level: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class IdentityMergeEvent(Base):
    """Event-sourced audit log enabling lossless reversible customer merges/splits."""

    __tablename__ = "identity_merge_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_customer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    moved_identity_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False
    )
    moved_conversation_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, default=list
    )
    reverted_merge_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("identity_merge_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    performed_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
```

Modify `apps/backend/alembic/env.py` to import the new model modules alongside Plan 01 models so `target_metadata` includes all tables:

```python
import erp_core.channels.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
```

- [ ] **Step 4: Run migration and test to verify pass**

Run:
```bash
cd apps/backend
uv run alembic upgrade head
uv run pytest tests/test_channels_migration_rls.py -v
```
Expected: `alembic upgrade head` applies `0002_channels_identity_inbox`, and `pytest` exits 0 with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/alembic/env.py apps/backend/alembic/versions/0002_channels_identity_inbox.py apps/backend/src/erp_core/channels/__init__.py apps/backend/src/erp_core/channels/models.py apps/backend/src/erp_core/identity/__init__.py apps/backend/src/erp_core/identity/models.py apps/backend/tests/test_channels_migration_rls.py
git commit -m "feat(channels,identity): add migration 0002 with FORCE RLS, pg_trgm, and ORM models"
```

---

### Task 2.2: `DualTokenBucketLimiter` (Atomic Redis Lua Rate Limiter & `4^X` Backoff)

**Files:**
- Create: `apps/backend/src/erp_core/channels/rate_limiter.py`
- Test: `apps/backend/tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: `erp_core.shared.enums.ChannelType`, `redis.asyncio.Redis`.
- Produces:
  ```python
  class DualTokenBucketLimiter:
      def __init__(
          self,
          redis: Redis,
          *,
          pair_burst_capacity: int = 5,
          pair_window_seconds: float = 6.0,
          cloud_mps: int = 80,
          coexistence_mps: int = 20,
      ) -> None: ...
      async def acquire(
          self,
          channel_type: ChannelType,
          account_id: str,
          recipient_id: str | None = None,
          is_coexistence: bool = False,
      ) -> tuple[bool, int]: ...
      def compute_backoff_seconds(
          self, attempt: int, error_code: int | None = None
      ) -> int: ...
  ```

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_rate_limiter.py`:

```python
from __future__ import annotations

import os
import uuid

import pytest
import redis.asyncio as aioredis

from erp_core.channels.rate_limiter import DualTokenBucketLimiter
from erp_core.shared.enums import ChannelType


@pytest.fixture
async def redis_client():
    client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_whatsapp_pair_limit_throttling_and_isolation(redis_client: aioredis.Redis) -> None:
    limiter = DualTokenBucketLimiter(
        redis_client,
        pair_burst_capacity=3,
        pair_window_seconds=6.0,
    )
    acct_id = f"pn-{uuid.uuid4().hex[:8]}"
    buyer_a = "+14155550101"
    buyer_b = "+14155550102"

    # First 3 burst messages to buyer_a pass immediately
    for _ in range(3):
        allowed, wait_ms = await limiter.acquire(
            ChannelType.WHATSAPP_CLOUD, acct_id, recipient_id=buyer_a
        )
        assert allowed is True
        assert wait_ms == 0

    # 4th immediate message to buyer_a is throttled by pair bucket (~6s / token)
    allowed_4, wait_ms_4 = await limiter.acquire(
        ChannelType.WHATSAPP_CLOUD, acct_id, recipient_id=buyer_a
    )
    assert allowed_4 is False
    assert wait_ms_4 > 0

    # Independent recipient buyer_b is unaffected
    allowed_b, wait_ms_b = await limiter.acquire(
        ChannelType.WHATSAPP_CLOUD, acct_id, recipient_id=buyer_b
    )
    assert allowed_b is True
    assert wait_ms_b == 0


@pytest.mark.asyncio
async def test_coexistence_20mps_vs_cloud_80mps_and_atomic_no_leak(
    redis_client: aioredis.Redis,
) -> None:
    limiter = DualTokenBucketLimiter(redis_client, pair_burst_capacity=1)

    coex_acct = f"coex-{uuid.uuid4().hex[:8]}"
    # 20 distinct recipients consume all 20 mps tokens on coexistence account
    for idx in range(20):
        allowed, wait_ms = await limiter.acquire(
            ChannelType.WHATSAPP_COEXISTENCE,
            coex_acct,
            recipient_id=f"+49170000{idx:04d}",
            is_coexistence=True,
        )
        assert allowed is True
        assert wait_ms == 0

    # 21st distinct recipient in same second hits Coexistence 20 mps ceiling
    allowed_21, wait_ms_21 = await limiter.acquire(
        ChannelType.WHATSAPP_COEXISTENCE,
        coex_acct,
        recipient_id="+491700009999",
        is_coexistence=True,
    )
    assert allowed_21 is False
    assert wait_ms_21 > 0

    # Standard Cloud API account allows 80 mps before throttling on 81st
    cloud_acct = f"cloud-{uuid.uuid4().hex[:8]}"
    for idx in range(80):
        allowed, wait_ms = await limiter.acquire(
            ChannelType.WHATSAPP_CLOUD,
            cloud_acct,
            recipient_id=f"+44770090{idx:04d}",
            is_coexistence=False,
        )
        assert allowed is True
        assert wait_ms == 0

    allowed_81, wait_ms_81 = await limiter.acquire(
        ChannelType.WHATSAPP_CLOUD,
        cloud_acct,
        recipient_id="+447700909999",
        is_coexistence=False,
    )
    assert allowed_81 is False
    assert wait_ms_81 > 0

    # Atomicity: pair rejection must NOT consume an account token
    atomic_acct = f"atomic-{uuid.uuid4().hex[:8]}"
    ok1, _ = await limiter.acquire(
        ChannelType.WHATSAPP_COEXISTENCE,
        atomic_acct,
        recipient_id="+33612345678",
        is_coexistence=True,
    )
    assert ok1 is True
    # Rejected twice by pair limit
    for _ in range(2):
        ok_rej, wait_rej = await limiter.acquire(
            ChannelType.WHATSAPP_COEXISTENCE,
            atomic_acct,
            recipient_id="+33612345678",
            is_coexistence=True,
        )
        assert ok_rej is False
        assert wait_rej > 0
    acct_tokens_raw = await redis_client.hget(
        f"bucket:acct:{ChannelType.WHATSAPP_COEXISTENCE.value}:{atomic_acct}",
        "tokens",
    )
    assert float(acct_tokens_raw) >= 18.9


def test_meta_4_power_x_backoff_computation(redis_client: aioredis.Redis) -> None:
    limiter = DualTokenBucketLimiter(redis_client)

    # Meta 131056 (pair limit) and 130429 (throughput limit) strictly follow 4^X backoff
    assert limiter.compute_backoff_seconds(1, 131056) == 4
    assert limiter.compute_backoff_seconds(2, 131056) == 16
    assert limiter.compute_backoff_seconds(3, 130429) == 64
    assert limiter.compute_backoff_seconds(4, 130429) == 256
    assert limiter.compute_backoff_seconds(6, 131056) == 4096
    assert limiter.compute_backoff_seconds(8, 131056) == 4096

    # Meta 131049 per-user marketing frequency cap requires 24h cooldown
    assert limiter.compute_backoff_seconds(1, 131049) == 86400

    # Generic transient error uses 2^X backoff
    assert limiter.compute_backoff_seconds(1, 500) == 2
    assert limiter.compute_backoff_seconds(3, None) == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_rate_limiter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.channels.rate_limiter'`.

- [ ] **Step 3: Write `rate_limiter.py`**

Create `apps/backend/src/erp_core/channels/rate_limiter.py`:

```python
from __future__ import annotations

from typing import Final

from redis.asyncio import Redis

from erp_core.shared.enums import ChannelType

_META_RATE_LIMIT_CODES: Final[frozenset[int]] = frozenset({131056, 130429})
_META_PER_USER_MARKETING_CAP_CODE: Final[int] = 131049

_DUAL_TOKEN_BUCKET_LUA: Final[str] = """
local acct_key = KEYS[1]
local pair_key = KEYS[2]

local acct_cap = tonumber(ARGV[1])
local acct_rate = tonumber(ARGV[2])
local has_pair = tonumber(ARGV[3])
local pair_cap = tonumber(ARGV[4])
local pair_rate = tonumber(ARGV[5])
local ttl_ms = tonumber(ARGV[6])

local now_t = redis.call('TIME')
local now_ms = tonumber(now_t[1]) * 1000 + math.floor(tonumber(now_t[2]) / 1000)

local function eval_bucket(key, cap, rate)
    local data = redis.call('HMGET', key, 'tokens', 'last_ms')
    local tokens = tonumber(data[1])
    local last_ms = tonumber(data[2])
    if tokens == nil or last_ms == nil then
        tokens = cap
        last_ms = now_ms
    else
        local elapsed_sec = math.max(0.0, (now_ms - last_ms) / 1000.0)
        tokens = math.min(cap, tokens + elapsed_sec * rate)
        last_ms = now_ms
    end
    local allowed = tokens >= 1.0
    local wait_ms = 0
    if not allowed then
        wait_ms = math.ceil(((1.0 - tokens) / rate) * 1000.0)
    end
    return allowed, tokens, last_ms, wait_ms
end

local ok1, tok1, ts1, wait1 = eval_bucket(acct_key, acct_cap, acct_rate)
local ok2, tok2, ts2, wait2 = true, 0.0, now_ms, 0
if has_pair == 1 then
    ok2, tok2, ts2, wait2 = eval_bucket(pair_key, pair_cap, pair_rate)
end

if ok1 and ok2 then
    tok1 = tok1 - 1.0
    redis.call('HSET', acct_key, 'tokens', string.format('%.6f', tok1), 'last_ms', tostring(ts1))
    redis.call('PEXPIRE', acct_key, ttl_ms)
    if has_pair == 1 then
        tok2 = tok2 - 1.0
        redis.call('HSET', pair_key, 'tokens', string.format('%.6f', tok2), 'last_ms', tostring(ts2))
        redis.call('PEXPIRE', pair_key, ttl_ms)
    end
    return {1, 0}
end

local wait_ms = math.max(wait1, wait2)
return {0, wait_ms}
"""


class DualTokenBucketLimiter:
    """Atomic Redis Lua dual token bucket enforcing account and WhatsApp pair rate limits."""

    def __init__(
        self,
        redis: Redis,
        *,
        pair_burst_capacity: int = 5,
        pair_window_seconds: float = 6.0,
        cloud_mps: int = 80,
        coexistence_mps: int = 20,
        default_mps: int = 50,
        key_ttl_ms: int = 120_000,
    ) -> None:
        self._redis = redis
        self._pair_burst_capacity = max(1, pair_burst_capacity)
        self._pair_refill_rate = 1.0 / max(0.1, pair_window_seconds)
        self._cloud_mps = max(1, cloud_mps)
        self._coexistence_mps = max(1, coexistence_mps)
        self._default_mps = max(1, default_mps)
        self._key_ttl_ms = max(10_000, key_ttl_ms)
        self._script = self._redis.register_script(_DUAL_TOKEN_BUCKET_LUA)

    def _resolve_account_rate(
        self, channel_type: ChannelType, is_coexistence: bool
    ) -> tuple[int, float]:
        if channel_type == ChannelType.WHATSAPP_COEXISTENCE or is_coexistence:
            return self._coexistence_mps, float(self._coexistence_mps)
        if channel_type == ChannelType.WHATSAPP_CLOUD:
            return self._cloud_mps, float(self._cloud_mps)
        return self._default_mps, float(self._default_mps)

    async def acquire(
        self,
        channel_type: ChannelType,
        account_id: str,
        recipient_id: str | None = None,
        is_coexistence: bool = False,
    ) -> tuple[bool, int]:
        """Atomically acquire account and (for WhatsApp) recipient pair tokens."""
        acct_cap, acct_rate = self._resolve_account_rate(channel_type, is_coexistence)
        acct_key = f"bucket:acct:{channel_type.value}:{account_id}"

        is_whatsapp = channel_type in (
            ChannelType.WHATSAPP_CLOUD,
            ChannelType.WHATSAPP_COEXISTENCE,
        )
        has_pair = 1 if (is_whatsapp and recipient_id) else 0
        pair_key = (
            f"bucket:pair:{channel_type.value}:{account_id}:{recipient_id}"
            if has_pair == 1
            else f"{acct_key}:nopair"
        )

        result = await self._script(
            keys=[acct_key, pair_key],
            args=[
                str(acct_cap),
                f"{acct_rate:.6f}",
                str(has_pair),
                str(self._pair_burst_capacity),
                f"{self._pair_refill_rate:.6f}",
                str(self._key_ttl_ms),
            ],
        )
        allowed = int(result[0]) == 1
        wait_ms = int(result[1])
        return allowed, wait_ms

    def compute_backoff_seconds(
        self, attempt: int, error_code: int | None = None
    ) -> int:
        """Compute retry delay seconds using Meta 4^X backoff on 131056/130429."""
        bounded_attempt = max(1, attempt)
        if error_code == _META_PER_USER_MARKETING_CAP_CODE:
            return 86_400
        if error_code in _META_RATE_LIMIT_CODES:
            return int(min(4**bounded_attempt, 4096))
        return int(min(2**bounded_attempt, 256))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_rate_limiter.py -v`
Expected: PASS (`3 passed`).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/channels/rate_limiter.py apps/backend/tests/test_rate_limiter.py
git commit -m "feat(channels): implement atomic Redis Lua DualTokenBucketLimiter with 4^X backoff"
```

---

### Task 2.3: Zero-Message-Loss Webhook Ingress Gateway, Taskiq Consumer & DLQ Replay

**Files:**
- Create: `apps/backend/src/erp_core/channels/adapters/__init__.py`
- Create: `apps/backend/src/erp_core/channels/adapters/base.py`
- Create: `apps/backend/src/erp_core/channels/webhook_router.py`
- Create: `apps/backend/src/erp_core/channels/ingress_worker.py`
- Modify: `apps/backend/src/erp_core/api_main.py`
- Modify: `apps/backend/src/erp_core/worker_main.py`
- Test: `apps/backend/tests/test_webhook_idempotency.py`

**Interfaces:**
- Consumes: `erp_core.shared.db` (`tenant_session`, `system_session`), `erp_core.shared.enums` (`ChannelType`, `MessageDirection`, `MessageSenderType`, `MessageContentType`, `DeliveryStatus`, `IdentityType`, `ConversationMode`, `ConversationStatus`, `UserRole`), `erp_core.shared.crypto.EnvelopeCryptoService`, `erp_core.tenancy.dependencies` (`CurrentPrincipal`, `require_roles`), `erp_core.tenancy.audit.record_audit_event`, `erp_core.channels.rate_limiter.DualTokenBucketLimiter`, `erp_core.identity.service.IdentityResolverService` (via dependency / lazy import for Task 2.4 compatibility).
- Produces: `MessagingChannelAdapter` ABC, `NormalizedInboundEvent`, `NormalizedStatusEvent`, `NormalizedWebhookBatch`, `OutboundMessageRequest`, `OutboundSendResult`, `AdapterRegistry`, `GET/POST /api/v1/webhooks/{channel}`, `OutOfOrderParentError`, `process_raw_webhook_event`, `consume_webhook_stream_batch`, `send_outbound_message`, `POST /api/v1/channels/dlq/{event_id}/replay` (`dlq_router`).

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_webhook_idempotency.py`:

```python
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, ClassVar

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import (
    AdapterRegistry,
    MessagingChannelAdapter,
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.ingress_worker import (
    dlq_router,
    process_raw_webhook_event,
)
from erp_core.channels.webhook_router import webhook_router
from erp_core.shared.enums import (
    ChannelType,
    ConversationMode,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
    UserRole,
)
from erp_core.tenancy.dependencies import CurrentPrincipal


class _TestWhatsAppEchoAdapter(MessagingChannelAdapter):
    channel_types: ClassVar[tuple[ChannelType, ...]] = (ChannelType.WHATSAPP_CLOUD,)

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        sig = headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        events: list[tuple[str, str | None]] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                val = change.get("value", {})
                phone_id = val.get("metadata", {}).get("phone_number_id")
                for msg in val.get("messages", []):
                    events.append((msg["id"], phone_id))
                for status in val.get("statuses", []):
                    events.append(
                        (f"status:{status['id']}:{status['status']}", phone_id)
                    )
        if not events:
            events.append((hashlib.sha256(raw_body).hexdigest(), None))
        return events

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        inbound: list[NormalizedInboundEvent] = []
        statuses: list[NormalizedStatusEvent] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                val = change.get("value", {})
                for msg in val.get("messages", []):
                    inbound.append(
                        NormalizedInboundEvent(
                            external_event_id=msg["id"],
                            external_account_id=val["metadata"]["phone_number_id"],
                            external_message_id=msg["id"],
                            external_thread_id=msg["from"],
                            sender_identity_type=IdentityType.WHATSAPP_WAID,
                            sender_identity_value=msg["from"],
                            sender_display_name="Storm Tester",
                            direction=MessageDirection.INBOUND,
                            sender_type=MessageSenderType.CUSTOMER,
                            content_type=MessageContentType.TEXT,
                            body_text=msg["text"]["body"],
                            source_timestamp=datetime.fromtimestamp(
                                int(msg["timestamp"]), tz=timezone.utc
                            ),
                            conversation_mode=ConversationMode.B2C_SUPPORT,
                            parent_external_id=msg.get("parent_external_id"),
                        )
                    )
                for st in val.get("statuses", []):
                    statuses.append(
                        NormalizedStatusEvent(
                            external_message_id=st["id"],
                            external_account_id=val["metadata"]["phone_number_id"],
                            delivery_status=DeliveryStatus(st["status"]),
                            timestamp=datetime.fromtimestamp(
                                int(st["timestamp"]), tz=timezone.utc
                            ),
                            require_existing_message=True,
                        )
                    )
        return NormalizedWebhookBatch(inbound_events=inbound, status_events=statuses)

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        return OutboundSendResult(success=True, external_message_id="wamid.out.1")


@pytest.mark.asyncio
async def test_webhook_hmac_latency_100x_storm_and_dlq_replay(monkeypatch) -> None:
    secret = "test_whatsapp_app_secret"
    monkeypatch.setenv("WHATSAPP_APP_SECRET", secret)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify_tok_123")

    migrator_engine = create_async_engine(os.environ["DATABASE_MIGRATOR_URL"], future=True)
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    phone_number_id = f"pn_{uuid.uuid4().hex[:10]}"

    registry = AdapterRegistry()
    registry.register("whatsapp", _TestWhatsAppEchoAdapter())
    registry.register(ChannelType.WHATSAPP_CLOUD.value, _TestWhatsAppEchoAdapter())

    app = FastAPI()
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.adapter_registry = registry
    app.include_router(webhook_router)
    app.include_router(dlq_router)

    # Override RBAC dependency on dlq_router to act as tenant admin
    from erp_core.channels.ingress_worker import _require_dlq_operator

    app.dependency_overrides[_require_dlq_operator] = lambda: CurrentPrincipal(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=UserRole.ADMIN,
        email="admin@factory.test",
    )

    try:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Storm Factory')"),
                {"id": tenant_id, "slug": f"storm-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile) "
                    "VALUES (:id, :tid, 'whatsapp_cloud', :ext, 'Storm WA', 'active', '{\"mps\": 80}'::jsonb)"
                ),
                {"id": account_id, "tid": tenant_id, "ext": phone_number_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_routing_directory "
                    "(channel_type, external_account_id, tenant_id, channel_account_id) "
                    "VALUES ('whatsapp_cloud', :ext, :tid, :aid)"
                ),
                {"ext": phone_number_id, "tid": tenant_id, "aid": account_id},
            )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Invalid HMAC must return 401
            bad_resp = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=b'{"entry":[]}',
                headers={"X-Hub-Signature-256": "sha256=deadbeef"},
            )
            assert bad_resp.status_code == 401

            # 2. Warmup + <100ms 200 OK persistence check
            warmup_payload = {
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {"phone_number_id": phone_number_id},
                                    "messages": [
                                        {
                                            "id": f"wamid.warm.{uuid.uuid4().hex[:8]}",
                                            "from": "14155550199",
                                            "timestamp": "1763500000",
                                            "type": "text",
                                            "text": {"body": "Warmup"},
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ]
            }
            warm_raw = json.dumps(warmup_payload).encode()
            warm_sig = "sha256=" + hmac.new(secret.encode(), warm_raw, hashlib.sha256).hexdigest()
            t0 = time.perf_counter()
            warm_resp = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=warm_raw,
                headers={"X-Hub-Signature-256": warm_sig, "Content-Type": "application/json"},
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert warm_resp.status_code == 200
            assert elapsed_ms < 100.0

            # 3. 100 duplicate webhook storm -> 1 persisted message + 0 duplicates
            storm_wamid = f"wamid.storm.{uuid.uuid4().hex[:8]}"
            storm_payload = {
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {"phone_number_id": phone_number_id},
                                    "messages": [
                                        {
                                            "id": storm_wamid,
                                            "from": "14155550188",
                                            "timestamp": "1763500100",
                                            "type": "text",
                                            "text": {"body": "Need 2000m LED strip quote"},
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ]
            }
            storm_raw = json.dumps(storm_payload).encode()
            storm_sig = "sha256=" + hmac.new(secret.encode(), storm_raw, hashlib.sha256).hexdigest()

            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/v1/webhooks/whatsapp",
                        content=storm_raw,
                        headers={
                            "X-Hub-Signature-256": storm_sig,
                            "Content-Type": "application/json",
                        },
                    )
                    for _ in range(100)
                ]
            )
            assert all(r.status_code == 200 for r in responses)
            new_accepts = [r.json() for r in responses if not r.json()["deduplicated"]]
            assert len(new_accepts) == 1

            raw_event_id = uuid.UUID(new_accepts[0]["raw_event_ids"][0])
            # Run consumer twice to prove worker idempotency too
            await process_raw_webhook_event(session_factory, redis, raw_event_id, registry)
            await process_raw_webhook_event(session_factory, redis, raw_event_id, registry)

            async with migrator_engine.connect() as conn:
                raw_cnt = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM raw_webhook_events WHERE external_event_id = :eid"
                        ),
                        {"eid": storm_wamid},
                    )
                ).scalar_one()
                msg_cnt = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM messages WHERE tenant_id = :tid AND external_message_id = :eid"
                        ),
                        {"tid": tenant_id, "eid": storm_wamid},
                    )
                ).scalar_one()
                assert raw_cnt == 1
                assert msg_cnt == 1

            # 4. Out-of-order parent -> DLQ after max attempts -> Replay API
            orphan_status_payload = {
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {"phone_number_id": phone_number_id},
                                    "statuses": [
                                        {
                                            "id": f"wamid.missing.{uuid.uuid4().hex[:8]}",
                                            "status": "delivered",
                                            "timestamp": "1763500200",
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ]
            }
            orphan_raw = json.dumps(orphan_status_payload).encode()
            orphan_sig = (
                "sha256=" + hmac.new(secret.encode(), orphan_raw, hashlib.sha256).hexdigest()
            )
            orphan_resp = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=orphan_raw,
                headers={"X-Hub-Signature-256": orphan_sig, "Content-Type": "application/json"},
            )
            assert orphan_resp.status_code == 200
            orphan_raw_id = uuid.UUID(orphan_resp.json()["raw_event_ids"][0])

            # Attempt 6 exceeds max 5 retries -> writes to dead_letter_events
            await process_raw_webhook_event(
                session_factory, redis, orphan_raw_id, registry, attempt=6
            )
            async with migrator_engine.connect() as conn:
                dlq_id = (
                    await conn.execute(
                        text(
                            "SELECT id FROM dead_letter_events WHERE raw_webhook_event_id = :rid"
                        ),
                        {"rid": orphan_raw_id},
                    )
                ).scalar_one()

            replay_resp = await client.post(f"/api/v1/channels/dlq/{dlq_id}/replay")
            assert replay_resp.status_code == 200
            assert replay_resp.json()["status"] == "replayed"
    finally:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM audit_events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM dead_letter_events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM raw_webhook_events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM channel_routing_directory WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM messages WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM conversations WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM customer_identities WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM channel_accounts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await redis.aclose()
        await runtime_engine.dispose()
        await migrator_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_webhook_idempotency.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.channels.adapters'`.

- [ ] **Step 3: Write `adapters/base.py`, `webhook_router.py`, `ingress_worker.py`, and wire `api_main.py` / `worker_main.py`**

Create `apps/backend/src/erp_core/channels/adapters/__init__.py`:

```python
"""Channel adapter registry and normalized messaging contracts."""
```

Create `apps/backend/src/erp_core/channels/adapters/base.py`:

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.enums import (
    ChannelType,
    ConversationMode,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)


class NormalizedAttachment(BaseModel):
    filename: str
    mime_type: str
    size_bytes: int = 0
    r2_key: str | None = None
    external_url: str | None = None
    external_media_id: str | None = None


class NormalizedInboundEvent(BaseModel):
    external_event_id: str
    external_account_id: str
    external_message_id: str | None = None
    external_thread_id: str | None = None
    subject: str | None = None
    sender_identity_type: IdentityType
    sender_identity_value: str
    sender_display_name: str
    country_hint: str | None = None
    direction: MessageDirection = MessageDirection.INBOUND
    sender_type: MessageSenderType = MessageSenderType.CUSTOMER
    content_type: MessageContentType = MessageContentType.TEXT
    body_text: str | None = None
    body_sanitized_html: str | None = None
    attachments: list[NormalizedAttachment] = Field(default_factory=list)
    source_timestamp: datetime
    conversation_mode: ConversationMode = ConversationMode.B2C_SUPPORT
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    ctwa_clid: str | None = None
    referral_ad_id: str | None = None
    extra_identities: list[tuple[IdentityType, str]] = Field(default_factory=list)
    b2b_inquiry_data: dict[str, Any] | None = None
    is_stop_command: bool = False
    opens_csw: bool = True
    parent_external_id: str | None = None
    resolved_conversation_id: UUID | None = None
    override_channel_type: ChannelType | None = None


class NormalizedStatusEvent(BaseModel):
    external_message_id: str
    external_account_id: str
    delivery_status: DeliveryStatus
    timestamp: datetime
    error_code: int | None = None
    error_message: str | None = None
    pricing_metadata: dict[str, Any] = Field(default_factory=dict)
    require_existing_message: bool = False


class NormalizedWebhookBatch(BaseModel):
    inbound_events: list[NormalizedInboundEvent] = Field(default_factory=list)
    status_events: list[NormalizedStatusEvent] = Field(default_factory=list)
    account_updates: list[dict[str, Any]] = Field(default_factory=list)


class OutboundMessageRequest(BaseModel):
    tenant_id: UUID
    channel_account_id: UUID
    channel_type: ChannelType
    external_account_id: str
    recipient_id: str
    conversation_id: UUID
    message_id: UUID
    content_type: MessageContentType = MessageContentType.TEXT
    body_text: str | None = None
    body_html: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_components: list[dict[str, Any]] = Field(default_factory=list)
    reply_to_address: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    subject: str | None = None
    from_address: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutboundSendResult(BaseModel):
    success: bool
    external_message_id: str | None = None
    error_code: int | None = None
    error_message: str | None = None
    retryable: bool = False
    estimated_cost_usd: Decimal = Decimal("0.000000")
    pricing_category: str | None = None


class MessagingChannelAdapter(ABC):
    """Canonical interface implemented by every messaging channel adapter."""

    channel_types: ClassVar[tuple[ChannelType, ...]]

    @abstractmethod
    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        raise NotImplementedError

    @abstractmethod
    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        raise NotImplementedError

    @abstractmethod
    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        raise NotImplementedError


class AdapterRegistry:
    """In-process registry resolving channel slugs and ChannelType values to adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, MessagingChannelAdapter] = {}

    def register(self, key: str, adapter: MessagingChannelAdapter) -> None:
        self._adapters[key.lower()] = adapter

    def get(self, key: str) -> MessagingChannelAdapter | None:
        return self._adapters.get(key.lower())
```

Create `apps/backend/src/erp_core/channels/webhook_router.py`:

```python
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import AdapterRegistry
from erp_core.shared.db import system_session
from erp_core.shared.enums import ChannelType

logger = logging.getLogger(__name__)
webhook_router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

_CHANNEL_ALIASES: dict[str, ChannelType] = {
    "whatsapp": ChannelType.WHATSAPP_CLOUD,
    "whatsapp_cloud": ChannelType.WHATSAPP_CLOUD,
    "whatsapp_coexistence": ChannelType.WHATSAPP_COEXISTENCE,
    "email": ChannelType.EMAIL,
    "resend": ChannelType.EMAIL,
    "postmark": ChannelType.EMAIL,
    "web_livechat": ChannelType.WEB_LIVECHAT,
}


def build_default_adapter_registry() -> AdapterRegistry:
    """Construct default adapter registry lazily importing implemented channel adapters."""
    registry = AdapterRegistry()
    try:
        from erp_core.channels.adapters.whatsapp import WhatsAppAdapter

        wa = WhatsAppAdapter()
        registry.register("whatsapp", wa)
        registry.register(ChannelType.WHATSAPP_CLOUD.value, wa)
        registry.register(ChannelType.WHATSAPP_COEXISTENCE.value, wa)
    except ImportError:
        logger.debug("WhatsAppAdapter not yet installed; skipping registration")

    try:
        from erp_core.channels.adapters.email import EmailAdapter

        email_adapter = EmailAdapter()
        registry.register("email", email_adapter)
        registry.register("resend", email_adapter)
        registry.register("postmark", email_adapter)
        registry.register(ChannelType.EMAIL.value, email_adapter)
    except ImportError:
        logger.debug("EmailAdapter not yet installed; skipping registration")
    return registry


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


def _get_redis(request: Request) -> aioredis.Redis:
    client = getattr(request.app.state, "redis", None)
    if client is None:
        client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        request.app.state.redis = client
    return client


def _get_adapter_registry(request: Request) -> AdapterRegistry:
    registry = getattr(request.app.state, "adapter_registry", None)
    if registry is None:
        registry = build_default_adapter_registry()
        request.app.state.adapter_registry = registry
    return registry


def _webhook_secret_for_channel(channel: str) -> str:
    key = channel.lower()
    if key in ("whatsapp", "whatsapp_cloud", "whatsapp_coexistence"):
        return os.environ.get("WHATSAPP_APP_SECRET", "")
    if key in ("email", "resend", "postmark"):
        return os.environ.get("RESEND_WEBHOOK_SECRET", os.environ.get("EMAIL_WEBHOOK_SECRET", ""))
    return os.environ.get("WEBHOOK_SHARED_SECRET", "")


@webhook_router.get("/{channel}")
async def verify_webhook_subscription(
    channel: str,
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    """Handle Meta webhook subscription verification handshake."""
    expected_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if (
        channel.lower() in ("whatsapp", "whatsapp_cloud", "whatsapp_coexistence")
        and hub_mode == "subscribe"
        and hub_verify_token is not None
        and expected_token
        and hmac.compare_digest(hub_verify_token, expected_token)
        and hub_challenge is not None
    ):
        return PlainTextResponse(content=hub_challenge, status_code=status.HTTP_200_OK)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid webhook verification token",
    )


@webhook_router.post("/{channel}")
async def receive_webhook(channel: str, request: Request) -> JSONResponse:
    """Verify raw HMAC signature, deduplicate into PostgreSQL, and stream to Taskiq in <100ms."""
    canonical_type = _CHANNEL_ALIASES.get(channel.lower())
    if canonical_type is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unsupported webhook channel: {channel}",
        )

    registry = _get_adapter_registry(request)
    adapter = registry.get(channel) or registry.get(canonical_type.value)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No adapter registered for channel: {channel}",
        )

    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    secret = _webhook_secret_for_channel(channel)
    if not secret or not adapter.verify_webhook_signature(raw_body, headers, secret):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            media_type="application/problem+json",
            content={
                "type": "https://erp.local/errors/webhook-signature-invalid",
                "title": "Invalid Webhook Signature",
                "status": 401,
                "detail": "HMAC verification failed for webhook request.",
                "instance": str(request.url.path),
                "code": "ERR_WEBHOOK_SIGNATURE_INVALID",
            },
        )

    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed JSON webhook body: {exc}",
        ) from exc

    extracted_events = adapter.extract_external_event_ids(payload, raw_body, headers)
    if not extracted_events:
        extracted_events = [(hashlib.sha256(raw_body).hexdigest(), None)]

    session_factory = _get_session_factory(request)
    redis = _get_redis(request)

    inserted_ids: list[str] = []
    stream_entries: list[tuple[str, str | None]] = []

    async with system_session(session_factory) as session:
        # Resolve tenant once per distinct external_account_id in batch
        resolved_tenants: dict[str, tuple[uuid.UUID | None, ChannelType]] = {}
        for _, ext_acct_id in extracted_events:
            if not ext_acct_id or ext_acct_id in resolved_tenants:
                continue
            found_tenant: uuid.UUID | None = None
            found_type = canonical_type
            candidate_types = (
                [ChannelType.WHATSAPP_CLOUD, ChannelType.WHATSAPP_COEXISTENCE]
                if canonical_type == ChannelType.WHATSAPP_CLOUD
                else [canonical_type]
            )
            for candidate in candidate_types:
                row = (
                    await session.execute(
                        text(
                            "SELECT tenant_id FROM resolve_tenant_by_channel_external_id("
                            "CAST(:ctype AS text), CAST(:ext_id AS text))"
                        ),
                        {"ctype": candidate.value, "ext_id": ext_acct_id},
                    )
                ).first()
                if row is not None:
                    found_tenant = row[0]
                    found_type = candidate
                    break
            resolved_tenants[ext_acct_id] = (found_tenant, found_type)

        headers_json = json.dumps(headers)
        payload_json = json.dumps(payload)

        for ext_event_id, ext_acct_id in extracted_events:
            resolved_tid: uuid.UUID | None = None
            resolved_ctype = canonical_type
            if ext_acct_id and ext_acct_id in resolved_tenants:
                resolved_tid, resolved_ctype = resolved_tenants[ext_acct_id]

            new_id = uuid.uuid4()
            inserted = (
                await session.execute(
                    text(
                        "INSERT INTO raw_webhook_events ("
                        "id, tenant_id, channel_type, external_event_id, external_account_id, "
                        "headers, raw_payload, signature_verified, processing_status"
                        ") VALUES ("
                        ":id, :tid, :ctype, :eeid, :eaid, "
                        "CAST(:headers AS jsonb), CAST(:payload AS jsonb), true, 'received'"
                        ") ON CONFLICT (channel_type, external_event_id) DO NOTHING "
                        "RETURNING id"
                    ),
                    {
                        "id": new_id,
                        "tid": resolved_tid,
                        "ctype": resolved_ctype.value,
                        "eeid": ext_event_id,
                        "eaid": ext_acct_id,
                        "headers": headers_json,
                        "payload": payload_json,
                    },
                )
            ).scalar_one_or_none()
            if inserted is not None:
                inserted_str = str(inserted)
                inserted_ids.append(inserted_str)
                stream_entries.append((inserted_str, resolved_ctype.value))

    for raw_id_str, ctype_val in stream_entries:
        stream_key = f"stream:webhooks:{ctype_val}"
        await redis.xadd(
            stream_key,
            {"raw_event_id": raw_id_str, "channel_type": str(ctype_val), "attempt": "1"},
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "accepted",
            "deduplicated": len(inserted_ids) == 0,
            "raw_event_ids": inserted_ids,
        },
    )
```

Create `apps/backend/src/erp_core/channels/ingress_worker.py`:

```python
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import (
    AdapterRegistry,
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    OutboundMessageRequest,
)
from erp_core.channels.rate_limiter import DualTokenBucketLimiter
from erp_core.channels.webhook_router import build_default_adapter_registry
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import (
    ChannelType,
    ConversationMode,
    ConversationStatus,
    DeliveryStatus,
    IdentityType,
    MessageDirection,
    UserRole,
)
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

logger = logging.getLogger(__name__)
dlq_router = APIRouter(prefix="/api/v1/channels/dlq", tags=["channels-dlq"])
_require_dlq_operator = require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)

MAX_INGRESS_ATTEMPTS = 5
MAX_OUTBOUND_ATTEMPTS = 6
DEFER_DELAY_SECONDS = 30


class OutOfOrderParentError(Exception):
    """Raised when a child webhook arrives prior to its parent entity/message."""


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


def _get_redis(request: Request) -> aioredis.Redis:
    client = getattr(request.app.state, "redis", None)
    if client is None:
        client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        request.app.state.redis = client
    return client


def _get_adapter_registry(request: Request) -> AdapterRegistry:
    registry = getattr(request.app.state, "adapter_registry", None)
    if registry is None:
        registry = build_default_adapter_registry()
        request.app.state.adapter_registry = registry
    return registry


async def _resolve_identity_customer(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    identity_type: IdentityType,
    raw_value: str,
    display_name: str,
    country_hint: str | None = None,
) -> tuple[uuid.UUID, bool]:
    """Delegate to IdentityResolverService (or lightweight deterministic fallback prior to Task 2.4)."""
    try:
        from erp_core.identity.service import IdentityResolverService

        service = IdentityResolverService()
        return await service.resolve_or_create_customer(
            session=session,
            tenant_id=tenant_id,
            identity_type=identity_type,
            raw_value=raw_value,
            display_name=display_name,
            country_hint=country_hint,
        )
    except ImportError:
        normalized = raw_value.strip().lower() if identity_type == IdentityType.EMAIL else raw_value.strip()
        existing = (
            await session.execute(
                text(
                    "SELECT ci.customer_id FROM customer_identities ci "
                    "WHERE ci.tenant_id = :tid AND ci.identity_type = :itype "
                    "AND ci.identity_value = :ival AND ci.is_active = true LIMIT 1"
                ),
                {"tid": tenant_id, "itype": identity_type.value, "ival": normalized},
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        cust_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2c_consumer', :name)"
            ),
            {"id": cust_id, "tid": tenant_id, "name": display_name or normalized},
        )
        await session.execute(
            text(
                "INSERT INTO customer_identities "
                "(id, tenant_id, customer_id, identity_type, identity_value, confidence_level) "
                "VALUES (:id, :tid, :cid, :itype, :ival, 'deterministic') "
                "ON CONFLICT (tenant_id, identity_type, identity_value) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "cid": cust_id,
                "itype": identity_type.value,
                "ival": normalized,
            },
        )
        return cust_id, True


async def _link_extra_identity(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    identity_type: IdentityType,
    raw_value: str,
) -> None:
    normalized = raw_value.strip().lower() if identity_type == IdentityType.EMAIL else raw_value.strip()
    if not normalized:
        return
    await session.execute(
        text(
            "INSERT INTO customer_identities "
            "(id, tenant_id, customer_id, identity_type, identity_value, confidence_level) "
            "VALUES (:id, :tid, :cid, :itype, :ival, 'deterministic') "
            "ON CONFLICT (tenant_id, identity_type, identity_value) DO NOTHING"
        ),
        {
            "id": uuid.uuid4(),
            "tid": tenant_id,
            "cid": customer_id,
            "itype": identity_type.value,
            "ival": normalized,
        },
    )


async def _persist_inbound_event(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    channel_account_id: uuid.UUID,
    base_channel_type: str,
    event: NormalizedInboundEvent,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    if event.parent_external_id:
        parent_exists = (
            await session.execute(
                text(
                    "SELECT 1 FROM messages WHERE tenant_id = :tid "
                    "AND external_message_id = :pid LIMIT 1"
                ),
                {"tid": tenant_id, "pid": event.parent_external_id},
            )
        ).scalar_one_or_none()
        if parent_exists is None:
            raise OutOfOrderParentError(
                f"Parent message {event.parent_external_id} not yet arrived"
            )

    customer_id, _ = await _resolve_identity_customer(
        session=session,
        tenant_id=tenant_id,
        identity_type=event.sender_identity_type,
        raw_value=event.sender_identity_value,
        display_name=event.sender_display_name,
        country_hint=event.country_hint,
    )
    for extra_type, extra_val in event.extra_identities:
        await _link_extra_identity(session, tenant_id, customer_id, extra_type, extra_val)

    identity_id = (
        await session.execute(
            text(
                "SELECT id FROM customer_identities "
                "WHERE tenant_id = :tid AND customer_id = :cid AND identity_type = :itype LIMIT 1"
            ),
            {
                "tid": tenant_id,
                "cid": customer_id,
                "itype": event.sender_identity_type.value,
            },
        )
    ).scalar_one_or_none()

    effective_channel = (
        event.override_channel_type.value if event.override_channel_type else base_channel_type
    )

    # Link B2B company if inquiry parser extracted one
    if event.b2b_inquiry_data and event.b2b_inquiry_data.get("company_name"):
        try:
            from erp_core.identity.service import IdentityResolverService

            await IdentityResolverService().resolve_or_create_b2b_company(
                session=session,
                tenant_id=tenant_id,
                customer_id=customer_id,
                legal_name=event.b2b_inquiry_data["company_name"],
                country_code=(event.country_hint or "US")[:2].upper(),
            )
        except (ImportError, AttributeError):
            logger.debug("IdentityResolverService.resolve_or_create_b2b_company unavailable; skipping company link")

    conv_id: uuid.UUID | None = event.resolved_conversation_id
    if conv_id is None and event.external_thread_id:
        conv_id = (
            await session.execute(
                text(
                    "SELECT id FROM conversations "
                    "WHERE tenant_id = :tid AND channel_account_id = :aid "
                    "AND external_thread_id = :thread AND status != 'resolved' "
                    "ORDER BY updated_at DESC LIMIT 1"
                ),
                {
                    "tid": tenant_id,
                    "aid": channel_account_id,
                    "thread": event.external_thread_id,
                },
            )
        ).scalar_one_or_none()

    if conv_id is None:
        conv_id = (
            await session.execute(
                text(
                    "SELECT id FROM conversations "
                    "WHERE tenant_id = :tid AND customer_id = :cid "
                    "AND channel_account_id = :aid AND status != 'resolved' "
                    "ORDER BY updated_at DESC LIMIT 1"
                ),
                {"tid": tenant_id, "cid": customer_id, "aid": channel_account_id},
            )
        ).scalar_one_or_none()

    now_ts = event.source_timestamp
    csw_until = now_ts + timedelta(hours=24) if (event.direction == MessageDirection.INBOUND and event.opens_csw) else None
    fep_until = now_ts + timedelta(days=7) if event.ctwa_clid else None

    if conv_id is None:
        conv_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO conversations ("
                "id, tenant_id, customer_id, channel_account_id, channel_type, external_thread_id, "
                "subject, conversation_mode, status, csw_expires_at, fep_expires_at, ctwa_clid, "
                "last_inbound_at, last_message_preview, unread_count, updated_at"
                ") VALUES ("
                ":id, :tid, :cid, :aid, :ctype, :thread, :subj, :mode, :status, "
                ":csw, :fep, :ctwa, :last_in, :preview, :unread, now()"
                ")"
            ),
            {
                "id": conv_id,
                "tid": tenant_id,
                "cid": customer_id,
                "aid": channel_account_id,
                "ctype": effective_channel,
                "thread": event.external_thread_id,
                "subj": event.subject,
                "mode": event.conversation_mode.value,
                "status": ConversationStatus.OPEN.value,
                "csw": csw_until,
                "fep": fep_until,
                "ctwa": event.ctwa_clid,
                "last_in": now_ts if event.direction == MessageDirection.INBOUND else None,
                "preview": (event.body_text or "")[:180],
                "unread": 1 if event.direction == MessageDirection.INBOUND else 0,
            },
        )
    else:
        await session.execute(
            text(
                "UPDATE conversations SET "
                "csw_expires_at = COALESCE(:csw, csw_expires_at), "
                "fep_expires_at = COALESCE(:fep, fep_expires_at), "
                "ctwa_clid = COALESCE(:ctwa, ctwa_clid), "
                "last_inbound_at = CASE WHEN :is_in THEN GREATEST(COALESCE(last_inbound_at, :ts), :ts) ELSE last_inbound_at END, "
                "last_outbound_at = CASE WHEN :is_out THEN GREATEST(COALESCE(last_outbound_at, :ts), :ts) ELSE last_outbound_at END, "
                "last_message_preview = :preview, "
                "unread_count = unread_count + CASE WHEN :is_in THEN 1 ELSE 0 END, "
                "status = CASE WHEN status = 'resolved' AND :is_in THEN 'open' ELSE status END, "
                "updated_at = now() "
                "WHERE tenant_id = :tid AND id = :cid"
            ),
            {
                "tid": tenant_id,
                "cid": conv_id,
                "csw": csw_until,
                "fep": fep_until,
                "ctwa": event.ctwa_clid,
                "is_in": event.direction == MessageDirection.INBOUND,
                "is_out": event.direction == MessageDirection.OUTBOUND,
                "ts": now_ts,
                "preview": (event.body_text or "")[:180],
            },
        )

    msg_id = uuid.uuid4()
    inserted_msg_id = (
        await session.execute(
            text(
                "INSERT INTO messages ("
                "id, tenant_id, conversation_id, customer_id, customer_identity_id, "
                "channel_type, external_message_id, external_sender_id, direction, sender_type, "
                "sender_id, content_type, body_text, body_sanitized_html, attachments, "
                "channel_metadata, delivery_status, source_timestamp"
                ") VALUES ("
                ":id, :tid, :conv_id, :cid, :ciid, :ctype, :emid, :esid, :dir, :stype, "
                ":sid, :cont, :btext, :bhtml, CAST(:att AS jsonb), CAST(:meta AS jsonb), :dstat, :ts"
                ") ON CONFLICT (tenant_id, channel_type, external_message_id) "
                "WHERE external_message_id IS NOT NULL DO NOTHING "
                "RETURNING id"
            ),
            {
                "id": msg_id,
                "tid": tenant_id,
                "conv_id": conv_id,
                "cid": customer_id,
                "ciid": identity_id,
                "ctype": effective_channel,
                "emid": event.external_message_id,
                "esid": event.sender_identity_value,
                "dir": event.direction.value,
                "stype": event.sender_type.value,
                "sid": str(customer_id) if event.direction == MessageDirection.INBOUND else "coexistence_mobile",
                "cont": event.content_type.value,
                "btext": event.body_text,
                "bhtml": event.body_sanitized_html,
                "att": json.dumps([a.model_dump(mode="json") for a in event.attachments]),
                "meta": json.dumps(event.channel_metadata),
                "dstat": DeliveryStatus.DELIVERED.value,
                "ts": event.source_timestamp,
            },
        )
    ).scalar_one_or_none()

    if inserted_msg_id is not None and event.is_stop_command:
        try:
            from erp_core.channels.consent_service import ConsentAndSuppressionService

            await ConsentAndSuppressionService().record_stop_opt_out(
                session=session,
                tenant_id=tenant_id,
                customer_id=customer_id,
                channel_type=effective_channel,
                normalized_address=event.sender_identity_value,
                reason="inbound_stop_keyword",
                source_message_id=inserted_msg_id,
            )
        except ImportError:
            logger.debug("ConsentAndSuppressionService unavailable; skipping STOP opt-out hook")

    return conv_id, inserted_msg_id


async def _apply_status_event(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    status_event: NormalizedStatusEvent,
) -> uuid.UUID | None:
    updated_id = (
        await session.execute(
            text(
                "UPDATE messages SET "
                "delivery_status = :dstat, "
                "channel_metadata = channel_metadata || CAST(:meta AS jsonb) "
                "WHERE tenant_id = :tid AND external_message_id = :emid "
                "RETURNING id"
            ),
            {
                "tid": tenant_id,
                "emid": status_event.external_message_id,
                "dstat": status_event.delivery_status.value,
                "meta": json.dumps(
                    {
                        "status_error_code": status_event.error_code,
                        "status_error_message": status_event.error_message,
                        "pricing": status_event.pricing_metadata,
                    }
                ),
            },
        )
    ).scalar_one_or_none()

    if updated_id is None and status_event.require_existing_message:
        raise OutOfOrderParentError(
            f"Status update arrived before message {status_event.external_message_id}"
        )
    return updated_id


async def _move_to_dlq(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID | None,
    raw_event_id: uuid.UUID | None,
    channel_type: str,
    direction: str,
    external_event_id: str | None,
    payload: dict[str, Any],
    error_code: str | None,
    error_detail: str,
    attempt_count: int,
) -> None:
    if raw_event_id is not None:
        async with system_session(session_factory) as sys_sess:
            await sys_sess.execute(
                text(
                    "UPDATE raw_webhook_events SET "
                    "processing_status = 'dead_letter', attempt_count = :att, error_message = :err "
                    "WHERE id = :id"
                ),
                {"id": raw_event_id, "att": attempt_count, "err": error_detail[:500]},
            )
    if tenant_id is not None:
        async with tenant_session(session_factory, tenant_id) as t_sess:
            await t_sess.execute(
                text(
                    "INSERT INTO dead_letter_events ("
                    "id, tenant_id, raw_webhook_event_id, channel_type, direction, "
                    "external_event_id, payload, error_code, error_detail, attempt_count, status"
                    ") VALUES ("
                    ":id, :tid, :rid, :ctype, :dir, :eeid, CAST(:payload AS jsonb), "
                    ":ecode, :edetail, :att, 'open'"
                    ")"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tenant_id,
                    "rid": raw_event_id,
                    "ctype": channel_type,
                    "dir": direction,
                    "eeid": external_event_id,
                    "payload": json.dumps(payload),
                    "ecode": error_code,
                    "edetail": error_detail[:2000],
                    "att": attempt_count,
                },
            )


async def process_raw_webhook_event(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    raw_event_id: uuid.UUID,
    adapter_registry: AdapterRegistry | None = None,
    attempt: int = 1,
) -> str:
    """Process one persisted raw webhook event with out-of-order retry and DLQ fallback."""
    registry = adapter_registry or build_default_adapter_registry()

    async with system_session(session_factory) as sys_sess:
        row = (
            await sys_sess.execute(
                text(
                    "SELECT id, tenant_id, channel_type, external_event_id, "
                    "external_account_id, headers, raw_payload, processing_status "
                    "FROM raw_webhook_events WHERE id = :id"
                ),
                {"id": raw_event_id},
            )
        ).mappings().first()

    if row is None:
        return "missing"
    if row["processing_status"] == "processed":
        return "already_processed"

    channel_type = str(row["channel_type"])
    tenant_id: uuid.UUID | None = row["tenant_id"]
    ext_acct_id: str | None = row["external_account_id"]
    headers: dict[str, Any] = dict(row["headers"] or {})
    payload: dict[str, Any] = dict(row["raw_payload"] or {})
    channel_account_id: uuid.UUID | None = None

    async with system_session(session_factory) as sys_sess:
        if ext_acct_id:
            dir_row = (
                await sys_sess.execute(
                    text(
                        "SELECT tenant_id, channel_account_id "
                        "FROM resolve_tenant_by_channel_external_id("
                        "CAST(:ctype AS text), CAST(:ext_id AS text))"
                    ),
                    {"ctype": channel_type, "ext_id": ext_acct_id},
                )
            ).first()
            if dir_row is not None:
                tenant_id = dir_row[0]
                channel_account_id = dir_row[1]

    if tenant_id is None or channel_account_id is None:
        await _move_to_dlq(
            session_factory,
            tenant_id=tenant_id,
            raw_event_id=raw_event_id,
            channel_type=channel_type,
            direction="inbound",
            external_event_id=row["external_event_id"],
            payload=payload,
            error_code="ERR_UNRESOLVED_CHANNEL_TENANT",
            error_detail=f"Cannot resolve tenant/channel_account for {channel_type}:{ext_acct_id}",
            attempt_count=attempt,
        )
        return "dead_letter"

    adapter = registry.get(channel_type)
    if adapter is None:
        await _move_to_dlq(
            session_factory,
            tenant_id=tenant_id,
            raw_event_id=raw_event_id,
            channel_type=channel_type,
            direction="inbound",
            external_event_id=row["external_event_id"],
            payload=payload,
            error_code="ERR_ADAPTER_MISSING",
            error_detail=f"No adapter registered for {channel_type}",
            attempt_count=attempt,
        )
        return "dead_letter"

    published_events: list[dict[str, Any]] = []
    try:
        async with tenant_session(session_factory, tenant_id) as t_sess:
            batch = await adapter.parse_webhook(
                session=t_sess,
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                payload=payload,
                headers={str(k).lower(): str(v) for k, v in headers.items()},
            )

            for inbound in batch.inbound_events:
                conv_id, msg_id = await _persist_inbound_event(
                    session=t_sess,
                    tenant_id=tenant_id,
                    channel_account_id=channel_account_id,
                    base_channel_type=channel_type,
                    event=inbound,
                )
                if msg_id is not None:
                    published_events.append(
                        {
                            "event": "message.created",
                            "conversation_id": str(conv_id),
                            "message_id": str(msg_id),
                            "channel_type": (
                                inbound.override_channel_type.value
                                if inbound.override_channel_type
                                else channel_type
                            ),
                        }
                    )

            for status_ev in batch.status_events:
                updated_msg_id = await _apply_status_event(
                    session=t_sess,
                    tenant_id=tenant_id,
                    status_event=status_ev,
                )
                if updated_msg_id is not None:
                    published_events.append(
                        {
                            "event": "message.status_updated",
                            "message_id": str(updated_msg_id),
                            "delivery_status": status_ev.delivery_status.value,
                        }
                    )

        async with system_session(session_factory) as sys_sess:
            await sys_sess.execute(
                text(
                    "UPDATE raw_webhook_events SET "
                    "tenant_id = :tid, processing_status = 'processed', "
                    "attempt_count = :att, processed_at = now(), error_message = NULL "
                    "WHERE id = :id"
                ),
                {"id": raw_event_id, "tid": tenant_id, "att": attempt},
            )

        for ev in published_events:
            await redis.publish(f"inbox:events:{tenant_id}", json.dumps(ev))
        return "processed"

    except OutOfOrderParentError as exc:
        if attempt <= MAX_INGRESS_ATTEMPTS:
            async with system_session(session_factory) as sys_sess:
                await sys_sess.execute(
                    text(
                        "UPDATE raw_webhook_events SET "
                        "processing_status = 'deferred', attempt_count = :att, error_message = :err "
                        "WHERE id = :id"
                    ),
                    {"id": raw_event_id, "att": attempt, "err": str(exc)[:500]},
                )
            retry_at = datetime.now(timezone.utc).timestamp() + DEFER_DELAY_SECONDS
            await redis.zadd(
                "queue:webhooks:deferred",
                {
                    json.dumps(
                        {
                            "raw_event_id": str(raw_event_id),
                            "channel_type": channel_type,
                            "attempt": attempt + 1,
                        }
                    ): retry_at
                },
            )
            return "deferred"

        await _move_to_dlq(
            session_factory,
            tenant_id=tenant_id,
            raw_event_id=raw_event_id,
            channel_type=channel_type,
            direction="inbound",
            external_event_id=row["external_event_id"],
            payload=payload,
            error_code="ERR_OUT_OF_ORDER_EXHAUSTED",
            error_detail=str(exc),
            attempt_count=attempt,
        )
        return "dead_letter"

    except Exception as exc:
        await _move_to_dlq(
            session_factory,
            tenant_id=tenant_id,
            raw_event_id=raw_event_id,
            channel_type=channel_type,
            direction="inbound",
            external_event_id=row["external_event_id"],
            payload=payload,
            error_code="ERR_WEBHOOK_PROCESSING_FAILED",
            error_detail=f"{type(exc).__name__}: {exc}",
            attempt_count=attempt,
        )
        return "dead_letter"


async def consume_webhook_stream_batch(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    channel_type: ChannelType,
    *,
    group_name: str = "erp_webhook_workers",
    consumer_name: str = "worker-1",
    count: int = 20,
    adapter_registry: AdapterRegistry | None = None,
) -> int:
    """Consume pending events from Redis Stream via XREADGROUP and ACK on completion."""
    stream_key = f"stream:webhooks:{channel_type.value}"
    try:
        await redis.xgroup_create(stream_key, group_name, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    entries = await redis.xreadgroup(
        groupname=group_name,
        consumername=consumer_name,
        streams={stream_key: ">"},
        count=count,
        block=100,
    )
    processed = 0
    for _, messages in entries:
        for stream_msg_id, fields in messages:
            raw_id_str = (
                fields.get(b"raw_event_id", fields.get("raw_event_id"))
            )
            attempt_raw = fields.get(b"attempt", fields.get("attempt", b"1"))
            if isinstance(raw_id_str, bytes):
                raw_id_str = raw_id_str.decode()
            if isinstance(attempt_raw, bytes):
                attempt_raw = attempt_raw.decode()
            await process_raw_webhook_event(
                session_factory=session_factory,
                redis=redis,
                raw_event_id=uuid.UUID(raw_id_str),
                adapter_registry=adapter_registry,
                attempt=int(attempt_raw),
            )
            await redis.xack(stream_key, group_name, stream_msg_id)
            processed += 1
    return processed


async def send_outbound_message(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    crypto_service: EnvelopeCryptoService,
    request_payload: OutboundMessageRequest,
    adapter_registry: AdapterRegistry | None = None,
    attempt: int = 1,
) -> str:
    """Rate-limited outbound dispatch with 4^X retry scheduling and DLQ fallback."""
    registry = adapter_registry or build_default_adapter_registry()
    limiter = DualTokenBucketLimiter(redis)

    async with tenant_session(session_factory, request_payload.tenant_id) as t_sess:
        acct = (
            await t_sess.execute(
                text(
                    "SELECT channel_type, external_account_id, rate_limit_profile "
                    "FROM channel_accounts WHERE tenant_id = :tid AND id = :aid"
                ),
                {
                    "tid": request_payload.tenant_id,
                    "aid": request_payload.channel_account_id,
                },
            )
        ).mappings().first()
        if acct is None:
            return "channel_account_missing"

        is_coexistence = bool((acct["rate_limit_profile"] or {}).get("coexistence", False))
        allowed, wait_ms = await limiter.acquire(
            channel_type=request_payload.channel_type,
            account_id=str(acct["external_account_id"]),
            recipient_id=request_payload.recipient_id,
            is_coexistence=is_coexistence,
        )
        if not allowed:
            retry_at = datetime.now(timezone.utc).timestamp() + (max(wait_ms, 500) / 1000.0)
            await redis.zadd(
                "queue:outbound:delayed",
                {
                    json.dumps(
                        {
                            "payload": request_payload.model_dump(mode="json"),
                            "attempt": attempt,
                        }
                    ): retry_at
                },
            )
            return "rate_limited_requeued"

        cred_row = (
            await t_sess.execute(
                text(
                    "SELECT ciphertext, nonce, auth_tag, key_version, credential_type "
                    "FROM channel_credentials "
                    "WHERE tenant_id = :tid AND channel_account_id = :aid "
                    "ORDER BY updated_at DESC LIMIT 1"
                ),
                {
                    "tid": request_payload.tenant_id,
                    "aid": request_payload.channel_account_id,
                },
            )
        ).mappings().first()

        credentials: dict[str, Any] = {}
        if cred_row is not None:
            decrypted = await crypto_service.decrypt_secret(
                session=t_sess,
                tenant_id=request_payload.tenant_id,
                account_id=request_payload.channel_account_id,
                cred_type=str(cred_row["credential_type"]),
                ciphertext=bytes(cred_row["ciphertext"]),
                nonce=bytes(cred_row["nonce"]),
                auth_tag=bytes(cred_row["auth_tag"]),
                key_version=int(cred_row["key_version"]),
            )
            try:
                credentials = json.loads(decrypted)
            except json.JSONDecodeError:
                credentials = {"access_token": decrypted}

        adapter = registry.get(request_payload.channel_type.value)
        if adapter is None:
            raise RuntimeError(f"Missing adapter for {request_payload.channel_type.value}")

        result = await adapter.send_message(
            session=t_sess,
            tenant_id=request_payload.tenant_id,
            credentials=credentials,
            request=request_payload,
        )

        if result.success:
            await t_sess.execute(
                text(
                    "UPDATE messages SET "
                    "delivery_status = 'sent', "
                    "external_message_id = COALESCE(:emid, external_message_id), "
                    "channel_msg_cost = :cost, "
                    "channel_metadata = channel_metadata || CAST(:meta AS jsonb) "
                    "WHERE tenant_id = :tid AND id = :mid"
                ),
                {
                    "tid": request_payload.tenant_id,
                    "mid": request_payload.message_id,
                    "emid": result.external_message_id,
                    "cost": result.estimated_cost_usd,
                    "meta": json.dumps({"pricing_category": result.pricing_category}),
                },
            )
            await redis.publish(
                f"inbox:events:{request_payload.tenant_id}",
                json.dumps(
                    {
                        "event": "message.status_updated",
                        "message_id": str(request_payload.message_id),
                        "delivery_status": DeliveryStatus.SENT.value,
                    }
                ),
            )
            return "sent"

        if result.retryable and attempt < MAX_OUTBOUND_ATTEMPTS:
            backoff_sec = limiter.compute_backoff_seconds(attempt, result.error_code)
            retry_at = datetime.now(timezone.utc).timestamp() + backoff_sec
            await redis.zadd(
                "queue:outbound:delayed",
                {
                    json.dumps(
                        {
                            "payload": request_payload.model_dump(mode="json"),
                            "attempt": attempt + 1,
                        }
                    ): retry_at
                },
            )
            return "retry_scheduled"

        await t_sess.execute(
            text(
                "UPDATE messages SET delivery_status = 'failed', "
                "channel_metadata = channel_metadata || CAST(:meta AS jsonb) "
                "WHERE tenant_id = :tid AND id = :mid"
            ),
            {
                "tid": request_payload.tenant_id,
                "mid": request_payload.message_id,
                "meta": json.dumps(
                    {
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                    }
                ),
            },
        )

    await _move_to_dlq(
        session_factory,
        tenant_id=request_payload.tenant_id,
        raw_event_id=None,
        channel_type=request_payload.channel_type.value,
        direction="outbound",
        external_event_id=str(request_payload.message_id),
        payload=request_payload.model_dump(mode="json"),
        error_code=str(result.error_code) if result.error_code else "ERR_OUTBOUND_FAILED",
        error_detail=result.error_message or "Outbound delivery permanently failed",
        attempt_count=attempt,
    )
    return "dead_letter"


@dlq_router.post("/{event_id}/replay")
async def replay_dead_letter_event(
    event_id: uuid.UUID,
    request: Request,
    principal: CurrentPrincipal = Depends(_require_dlq_operator),
) -> dict[str, Any]:
    """Replay a failed inbound or outbound dead-letter event within the current tenant."""
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    registry = _get_adapter_registry(request)

    async with tenant_session(session_factory, principal.tenant_id) as t_sess:
        dlq_row = (
            await t_sess.execute(
                text(
                    "SELECT id, raw_webhook_event_id, channel_type, direction, payload, status "
                    "FROM dead_letter_events WHERE tenant_id = :tid AND id = :id"
                ),
                {"tid": principal.tenant_id, "id": event_id},
            )
        ).mappings().first()
        if dlq_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Dead letter event not found",
            )

        await t_sess.execute(
            text(
                "UPDATE dead_letter_events SET status = 'replayed', replayed_at = now() "
                "WHERE tenant_id = :tid AND id = :id"
            ),
            {"tid": principal.tenant_id, "id": event_id},
        )
        await record_audit_event(
            session=t_sess,
            tenant_id=principal.tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="channel.dlq_replayed",
            resource_type="dead_letter_event",
            resource_id=event_id,
            before_state={"status": dlq_row["status"]},
            after_state={"status": "replayed"},
        )

    outcome = "replayed"
    if dlq_row["direction"] == "inbound" and dlq_row["raw_webhook_event_id"] is not None:
        raw_id = dlq_row["raw_webhook_event_id"]
        async with system_session(session_factory) as sys_sess:
            await sys_sess.execute(
                text(
                    "UPDATE raw_webhook_events SET processing_status = 'queued', "
                    "attempt_count = 0, error_message = NULL WHERE id = :id"
                ),
                {"id": raw_id},
            )
        await redis.xadd(
            f"stream:webhooks:{dlq_row['channel_type']}",
            {
                "raw_event_id": str(raw_id),
                "channel_type": str(dlq_row["channel_type"]),
                "attempt": "1",
            },
        )
        outcome = await process_raw_webhook_event(
            session_factory=session_factory,
            redis=redis,
            raw_event_id=raw_id,
            adapter_registry=registry,
            attempt=1,
        )
    elif dlq_row["direction"] == "outbound":
        await redis.xadd(
            "stream:outbound:replay",
            {"payload": json.dumps(dlq_row["payload"])},
        )

    return {
        "event_id": str(event_id),
        "status": "replayed",
        "outcome": outcome,
    }


def register_channel_worker_tasks(broker: Any, session_factory: Any, redis: Any) -> dict[str, Any]:
    """Attach Taskiq webhook ingress and outbound dispatch tasks to the shared broker."""

    @broker.task
    async def process_webhook_event_task(raw_event_id: str, attempt: int = 1) -> str:
        return await process_raw_webhook_event(
            session_factory=session_factory,
            redis=redis,
            raw_event_id=uuid.UUID(raw_event_id),
            attempt=attempt,
        )

    @broker.task
    async def send_outbound_message_task(request_json: dict[str, Any], attempt: int = 1) -> str:
        req = OutboundMessageRequest.model_validate(request_json)
        crypto = EnvelopeCryptoService()
        return await send_outbound_message(
            session_factory=session_factory,
            redis=redis,
            crypto_service=crypto,
            request_payload=req,
            attempt=attempt,
        )

    @broker.task
    async def drain_delayed_queues_task() -> int:
        now_score = datetime.now(timezone.utc).timestamp()
        due_items = await redis.zrangebyscore("queue:webhooks:deferred", "-inf", now_score)
        count = 0
        for raw in due_items:
            removed = await redis.zrem("queue:webhooks:deferred", raw)
            if not removed:
                continue
            item = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            await process_raw_webhook_event(
                session_factory=session_factory,
                redis=redis,
                raw_event_id=uuid.UUID(item["raw_event_id"]),
                attempt=int(item.get("attempt", 1)),
            )
            count += 1
        return count

    return {
        "process_webhook_event_task": process_webhook_event_task,
        "send_outbound_message_task": send_outbound_message_task,
        "drain_delayed_queues_task": drain_delayed_queues_task,
    }
```

Modify `apps/backend/src/erp_core/api_main.py` to mount `webhook_router` and `dlq_router`:

```python
from erp_core.channels.webhook_router import webhook_router
from erp_core.channels.ingress_worker import dlq_router

app.include_router(webhook_router)
app.include_router(dlq_router)
```

Modify `apps/backend/src/erp_core/worker_main.py` to call `register_channel_worker_tasks(broker, session_factory, redis)` during worker startup.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_webhook_idempotency.py -v`
Expected: PASS (`1 passed`, validating 401 HMAC rejection, `< 100ms` 200 OK persistence, 100 duplicate webhook storm producing 1 message, out-of-order DLQ routing, and DLQ replay).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/channels/adapters/__init__.py apps/backend/src/erp_core/channels/adapters/base.py apps/backend/src/erp_core/channels/webhook_router.py apps/backend/src/erp_core/channels/ingress_worker.py apps/backend/src/erp_core/api_main.py apps/backend/src/erp_core/worker_main.py apps/backend/tests/test_webhook_idempotency.py
git commit -m "feat(channels): add <100ms HMAC webhook gateway, idempotent consumer, and DLQ replay"
```

---

### Task 2.4: `IdentityResolverService` (Deterministic Auto-Link, BSUID, `pg_trgm` L1 Merge Proposal, Reversible Split & `buyer_local_time`)

**Files:**
- Create: `apps/backend/src/erp_core/identity/schemas.py`
- Create: `apps/backend/src/erp_core/identity/service.py`
- Create: `apps/backend/src/erp_core/identity/router.py`
- Modify: `apps/backend/src/erp_core/api_main.py`
- Test: `apps/backend/tests/test_identity_graph.py`

**Interfaces:**
- Consumes: `erp_core.shared.db.tenant_session`, `erp_core.shared.enums` (`IdentityType`, `ConfidenceLevel`, `CustomerType`, `ProposalType`, `ProposalStatus`, `UserRole`), `erp_core.tenancy.dependencies` (`CurrentPrincipal`, `require_roles`), `erp_core.tenancy.audit.record_audit_event`, `erp_core.identity.models`.
- Produces:
  ```python
  class BuyerLocalTimeInfo(BaseModel): ...
  class IdentityResolverService:
      async def resolve_or_create_customer(self, session: AsyncSession, tenant_id: UUID, identity_type: IdentityType, raw_value: str, display_name: str, country_hint: str | None = None) -> tuple[UUID, bool]: ...
      async def merge_customers(self, session: AsyncSession, tenant_id: UUID, source_customer_id: UUID, target_customer_id: UUID, performed_by: str, reason: str) -> UUID: ...
      async def split_customer_identity(self, session: AsyncSession, tenant_id: UUID, merge_event_id: UUID, performed_by: str, reason: str) -> UUID: ...
      def compute_buyer_local_time(self, iana_timezone: str | None, now_utc: datetime | None = None) -> BuyerLocalTimeInfo: ...
      async def list_merge_proposals(self, session: AsyncSession, tenant_id: UUID, customer_id: UUID | None = None) -> list[MergeProposalCandidate]: ...
      async def get_customer_summaries(self, session: AsyncSession, tenant_id: UUID, customer_ids: list[UUID]) -> dict[UUID, CustomerSummaryRead]: ...
      async def resolve_or_create_b2b_company(self, session: AsyncSession, tenant_id: UUID, customer_id: UUID, legal_name: str, country_code: str = "US", website_domain: str | None = None) -> UUID: ...
  ```
  and FastAPI routes under `/api/v1/customers` (`GET /`, `GET /{customer_id}`, `PATCH /{customer_id}`, `POST /merge`, `POST /split`).

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_identity_graph.py`:

```python
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.identity.router import customer_router, _require_customer_editor
from erp_core.identity.service import IdentityResolverService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import IdentityType, ProposalType, UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal


@pytest.mark.asyncio
async def test_identity_deterministic_bsuid_probabilistic_and_reversible_split() -> None:
    migrator_engine = create_async_engine(os.environ["DATABASE_MIGRATOR_URL"], future=True)
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )

    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    service = IdentityResolverService()

    try:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'ID Graph Co')"),
                {"id": tenant_id, "slug": f"idg-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile) "
                    "VALUES (:id, :tid, 'whatsapp_cloud', :ext, 'WA', 'active', '{\"mps\": 80}'::jsonb)"
                ),
                {"id": account_id, "tid": tenant_id, "ext": f"pn_{account_id.hex[:8]}"},
            )

        async with tenant_session(session_factory, tenant_id) as sess:
            # 1. Phone E.164 normalization + deterministic WAID match
            cid_phone, created_1 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.PHONE_E164,
                "(415) 555-2671",
                "John Miller",
                country_hint="US",
            )
            assert created_1 is True

            cid_waid, created_2 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.WHATSAPP_WAID,
                "14155552671",
                "John Miller WA",
                country_hint="US",
            )
            assert created_2 is False
            assert cid_waid == cid_phone

            # Verify timezone populated from US phone prefix
            tz_val = (
                await sess.execute(
                    text("SELECT iana_timezone FROM customers WHERE id = :cid"),
                    {"cid": cid_phone},
                )
            ).scalar_one()
            assert tz_val is not None and "America/" in tz_val

            # 2. Email deterministic link (case & whitespace normalized)
            cid_email_1, created_e1 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.EMAIL,
                "  Buyer@LEDImport.DE ",
                "Klaus Weber",
                country_hint="DE",
            )
            assert created_e1 is True
            cid_email_2, created_e2 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.EMAIL,
                "buyer@ledimport.de",
                "Klaus Weber",
            )
            assert created_e2 is False
            assert cid_email_2 == cid_email_1

            # 3. WhatsApp BSUID fallback (no phone number exposed)
            cid_bsuid_1, created_b1 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.WHATSAPP_BSUID,
                "BSUID_9988776655",
                "Privacy Buyer",
            )
            assert created_b1 is True
            cid_bsuid_2, created_b2 = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.WHATSAPP_BSUID,
                "BSUID_9988776655",
                "Privacy Buyer",
            )
            assert created_b2 is False
            assert cid_bsuid_2 == cid_bsuid_1

            # 4. Probabilistic name similarity > 0.85 MUST NOT auto-merge; creates L1 proposal
            cid_ig, created_ig = await service.resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.IG_PSID,
                "ig_psid_john_90210",
                "John Miller",
                country_hint="US",
            )
            assert created_ig is True
            assert cid_ig != cid_phone

            merged_flag = (
                await sess.execute(
                    text("SELECT merged_into_customer_id FROM customers WHERE id = :cid"),
                    {"cid": cid_ig},
                )
            ).scalar_one()
            assert merged_flag is None

            proposals = await service.list_merge_proposals(sess, tenant_id, cid_ig)
            assert len(proposals) >= 1
            assert proposals[0].proposal_type == ProposalType.IDENTITY_MERGE
            assert proposals[0].target_customer_id == cid_phone
            assert proposals[0].similarity_score > 0.85

            # Attach a conversation to cid_ig before merging, then merge -> split -> verify lossless restore
            conv_ig_id = uuid.uuid4()
            await sess.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, tenant_id, customer_id, channel_account_id, channel_type, conversation_mode, status) "
                    "VALUES (:id, :tid, :cid, :aid, 'instagram_dm', 'b2c_support', 'open')"
                ),
                {"id": conv_ig_id, "tid": tenant_id, "cid": cid_ig, "aid": account_id},
            )

            merge_event_id = await service.merge_customers(
                sess,
                tenant_id,
                source_customer_id=cid_ig,
                target_customer_id=cid_phone,
                performed_by="agent_1",
                reason="Confirmed same buyer via order #1024",
            )

            conv_owner_after_merge = (
                await sess.execute(
                    text("SELECT customer_id FROM conversations WHERE id = :id"),
                    {"id": conv_ig_id},
                )
            ).scalar_one()
            assert conv_owner_after_merge == cid_phone

            split_event_id = await service.split_customer_identity(
                sess,
                tenant_id,
                merge_event_id=merge_event_id,
                performed_by="manager_1",
                reason="False positive merge",
            )
            assert split_event_id != merge_event_id

            conv_owner_after_split = (
                await sess.execute(
                    text("SELECT customer_id FROM conversations WHERE id = :id"),
                    {"id": conv_ig_id},
                )
            ).scalar_one()
            assert conv_owner_after_split == cid_ig

            src_merged_after_split = (
                await sess.execute(
                    text("SELECT merged_into_customer_id FROM customers WHERE id = :cid"),
                    {"cid": cid_ig},
                )
            ).scalar_one()
            assert src_merged_after_split is None

            ig_owner_after_split = (
                await sess.execute(
                    text(
                        "SELECT customer_id FROM customer_identities "
                        "WHERE tenant_id = :tid AND identity_value = 'ig_psid_john_90210'"
                    ),
                    {"tid": tenant_id},
                )
            ).scalar_one()
            assert ig_owner_after_split == cid_ig

        # 5. Router smoke check for /api/v1/customers
        app = FastAPI()
        app.state.session_factory = session_factory
        app.include_router(customer_router)
        app.dependency_overrides[_require_customer_editor] = lambda: CurrentPrincipal(
            user_id=uuid.uuid4(),
            tenant_id=tenant_id,
            role=UserRole.MANAGER,
            email="mgr@factory.test",
        )
        from erp_core.identity.router import _require_customer_reader

        app.dependency_overrides[_require_customer_reader] = lambda: CurrentPrincipal(
            user_id=uuid.uuid4(),
            tenant_id=tenant_id,
            role=UserRole.AGENT,
            email="agent@factory.test",
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            list_resp = await client.get("/api/v1/customers")
            assert list_resp.status_code == 200
            assert list_resp.json()["total"] >= 3
    finally:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM audit_events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM identity_merge_events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM conversations WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM customer_identities WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM channel_accounts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await runtime_engine.dispose()
        await migrator_engine.dispose()


def test_compute_buyer_local_time_quiet_hours() -> None:
    service = IdentityResolverService()

    # 2026-11-19 04:00 UTC is 23:00 (11:00 PM) America/New_York (EST UTC-5) -> quiet hours!
    night_utc = datetime(2026, 11, 19, 4, 0, tzinfo=timezone.utc)
    night_info = service.compute_buyer_local_time("America/New_York", night_utc)
    assert night_info.is_quiet_hours is True
    assert night_info.local_hour == 23
    assert night_info.recommended_send_at_utc == datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)

    # 2026-11-19 15:00 UTC is 10:00 AM EST -> working hours
    day_utc = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    day_info = service.compute_buyer_local_time("America/New_York", day_utc)
    assert day_info.is_quiet_hours is False
    assert day_info.local_hour == 10
    assert day_info.recommended_send_at_utc is None

    # Missing or unknown timezone returns safe non-quiet defaults
    unknown_info = service.compute_buyer_local_time(None, day_utc)
    assert unknown_info.iana_timezone is None
    assert unknown_info.is_quiet_hours is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_identity_graph.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.identity.schemas'`.

- [ ] **Step 3: Write `schemas.py`, `service.py`, `router.py` and mount in `api_main.py`**

Create `apps/backend/src/erp_core/identity/schemas.py`:

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.shared.enums import ConfidenceLevel, CustomerType, IdentityType, ProposalType


class BuyerLocalTimeInfo(BaseModel):
    iana_timezone: str | None = None
    local_iso: str | None = None
    formatted_time: str | None = None
    local_hour: int | None = None
    is_quiet_hours: bool = False
    recommended_send_at_utc: datetime | None = None


class CustomerIdentityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    identity_type: IdentityType
    identity_value: str
    confidence_level: ConfidenceLevel
    is_active: bool
    created_at: datetime


class MergeProposalCandidate(BaseModel):
    proposal_type: ProposalType = ProposalType.IDENTITY_MERGE
    source_customer_id: UUID
    target_customer_id: UUID
    target_display_name: str
    similarity_score: float
    reason: str
    created_at: datetime


class CustomerSummaryRead(BaseModel):
    id: UUID
    display_name: str
    customer_type: CustomerType
    primary_email: str | None = None
    primary_phone_e164: str | None = None
    country_code: str | None = None
    preferred_language: str = "en"
    buyer_local_time: BuyerLocalTimeInfo


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    customer_type: CustomerType
    company_id: UUID | None = None
    display_name: str
    primary_email: str | None = None
    primary_phone_e164: str | None = None
    country_code: str | None = None
    postal_code: str | None = None
    preferred_language: str
    iana_timezone: str | None = None
    timezone_source: str | None = None
    lifecycle_stage: str
    ltv_usd: Decimal
    merged_into_customer_id: UUID | None = None
    created_at: datetime
    buyer_local_time: BuyerLocalTimeInfo
    identities: list[CustomerIdentityRead] = Field(default_factory=list)
    merge_proposals: list[MergeProposalCandidate] = Field(default_factory=list)


class CustomerUpdateRequest(BaseModel):
    display_name: str | None = None
    customer_type: CustomerType | None = None
    company_id: UUID | None = None
    preferred_language: str | None = None
    iana_timezone: str | None = None
    postal_code: str | None = None
    lifecycle_stage: str | None = None


class MergeCustomersRequest(BaseModel):
    source_customer_id: UUID
    target_customer_id: UUID
    reason: str = Field(min_length=2, max_length=500)


class MergeCustomersResponse(BaseModel):
    merge_event_id: UUID
    source_customer_id: UUID
    target_customer_id: UUID


class SplitCustomerRequest(BaseModel):
    merge_event_id: UUID
    reason: str = Field(min_length=2, max_length=500)


class SplitCustomerResponse(BaseModel):
    split_event_id: UUID
    merge_event_id: UUID
```

Create `apps/backend/src/erp_core/identity/service.py`:

```python
from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from phonenumbers import timezone as pn_timezone
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.identity.models import (
    B2bCompany,
    Customer,
    CustomerIdentity,
    IdentityMergeEvent,
)
from erp_core.identity.schemas import (
    BuyerLocalTimeInfo,
    CustomerSummaryRead,
    MergeProposalCandidate,
)
from erp_core.shared.enums import (
    ConfidenceLevel,
    CustomerType,
    IdentityType,
    ProposalStatus,
    ProposalType,
)
from erp_core.tenancy.audit import record_audit_event

_COUNTRY_DEFAULT_TZ: dict[str, str] = {
    "US": "America/New_York",
    "CA": "America/Toronto",
    "GB": "Europe/London",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "ES": "Europe/Madrid",
    "IT": "Europe/Rome",
    "NL": "Europe/Amsterdam",
    "AU": "Australia/Sydney",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "SG": "Asia/Singapore",
    "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh",
    "BR": "America/Sao_Paulo",
    "MX": "America/Mexico_City",
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
}


class IdentityResolverService:
    """Deterministic identity resolution, L1 pg_trgm merge proposals, and reversible splits."""

    def _normalize_identity(
        self,
        identity_type: IdentityType,
        raw_value: str,
        country_hint: str | None = None,
    ) -> tuple[str, str | None, str | None, str | None]:
        """Return (normalized_value, iana_timezone, tz_source, derived_country_code)."""
        cleaned = raw_value.strip()
        if identity_type in (IdentityType.EMAIL, IdentityType.AMAZON_BUYER_EMAIL):
            return cleaned.lower(), None, None, country_hint.upper() if country_hint else None

        if identity_type in (IdentityType.PHONE_E164, IdentityType.WHATSAPP_WAID):
            candidate = cleaned
            if not candidate.startswith("+") and candidate.isdigit() and not country_hint:
                candidate = f"+{candidate}"
            try:
                parsed = phonenumbers.parse(
                    candidate, country_hint.upper() if country_hint else None
                )
            except phonenumbers.NumberParseException:
                parsed = None
            if parsed is not None and phonenumbers.is_possible_number(parsed):
                e164 = phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
                tz_list = pn_timezone.time_zones_for_number(parsed)
                iana_tz = (
                    tz_list[0]
                    if tz_list and tz_list[0] != "Etc/Unknown"
                    else None
                )
                region = phonenumbers.region_code_for_number(parsed)
                return (
                    e164,
                    iana_tz,
                    "phone_prefix" if iana_tz else None,
                    region or (country_hint.upper() if country_hint else None),
                )
            digits = "".join(ch for ch in cleaned if ch.isdigit())
            fallback_e164 = f"+{digits}" if digits else cleaned
            return fallback_e164, None, None, country_hint.upper() if country_hint else None

        hint_upper = country_hint.upper() if country_hint else None
        fallback_tz = _COUNTRY_DEFAULT_TZ.get(hint_upper) if hint_upper else None
        return (
            cleaned,
            fallback_tz,
            "shipping_country" if fallback_tz else None,
            hint_upper,
        )

    async def _follow_merge_target(
        self, session: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID
    ) -> uuid.UUID:
        current = customer_id
        for _ in range(5):
            merged_into = (
                await session.execute(
                    select(Customer.merged_into_customer_id).where(
                        Customer.tenant_id == tenant_id,
                        Customer.id == current,
                    )
                )
            ).scalar_one_or_none()
            if merged_into is None:
                return current
            current = merged_into
        return current

    async def _record_l1_merge_proposal(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        source_customer_id: uuid.UUID,
        target_customer_id: uuid.UUID,
        target_display_name: str,
        similarity_score: float,
    ) -> None:
        proposal_payload = {
            "proposal_type": ProposalType.IDENTITY_MERGE.value,
            "status": ProposalStatus.PENDING.value,
            "source_customer_id": str(source_customer_id),
            "target_customer_id": str(target_customer_id),
            "target_display_name": target_display_name,
            "similarity_score": round(float(similarity_score), 4),
            "reason": "pg_trgm_name_similarity_gt_0.85",
        }
        await record_audit_event(
            session=session,
            tenant_id=tenant_id,
            actor_type="system_cron",
            actor_id="identity_resolver_pg_trgm",
            action="identity.merge_proposal_created",
            resource_type="customer",
            resource_id=source_customer_id,
            after_state=proposal_payload,
        )
        has_ai_proposals = (
            await session.execute(text("SELECT to_regclass('public.ai_proposals')"))
        ).scalar_one_or_none()
        if has_ai_proposals is not None:
            clamped_score = round(min(max(float(similarity_score), 0.0), 1.0), 3)
            await session.execute(
                text(
                    "INSERT INTO ai_proposals ("
                    "id, tenant_id, proposal_type, status, capability, target_entity_type, "
                    "target_entity_id, proposed_payload, confidence_score"
                    ") VALUES ("
                    ":id, :tid, :ptype, :pstatus, 'cs_support', 'customer', "
                    ":src_id, CAST(:payload AS jsonb), :score"
                    ")"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tenant_id,
                    "ptype": ProposalType.IDENTITY_MERGE.value,
                    "pstatus": ProposalStatus.PENDING.value,
                    "src_id": source_customer_id,
                    "payload": json.dumps(proposal_payload),
                    "score": Decimal(str(clamped_score)),
                },
            )

    async def resolve_or_create_customer(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        identity_type: IdentityType,
        raw_value: str,
        display_name: str,
        country_hint: str | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Deterministically link existing customer or create a new one without silent probabilistic merges."""
        normalized, inferred_tz, tz_source, derived_country = self._normalize_identity(
            identity_type, raw_value, country_hint
        )

        # 1. Exact identity lookup
        existing_cid = (
            await session.execute(
                select(CustomerIdentity.customer_id).where(
                    CustomerIdentity.tenant_id == tenant_id,
                    CustomerIdentity.identity_type == identity_type.value,
                    CustomerIdentity.identity_value == normalized,
                    CustomerIdentity.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if existing_cid is not None:
            resolved = await self._follow_merge_target(session, tenant_id, existing_cid)
            return resolved, False

        # 2. Cross-type deterministic link for phone_e164 <-> whatsapp_waid
        if identity_type in (IdentityType.PHONE_E164, IdentityType.WHATSAPP_WAID):
            counterpart = (
                IdentityType.WHATSAPP_WAID
                if identity_type == IdentityType.PHONE_E164
                else IdentityType.PHONE_E164
            )
            linked_cid = (
                await session.execute(
                    select(CustomerIdentity.customer_id).where(
                        CustomerIdentity.tenant_id == tenant_id,
                        CustomerIdentity.identity_type == counterpart.value,
                        CustomerIdentity.identity_value == normalized,
                        CustomerIdentity.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if linked_cid is not None:
                resolved = await self._follow_merge_target(session, tenant_id, linked_cid)
                session.add(
                    CustomerIdentity(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        customer_id=resolved,
                        identity_type=identity_type.value,
                        identity_value=normalized,
                        confidence_level=ConfidenceLevel.DETERMINISTIC.value,
                        is_active=True,
                    )
                )
                await session.flush()
                return resolved, False

        # 3. Deterministic lookup on customers.primary_email / primary_phone_e164
        if identity_type in (IdentityType.EMAIL, IdentityType.AMAZON_BUYER_EMAIL):
            email_cid = (
                await session.execute(
                    select(Customer.id).where(
                        Customer.tenant_id == tenant_id,
                        Customer.primary_email == normalized,
                        Customer.merged_into_customer_id.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if email_cid is not None:
                session.add(
                    CustomerIdentity(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        customer_id=email_cid,
                        identity_type=identity_type.value,
                        identity_value=normalized,
                        confidence_level=ConfidenceLevel.DETERMINISTIC.value,
                        is_active=True,
                    )
                )
                await session.flush()
                return email_cid, False

        if identity_type in (IdentityType.PHONE_E164, IdentityType.WHATSAPP_WAID):
            phone_cid = (
                await session.execute(
                    select(Customer.id).where(
                        Customer.tenant_id == tenant_id,
                        Customer.primary_phone_e164 == normalized,
                        Customer.merged_into_customer_id.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if phone_cid is not None:
                session.add(
                    CustomerIdentity(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        customer_id=phone_cid,
                        identity_type=identity_type.value,
                        identity_value=normalized,
                        confidence_level=ConfidenceLevel.DETERMINISTIC.value,
                        is_active=True,
                    )
                )
                await session.flush()
                return phone_cid, False

        # 4. Create new customer record (never auto-merge probabilistically!)
        new_cid = uuid.uuid4()
        clean_name = display_name.strip() or normalized
        new_customer = Customer(
            id=new_cid,
            tenant_id=tenant_id,
            customer_type=CustomerType.B2C_CONSUMER.value,
            display_name=clean_name,
            primary_email=normalized if identity_type == IdentityType.EMAIL else None,
            primary_phone_e164=(
                normalized
                if identity_type in (IdentityType.PHONE_E164, IdentityType.WHATSAPP_WAID)
                else None
            ),
            country_code=derived_country[:2] if derived_country else None,
            preferred_language="en",
            iana_timezone=inferred_tz,
            timezone_source=tz_source,
            lifecycle_stage="lead",
            ltv_usd=Decimal("0.00"),
        )
        session.add(new_customer)
        session.add(
            CustomerIdentity(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                customer_id=new_cid,
                identity_type=identity_type.value,
                identity_value=normalized,
                confidence_level=ConfidenceLevel.DETERMINISTIC.value,
                is_active=True,
            )
        )
        await session.flush()

        # 5. Probabilistic pg_trgm similarity > 0.85 -> L1 merge proposal only
        if len(clean_name) >= 3:
            candidates = (
                await session.execute(
                    text(
                        "SELECT id, display_name, similarity(display_name, :name) AS sim "
                        "FROM customers "
                        "WHERE tenant_id = :tid AND id != :cid "
                        "AND merged_into_customer_id IS NULL "
                        "AND display_name % :name "
                        "AND similarity(display_name, :name) > 0.85 "
                        "ORDER BY sim DESC LIMIT 3"
                    ),
                    {"tid": tenant_id, "cid": new_cid, "name": clean_name},
                )
            ).mappings().all()
            for cand in candidates:
                await self._record_l1_merge_proposal(
                    session=session,
                    tenant_id=tenant_id,
                    source_customer_id=new_cid,
                    target_customer_id=cand["id"],
                    target_display_name=cand["display_name"],
                    similarity_score=float(cand["sim"]),
                )

        return new_cid, True

    async def list_merge_proposals(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID | None = None,
    ) -> list[MergeProposalCandidate]:
        """Return recorded L1 pg_trgm merge suggestions for human review."""
        if customer_id is not None:
            rows = (
                await session.execute(
                    text(
                        "SELECT after_state, created_at FROM audit_events "
                        "WHERE tenant_id = :tid AND action = 'identity.merge_proposal_created' "
                        "AND resource_id = :cid ORDER BY created_at DESC LIMIT 20"
                    ),
                    {"tid": tenant_id, "cid": customer_id},
                )
            ).mappings().all()
        else:
            rows = (
                await session.execute(
                    text(
                        "SELECT after_state, created_at FROM audit_events "
                        "WHERE tenant_id = :tid AND action = 'identity.merge_proposal_created' "
                        "ORDER BY created_at DESC LIMIT 50"
                    ),
                    {"tid": tenant_id},
                )
            ).mappings().all()

        results: list[MergeProposalCandidate] = []
        for row in rows:
            state = row["after_state"] or {}
            results.append(
                MergeProposalCandidate(
                    proposal_type=ProposalType(
                        state.get("proposal_type", ProposalType.IDENTITY_MERGE.value)
                    ),
                    source_customer_id=uuid.UUID(state["source_customer_id"]),
                    target_customer_id=uuid.UUID(state["target_customer_id"]),
                    target_display_name=str(state.get("target_display_name", "")),
                    similarity_score=float(state.get("similarity_score", 0.0)),
                    reason=str(state.get("reason", "pg_trgm_name_similarity_gt_0.85")),
                    created_at=row["created_at"],
                )
            )
        return results

    async def merge_customers(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        source_customer_id: uuid.UUID,
        target_customer_id: uuid.UUID,
        performed_by: str,
        reason: str,
    ) -> uuid.UUID:
        """Atomically merge source_customer into target_customer with full split lineage."""
        if source_customer_id == target_customer_id:
            raise ValueError("source_customer_id and target_customer_id must differ")

        ordered_ids = sorted([source_customer_id, target_customer_id], key=str)
        locked_rows = (
            await session.execute(
                select(Customer)
                .where(Customer.tenant_id == tenant_id, Customer.id.in_(ordered_ids))
                .with_for_update()
            )
        ).scalars().all()
        by_id = {c.id: c for c in locked_rows}
        source = by_id.get(source_customer_id)
        target = by_id.get(target_customer_id)
        if source is None or target is None:
            raise LookupError("Source or target customer not found")
        if source.merged_into_customer_id is not None:
            raise ValueError("Source customer is already merged")
        if target.merged_into_customer_id is not None:
            raise ValueError("Target customer is already merged into another customer")

        moved_identity_ids = list(
            (
                await session.execute(
                    select(CustomerIdentity.id).where(
                        CustomerIdentity.tenant_id == tenant_id,
                        CustomerIdentity.customer_id == source_customer_id,
                    )
                )
            ).scalars()
        )
        if moved_identity_ids:
            await session.execute(
                text(
                    "UPDATE customer_identities SET customer_id = :target "
                    "WHERE tenant_id = :tid AND id = ANY(:ids)"
                ),
                {"tid": tenant_id, "target": target_customer_id, "ids": moved_identity_ids},
            )

        moved_conv_ids = list(
            (
                await session.execute(
                    text(
                        "UPDATE conversations SET customer_id = :target, updated_at = now() "
                        "WHERE tenant_id = :tid AND customer_id = :source RETURNING id"
                    ),
                    {"tid": tenant_id, "target": target_customer_id, "source": source_customer_id},
                )
            ).scalars()
        )
        await session.execute(
            text(
                "UPDATE messages SET customer_id = :target "
                "WHERE tenant_id = :tid AND customer_id = :source"
            ),
            {"tid": tenant_id, "target": target_customer_id, "source": source_customer_id},
        )

        source.merged_into_customer_id = target_customer_id
        if target.primary_email is None and source.primary_email is not None:
            target.primary_email = source.primary_email
        if target.primary_phone_e164 is None and source.primary_phone_e164 is not None:
            target.primary_phone_e164 = source.primary_phone_e164
        if target.iana_timezone is None and source.iana_timezone is not None:
            target.iana_timezone = source.iana_timezone
            target.timezone_source = source.timezone_source
        target.ltv_usd = (target.ltv_usd or Decimal("0.00")) + (source.ltv_usd or Decimal("0.00"))

        merge_event = IdentityMergeEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            source_customer_id=source_customer_id,
            target_customer_id=target_customer_id,
            moved_identity_ids=moved_identity_ids,
            moved_conversation_ids=moved_conv_ids,
            action="merge",
            performed_by=performed_by,
            reason=reason,
        )
        session.add(merge_event)
        await session.flush()

        await record_audit_event(
            session=session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=performed_by,
            action="identity.merged",
            resource_type="customer",
            resource_id=target_customer_id,
            after_state={
                "merge_event_id": str(merge_event.id),
                "source_customer_id": str(source_customer_id),
                "moved_identity_count": len(moved_identity_ids),
                "moved_conversation_count": len(moved_conv_ids),
            },
        )
        return merge_event.id

    async def split_customer_identity(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        merge_event_id: uuid.UUID,
        performed_by: str,
        reason: str,
    ) -> uuid.UUID:
        """Losslessly reverse an earlier merge event, restoring original customer ownership."""
        merge_event = (
            await session.execute(
                select(IdentityMergeEvent).where(
                    IdentityMergeEvent.tenant_id == tenant_id,
                    IdentityMergeEvent.id == merge_event_id,
                    IdentityMergeEvent.action == "merge",
                )
            )
        ).scalar_one_or_none()
        if merge_event is None:
            raise LookupError("Merge event not found")

        already_reverted = (
            await session.execute(
                select(IdentityMergeEvent.id).where(
                    IdentityMergeEvent.tenant_id == tenant_id,
                    IdentityMergeEvent.reverted_merge_event_id == merge_event_id,
                    IdentityMergeEvent.action == "split",
                )
            )
        ).scalar_one_or_none()
        if already_reverted is not None:
            raise ValueError("Merge event has already been split")

        ordered_ids = sorted(
            [merge_event.source_customer_id, merge_event.target_customer_id], key=str
        )
        locked_rows = (
            await session.execute(
                select(Customer)
                .where(Customer.tenant_id == tenant_id, Customer.id.in_(ordered_ids))
                .with_for_update()
            )
        ).scalars().all()
        by_id = {c.id: c for c in locked_rows}
        source = by_id.get(merge_event.source_customer_id)
        if source is None:
            raise LookupError("Original source customer missing")

        source.merged_into_customer_id = None
        if merge_event.moved_identity_ids:
            await session.execute(
                text(
                    "UPDATE customer_identities SET customer_id = :source "
                    "WHERE tenant_id = :tid AND id = ANY(:ids)"
                ),
                {
                    "tid": tenant_id,
                    "source": merge_event.source_customer_id,
                    "ids": merge_event.moved_identity_ids,
                },
            )
            await session.execute(
                text(
                    "UPDATE messages SET customer_id = :source "
                    "WHERE tenant_id = :tid AND customer_identity_id = ANY(:ids)"
                ),
                {
                    "tid": tenant_id,
                    "source": merge_event.source_customer_id,
                    "ids": merge_event.moved_identity_ids,
                },
            )

        if merge_event.moved_conversation_ids:
            await session.execute(
                text(
                    "UPDATE conversations SET customer_id = :source, updated_at = now() "
                    "WHERE tenant_id = :tid AND id = ANY(:cids)"
                ),
                {
                    "tid": tenant_id,
                    "source": merge_event.source_customer_id,
                    "cids": merge_event.moved_conversation_ids,
                },
            )
            await session.execute(
                text(
                    "UPDATE messages SET customer_id = :source "
                    "WHERE tenant_id = :tid AND conversation_id = ANY(:cids)"
                ),
                {
                    "tid": tenant_id,
                    "source": merge_event.source_customer_id,
                    "cids": merge_event.moved_conversation_ids,
                },
            )

        split_event = IdentityMergeEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            source_customer_id=merge_event.source_customer_id,
            target_customer_id=merge_event.target_customer_id,
            moved_identity_ids=list(merge_event.moved_identity_ids),
            moved_conversation_ids=list(merge_event.moved_conversation_ids),
            reverted_merge_event_id=merge_event.id,
            action="split",
            performed_by=performed_by,
            reason=reason,
        )
        session.add(split_event)
        await session.flush()

        await record_audit_event(
            session=session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=performed_by,
            action="identity.split",
            resource_type="customer",
            resource_id=merge_event.source_customer_id,
            after_state={
                "split_event_id": str(split_event.id),
                "reverted_merge_event_id": str(merge_event.id),
            },
        )
        return split_event.id

    def compute_buyer_local_time(
        self, iana_timezone: str | None, now_utc: datetime | None = None
    ) -> BuyerLocalTimeInfo:
        """Compute buyer's local wall-clock time, quiet hours (21:00-08:00), and next 09:30 send slot."""
        if not iana_timezone:
            return BuyerLocalTimeInfo(is_quiet_hours=False)
        try:
            tz = ZoneInfo(iana_timezone)
        except ZoneInfoNotFoundError:
            return BuyerLocalTimeInfo(is_quiet_hours=False)

        ref_utc = now_utc or datetime.now(timezone.utc)
        if ref_utc.tzinfo is None:
            ref_utc = ref_utc.replace(tzinfo=timezone.utc)
        local_dt = ref_utc.astimezone(tz)
        is_quiet = local_dt.hour >= 21 or local_dt.hour < 8

        recommended_utc: datetime | None = None
        if is_quiet:
            target_date = local_dt.date() + timedelta(days=1) if local_dt.hour >= 21 else local_dt.date()
            next_morning_local = datetime.combine(target_date, time(9, 30), tzinfo=tz)
            recommended_utc = next_morning_local.astimezone(timezone.utc)

        return BuyerLocalTimeInfo(
            iana_timezone=iana_timezone,
            local_iso=local_dt.isoformat(),
            formatted_time=local_dt.strftime("%I:%M %p").lstrip("0"),
            local_hour=local_dt.hour,
            is_quiet_hours=is_quiet,
            recommended_send_at_utc=recommended_utc,
        )

    async def get_customer_summaries(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, CustomerSummaryRead]:
        """Public cross-module read API returning customer summaries + buyer_local_time."""
        if not customer_ids:
            return {}
        rows = (
            await session.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id,
                    Customer.id.in_(customer_ids),
                )
            )
        ).scalars().all()
        now_utc = datetime.now(timezone.utc)
        return {
            c.id: CustomerSummaryRead(
                id=c.id,
                display_name=c.display_name,
                customer_type=CustomerType(c.customer_type),
                primary_email=c.primary_email,
                primary_phone_e164=c.primary_phone_e164,
                country_code=c.country_code,
                preferred_language=c.preferred_language,
                buyer_local_time=self.compute_buyer_local_time(c.iana_timezone, now_utc),
            )
            for c in rows
        }

    async def resolve_or_create_b2b_company(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        legal_name: str,
        country_code: str = "US",
        website_domain: str | None = None,
    ) -> uuid.UUID:
        """Create or link a B2B company entity and promote the contact customer type."""
        clean_name = legal_name.strip()
        existing = (
            await session.execute(
                select(B2bCompany.id).where(
                    B2bCompany.tenant_id == tenant_id,
                    B2bCompany.legal_name == clean_name,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = uuid.uuid4()
            session.add(
                B2bCompany(
                    id=existing,
                    tenant_id=tenant_id,
                    legal_name=clean_name,
                    country_code=(country_code or "US")[:2].upper(),
                    website_domain=website_domain,
                )
            )
            await session.flush()

        cust = (
            await session.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id, Customer.id == customer_id
                )
            )
        ).scalar_one_or_none()
        if cust is not None:
            cust.company_id = existing
            cust.customer_type = CustomerType.B2B_CONTACT.value
            await session.flush()
        return existing
```

Create `apps/backend/src/erp_core/identity/router.py`:

```python
from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.identity.models import Customer, CustomerIdentity
from erp_core.identity.schemas import (
    CustomerIdentityRead,
    CustomerRead,
    CustomerUpdateRequest,
    MergeCustomersRequest,
    MergeCustomersResponse,
    SplitCustomerRequest,
    SplitCustomerResponse,
)
from erp_core.identity.service import IdentityResolverService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import CustomerType, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

customer_router = APIRouter(prefix="/api/v1/customers", tags=["identity"])

_require_customer_reader = require_roles(
    UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT, UserRole.FINANCE
)
_require_customer_editor = require_roles(
    UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT
)
_require_customer_splitter = require_roles(
    UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER
)


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


@customer_router.get("", response_model=PaginatedResponse[CustomerRead])
async def list_customers(
    request: Request,
    q: str | None = Query(default=None),
    customer_type: CustomerType | None = Query(default=None),
    include_merged: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(_require_customer_reader),
) -> PaginatedResponse[CustomerRead]:
    session_factory = _get_session_factory(request)
    service = IdentityResolverService()
    now_utc = datetime.now(timezone.utc)

    async with tenant_session(session_factory, principal.tenant_id) as session:
        stmt = select(Customer).where(Customer.tenant_id == principal.tenant_id)
        if not include_merged:
            stmt = stmt.where(Customer.merged_into_customer_id.is_(None))
        if customer_type is not None:
            stmt = stmt.where(Customer.customer_type == customer_type.value)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Customer.display_name.ilike(pattern),
                    Customer.primary_email.ilike(pattern),
                    Customer.primary_phone_e164.ilike(pattern),
                )
            )

        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        customers = (
            await session.execute(
                stmt.order_by(Customer.created_at.desc()).limit(limit).offset(offset)
            )
        ).scalars().all()

        cids = [c.id for c in customers]
        identities_by_cid: dict[UUID, list[CustomerIdentityRead]] = {cid: [] for cid in cids}
        if cids:
            ident_rows = (
                await session.execute(
                    select(CustomerIdentity).where(
                        CustomerIdentity.tenant_id == principal.tenant_id,
                        CustomerIdentity.customer_id.in_(cids),
                    )
                )
            ).scalars().all()
            for ident in ident_rows:
                identities_by_cid[ident.customer_id].append(
                    CustomerIdentityRead.model_validate(ident)
                )

        items = [
            CustomerRead(
                id=c.id,
                tenant_id=c.tenant_id,
                customer_type=CustomerType(c.customer_type),
                company_id=c.company_id,
                display_name=c.display_name,
                primary_email=c.primary_email,
                primary_phone_e164=c.primary_phone_e164,
                country_code=c.country_code,
                postal_code=c.postal_code,
                preferred_language=c.preferred_language,
                iana_timezone=c.iana_timezone,
                timezone_source=c.timezone_source,
                lifecycle_stage=c.lifecycle_stage,
                ltv_usd=c.ltv_usd,
                merged_into_customer_id=c.merged_into_customer_id,
                created_at=c.created_at,
                buyer_local_time=service.compute_buyer_local_time(c.iana_timezone, now_utc),
                identities=identities_by_cid.get(c.id, []),
                merge_proposals=[],
            )
            for c in customers
        ]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )


@customer_router.get("/{customer_id}", response_model=CustomerRead)
async def get_customer(
    customer_id: UUID,
    request: Request,
    principal: CurrentPrincipal = Depends(_require_customer_reader),
) -> CustomerRead:
    session_factory = _get_session_factory(request)
    service = IdentityResolverService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        cust = (
            await session.execute(
                select(Customer).where(
                    Customer.tenant_id == principal.tenant_id,
                    Customer.id == customer_id,
                )
            )
        ).scalar_one_or_none()
        if cust is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
            )
        ident_rows = (
            await session.execute(
                select(CustomerIdentity).where(
                    CustomerIdentity.tenant_id == principal.tenant_id,
                    CustomerIdentity.customer_id == customer_id,
                )
            )
        ).scalars().all()
        proposals = await service.list_merge_proposals(session, principal.tenant_id, customer_id)
        return CustomerRead(
            id=cust.id,
            tenant_id=cust.tenant_id,
            customer_type=CustomerType(cust.customer_type),
            company_id=cust.company_id,
            display_name=cust.display_name,
            primary_email=cust.primary_email,
            primary_phone_e164=cust.primary_phone_e164,
            country_code=cust.country_code,
            postal_code=cust.postal_code,
            preferred_language=cust.preferred_language,
            iana_timezone=cust.iana_timezone,
            timezone_source=cust.timezone_source,
            lifecycle_stage=cust.lifecycle_stage,
            ltv_usd=cust.ltv_usd,
            merged_into_customer_id=cust.merged_into_customer_id,
            created_at=cust.created_at,
            buyer_local_time=service.compute_buyer_local_time(cust.iana_timezone),
            identities=[CustomerIdentityRead.model_validate(i) for i in ident_rows],
            merge_proposals=proposals,
        )


@customer_router.patch("/{customer_id}", response_model=CustomerRead)
async def update_customer(
    customer_id: UUID,
    payload: CustomerUpdateRequest,
    request: Request,
    principal: CurrentPrincipal = Depends(_require_customer_editor),
) -> CustomerRead:
    session_factory = _get_session_factory(request)
    service = IdentityResolverService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        cust = (
            await session.execute(
                select(Customer).where(
                    Customer.tenant_id == principal.tenant_id,
                    Customer.id == customer_id,
                )
            )
        ).scalar_one_or_none()
        if cust is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
            )

        if payload.display_name is not None:
            cust.display_name = payload.display_name.strip()
        if payload.customer_type is not None:
            cust.customer_type = payload.customer_type.value
        if payload.company_id is not None:
            cust.company_id = payload.company_id
        if payload.preferred_language is not None:
            cust.preferred_language = payload.preferred_language.strip()
        if payload.iana_timezone is not None:
            tz_info = service.compute_buyer_local_time(payload.iana_timezone)
            if tz_info.iana_timezone is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid IANA timezone",
                )
            cust.iana_timezone = tz_info.iana_timezone
            cust.timezone_source = "manual"
        if payload.postal_code is not None:
            cust.postal_code = payload.postal_code.strip()
        if payload.lifecycle_stage is not None:
            cust.lifecycle_stage = payload.lifecycle_stage.strip()
        await session.flush()

        ident_rows = (
            await session.execute(
                select(CustomerIdentity).where(
                    CustomerIdentity.tenant_id == principal.tenant_id,
                    CustomerIdentity.customer_id == customer_id,
                )
            )
        ).scalars().all()
        proposals = await service.list_merge_proposals(session, principal.tenant_id, customer_id)
        return CustomerRead(
            id=cust.id,
            tenant_id=cust.tenant_id,
            customer_type=CustomerType(cust.customer_type),
            company_id=cust.company_id,
            display_name=cust.display_name,
            primary_email=cust.primary_email,
            primary_phone_e164=cust.primary_phone_e164,
            country_code=cust.country_code,
            postal_code=cust.postal_code,
            preferred_language=cust.preferred_language,
            iana_timezone=cust.iana_timezone,
            timezone_source=cust.timezone_source,
            lifecycle_stage=cust.lifecycle_stage,
            ltv_usd=cust.ltv_usd,
            merged_into_customer_id=cust.merged_into_customer_id,
            created_at=cust.created_at,
            buyer_local_time=service.compute_buyer_local_time(cust.iana_timezone),
            identities=[CustomerIdentityRead.model_validate(i) for i in ident_rows],
            merge_proposals=proposals,
        )


@customer_router.post("/merge", response_model=MergeCustomersResponse)
async def merge_customers_endpoint(
    payload: MergeCustomersRequest,
    request: Request,
    principal: CurrentPrincipal = Depends(_require_customer_editor),
) -> MergeCustomersResponse:
    session_factory = _get_session_factory(request)
    service = IdentityResolverService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            merge_event_id = await service.merge_customers(
                session=session,
                tenant_id=principal.tenant_id,
                source_customer_id=payload.source_customer_id,
                target_customer_id=payload.target_customer_id,
                performed_by=str(principal.user_id),
                reason=payload.reason,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    return MergeCustomersResponse(
        merge_event_id=merge_event_id,
        source_customer_id=payload.source_customer_id,
        target_customer_id=payload.target_customer_id,
    )


@customer_router.post("/split", response_model=SplitCustomerResponse)
async def split_customers_endpoint(
    payload: SplitCustomerRequest,
    request: Request,
    principal: CurrentPrincipal = Depends(_require_customer_splitter),
) -> SplitCustomerResponse:
    session_factory = _get_session_factory(request)
    service = IdentityResolverService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            split_event_id = await service.split_customer_identity(
                session=session,
                tenant_id=principal.tenant_id,
                merge_event_id=payload.merge_event_id,
                performed_by=str(principal.user_id),
                reason=payload.reason,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    return SplitCustomerResponse(
        split_event_id=split_event_id,
        merge_event_id=payload.merge_event_id,
    )
```

Modify `apps/backend/src/erp_core/api_main.py` to mount `customer_router`:

```python
from erp_core.identity.router import customer_router

app.include_router(customer_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_identity_graph.py -v`
Expected: PASS (`2 passed`).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/identity/schemas.py apps/backend/src/erp_core/identity/service.py apps/backend/src/erp_core/identity/router.py apps/backend/src/erp_core/api_main.py apps/backend/tests/test_identity_graph.py
git commit -m "feat(identity): implement deterministic resolution, pg_trgm proposals, reversible split, and local time"
```

---
### Task 2.5: WhatsApp Cloud API + Coexistence Adapter (`WhatsAppAdapter`)

**Files:**
- Create: `apps/backend/src/erp_core/channels/consent_service.py`
- Create: `apps/backend/src/erp_core/channels/adapters/whatsapp.py`
- Modify: `apps/backend/src/erp_core/api_main.py`
- Test: `apps/backend/tests/test_whatsapp_adapter.py`

**Interfaces:**
- Consumes: `erp_core.channels.adapters.base` (`MessagingChannelAdapter`, `NormalizedInboundEvent`, `NormalizedStatusEvent`, `NormalizedWebhookBatch`, `OutboundMessageRequest`, `OutboundSendResult`), `erp_core.shared.crypto.EnvelopeCryptoService`, `erp_core.shared.db.tenant_session`, `erp_core.shared.enums` (`ChannelType`, `IdentityType`, `MessageDirection`, `MessageSenderType`, `MessageContentType`, `DeliveryStatus`, `UserRole`), `erp_core.tenancy.dependencies` (`CurrentPrincipal`, `require_roles`), `erp_core.tenancy.audit.record_audit_event`.
- Produces:
  ```python
  class ConsentAndSuppressionService:
      def normalize_address(self, channel_type: str, raw_address: str) -> str: ...
      async def record_opt_in(self, session: AsyncSession, tenant_id: UUID, customer_id: UUID, channel_type: str, proof_source: str, scope: list[str] | None = None, disclosed_business_name: str | None = None, proof_ip: str | None = None, evidence_ref: str | None = None) -> UUID: ...
      async def record_stop_opt_out(self, session: AsyncSession, tenant_id: UUID, customer_id: UUID, channel_type: str, normalized_address: str, reason: str, source_message_id: UUID | None = None) -> UUID: ...
      async def is_suppressed(self, session: AsyncSession, tenant_id: UUID, channel_type: str, normalized_address: str) -> bool: ...
      async def has_valid_opt_in(self, session: AsyncSession, tenant_id: UUID, customer_id: UUID, channel_type: str) -> bool: ...

  class WhatsAppAdapter(MessagingChannelAdapter):
      async def complete_embedded_signup(self, session: AsyncSession, tenant_id: UUID, crypto_service: EnvelopeCryptoService, code: str, waba_id: str, phone_number_id: str, is_coexistence: bool = False, display_name: str = "WhatsApp Business", http_client: httpx.AsyncClient | None = None) -> UUID: ...
      async def evaluate_send_policy(self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID, customer_id: UUID, recipient_address: str, content_type: MessageContentType, template_category: str | None = None, now_utc: datetime | None = None) -> WhatsAppSendPolicyDecision: ...
  ```
  and `whatsapp_router` (`/api/v1/channels/whatsapp/embedded-signup`, `/api/v1/channels/whatsapp/consent/opt-in`).

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_whatsapp_adapter.py`:

```python
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import AdapterRegistry, OutboundMessageRequest
from erp_core.channels.adapters.whatsapp import WhatsAppAdapter
from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.channels.ingress_worker import process_raw_webhook_event
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, MessageContentType


@pytest.mark.asyncio
async def test_whatsapp_embedded_signup_coexistence_bsuid_ctwa_stop_and_csw_gate(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_APP_ID", "meta_app_123")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "meta_secret_123")

    migrator_engine = create_async_engine(os.environ["DATABASE_MIGRATOR_URL"], future=True)
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    tenant_id = uuid.uuid4()
    phone_number_id = f"pn_{uuid.uuid4().hex[:10]}"
    waba_id = f"waba_{uuid.uuid4().hex[:10]}"
    crypto = EnvelopeCryptoService()

    graph_calls: list[tuple[str, str, dict]] = []

    def _mock_transport_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content.decode()) if request.content else {}
        graph_calls.append((request.method, url, body))
        if "/oauth/access_token" in url:
            return httpx.Response(200, json={"access_token": "EAAG_SYSTEM_USER_TOK"})
        if "/subscribed_apps" in url:
            return httpx.Response(200, json={"success": True})
        if f"/{phone_number_id}?" in url or "fields=is_on_biz_app" in url:
            return httpx.Response(
                200, json={"is_on_biz_app": True, "platform_type": "CLOUD_API"}
            )
        if "/smb_app_data" in url:
            return httpx.Response(200, json={"success": True})
        if "/messages" in url:
            return httpx.Response(
                200, json={"messages": [{"id": f"wamid.out.{uuid.uuid4().hex[:8]}"}]}
            )
        return httpx.Response(404, json={"error": "unhandled"})

    adapter = WhatsAppAdapter(http_transport=httpx.MockTransport(_mock_transport_handler))
    registry = AdapterRegistry()
    registry.register("whatsapp", adapter)
    registry.register(ChannelType.WHATSAPP_CLOUD.value, adapter)
    registry.register(ChannelType.WHATSAPP_COEXISTENCE.value, adapter)

    try:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'WA Factory')"),
                {"id": tenant_id, "slug": f"wa-{tenant_id.hex[:8]}"},
            )

        async with tenant_session(session_factory, tenant_id) as sess:
            await crypto.provision_tenant_dek(sess, tenant_id)
            channel_account_id = await adapter.complete_embedded_signup(
                session=sess,
                tenant_id=tenant_id,
                crypto_service=crypto,
                code="oauth_code_v4",
                waba_id=waba_id,
                phone_number_id=phone_number_id,
                is_coexistence=True,
                display_name="LED Factory WA",
            )

            acct_row = (
                await sess.execute(
                    text(
                        "SELECT channel_type, rate_limit_profile FROM channel_accounts WHERE id = :id"
                    ),
                    {"id": channel_account_id},
                )
            ).mappings().one()
            assert acct_row["channel_type"] == ChannelType.WHATSAPP_COEXISTENCE.value
            assert acct_row["rate_limit_profile"]["mps"] == 20
            assert acct_row["rate_limit_profile"]["coexistence"] is True

        # Ensure /register was NOT called for Coexistence and smb_app_data WAS called twice
        assert not any(url.endswith("/register") for _, url, _ in graph_calls)
        smb_sync_types = [
            b.get("sync_type") for _, url, b in graph_calls if "/smb_app_data" in url
        ]
        assert smb_sync_types == ["smb_app_state_sync", "history"]

        # 1. Test BSUID without wa_id + CTWA 7-day FEP referral
        ctwa_wamid = f"wamid.ctwa.{uuid.uuid4().hex[:8]}"
        bsuid_val = f"US.BSUID.{uuid.uuid4().hex[:10]}"
        ctwa_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": phone_number_id},
                                "contacts": [
                                    {"profile": {"name": "BSUID Buyer"}, "user_id": bsuid_val}
                                ],
                                "messages": [
                                    {
                                        "id": ctwa_wamid,
                                        "from_user_id": bsuid_val,
                                        "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                                        "type": "text",
                                        "text": {"body": "Saw your LED strip ad, MOQ?"},
                                        "referral": {
                                            "source_type": "ad",
                                            "source_id": "ad_998877",
                                            "ctwa_clid": "CTWA_CLICK_ABC_123",
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                }
            ]
        }
        raw_ctwa_id = uuid.uuid4()
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO raw_webhook_events "
                    "(id, tenant_id, channel_type, external_event_id, external_account_id, raw_payload) "
                    "VALUES (:id, :tid, 'whatsapp_coexistence', :eid, :eaid, CAST(:payload AS jsonb))"
                ),
                {
                    "id": raw_ctwa_id,
                    "tid": tenant_id,
                    "eid": ctwa_wamid,
                    "eaid": phone_number_id,
                    "payload": json.dumps(ctwa_payload),
                },
            )
        assert await process_raw_webhook_event(session_factory, redis, raw_ctwa_id, registry) == "processed"

        async with tenant_session(session_factory, tenant_id) as sess:
            conv_row = (
                await sess.execute(
                    text(
                        "SELECT id, customer_id, ctwa_clid, fep_expires_at, csw_expires_at "
                        "FROM conversations WHERE ctwa_clid = 'CTWA_CLICK_ABC_123'"
                    )
                )
            ).mappings().one()
            assert conv_row["fep_expires_at"] is not None
            assert (conv_row["fep_expires_at"] - conv_row["csw_expires_at"]).days >= 5

            ident_row = (
                await sess.execute(
                    text(
                        "SELECT identity_type, identity_value FROM customer_identities "
                        "WHERE customer_id = :cid"
                    ),
                    {"cid": conv_row["customer_id"]},
                )
            ).mappings().one()
            assert ident_row["identity_type"] == "whatsapp_bsuid"
            assert ident_row["identity_value"] == bsuid_val

            # Outbound during 7-day FEP is allowed with free_entry_point pricing
            fep_decision = await adapter.evaluate_send_policy(
                session=sess,
                tenant_id=tenant_id,
                conversation_id=conv_row["id"],
                customer_id=conv_row["customer_id"],
                recipient_address=bsuid_val,
                content_type=MessageContentType.TEXT,
            )
            assert fep_decision.allowed is True
            assert fep_decision.pricing_category == "free_entry_point"

        # 2. Test Coexistence smb_message_echoes does NOT extend csw_expires_at
        echo_wamid = f"wamid.echo.{uuid.uuid4().hex[:8]}"
        echo_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "smb_message_echoes",
                            "value": {
                                "metadata": {"phone_number_id": phone_number_id},
                                "message_echoes": [
                                    {
                                        "id": echo_wamid,
                                        "from": "8613800138000",
                                        "to_user_id": bsuid_val,
                                        "timestamp": str(
                                            int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())
                                        ),
                                        "type": "text",
                                        "text": {"body": "Sent from phone app"},
                                    }
                                ],
                            },
                        }
                    ]
                }
            ]
        }
        raw_echo_id = uuid.uuid4()
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO raw_webhook_events "
                    "(id, tenant_id, channel_type, external_event_id, external_account_id, raw_payload) "
                    "VALUES (:id, :tid, 'whatsapp_coexistence', :eid, :eaid, CAST(:payload AS jsonb))"
                ),
                {
                    "id": raw_echo_id,
                    "tid": tenant_id,
                    "eid": echo_wamid,
                    "eaid": phone_number_id,
                    "payload": json.dumps(echo_payload),
                },
            )
        assert await process_raw_webhook_event(session_factory, redis, raw_echo_id, registry) == "processed"

        async with tenant_session(session_factory, tenant_id) as sess:
            echo_msg = (
                await sess.execute(
                    text(
                        "SELECT direction, sender_type, channel_metadata "
                        "FROM messages WHERE external_message_id = :eid"
                    ),
                    {"eid": echo_wamid},
                )
            ).mappings().one()
            assert echo_msg["direction"] == "outbound"
            assert echo_msg["sender_type"] == "human_agent"
            assert echo_msg["channel_metadata"]["coexistence_echo"] is True

        # 3. Test 24h expired non-template block & Oct 2026 utility template pricing
        async with tenant_session(session_factory, tenant_id) as sess:
            past = datetime.now(timezone.utc) - timedelta(hours=48)
            await sess.execute(
                text(
                    "UPDATE conversations SET csw_expires_at = :past, fep_expires_at = NULL "
                    "WHERE id = :cid"
                ),
                {"past": past, "cid": conv_row["id"]},
            )
            expired_decision = await adapter.evaluate_send_policy(
                session=sess,
                tenant_id=tenant_id,
                conversation_id=conv_row["id"],
                customer_id=conv_row["customer_id"],
                recipient_address=bsuid_val,
                content_type=MessageContentType.TEXT,
            )
            assert expired_decision.allowed is False
            assert expired_decision.block_reason == "ERR_WHATSAPP_CSW_EXPIRED"

            # Inside CSW (no FEP), Oct 2026 rules charge utility templates as billable_regular
            future_csw = datetime.now(timezone.utc) + timedelta(hours=12)
            await sess.execute(
                text("UPDATE conversations SET csw_expires_at = :fut WHERE id = :cid"),
                {"fut": future_csw, "cid": conv_row["id"]},
            )
            util_decision = await adapter.evaluate_send_policy(
                session=sess,
                tenant_id=tenant_id,
                conversation_id=conv_row["id"],
                customer_id=conv_row["customer_id"],
                recipient_address=bsuid_val,
                content_type=MessageContentType.TEMPLATE,
                template_category="utility",
            )
            assert util_decision.allowed is True
            assert util_decision.pricing_category == "billable_regular_2026_10"

        # 4. Test STOP auto-suppression blocking subsequent outbound sends
        stop_wamid = f"wamid.stop.{uuid.uuid4().hex[:8]}"
        stop_phone = "491701234567"
        stop_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": phone_number_id},
                                "contacts": [
                                    {"profile": {"name": "OptOut Buyer"}, "wa_id": stop_phone}
                                ],
                                "messages": [
                                    {
                                        "id": stop_wamid,
                                        "from": stop_phone,
                                        "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                                        "type": "text",
                                        "text": {"body": "STOP"},
                                    }
                                ],
                            },
                        }
                    ]
                }
            ]
        }
        raw_stop_id = uuid.uuid4()
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO raw_webhook_events "
                    "(id, tenant_id, channel_type, external_event_id, external_account_id, raw_payload) "
                    "VALUES (:id, :tid, 'whatsapp_coexistence', :eid, :eaid, CAST(:payload AS jsonb))"
                ),
                {
                    "id": raw_stop_id,
                    "tid": tenant_id,
                    "eid": stop_wamid,
                    "eaid": phone_number_id,
                    "payload": json.dumps(stop_payload),
                },
            )
        assert await process_raw_webhook_event(session_factory, redis, raw_stop_id, registry) == "processed"

        async with tenant_session(session_factory, tenant_id) as sess:
            supp_svc = ConsentAndSuppressionService()
            assert await supp_svc.is_suppressed(sess, tenant_id, "whatsapp_cloud", "+491701234567") is True

            stop_conv = (
                await sess.execute(
                    text(
                        "SELECT conv.id, conv.customer_id FROM conversations conv "
                        "JOIN customer_identities ci ON ci.customer_id = conv.customer_id "
                        "WHERE ci.identity_value = '+491701234567' LIMIT 1"
                    )
                )
            ).mappings().one()
            send_res = await adapter.send_message(
                session=sess,
                tenant_id=tenant_id,
                credentials={"access_token": "EAAG_SYSTEM_USER_TOK"},
                request=OutboundMessageRequest(
                    tenant_id=tenant_id,
                    channel_account_id=channel_account_id,
                    channel_type=ChannelType.WHATSAPP_COEXISTENCE,
                    external_account_id=phone_number_id,
                    recipient_id="+491701234567",
                    conversation_id=stop_conv["id"],
                    message_id=uuid.uuid4(),
                    content_type=MessageContentType.TEXT,
                    body_text="Follow up promo",
                ),
            )
            assert send_res.success is False
            assert "ERR_RECIPIENT_SUPPRESSED" in (send_res.error_message or "")
    finally:
        async with migrator_engine.begin() as conn:
            for tbl in (
                "audit_events",
                "suppression_list",
                "consent_records",
                "messages",
                "conversations",
                "customer_identities",
                "customers",
                "channel_credentials",
                "channel_routing_directory",
                "channel_accounts",
                "raw_webhook_events",
                "tenant_keys",
            ):
                await conn.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id = :tid"), {"tid": tenant_id}
                )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await redis.aclose()
        await runtime_engine.dispose()
        await migrator_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_whatsapp_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.channels.consent_service'`.

- [ ] **Step 3: Write `consent_service.py`, `adapters/whatsapp.py`, and mount `whatsapp_router` in `api_main.py`**

Create `apps/backend/src/erp_core/channels/consent_service.py`:

```python
from __future__ import annotations

import uuid

import phonenumbers
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ConsentAndSuppressionService:
    """Manages immutable opt-in/opt-out consent_records and zero-delay suppression_list gates."""

    def _canonical_channel(self, channel_type: str) -> str:
        lower = channel_type.strip().lower()
        if lower.startswith("whatsapp"):
            return "whatsapp"
        if lower.startswith("email") or lower in ("made_in_china_email", "alibaba_email"):
            return "email"
        return lower

    def normalize_address(self, channel_type: str, raw_address: str) -> str:
        cleaned = raw_address.strip()
        family = self._canonical_channel(channel_type)
        if family == "email":
            return cleaned.lower()
        if family == "whatsapp":
            if any(c.isalpha() for c in cleaned):
                return cleaned
            candidate = cleaned if cleaned.startswith("+") else f"+{cleaned}"
            try:
                parsed = phonenumbers.parse(candidate, None)
            except phonenumbers.NumberParseException:
                parsed = None
            if parsed is not None and phonenumbers.is_possible_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
            digits = "".join(ch for ch in cleaned if ch.isdigit())
            return f"+{digits}" if digits else cleaned
        return cleaned

    async def record_opt_in(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        channel_type: str,
        proof_source: str,
        scope: list[str] | None = None,
        disclosed_business_name: str | None = None,
        proof_ip: str | None = None,
        evidence_ref: str | None = None,
        normalized_address: str | None = None,
    ) -> uuid.UUID:
        import json

        rec_id = uuid.uuid4()
        canonical = self._canonical_channel(channel_type)
        await session.execute(
            text(
                "INSERT INTO consent_records ("
                "id, tenant_id, customer_id, channel_type, consent_status, opt_in_scope, "
                "disclosed_business_name, proof_source, proof_ip, evidence_ref"
                ") VALUES ("
                ":id, :tid, :cid, :ctype, 'opted_in', CAST(:scope AS jsonb), "
                ":biz, :src, CAST(:ip AS inet), :eref"
                ")"
            ),
            {
                "id": rec_id,
                "tid": tenant_id,
                "cid": customer_id,
                "ctype": canonical,
                "scope": json.dumps(scope or ["order_updates", "promotions"]),
                "biz": disclosed_business_name,
                "src": proof_source,
                "ip": proof_ip,
                "eref": evidence_ref,
            },
        )
        if normalized_address:
            norm_addr = self.normalize_address(channel_type, normalized_address)
            await session.execute(
                text(
                    "DELETE FROM suppression_list "
                    "WHERE tenant_id = :tid AND channel_type = :ctype AND normalized_address = :addr"
                ),
                {"tid": tenant_id, "ctype": canonical, "addr": norm_addr},
            )
        return rec_id

    async def record_stop_opt_out(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        channel_type: str,
        normalized_address: str,
        reason: str,
        source_message_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        canonical = self._canonical_channel(channel_type)
        norm_addr = self.normalize_address(channel_type, normalized_address)
        rec_id = uuid.uuid4()

        await session.execute(
            text(
                "INSERT INTO consent_records ("
                "id, tenant_id, customer_id, channel_type, consent_status, proof_source"
                ") VALUES (:id, :tid, :cid, :ctype, 'opted_out', :src)"
            ),
            {
                "id": rec_id,
                "tid": tenant_id,
                "cid": customer_id,
                "ctype": canonical,
                "src": reason,
            },
        )
        await session.execute(
            text(
                "INSERT INTO suppression_list ("
                "id, tenant_id, channel_type, normalized_address, reason, source_message_id"
                ") VALUES (:id, :tid, :ctype, :addr, :reason, :smid) "
                "ON CONFLICT (tenant_id, channel_type, normalized_address) "
                "DO UPDATE SET reason = EXCLUDED.reason, source_message_id = EXCLUDED.source_message_id"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "ctype": canonical,
                "addr": norm_addr,
                "reason": reason,
                "smid": source_message_id,
            },
        )
        return rec_id

    async def is_suppressed(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_type: str,
        normalized_address: str,
    ) -> bool:
        canonical = self._canonical_channel(channel_type)
        norm_addr = self.normalize_address(channel_type, normalized_address)
        found = (
            await session.execute(
                text(
                    "SELECT 1 FROM suppression_list "
                    "WHERE tenant_id = :tid AND channel_type IN (:c1, :c2) "
                    "AND normalized_address = :addr LIMIT 1"
                ),
                {
                    "tid": tenant_id,
                    "c1": canonical,
                    "c2": channel_type,
                    "addr": norm_addr,
                },
            )
        ).scalar_one_or_none()
        return found is not None

    async def has_valid_opt_in(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        channel_type: str,
    ) -> bool:
        canonical = self._canonical_channel(channel_type)
        latest = (
            await session.execute(
                text(
                    "SELECT consent_status FROM consent_records "
                    "WHERE tenant_id = :tid AND customer_id = :cid "
                    "AND channel_type IN (:c1, :c2) "
                    "ORDER BY recorded_at DESC LIMIT 1"
                ),
                {"tid": tenant_id, "cid": customer_id, "c1": canonical, "c2": channel_type},
            )
        ).scalar_one_or_none()
        return latest == "opted_in"
```

Create `apps/backend/src/erp_core/channels/adapters/whatsapp.py`:

```python
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, ClassVar

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedAttachment,
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import (
    ChannelStatus,
    ChannelType,
    ConversationMode,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
    UserRole,
)
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

whatsapp_router = APIRouter(prefix="/api/v1/channels/whatsapp", tags=["whatsapp"])
_require_channel_admin = require_roles(UserRole.OWNER, UserRole.ADMIN)
_require_consent_writer = require_roles(
    UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT
)

_STOP_KEYWORDS: frozenset[str] = frozenset(
    {"stop", "unsubscribe", "cancel", "opt out", "opt-out", "stop promotions", "退订"}
)
_WA_FREE_MONTHLY_SERVICE_QUOTA = 1000
_WA_RETRYABLE_CODES: frozenset[int] = frozenset({131056, 130429, 131049})


class WhatsAppSendPolicyDecision(BaseModel):
    allowed: bool
    block_reason: str | None = None
    pricing_category: str | None = None
    estimated_cost_usd: Decimal = Decimal("0.000000")
    csw_active: bool = False
    fep_active: bool = False


class EmbeddedSignupRequest(BaseModel):
    code: str = Field(min_length=4)
    waba_id: str = Field(min_length=3)
    phone_number_id: str = Field(min_length=3)
    is_coexistence: bool = False
    display_name: str = "WhatsApp Business"


class EmbeddedSignupResponse(BaseModel):
    channel_account_id: uuid.UUID
    channel_type: ChannelType
    mps_cap: int


class ConsentOptInRequest(BaseModel):
    customer_id: uuid.UUID
    normalized_phone: str
    proof_source: str = "manual_verified_form"
    disclosed_business_name: str
    scope: list[str] = Field(default_factory=lambda: ["order_updates", "promotions"])


class WhatsAppAdapter(MessagingChannelAdapter):
    """Meta WhatsApp Cloud API + Coexistence adapter enforcing 2026-10 pricing and compliance."""

    channel_types: ClassVar[tuple[ChannelType, ...]] = (
        ChannelType.WHATSAPP_CLOUD,
        ChannelType.WHATSAPP_COEXISTENCE,
    )

    def __init__(
        self,
        *,
        graph_base_url: str = "https://graph.facebook.com/v24.0",
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._graph_base_url = graph_base_url.rstrip("/")
        self._http_transport = http_transport
        self._consent_service = ConsentAndSuppressionService()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15.0, transport=self._http_transport)

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        sig_header = headers.get("x-hub-signature-256", "")
        if not sig_header.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig_header, expected)

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        events: list[tuple[str, str | None]] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                field = change.get("field", "")
                val = change.get("value", {})
                phone_id = (val.get("metadata") or {}).get("phone_number_id")
                if field == "messages":
                    for msg in val.get("messages", []):
                        events.append((str(msg["id"]), phone_id))
                    for st in val.get("statuses", []):
                        events.append(
                            (f"status:{st['id']}:{st.get('status')}:{st.get('timestamp', '')}", phone_id)
                        )
                elif field == "smb_message_echoes":
                    for echo in val.get("message_echoes", []):
                        events.append((str(echo["id"]), phone_id))
                elif field == "history":
                    for hist in val.get("history", []):
                        phase = hist.get("phase", 0)
                        chunk = hist.get("chunk_order", 0)
                        events.append((f"history:{phone_id}:{phase}:{chunk}", phone_id))
                elif field in (
                    "smb_app_state_sync",
                    "message_template_status_update",
                    "phone_number_quality_update",
                    "account_update",
                ):
                    digest = hashlib.sha256(
                        json.dumps(change, sort_keys=True).encode("utf-8")
                    ).hexdigest()[:24]
                    events.append((f"{field}:{digest}", phone_id or entry.get("id")))
        if not events:
            events.append((hashlib.sha256(raw_body).hexdigest(), None))
        return events

    def _detect_stop_command(self, msg: dict[str, Any]) -> bool:
        mtype = msg.get("type")
        candidate = ""
        if mtype == "text":
            candidate = (msg.get("text") or {}).get("body", "")
        elif mtype == "button":
            candidate = (msg.get("button") or {}).get("payload", "") or (
                msg.get("button") or {}
            ).get("text", "")
        elif mtype == "interactive":
            ir = msg.get("interactive") or {}
            candidate = (ir.get("button_reply") or {}).get("id", "") or (
                ir.get("button_reply") or {}
            ).get("title", "")
        return candidate.strip().lower() in _STOP_KEYWORDS

    def _extract_body_and_attachments(
        self, msg: dict[str, Any]
    ) -> tuple[MessageContentType, str | None, list[NormalizedAttachment]]:
        mtype = msg.get("type", "text")
        if mtype == "text":
            return MessageContentType.TEXT, (msg.get("text") or {}).get("body"), []
        if mtype == "button":
            return MessageContentType.TEXT, (msg.get("button") or {}).get("text"), []
        if mtype == "interactive":
            ir = msg.get("interactive") or {}
            title = (ir.get("button_reply") or ir.get("list_reply") or {}).get("title")
            return MessageContentType.TEXT, title, []
        if mtype in ("image", "document", "audio", "video"):
            media = msg.get(mtype) or {}
            caption = media.get("caption") or f"[{mtype}]"
            att = NormalizedAttachment(
                filename=media.get("filename") or f"whatsapp_{mtype}_{media.get('id', 'file')}",
                mime_type=media.get("mime_type") or f"{mtype}/octet-stream",
                external_media_id=media.get("id"),
            )
            return MessageContentType.MEDIA, caption, [att]
        return MessageContentType.TEXT, f"[{mtype}]", []

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        inbound: list[NormalizedInboundEvent] = []
        statuses: list[NormalizedStatusEvent] = []
        account_updates: list[dict[str, Any]] = []

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                field = change.get("field", "")
                val = change.get("value") or {}
                phone_id = str((val.get("metadata") or {}).get("phone_number_id") or "")

                contacts_by_key: dict[str, dict[str, Any]] = {}
                for c in val.get("contacts", []):
                    if c.get("wa_id"):
                        contacts_by_key[str(c["wa_id"])] = c
                    if c.get("user_id"):
                        contacts_by_key[str(c["user_id"])] = c

                if field == "messages":
                    for msg in val.get("messages", []):
                        wa_id = msg.get("from")
                        bsuid = msg.get("from_user_id")
                        contact = (
                            contacts_by_key.get(str(wa_id))
                            or contacts_by_key.get(str(bsuid))
                            or {}
                        )
                        if not bsuid:
                            bsuid = contact.get("user_id")
                        if not wa_id:
                            wa_id = contact.get("wa_id")

                        if wa_id:
                            primary_type = IdentityType.WHATSAPP_WAID
                            primary_val = str(wa_id)
                            extras = (
                                [(IdentityType.WHATSAPP_BSUID, str(bsuid))] if bsuid else []
                            )
                        elif bsuid:
                            primary_type = IdentityType.WHATSAPP_BSUID
                            primary_val = str(bsuid)
                            extras = []
                        else:
                            continue

                        display_name = (contact.get("profile") or {}).get(
                            "name"
                        ) or primary_val
                        referral = msg.get("referral") or {}
                        ctwa_clid = referral.get("ctwa_clid")
                        referral_ad_id = referral.get("source_id")
                        ctype, body_text, attachments = self._extract_body_and_attachments(msg)

                        inbound.append(
                            NormalizedInboundEvent(
                                external_event_id=str(msg["id"]),
                                external_account_id=phone_id,
                                external_message_id=str(msg["id"]),
                                external_thread_id=primary_val,
                                sender_identity_type=primary_type,
                                sender_identity_value=primary_val,
                                sender_display_name=display_name,
                                direction=MessageDirection.INBOUND,
                                sender_type=MessageSenderType.CUSTOMER,
                                content_type=ctype,
                                body_text=body_text,
                                attachments=attachments,
                                source_timestamp=datetime.fromtimestamp(
                                    int(msg["timestamp"]), tz=timezone.utc
                                ),
                                conversation_mode=ConversationMode.B2C_SUPPORT,
                                channel_metadata={
                                    "bsuid": bsuid,
                                    "wa_id": wa_id,
                                    "referral": referral or None,
                                },
                                ctwa_clid=ctwa_clid,
                                referral_ad_id=referral_ad_id,
                                extra_identities=extras,
                                is_stop_command=self._detect_stop_command(msg),
                                opens_csw=True,
                            )
                        )

                    for st in val.get("statuses", []):
                        raw_status = str(st.get("status", "sent")).lower()
                        mapped_status = DeliveryStatus(
                            raw_status
                            if raw_status in {"sent", "delivered", "read", "failed"}
                            else "sent"
                        )
                        err = (st.get("errors") or [{}])[0]
                        statuses.append(
                            NormalizedStatusEvent(
                                external_message_id=str(st["id"]),
                                external_account_id=phone_id,
                                delivery_status=mapped_status,
                                timestamp=datetime.fromtimestamp(
                                    int(st.get("timestamp", 0)), tz=timezone.utc
                                ),
                                error_code=err.get("code"),
                                error_message=err.get("title") or err.get("message"),
                                pricing_metadata=st.get("pricing") or {},
                                require_existing_message=False,
                            )
                        )

                elif field == "smb_message_echoes":
                    for echo in val.get("message_echoes", []):
                        to_waid = echo.get("to")
                        to_bsuid = echo.get("to_user_id")
                        if to_waid:
                            p_type, p_val = IdentityType.WHATSAPP_WAID, str(to_waid)
                            extras = (
                                [(IdentityType.WHATSAPP_BSUID, str(to_bsuid))]
                                if to_bsuid
                                else []
                            )
                        elif to_bsuid:
                            p_type, p_val = IdentityType.WHATSAPP_BSUID, str(to_bsuid)
                            extras = []
                        else:
                            continue

                        ctype, body_text, attachments = self._extract_body_and_attachments(echo)
                        inbound.append(
                            NormalizedInboundEvent(
                                external_event_id=str(echo["id"]),
                                external_account_id=phone_id,
                                external_message_id=str(echo["id"]),
                                external_thread_id=p_val,
                                sender_identity_type=p_type,
                                sender_identity_value=p_val,
                                sender_display_name=p_val,
                                direction=MessageDirection.OUTBOUND,
                                sender_type=MessageSenderType.HUMAN_AGENT,
                                content_type=ctype,
                                body_text=body_text,
                                attachments=attachments,
                                source_timestamp=datetime.fromtimestamp(
                                    int(echo["timestamp"]), tz=timezone.utc
                                ),
                                channel_metadata={
                                    "coexistence_echo": True,
                                    "opens_csw": False,
                                },
                                extra_identities=extras,
                                opens_csw=False,
                            )
                        )

                elif field == "history":
                    biz_phone = str((val.get("metadata") or {}).get("display_phone_number") or "")
                    for hist in val.get("history", []):
                        phase = hist.get("phase", 0)
                        chunk_order = hist.get("chunk_order", 0)
                        for thread in hist.get("threads", []):
                            thread_id = str(thread.get("id") or "")
                            for msg in thread.get("messages", []):
                                is_from_biz = str(msg.get("from", "")) == biz_phone
                                peer_val = str(msg.get("to") if is_from_biz else msg.get("from", thread_id))
                                ctype, body_text, attachments = self._extract_body_and_attachments(
                                    msg
                                )
                                inbound.append(
                                    NormalizedInboundEvent(
                                        external_event_id=str(msg["id"]),
                                        external_account_id=phone_id,
                                        external_message_id=str(msg["id"]),
                                        external_thread_id=peer_val,
                                        sender_identity_type=IdentityType.WHATSAPP_WAID,
                                        sender_identity_value=peer_val,
                                        sender_display_name=peer_val,
                                        direction=(
                                            MessageDirection.OUTBOUND
                                            if is_from_biz
                                            else MessageDirection.INBOUND
                                        ),
                                        sender_type=(
                                            MessageSenderType.HUMAN_AGENT
                                            if is_from_biz
                                            else MessageSenderType.CUSTOMER
                                        ),
                                        content_type=ctype,
                                        body_text=body_text,
                                        attachments=attachments,
                                        source_timestamp=datetime.fromtimestamp(
                                            int(msg["timestamp"]), tz=timezone.utc
                                        ),
                                        channel_metadata={
                                            "coexistence_history": True,
                                            "phase": phase,
                                            "chunk_order": chunk_order,
                                        },
                                        opens_csw=False,
                                    )
                                )

                elif field in (
                    "smb_app_state_sync",
                    "message_template_status_update",
                    "phone_number_quality_update",
                    "account_update",
                ):
                    account_updates.append({"field": field, "value": val})
                    if field == "account_update" and val.get("event") in (
                        "PARTNER_REMOVED",
                        "PRIMARY_INACTIVITY",
                        "COMPANION_INACTIVITY",
                    ):
                        await session.execute(
                            text(
                                "UPDATE channel_accounts SET status = :st, updated_at = now() "
                                "WHERE tenant_id = :tid AND id = :aid"
                            ),
                            {
                                "tid": tenant_id,
                                "aid": channel_account_id,
                                "st": ChannelStatus.DISCONNECTED.value,
                            },
                        )

        return NormalizedWebhookBatch(
            inbound_events=inbound,
            status_events=statuses,
            account_updates=account_updates,
        )

    async def complete_embedded_signup(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        crypto_service: EnvelopeCryptoService,
        code: str,
        waba_id: str,
        phone_number_id: str,
        is_coexistence: bool = False,
        display_name: str = "WhatsApp Business",
    ) -> uuid.UUID:
        """Complete Embedded Signup v4 code exchange, WABA subscription, and Coexistence sync."""
        app_id = os.environ["WHATSAPP_APP_ID"]
        app_secret = os.environ["WHATSAPP_APP_SECRET"]

        async with self._client() as client:
            tok_resp = await client.get(
                f"{self._graph_base_url}/oauth/access_token",
                params={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "code": code,
                },
            )
            tok_resp.raise_for_status()
            access_token = tok_resp.json()["access_token"]
            auth_headers = {"Authorization": f"Bearer {access_token}"}

            sub_resp = await client.post(
                f"{self._graph_base_url}/{waba_id}/subscribed_apps",
                headers=auth_headers,
            )
            sub_resp.raise_for_status()

            if is_coexistence:
                # Coexistence MUST NOT call /register; verify status & trigger 24h sync
                chk_resp = await client.get(
                    f"{self._graph_base_url}/{phone_number_id}",
                    params={"fields": "is_on_biz_app,platform_type"},
                    headers=auth_headers,
                )
                chk_resp.raise_for_status()
                for sync_type in ("smb_app_state_sync", "history"):
                    sync_resp = await client.post(
                        f"{self._graph_base_url}/{phone_number_id}/smb_app_data",
                        headers=auth_headers,
                        json={"messaging_product": "whatsapp", "sync_type": sync_type},
                    )
                    sync_resp.raise_for_status()

        channel_type = (
            ChannelType.WHATSAPP_COEXISTENCE if is_coexistence else ChannelType.WHATSAPP_CLOUD
        )
        rate_profile = {"mps": 20 if is_coexistence else 80, "coexistence": is_coexistence}
        cfg = {
            "waba_id": waba_id,
            "phone_number_id": phone_number_id,
            "is_on_biz_app": is_coexistence,
            "service_msg_period_ym": datetime.now(timezone.utc).strftime("%Y-%m"),
            "service_msg_monthly_count": 0,
        }

        channel_account_id = uuid.uuid4()
        upserted_id = (
            await session.execute(
                text(
                    "INSERT INTO channel_accounts ("
                    "id, tenant_id, channel_type, external_account_id, display_name, status, "
                    "rate_limit_profile, config"
                    ") VALUES ("
                    ":id, :tid, :ctype, :ext, :dname, 'active', "
                    "CAST(:rate AS jsonb), CAST(:cfg AS jsonb)"
                    ") ON CONFLICT (tenant_id, channel_type, external_account_id) "
                    "DO UPDATE SET display_name = EXCLUDED.display_name, status = 'active', "
                    "rate_limit_profile = EXCLUDED.rate_limit_profile, config = EXCLUDED.config, "
                    "updated_at = now() "
                    "RETURNING id"
                ),
                {
                    "id": channel_account_id,
                    "tid": tenant_id,
                    "ctype": channel_type.value,
                    "ext": phone_number_id,
                    "dname": display_name,
                    "rate": json.dumps(rate_profile),
                    "cfg": json.dumps(cfg),
                },
            )
        ).scalar_one()

        await session.execute(
            text(
                "INSERT INTO channel_routing_directory "
                "(channel_type, external_account_id, tenant_id, channel_account_id, is_active) "
                "VALUES (:ctype, :ext, :tid, :aid, true) "
                "ON CONFLICT (channel_type, external_account_id) DO UPDATE SET "
                "tenant_id = EXCLUDED.tenant_id, channel_account_id = EXCLUDED.channel_account_id, "
                "is_active = true"
            ),
            {
                "ctype": channel_type.value,
                "ext": phone_number_id,
                "tid": tenant_id,
                "aid": upserted_id,
            },
        )

        ciphertext, nonce, auth_tag, key_version = await crypto_service.encrypt_secret(
            session=session,
            tenant_id=tenant_id,
            account_id=upserted_id,
            cred_type="oauth_token",
            plaintext=json.dumps({"access_token": access_token, "waba_id": waba_id}),
        )
        await session.execute(
            text(
                "INSERT INTO channel_credentials ("
                "id, tenant_id, channel_account_id, credential_type, "
                "ciphertext, nonce, auth_tag, key_version"
                ") VALUES ("
                ":id, :tid, :aid, 'oauth_token', :ct, :nonce, :tag, :kv"
                ") ON CONFLICT (tenant_id, channel_account_id, credential_type) "
                "DO UPDATE SET ciphertext = EXCLUDED.ciphertext, nonce = EXCLUDED.nonce, "
                "auth_tag = EXCLUDED.auth_tag, key_version = EXCLUDED.key_version, updated_at = now()"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "aid": upserted_id,
                "ct": ciphertext,
                "nonce": nonce,
                "tag": auth_tag,
                "kv": key_version,
            },
        )
        return upserted_id

    async def evaluate_send_policy(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        recipient_address: str,
        content_type: MessageContentType,
        template_category: str | None = None,
        now_utc: datetime | None = None,
    ) -> WhatsAppSendPolicyDecision:
        """Enforce suppression_list, 24h CSW vs 7-day CTWA FEP, consent, and Oct 2026 pricing."""
        ref_now = now_utc or datetime.now(timezone.utc)

        if await self._consent_service.is_suppressed(
            session, tenant_id, "whatsapp", recipient_address
        ):
            return WhatsAppSendPolicyDecision(
                allowed=False,
                block_reason="ERR_RECIPIENT_SUPPRESSED",
            )

        conv_row = (
            await session.execute(
                text(
                    "SELECT conv.csw_expires_at, conv.fep_expires_at, acct.config "
                    "FROM conversations conv "
                    "JOIN channel_accounts acct ON acct.id = conv.channel_account_id "
                    "WHERE conv.tenant_id = :tid AND conv.id = :cid"
                ),
                {"tid": tenant_id, "cid": conversation_id},
            )
        ).mappings().first()
        if conv_row is None:
            return WhatsAppSendPolicyDecision(
                allowed=False, block_reason="ERR_CONVERSATION_NOT_FOUND"
            )

        csw_exp = conv_row["csw_expires_at"]
        fep_exp = conv_row["fep_expires_at"]
        csw_active = csw_exp is not None and csw_exp > ref_now
        fep_active = fep_exp is not None and fep_exp > ref_now

        if content_type != MessageContentType.TEMPLATE:
            if not (csw_active or fep_active):
                return WhatsAppSendPolicyDecision(
                    allowed=False,
                    block_reason="ERR_WHATSAPP_CSW_EXPIRED",
                    csw_active=False,
                    fep_active=False,
                )
            if fep_active:
                return WhatsAppSendPolicyDecision(
                    allowed=True,
                    pricing_category="free_entry_point",
                    estimated_cost_usd=Decimal("0.000000"),
                    csw_active=csw_active,
                    fep_active=True,
                )
            cfg = conv_row["config"] or {}
            current_ym = ref_now.strftime("%Y-%m")
            used_count = (
                int(cfg.get("service_msg_monthly_count", 0))
                if cfg.get("service_msg_period_ym") == current_ym
                else 0
            )
            if used_count < _WA_FREE_MONTHLY_SERVICE_QUOTA:
                return WhatsAppSendPolicyDecision(
                    allowed=True,
                    pricing_category="free_service_tier",
                    estimated_cost_usd=Decimal("0.000000"),
                    csw_active=True,
                    fep_active=False,
                )
            return WhatsAppSendPolicyDecision(
                allowed=True,
                pricing_category="billable_service_2026_10",
                estimated_cost_usd=Decimal("0.008000"),
                csw_active=True,
                fep_active=False,
            )

        category = (template_category or "utility").lower()
        if category == "marketing":
            norm_addr = self._consent_service.normalize_address("whatsapp", recipient_address)
            if norm_addr.startswith("+1"):
                return WhatsAppSendPolicyDecision(
                    allowed=False,
                    block_reason="ERR_WHATSAPP_US_MARKETING_PROHIBITED",
                )
            if not (csw_active or fep_active):
                has_opt_in = await self._consent_service.has_valid_opt_in(
                    session, tenant_id, customer_id, "whatsapp"
                )
                if not has_opt_in:
                    return WhatsAppSendPolicyDecision(
                        allowed=False,
                        block_reason="ERR_WHATSAPP_OPT_IN_REQUIRED",
                    )

        if fep_active:
            return WhatsAppSendPolicyDecision(
                allowed=True,
                pricing_category="free_entry_point",
                estimated_cost_usd=Decimal("0.000000"),
                csw_active=csw_active,
                fep_active=True,
            )

        # Oct 2026 rule: utility templates inside CSW are billed as regular
        return WhatsAppSendPolicyDecision(
            allowed=True,
            pricing_category="billable_regular_2026_10",
            estimated_cost_usd=Decimal("0.020000") if category == "utility" else Decimal("0.050000"),
            csw_active=csw_active,
            fep_active=False,
        )

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        conv_cust = (
            await session.execute(
                text(
                    "SELECT customer_id FROM conversations WHERE tenant_id = :tid AND id = :cid"
                ),
                {"tid": tenant_id, "cid": request.conversation_id},
            )
        ).scalar_one_or_none()
        if conv_cust is None:
            return OutboundSendResult(
                success=False, error_message="ERR_CONVERSATION_NOT_FOUND", retryable=False
            )

        decision = await self.evaluate_send_policy(
            session=session,
            tenant_id=tenant_id,
            conversation_id=request.conversation_id,
            customer_id=conv_cust,
            recipient_address=request.recipient_id,
            content_type=request.content_type,
            template_category=request.metadata.get("template_category"),
        )
        if not decision.allowed:
            return OutboundSendResult(
                success=False,
                error_message=decision.block_reason,
                retryable=False,
            )

        access_token = str(credentials.get("access_token", ""))
        body: dict[str, Any] = {"messaging_product": "whatsapp", "recipient_type": "individual"}
        norm = self._consent_service.normalize_address("whatsapp", request.recipient_id)
        if any(ch.isalpha() for ch in norm):
            body["recipient"] = norm
        else:
            body["to"] = norm.lstrip("+")

        if request.content_type == MessageContentType.TEMPLATE and request.template_name:
            body["type"] = "template"
            body["template"] = {
                "name": request.template_name,
                "language": {"code": request.template_language or "en_US"},
                "components": request.template_components,
            }
        else:
            body["type"] = "text"
            body["text"] = {"preview_url": False, "body": request.body_text or ""}

        async with self._client() as client:
            resp = await client.post(
                f"{self._graph_base_url}/{request.external_account_id}/messages",
                headers={"Authorization": f"Bearer {access_token}"},
                json=body,
            )

        if resp.status_code >= 400:
            err_data = resp.json().get("error", {}) if resp.content else {}
            err_code = err_data.get("code")
            retryable = resp.status_code >= 500 or resp.status_code == 429 or err_code in _WA_RETRYABLE_CODES
            return OutboundSendResult(
                success=False,
                error_code=int(err_code) if isinstance(err_code, int) else resp.status_code,
                error_message=err_data.get("message") or resp.text,
                retryable=retryable,
            )

        wamid = ((resp.json().get("messages") or [{}])[0]).get("id")
        if request.content_type != MessageContentType.TEMPLATE:
            current_ym = datetime.now(timezone.utc).strftime("%Y-%m")
            await session.execute(
                text(
                    "UPDATE channel_accounts SET config = jsonb_set("
                    "jsonb_set(config, '{service_msg_period_ym}', to_jsonb(CAST(:ym AS text))), "
                    "'{service_msg_monthly_count}', "
                    "to_jsonb(CASE WHEN config->>'service_msg_period_ym' = :ym "
                    "THEN COALESCE((config->>'service_msg_monthly_count')::int, 0) + 1 ELSE 1 END)"
                    "), updated_at = now() WHERE tenant_id = :tid AND id = :aid"
                ),
                {"tid": tenant_id, "aid": request.channel_account_id, "ym": current_ym},
            )

        return OutboundSendResult(
            success=True,
            external_message_id=wamid,
            estimated_cost_usd=decision.estimated_cost_usd,
            pricing_category=decision.pricing_category,
        )


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


@whatsapp_router.post("/embedded-signup", response_model=EmbeddedSignupResponse)
async def complete_whatsapp_embedded_signup(
    payload: EmbeddedSignupRequest,
    request: Request,
    principal: CurrentPrincipal = Depends(_require_channel_admin),
) -> EmbeddedSignupResponse:
    session_factory = _get_session_factory(request)
    crypto = EnvelopeCryptoService()
    adapter = WhatsAppAdapter()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            account_id = await adapter.complete_embedded_signup(
                session=session,
                tenant_id=principal.tenant_id,
                crypto_service=crypto,
                code=payload.code,
                waba_id=payload.waba_id,
                phone_number_id=payload.phone_number_id,
                is_coexistence=payload.is_coexistence,
                display_name=payload.display_name,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Meta Embedded Signup v4 failed: {exc}",
            ) from exc

        await record_audit_event(
            session=session,
            tenant_id=principal.tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="channel.whatsapp_connected",
            resource_type="channel_account",
            resource_id=account_id,
            after_state={
                "waba_id": payload.waba_id,
                "phone_number_id": payload.phone_number_id,
                "is_coexistence": payload.is_coexistence,
            },
        )

    ctype = (
        ChannelType.WHATSAPP_COEXISTENCE
        if payload.is_coexistence
        else ChannelType.WHATSAPP_CLOUD
    )
    return EmbeddedSignupResponse(
        channel_account_id=account_id,
        channel_type=ctype,
        mps_cap=20 if payload.is_coexistence else 80,
    )


@whatsapp_router.post("/consent/opt-in")
async def record_whatsapp_opt_in(
    payload: ConsentOptInRequest,
    request: Request,
    principal: CurrentPrincipal = Depends(_require_consent_writer),
) -> dict[str, str]:
    session_factory = _get_session_factory(request)
    consent = ConsentAndSuppressionService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        rec_id = await consent.record_opt_in(
            session=session,
            tenant_id=principal.tenant_id,
            customer_id=payload.customer_id,
            channel_type="whatsapp",
            proof_source=payload.proof_source,
            scope=payload.scope,
            disclosed_business_name=payload.disclosed_business_name,
            proof_ip=request.client.host if request.client else None,
            normalized_address=payload.normalized_phone,
        )
    return {"consent_record_id": str(rec_id), "status": "opted_in"}
```

Modify `apps/backend/src/erp_core/api_main.py` to mount `whatsapp_router`:

```python
from erp_core.channels.adapters.whatsapp import whatsapp_router

app.include_router(whatsapp_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_whatsapp_adapter.py -v`
Expected: PASS (`1 passed`).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/channels/consent_service.py apps/backend/src/erp_core/channels/adapters/whatsapp.py apps/backend/src/erp_core/api_main.py apps/backend/tests/test_whatsapp_adapter.py
git commit -m "feat(channels): add WhatsApp Cloud/Coexistence adapter, BSUID, CTWA FEP, and STOP suppression"
```

---

### Task 2.6: Zero-CASA Email Adapter (`EmailAdapter`) & Made-in-China / Alibaba B2B Inquiry Parser

**Files:**
- Create: `apps/backend/src/erp_core/channels/parsers/__init__.py`
- Create: `apps/backend/src/erp_core/channels/parsers/b2b_inquiry_email.py`
- Create: `apps/backend/src/erp_core/channels/adapters/email.py`
- Test: `apps/backend/tests/test_email_and_b2b_parser.py`

**Interfaces:**
- Consumes: `erp_core.channels.adapters.base`, `erp_core.shared.enums` (`ChannelType`, `ConversationMode`, `IdentityType`, `MessageContentType`, `MessageDirection`, `MessageSenderType`).
- Produces:
  ```python
  class ParsedB2BInquiry(BaseModel): ...
  class B2BInquiryEmailParser:
      def parse(self, sender_email: str, subject: str, body_text: str, reply_to: str | None = None) -> ParsedB2BInquiry | None: ...

  class EmailAdapter(MessagingChannelAdapter):
      def sanitize_email_html(self, raw_html: str) -> str: ...
      async def resolve_conversation_id(self, session: AsyncSession, tenant_id: UUID, to_addresses: list[str], in_reply_to: str | None, references: list[str]) -> UUID | None: ...
  ```

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_email_and_b2b_parser.py`:

```python
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import AdapterRegistry, OutboundMessageRequest
from erp_core.channels.adapters.email import EmailAdapter, S3AttachmentStore
from erp_core.channels.ingress_worker import process_raw_webhook_event
from erp_core.channels.parsers.b2b_inquiry_email import B2BInquiryEmailParser
from erp_core.shared.enums import ChannelType, MessageContentType


class _FakeS3AttachmentStore(S3AttachmentStore):
    def __init__(self) -> None:
        self.uploaded: dict[str, tuple[bytes, str]] = {}

    async def upload_bytes(
        self, *, key: str, data: bytes, content_type: str
    ) -> str:
        self.uploaded[key] = (data, content_type)
        return key


def test_b2b_inquiry_parser_made_in_china_and_alibaba() -> None:
    parser = B2BInquiryEmailParser()

    mic_body = """
    Dear Supplier,
    You have received a new business inquiry from Made-in-China.com!

    Inquiry ID: MIC-20261119-88421
    Product Name: 24V COB LED Strip Light Waterproof IP67 (SKU: LED-COB24V-IP67)
    Purchase Quantity: 5,000 meters
    Buyer Name: Marcus Weber
    Email: marcus@lichttechnik-gmbh.de
    Company Name: Lichttechnik Weber GmbH
    Country/Region: Germany
    Message:
    Hi team, we need 5,000 meters of 24V COB LED Strip 4000K with CE and RoHS certificates. Please quote FOB Xiamen.
    """
    parsed = parser.parse(
        sender_email="en_notification@made-in-china.com",
        subject="Inquiry about 24V COB LED Strip Light Waterproof IP67 from Germany",
        body_text=mic_body,
        reply_to="marcus@lichttechnik-gmbh.de",
    )
    assert parsed is not None
    assert parsed.platform == "made_in_china"
    assert parsed.channel_type == ChannelType.MADE_IN_CHINA_EMAIL
    assert parsed.buyer_name == "Marcus Weber"
    assert parsed.buyer_email == "marcus@lichttechnik-gmbh.de"
    assert parsed.company_name == "Lichttechnik Weber GmbH"
    assert parsed.country == "Germany"
    assert "LED-COB24V-IP67" in (parsed.product_sku_or_title or "")
    assert parsed.target_quantity == "5,000 meters"
    assert parsed.target_quantity_numeric == 5000

    ali_body = """
    Alibaba.com Trade Inquiry
    Buyer Name: Elena Petrova
    Buyer Email: elena@nordiclight.se
    Company: NordicLight AB
    Country: Sweden
    Product: High Bay LED 150W MeanWell Driver (SKU: HB-150W-MW)
    Target Quantity: 300 pcs
    Inquiry Details: Can you do OEM laser logo and DDP Stockholm?
    """
    ali_parsed = parser.parse(
        sender_email="notice@service.alibaba.com",
        subject="You have a new inquiry on Alibaba.com",
        body_text=ali_body,
    )
    assert ali_parsed is not None
    assert ali_parsed.platform == "alibaba"
    assert ali_parsed.channel_type == ChannelType.ALIBABA_EMAIL
    assert ali_parsed.buyer_email == "elena@nordiclight.se"
    assert ali_parsed.target_quantity_numeric == 300


def test_email_nh3_xss_and_tracking_pixel_stripping() -> None:
    adapter = EmailAdapter()
    dirty_html = (
        '<div><p>Hello, quote attached.</p>'
        '<script>alert("xss")</script>'
        '<iframe src="https://evil.example/payload"></iframe>'
        '<img src="https://track.example/open.gif" width="1" height="1" />'
        '<a href="javascript:alert(1)" onclick="steal()">Click</a>'
        '<p><strong>MOQ: 500 pcs</strong></p></div>'
    )
    clean = adapter.sanitize_email_html(dirty_html)
    assert "<script" not in clean.lower()
    assert "alert(" not in clean
    assert "<iframe" not in clean.lower()
    assert "track.example/open.gif" not in clean
    assert "onclick" not in clean.lower()
    assert "<strong>MOQ: 500 pcs</strong>" in clean


@pytest.mark.asyncio
async def test_email_svix_verify_rfc5233_threading_attachments_and_mic_ingest() -> None:
    migrator_engine = create_async_engine(os.environ["DATABASE_MIGRATOR_URL"], future=True)
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    fake_s3 = _FakeS3AttachmentStore()
    outbound_calls: list[dict] = []

    def _mock_resend(request: httpx.Request) -> httpx.Response:
        outbound_calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"id": f"re_{uuid.uuid4().hex[:12]}"})

    adapter = EmailAdapter(
        storage_store=fake_s3,
        http_transport=httpx.MockTransport(_mock_resend),
    )
    registry = AdapterRegistry()
    registry.register("email", adapter)
    registry.register(ChannelType.EMAIL.value, adapter)

    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    inbound_addr = f"factory-{tenant_id.hex[:6]}@inbound.mail.example.com"

    try:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'LED Export Co')"),
                {"id": tenant_id, "slug": f"email-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile) "
                    "VALUES (:id, :tid, 'email', :ext, 'Sales Inbox', 'active', '{\"mps\": 50}'::jsonb)"
                ),
                {"id": account_id, "tid": tenant_id, "ext": inbound_addr},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_routing_directory "
                    "(channel_type, external_account_id, tenant_id, channel_account_id) "
                    "VALUES ('email', :ext, :tid, :aid)"
                ),
                {"ext": inbound_addr, "tid": tenant_id, "aid": account_id},
            )

        # Verify Svix HMAC-SHA256 webhook signature
        raw_secret = b"whsec_test_email_secret_32bytes!!"
        svix_secret = "whsec_" + base64.b64encode(raw_secret).decode()
        svix_id = f"msg_{uuid.uuid4().hex[:8]}"
        svix_ts = str(int(time.time()))

        mic_payload = {
            "type": "email.received",
            "data": {
                "email_id": f"re_in_{uuid.uuid4().hex[:10]}",
                "from": "en_notification@made-in-china.com",
                "to": [inbound_addr],
                "reply_to": ["marcus@lichttechnik-gmbh.de"],
                "subject": "Inquiry about 24V COB LED Strip Light",
                "message_id": "<mic-lead-001@made-in-china.com>",
                "text": (
                    "Product Name: 24V COB LED Strip (SKU: LED-COB24V-IP67)\n"
                    "Purchase Quantity: 5,000 meters\n"
                    "Buyer Name: Marcus Weber\n"
                    "Email: marcus@lichttechnik-gmbh.de\n"
                    "Company Name: Lichttechnik Weber GmbH\n"
                    "Country/Region: Germany\n"
                    "Message: Need FOB Xiamen quote with LM-80 report."
                ),
                "html": "<p>Need FOB Xiamen quote</p><script>evil()</script>",
                "attachments": [
                    {
                        "filename": "led_drawing.pdf",
                        "content_type": "application/pdf",
                        "content_base64": base64.b64encode(b"%PDF-1.4 test").decode(),
                    }
                ],
            },
        }
        raw_bytes = json.dumps(mic_payload).encode()
        to_sign = f"{svix_id}.{svix_ts}.".encode() + raw_bytes
        sig_b64 = base64.b64encode(
            hmac.new(raw_secret, to_sign, hashlib.sha256).digest()
        ).decode()
        headers = {
            "svix-id": svix_id,
            "svix-timestamp": svix_ts,
            "svix-signature": f"v1,{sig_b64}",
        }
        assert adapter.verify_webhook_signature(raw_bytes, headers, svix_secret) is True

        raw_id_1 = uuid.uuid4()
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO raw_webhook_events "
                    "(id, tenant_id, channel_type, external_event_id, external_account_id, headers, raw_payload) "
                    "VALUES (:id, :tid, 'email', :eid, :eaid, CAST(:hdr AS jsonb), CAST(:payload AS jsonb))"
                ),
                {
                    "id": raw_id_1,
                    "tid": tenant_id,
                    "eid": svix_id,
                    "eaid": inbound_addr,
                    "hdr": json.dumps(headers),
                    "payload": json.dumps(mic_payload),
                },
            )
        assert await process_raw_webhook_event(session_factory, redis, raw_id_1, registry) == "processed"

        async with tenant_session(session_factory, tenant_id) as sess:
            conv = (
                await sess.execute(
                    text(
                        "SELECT conv.id, conv.conversation_mode, conv.channel_type, "
                        "cust.primary_email, comp.legal_name "
                        "FROM conversations conv "
                        "JOIN customers cust ON cust.id = conv.customer_id "
                        "LEFT JOIN b2b_companies comp ON comp.id = cust.company_id "
                        "LIMIT 1"
                    )
                )
            ).mappings().one()
            assert conv["conversation_mode"] == "b2b_inquiry"
            assert conv["channel_type"] == ChannelType.MADE_IN_CHINA_EMAIL.value
            assert conv["primary_email"] == "marcus@lichttechnik-gmbh.de"
            assert conv["legal_name"] == "Lichttechnik Weber GmbH"
            assert len(fake_s3.uploaded) == 1

            # Outbound send sets RFC 5322 Message-ID + RFC 5233 Reply-To +th_<conv_id>
            send_res = await adapter.send_message(
                session=sess,
                tenant_id=tenant_id,
                credentials={"api_key": "re_test_key"},
                request=OutboundMessageRequest(
                    tenant_id=tenant_id,
                    channel_account_id=account_id,
                    channel_type=ChannelType.EMAIL,
                    external_account_id=inbound_addr,
                    recipient_id="marcus@lichttechnik-gmbh.de",
                    conversation_id=conv["id"],
                    message_id=uuid.uuid4(),
                    content_type=MessageContentType.TEXT,
                    subject="Re: 24V COB LED Strip Quote",
                    body_text="Attached PI for 5,000m.",
                    from_address="sales@ledfactory.example.com",
                ),
            )
            assert send_res.success is True
            assert len(outbound_calls) == 1
            assert f"+th_{conv['id']}@" in outbound_calls[0]["reply_to"]

        # Reply WITHOUT In-Reply-To / References and WITH changed subject MUST still match via RFC 5233 Reply-To
        reply_payload = {
            "type": "email.received",
            "data": {
                "email_id": f"re_in_{uuid.uuid4().hex[:10]}",
                "from": "marcus@lichttechnik-gmbh.de",
                "to": [f"support+th_{conv['id']}@inbound.mail.example.com"],
                "subject": "Completely different subject line",
                "message_id": "<reply-no-headers-002@lichttechnik-gmbh.de>",
                "text": "Confirmed, please send PI PDF.",
            },
        }
        raw_id_2 = uuid.uuid4()
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO raw_webhook_events "
                    "(id, tenant_id, channel_type, external_event_id, external_account_id, raw_payload) "
                    "VALUES (:id, :tid, 'email', :eid, :eaid, CAST(:payload AS jsonb))"
                ),
                {
                    "id": raw_id_2,
                    "tid": tenant_id,
                    "eid": f"svix_{uuid.uuid4().hex[:8]}",
                    "eaid": inbound_addr,
                    "payload": json.dumps(reply_payload),
                },
            )
        assert await process_raw_webhook_event(session_factory, redis, raw_id_2, registry) == "processed"

        async with tenant_session(session_factory, tenant_id) as sess:
            msg_count = (
                await sess.execute(
                    text("SELECT count(*) FROM messages WHERE conversation_id = :cid"),
                    {"cid": conv["id"]},
                )
            ).scalar_one()
            assert msg_count == 2
    finally:
        async with migrator_engine.begin() as conn:
            for tbl in (
                "messages",
                "conversations",
                "customer_identities",
                "customers",
                "b2b_companies",
                "channel_routing_directory",
                "channel_accounts",
                "raw_webhook_events",
            ):
                await conn.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id = :tid"), {"tid": tenant_id}
                )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await redis.aclose()
        await runtime_engine.dispose()
        await migrator_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_email_and_b2b_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.channels.parsers'`.

- [ ] **Step 3: Write `parsers/b2b_inquiry_email.py` and `adapters/email.py`**

Create `apps/backend/src/erp_core/channels/parsers/__init__.py`:

```python
"""Structured email and marketplace inquiry parsers."""
```

Create `apps/backend/src/erp_core/channels/parsers/b2b_inquiry_email.py`:

```python
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from erp_core.shared.enums import ChannelType

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_QTY_NUM_RE = re.compile(r"(\d[\d,]*)")


class ParsedB2BInquiry(BaseModel):
    platform: Literal["made_in_china", "alibaba"]
    channel_type: ChannelType
    buyer_name: str | None = None
    buyer_email: str | None = None
    company_name: str | None = None
    country: str | None = None
    product_sku_or_title: str | None = None
    target_quantity: str | None = None
    target_quantity_numeric: int | None = None
    inquiry_message: str | None = None
    inquiry_id: str | None = None


class B2BInquiryEmailParser:
    """Extracts structured B2B buyer and SKU intent from Made-in-China and Alibaba inquiry emails."""

    def _extract_field(self, text_body: str, labels: tuple[str, ...]) -> str | None:
        for label in labels:
            pattern = re.compile(
                rf"^\s*{re.escape(label)}\s*[:：]\s*(.+?)\s*$",
                re.IGNORECASE | re.MULTILINE,
            )
            match = pattern.search(text_body)
            if match:
                val = match.group(1).strip()
                if val:
                    return val
        return None

    def _extract_message_block(self, text_body: str) -> str | None:
        pattern = re.compile(
            r"(?:Message|Inquiry Details|Content)\s*[:：]\s*(.+)",
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(text_body)
        if match:
            return match.group(1).strip()
        return None

    def parse(
        self,
        sender_email: str,
        subject: str,
        body_text: str,
        reply_to: str | None = None,
    ) -> ParsedB2BInquiry | None:
        sender_lower = sender_email.strip().lower()
        subj_lower = subject.strip().lower()

        platform: Literal["made_in_china", "alibaba"] | None = None
        if "en_notification@made-in-china.com" in sender_lower or "made-in-china.com" in sender_lower:
            platform = "made_in_china"
        elif any(
            dom in sender_lower
            for dom in ("service.alibaba.com", "alibaba-inc.com", "alibaba.com")
        ) or ("alibaba.com" in subj_lower and "inquiry" in subj_lower):
            platform = "alibaba"

        if platform is None:
            return None

        channel_type = (
            ChannelType.MADE_IN_CHINA_EMAIL
            if platform == "made_in_china"
            else ChannelType.ALIBABA_EMAIL
        )

        buyer_name = self._extract_field(
            body_text, ("Buyer Name", "Contact Name", "Sender", "From")
        )
        raw_email = self._extract_field(
            body_text, ("Buyer Email", "Email", "E-mail")
        )
        buyer_email: str | None = None
        if raw_email:
            email_match = _EMAIL_RE.search(raw_email)
            if email_match:
                buyer_email = email_match.group(0).lower()
        if buyer_email is None and reply_to:
            rt_match = _EMAIL_RE.search(reply_to)
            if rt_match and not any(
                d in rt_match.group(0).lower()
                for d in ("made-in-china.com", "alibaba.com")
            ):
                buyer_email = rt_match.group(0).lower()

        company_name = self._extract_field(
            body_text, ("Company Name", "Company", "Buyer Company")
        )
        country = self._extract_field(
            body_text, ("Country/Region", "Country", "Region")
        )
        product = self._extract_field(
            body_text, ("Product Name", "Product", "Subject")
        )
        if not product and "inquiry about " in subj_lower:
            product = subject.split("about ", 1)[-1].strip()

        target_qty = self._extract_field(
            body_text,
            ("Purchase Quantity", "Target Quantity", "Order Quantity", "Quantity", "Qty"),
        )
        qty_numeric: int | None = None
        if target_qty:
            num_match = _QTY_NUM_RE.search(target_qty)
            if num_match:
                qty_numeric = int(num_match.group(1).replace(",", ""))

        inquiry_id = self._extract_field(body_text, ("Inquiry ID", "Ref No", "Order/RFQ ID"))
        inquiry_msg = self._extract_message_block(body_text) or body_text.strip()

        return ParsedB2BInquiry(
            platform=platform,
            channel_type=channel_type,
            buyer_name=buyer_name,
            buyer_email=buyer_email,
            company_name=company_name,
            country=country,
            product_sku_or_title=product,
            target_quantity=target_qty,
            target_quantity_numeric=qty_numeric,
            inquiry_message=inquiry_msg,
            inquiry_id=inquiry_id,
        )
```

Create `apps/backend/src/erp_core/channels/adapters/email.py`:

```python
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, ClassVar

import boto3
import httpx
import nh3
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedAttachment,
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.parsers.b2b_inquiry_email import B2BInquiryEmailParser
from erp_core.shared.enums import (
    ChannelType,
    ConversationMode,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)

_SUBADDRESS_THREAD_RE = re.compile(
    r"\+th_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})@"
)
_TRACKING_PIXEL_IMG_RE = re.compile(
    r"<img\b[^>]*(?:width\s*=\s*[\"']?[01][\"']?[^>]*height\s*=\s*[\"']?[01][\"']?|"
    r"height\s*=\s*[\"']?[01][\"']?[^>]*width\s*=\s*[\"']?[01][\"']?|"
    r"display\s*:\s*none)[^>]*>",
    re.IGNORECASE,
)
_SAFE_HTML_TAGS: set[str] = {
    "p",
    "br",
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ul",
    "ol",
    "li",
    "blockquote",
    "pre",
    "code",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "h1",
    "h2",
    "h3",
    "h4",
    "a",
    "span",
    "div",
    "img",
}
_SAFE_HTML_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title", "target", "rel"},
    "img": {"alt", "width", "height", "data-blocked-src"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}


class S3AttachmentStore:
    """Uploads inbound email attachments to Cloudflare R2 / MinIO S3 under tenant isolation."""

    async def upload_bytes(
        self, *, key: str, data: bytes, content_type: str
    ) -> str:
        endpoint = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
        bucket = os.environ.get("S3_BUCKET", "erp-assets")
        access_key = os.environ.get("S3_ACCESS_KEY", "minioadmin")
        secret_key = os.environ.get("S3_SECRET_KEY", "minioadmin")
        region = os.environ.get("S3_REGION", "us-east-1")

        def _put() -> None:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
            )
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )

        await asyncio.to_thread(_put)
        return key


def _sanitize_img_attribute(tag: str, attr: str, value: str) -> str | None:
    if tag == "img" and attr == "src":
        # Proxy/block external tracking images by default
        return None
    if tag == "a" and attr == "href" and value.strip().lower().startswith("javascript:"):
        return None
    return value


class EmailAdapter(MessagingChannelAdapter):
    """Zero-CASA transactional Email adapter (Resend/Postmark/SES) with RFC 5322/5233 threading."""

    channel_types: ClassVar[tuple[ChannelType, ...]] = (
        ChannelType.EMAIL,
        ChannelType.MADE_IN_CHINA_EMAIL,
        ChannelType.ALIBABA_EMAIL,
    )

    def __init__(
        self,
        *,
        storage_store: S3AttachmentStore | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        resend_base_url: str = "https://api.resend.com",
    ) -> None:
        self._storage = storage_store or S3AttachmentStore()
        self._http_transport = http_transport
        self._resend_base_url = resend_base_url.rstrip("/")
        self._b2b_parser = B2BInquiryEmailParser()

    def sanitize_email_html(self, raw_html: str) -> str:
        if not raw_html:
            return ""
        without_pixels = _TRACKING_PIXEL_IMG_RE.sub("", raw_html)
        return nh3.clean(
            without_pixels,
            tags=_SAFE_HTML_TAGS,
            attributes=_SAFE_HTML_ATTRS,
            attribute_filter=_sanitize_img_attribute,
            link_rel="noopener noreferrer nofollow",
            strip_comments=True,
        )

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        svix_id = headers.get("svix-id")
        svix_ts = headers.get("svix-timestamp")
        svix_sig = headers.get("svix-signature")
        if svix_id and svix_ts and svix_sig:
            key_bytes = (
                base64.b64decode(secret.removeprefix("whsec_"))
                if secret.startswith("whsec_")
                else secret.encode("utf-8")
            )
            signed_payload = f"{svix_id}.{svix_ts}.".encode("utf-8") + raw_body
            expected_b64 = base64.b64encode(
                hmac.new(key_bytes, signed_payload, hashlib.sha256).digest()
            ).decode("utf-8")
            for token in svix_sig.split():
                if "," in token:
                    _, sig_val = token.split(",", 1)
                    if hmac.compare_digest(sig_val, expected_b64):
                        return True
            return False

        direct_sig = headers.get("x-email-signature", headers.get("x-webhook-signature", ""))
        if direct_sig:
            raw_sig = direct_sig.removeprefix("sha256=")
            expected_hex = hmac.new(
                secret.encode("utf-8"), raw_body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(raw_sig, expected_hex)
        return False

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        svix_id = headers.get("svix-id")
        data = payload.get("data") or payload
        to_list = data.get("to") or []
        if isinstance(to_list, str):
            to_list = [to_list]
        ext_acct = None
        for addr in to_list:
            _, parsed_email = parseaddr(str(addr))
            if parsed_email and "+th_" not in parsed_email:
                ext_acct = parsed_email.lower()
                break
        if ext_acct is None and to_list:
            _, parsed_email = parseaddr(str(to_list[0]))
            if parsed_email:
                ext_acct = _SUBADDRESS_THREAD_RE.sub("@", parsed_email.lower())

        event_id = (
            svix_id
            or data.get("message_id")
            or data.get("email_id")
            or hashlib.sha256(raw_body).hexdigest()
        )
        return [(str(event_id), ext_acct)]

    async def resolve_conversation_id(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        to_addresses: list[str],
        in_reply_to: str | None,
        references: list[str],
    ) -> uuid.UUID | None:
        """Resolve thread strictly via RFC 5233 +th_<uuid>, In-Reply-To, or References (never Subject)."""
        for addr in to_addresses:
            match = _SUBADDRESS_THREAD_RE.search(addr)
            if match:
                candidate_id = uuid.UUID(match.group(1))
                exists = (
                    await session.execute(
                        text(
                            "SELECT id FROM conversations WHERE tenant_id = :tid AND id = :cid LIMIT 1"
                        ),
                        {"tid": tenant_id, "cid": candidate_id},
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    return exists

        candidate_msg_ids: list[str] = []
        if in_reply_to:
            candidate_msg_ids.append(in_reply_to.strip())
        for ref in reversed(references):
            cleaned = ref.strip()
            if cleaned and cleaned not in candidate_msg_ids:
                candidate_msg_ids.append(cleaned)

        for ref_msg_id in candidate_msg_ids:
            conv_id = (
                await session.execute(
                    text(
                        "SELECT conversation_id FROM messages "
                        "WHERE tenant_id = :tid AND external_message_id = :emid LIMIT 1"
                    ),
                    {"tid": tenant_id, "emid": ref_msg_id},
                )
            ).scalar_one_or_none()
            if conv_id is not None:
                return conv_id
        return None

    async def _upload_attachments(
        self, tenant_id: uuid.UUID, raw_attachments: list[dict[str, Any]]
    ) -> list[NormalizedAttachment]:
        stored: list[NormalizedAttachment] = []
        for item in raw_attachments:
            filename = str(item.get("filename") or "attachment.bin").replace("/", "_")
            mime = str(item.get("content_type") or "application/octet-stream")
            raw_b64 = item.get("content_base64") or item.get("content")
            if raw_b64:
                raw_bytes = base64.b64decode(raw_b64)
            else:
                raw_bytes = b""
            r2_key = f"tenants/{tenant_id}/channels/email/attachments/{uuid.uuid4()}/{filename}"
            if raw_bytes:
                await self._storage.upload_bytes(key=r2_key, data=raw_bytes, content_type=mime)
            stored.append(
                NormalizedAttachment(
                    filename=filename,
                    mime_type=mime,
                    size_bytes=len(raw_bytes),
                    r2_key=r2_key,
                )
            )
        return stored

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        event_type = str(payload.get("type") or "email.received")
        data = payload.get("data") or payload

        if event_type in ("email.sent", "email.delivered", "email.bounced", "email.complained"):
            status_map = {
                "email.sent": DeliveryStatus.SENT,
                "email.delivered": DeliveryStatus.DELIVERED,
                "email.bounced": DeliveryStatus.FAILED,
                "email.complained": DeliveryStatus.FAILED,
            }
            msg_id_ref = str(data.get("message_id") or data.get("email_id") or "")
            return NormalizedWebhookBatch(
                status_events=[
                    NormalizedStatusEvent(
                        external_message_id=msg_id_ref,
                        external_account_id="",
                        delivery_status=status_map[event_type],
                        timestamp=datetime.now(timezone.utc),
                    )
                ]
            )

        if data.get("raw_mime_base64"):
            mime_bytes = base64.b64decode(data["raw_mime_base64"])
            parsed_msg = BytesParser(policy=policy.default).parsebytes(mime_bytes)
            from_header = str(parsed_msg.get("From", ""))
            to_headers = [str(v) for v in parsed_msg.get_all("To", [])]
            reply_to_list = [str(parsed_msg.get("Reply-To", ""))] if parsed_msg.get("Reply-To") else []
            subject = str(parsed_msg.get("Subject", ""))
            message_id = str(parsed_msg.get("Message-ID", f"<{uuid.uuid4()}@inbound>"))
            in_reply_to = parsed_msg.get("In-Reply-To")
            references_raw = str(parsed_msg.get("References", ""))
            text_part = parsed_msg.get_body(preferencelist=("plain",))
            html_part = parsed_msg.get_body(preferencelist=("html",))
            body_text = text_part.get_content() if text_part else ""
            raw_html = html_part.get_content() if html_part else ""
            raw_attachments = []
            for part in parsed_msg.iter_attachments():
                raw_attachments.append(
                    {
                        "filename": part.get_filename() or "attachment.bin",
                        "content_type": part.get_content_type(),
                        "content_base64": base64.b64encode(part.get_payload(decode=True) or b"").decode(),
                    }
                )
            date_hdr = parsed_msg.get("Date")
            source_ts = (
                parsedate_to_datetime(date_hdr) if date_hdr else datetime.now(timezone.utc)
            )
        else:
            from_header = str(data.get("from", ""))
            to_raw = data.get("to") or []
            to_headers = [to_raw] if isinstance(to_raw, str) else [str(x) for x in to_raw]
            reply_raw = data.get("reply_to") or []
            reply_to_list = [reply_raw] if isinstance(reply_raw, str) else [str(x) for x in reply_raw]
            subject = str(data.get("subject", ""))
            hdrs = {str(k).lower(): str(v) for k, v in (data.get("headers") or {}).items()}
            message_id = str(
                data.get("message_id")
                or hdrs.get("message-id")
                or f"<{data.get('email_id', uuid.uuid4())}@inbound>"
            )
            in_reply_to = data.get("in_reply_to") or hdrs.get("in-reply-to")
            references_raw = str(data.get("references") or hdrs.get("references") or "")
            body_text = str(data.get("text") or "")
            raw_html = str(data.get("html") or "")
            raw_attachments = list(data.get("attachments") or [])
            created_at_str = data.get("created_at")
            source_ts = (
                datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_at_str
                else datetime.now(timezone.utc)
            )

        sender_name, sender_email = parseaddr(from_header)
        sender_email = sender_email.strip().lower()
        _, reply_to_email = parseaddr(reply_to_list[0]) if reply_to_list else ("", "")

        b2b_inquiry = self._b2b_parser.parse(
            sender_email=sender_email,
            subject=subject,
            body_text=body_text,
            reply_to=reply_to_email or None,
        )

        effective_email = (
            b2b_inquiry.buyer_email
            if (b2b_inquiry and b2b_inquiry.buyer_email)
            else sender_email
        )
        effective_name = (
            b2b_inquiry.buyer_name
            if (b2b_inquiry and b2b_inquiry.buyer_name)
            else (sender_name or effective_email)
        )
        country_hint = b2b_inquiry.country if b2b_inquiry else None
        if country_hint and len(country_hint) > 2:
            country_map = {"germany": "DE", "sweden": "SE", "united states": "US", "uk": "GB"}
            country_hint = country_map.get(country_hint.lower(), "US")

        references = [r for r in references_raw.split() if r.strip()]
        resolved_conv_id = await self.resolve_conversation_id(
            session=session,
            tenant_id=tenant_id,
            to_addresses=to_headers,
            in_reply_to=in_reply_to,
            references=references,
        )

        attachments = await self._upload_attachments(tenant_id, raw_attachments)
        sanitized_html = self.sanitize_email_html(raw_html) if raw_html else None

        ext_account = ""
        for addr in to_headers:
            _, parsed_to = parseaddr(addr)
            if parsed_to:
                ext_account = parsed_to.lower()
                break

        inbound_event = NormalizedInboundEvent(
            external_event_id=message_id,
            external_account_id=ext_account,
            external_message_id=message_id,
            external_thread_id=str(resolved_conv_id) if resolved_conv_id else message_id,
            subject=subject,
            sender_identity_type=IdentityType.EMAIL,
            sender_identity_value=effective_email,
            sender_display_name=effective_name,
            country_hint=country_hint,
            direction=MessageDirection.INBOUND,
            sender_type=MessageSenderType.CUSTOMER,
            content_type=MessageContentType.RICH_HTML if sanitized_html else MessageContentType.TEXT,
            body_text=body_text,
            body_sanitized_html=sanitized_html,
            attachments=attachments,
            source_timestamp=source_ts,
            conversation_mode=(
                ConversationMode.B2B_INQUIRY
                if b2b_inquiry
                else ConversationMode.B2C_SUPPORT
            ),
            channel_metadata={
                "rfc5322": {
                    "message_id": message_id,
                    "in_reply_to": in_reply_to,
                    "references": references,
                    "subject": subject,
                },
                "b2b_inquiry": b2b_inquiry.model_dump(mode="json") if b2b_inquiry else None,
            },
            b2b_inquiry_data=b2b_inquiry.model_dump(mode="json") if b2b_inquiry else None,
            resolved_conversation_id=resolved_conv_id,
            override_channel_type=b2b_inquiry.channel_type if b2b_inquiry else None,
        )
        return NormalizedWebhookBatch(inbound_events=[inbound_event])

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        api_key = str(credentials.get("api_key") or os.environ.get("RESEND_API_KEY", ""))
        inbound_domain = os.environ.get("INBOUND_EMAIL_DOMAIN", "inbound.mail.example.com")
        generated_message_id = f"<{uuid.uuid4()}@{inbound_domain}>"
        if "@" in request.external_account_id:
            acct_local, acct_domain = request.external_account_id.split("@", 1)
            default_reply_to = f"{acct_local}+th_{request.conversation_id}@{acct_domain}"
        else:
            default_reply_to = f"support+th_{request.conversation_id}@{inbound_domain}"
        reply_to = request.reply_to_address or default_reply_to

        headers_dict: dict[str, str] = {"Message-ID": generated_message_id}
        if request.in_reply_to:
            headers_dict["In-Reply-To"] = request.in_reply_to
        if request.references:
            headers_dict["References"] = " ".join(request.references)

        payload = {
            "from": request.from_address or f"Support <support@{inbound_domain}>",
            "to": [request.recipient_id],
            "reply_to": reply_to,
            "subject": request.subject or "Re: Your Inquiry",
            "text": request.body_text or "",
            "html": request.body_html,
            "headers": headers_dict,
        }
        async with httpx.AsyncClient(timeout=15.0, transport=self._http_transport) as client:
            resp = await client.post(
                f"{self._resend_base_url}/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )

        if resp.status_code >= 400:
            return OutboundSendResult(
                success=False,
                error_code=resp.status_code,
                error_message=resp.text,
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )
        return OutboundSendResult(
            success=True,
            external_message_id=generated_message_id,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/test_email_and_b2b_parser.py -v`
Expected: PASS (`3 passed`).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/channels/parsers/__init__.py apps/backend/src/erp_core/channels/parsers/b2b_inquiry_email.py apps/backend/src/erp_core/channels/adapters/email.py apps/backend/tests/test_email_and_b2b_parser.py
git commit -m "feat(channels): add Zero-CASA EmailAdapter (RFC 5322/5233, nh3, S3) and Made-in-China/Alibaba parser"
```

---

### Task 2.7: Self-Hosted Web LiveChat Backend & Embeddable JS Widget (`apps/widget`)

**Files:**
- Create: `apps/backend/src/erp_core/channels/adapters/web_livechat.py`
- Create: `apps/backend/src/erp_core/channels/widget_router.py`
- Create: `apps/widget/package.json`
- Create: `apps/widget/tsconfig.json`
- Create: `apps/widget/vite.config.ts`
- Create: `apps/widget/src/api.ts`
- Create: `apps/widget/src/widget-app.ts`
- Create: `apps/widget/src/index.ts`
- Create: `apps/widget/shopify-theme-extension/blocks/livechat-embed.liquid`
- Modify: `apps/backend/src/erp_core/api_main.py`
- Test: `apps/backend/tests/test_web_livechat.py`

**Interfaces:**
- Consumes: `erp_core.shared.db` (`system_session`, `tenant_session`), `erp_core.shared.crypto.EnvelopeCryptoService`, `erp_core.shared.enums` (`ChannelType`, `IdentityType`, `MessageDirection`, `MessageSenderType`, `MessageContentType`, `DeliveryStatus`), `erp_core.identity.service.IdentityResolverService`, `erp_core.channels.consent_service.ConsentAndSuppressionService`.
- Produces: `WebLiveChatAdapter`, `widget_router` (`POST /api/v1/widget/session`, `GET /api/v1/widget/messages`, `POST /api/v1/widget/messages`, `POST /api/v1/widget/identify`, `OPTIONS` CORS preflight enforcing `allowed_origins`), and Shadow DOM Vite bundle `apps/widget/dist/livechat-widget.iife.js` (`< 35KB` gzipped) + Shopify App Embed `livechat-embed.liquid`.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_web_livechat.py`:

```python
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.widget_router import widget_router
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import IdentityType


@pytest.mark.asyncio
async def test_web_livechat_cors_session_prechat_optin_and_hmac_identify(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET", "widget-test-jwt-secret-key-2026")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")

    migrator_engine = create_async_engine(os.environ["DATABASE_MIGRATOR_URL"], future=True)
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    crypto = EnvelopeCryptoService()

    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    site_key = f"site_{uuid.uuid4().hex[:10]}"
    identify_secret = "shop_hmac_shared_secret_2026"
    existing_email = "returning.vip@ledbuyer.de"

    app = FastAPI()
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.include_router(widget_router)

    try:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Widget Store')"),
                {"id": tenant_id, "slug": f"wid-{tenant_id.hex[:8]}"},
            )

        async with tenant_session(session_factory, tenant_id) as sess:
            await crypto.provision_tenant_dek(sess, tenant_id)
            await sess.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status, "
                    "rate_limit_profile, config) VALUES ("
                    ":id, :tid, 'web_livechat', :skey, 'LED Store Chat', 'active', "
                    "'{\"mps\": 100}'::jsonb, CAST(:cfg AS jsonb))"
                ),
                {
                    "id": account_id,
                    "tid": tenant_id,
                    "skey": site_key,
                    "cfg": json.dumps(
                        {
                            "allowed_origins": ["https://ledstore.example.com"],
                            "business_name": "Quanzhou LED Co.",
                        }
                    ),
                },
            )
            await sess.execute(
                text(
                    "INSERT INTO channel_routing_directory "
                    "(channel_type, external_account_id, tenant_id, channel_account_id) "
                    "VALUES ('web_livechat', :skey, :tid, :aid)"
                ),
                {"skey": site_key, "tid": tenant_id, "aid": account_id},
            )
            ct, nonce, tag, kv = await crypto.encrypt_secret(
                sess, tenant_id, account_id, "widget_hmac_secret", identify_secret
            )
            await sess.execute(
                text(
                    "INSERT INTO channel_credentials "
                    "(id, tenant_id, channel_account_id, credential_type, "
                    "ciphertext, nonce, auth_tag, key_version) VALUES ("
                    ":id, :tid, :aid, 'widget_hmac_secret', :ct, :nonce, :tag, :kv)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tenant_id,
                    "aid": account_id,
                    "ct": ct,
                    "nonce": nonce,
                    "tag": tag,
                    "kv": kv,
                },
            )
            # Pre-create identified customer for HMAC merge assertion
            from erp_core.identity.service import IdentityResolverService

            vip_customer_id, _ = await IdentityResolverService().resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.EMAIL,
                existing_email,
                "VIP Buyer",
                country_hint="DE",
            )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Untrusted Origin blocked with 403
            bad_origin = await client.post(
                "/api/v1/widget/session",
                json={"site_key": site_key},
                headers={"Origin": "https://evil-phishing.example"},
            )
            assert bad_origin.status_code == 403

            # 2. Trusted Origin succeeds and issues anonymous JWT + records Pre-Chat opt-in
            sess_resp = await client.post(
                "/api/v1/widget/session",
                json={
                    "site_key": site_key,
                    "page_url": "https://ledstore.example.com/products/cob-24v",
                    "sku": "LED-COB-24V",
                    "prechat_opt_in": {
                        "display_name": "Anonymous Shopper",
                        "whatsapp_phone": "+491701112233",
                        "marketing_opt_in": True,
                    },
                },
                headers={"Origin": "https://ledstore.example.com"},
            )
            assert sess_resp.status_code == 200
            assert (
                sess_resp.headers.get("access-control-allow-origin")
                == "https://ledstore.example.com"
            )
            data = sess_resp.json()
            token = data["session_token"]
            visitor_id = data["visitor_id"]
            anon_customer_id = uuid.UUID(data["customer_id"])
            conv_id = uuid.UUID(data["conversation_id"])

            # Verify Pre-Chat opt-in recorded in consent_records
            async with tenant_session(session_factory, tenant_id) as sess:
                consent_status = (
                    await sess.execute(
                        text(
                            "SELECT consent_status FROM consent_records "
                            "WHERE customer_id = :cid ORDER BY recorded_at DESC LIMIT 1"
                        ),
                        {"cid": anon_customer_id},
                    )
                ).scalar_one()
                assert consent_status == "opted_in"

                # Insert an internal_note to verify visitor GET /messages never leaks internal notes
                await sess.execute(
                    text(
                        "INSERT INTO messages "
                        "(id, tenant_id, conversation_id, channel_type, direction, sender_type, "
                        "content_type, body_text, delivery_status, source_timestamp) VALUES ("
                        ":id, :tid, :conv_id, 'web_livechat', 'internal_note', 'human_agent', "
                        "'text', 'SECRET INTERNAL MARGIN NOTE', 'delivered', now())"
                    ),
                    {"id": uuid.uuid4(), "tid": tenant_id, "conv_id": conv_id},
                )

            # 3. Visitor sends message and lists messages (internal_note hidden!)
            msg_resp = await client.post(
                "/api/v1/widget/messages",
                json={"body_text": "Is IP67 strip dimmable?"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://ledstore.example.com",
                },
            )
            assert msg_resp.status_code == 201

            list_resp = await client.get(
                "/api/v1/widget/messages",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://ledstore.example.com",
                },
            )
            assert list_resp.status_code == 200
            bodies = [m["body_text"] for m in list_resp.json()["items"]]
            assert "Is IP67 strip dimmable?" in bodies
            assert "SECRET INTERNAL MARGIN NOTE" not in bodies

            # 4. Invalid HMAC on /identify returns 401
            ts_now = int(time.time())
            bad_ident = await client.post(
                "/api/v1/widget/identify",
                json={
                    "email": existing_email,
                    "timestamp": ts_now,
                    "hmac_signature": "bad_sig",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://ledstore.example.com",
                },
            )
            assert bad_ident.status_code == 401

            # 5. Valid HMAC merges anonymous session into existing VIP customer
            canonical_payload = f"{visitor_id}:{existing_email}:::{ts_now}"
            valid_sig = hmac.new(
                identify_secret.encode("utf-8"),
                canonical_payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            ok_ident = await client.post(
                "/api/v1/widget/identify",
                json={
                    "email": existing_email,
                    "timestamp": ts_now,
                    "hmac_signature": valid_sig,
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://ledstore.example.com",
                },
            )
            assert ok_ident.status_code == 200
            assert uuid.UUID(ok_ident.json()["customer_id"]) == vip_customer_id
            assert ok_ident.json()["merged"] is True
    finally:
        async with migrator_engine.begin() as conn:
            for tbl in (
                "audit_events",
                "identity_merge_events",
                "consent_records",
                "messages",
                "conversations",
                "customer_identities",
                "customers",
                "channel_credentials",
                "channel_routing_directory",
                "channel_accounts",
                "tenant_keys",
            ):
                await conn.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id = :tid"), {"tid": tenant_id}
                )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await redis.aclose()
        await runtime_engine.dispose()
        await migrator_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_web_livechat.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.channels.widget_router'`.

- [ ] **Step 3: Write backend WebLiveChat adapter/router and `apps/widget` Shadow DOM Vite package**

Create `apps/backend/src/erp_core/channels/adapters/web_livechat.py`:

```python
from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, ClassVar

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedInboundEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.shared.enums import (
    ChannelType,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)


class WebLiveChatAdapter(MessagingChannelAdapter):
    """Self-hosted Web LiveChat adapter emitting live SSE replies back to storefront visitors."""

    channel_types: ClassVar[tuple[ChannelType, ...]] = (ChannelType.WEB_LIVECHAT,)

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        return True

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        return [(str(payload.get("event_id") or uuid.uuid4()), payload.get("site_key"))]

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        visitor_id = str(payload["visitor_id"])
        return NormalizedWebhookBatch(
            inbound_events=[
                NormalizedInboundEvent(
                    external_event_id=str(payload.get("event_id") or uuid.uuid4()),
                    external_account_id=str(payload["site_key"]),
                    external_message_id=str(payload.get("message_id") or uuid.uuid4()),
                    external_thread_id=visitor_id,
                    sender_identity_type=IdentityType.WEB_VISITOR_ID,
                    sender_identity_value=visitor_id,
                    sender_display_name=str(payload.get("display_name") or f"Visitor {visitor_id[:6]}"),
                    direction=MessageDirection.INBOUND,
                    sender_type=MessageSenderType.CUSTOMER,
                    content_type=MessageContentType.TEXT,
                    body_text=str(payload.get("body_text") or ""),
                    source_timestamp=datetime.now(timezone.utc),
                )
            ]
        )

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        """Deliver outbound reply directly to visitor widget stream via Redis Pub/Sub."""
        ext_id = f"wc_{request.message_id}"
        await session.execute(
            text(
                "UPDATE messages SET delivery_status = 'delivered', external_message_id = :emid "
                "WHERE tenant_id = :tid AND id = :mid"
            ),
            {"tid": tenant_id, "mid": request.message_id, "emid": ext_id},
        )
        if self._redis is not None:
            await self._redis.publish(
                f"widget:conv:{request.conversation_id}",
                json.dumps(
                    {
                        "id": str(request.message_id),
                        "direction": "outbound",
                        "sender_type": "human_agent",
                        "body_text": request.body_text,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
            )
        return OutboundSendResult(success=True, external_message_id=ext_id)
```

Create `apps/backend/src/erp_core/channels/widget_router.py`:

```python
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.identity.service import IdentityResolverService
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import ChannelType, ConversationMode, ConversationStatus, IdentityType

widget_router = APIRouter(prefix="/api/v1/widget", tags=["widget"])


class PreChatOptInPayload(BaseModel):
    display_name: str | None = None
    email: str | None = None
    whatsapp_phone: str | None = None
    marketing_opt_in: bool = False


class WidgetSessionRequest(BaseModel):
    site_key: str = Field(min_length=3)
    visitor_id: str | None = None
    page_url: str | None = None
    sku: str | None = None
    cart_value_usd: str | None = None
    utm_params: dict[str, str] = Field(default_factory=dict)
    prechat_opt_in: PreChatOptInPayload | None = None


class WidgetMessageItem(BaseModel):
    id: uuid.UUID
    direction: str
    sender_type: str
    body_text: str | None
    created_at: datetime


class WidgetSessionResponse(BaseModel):
    session_token: str
    visitor_id: str
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    business_name: str
    messages: list[WidgetMessageItem]


class WidgetSendMessageRequest(BaseModel):
    body_text: str = Field(min_length=1, max_length=4000)
    page_url: str | None = None
    sku: str | None = None


class WidgetIdentifyRequest(BaseModel):
    email: str | None = None
    phone_e164: str | None = None
    external_customer_id: str | None = None
    display_name: str | None = None
    timestamp: int
    hmac_signature: str


class WidgetTokenClaims(BaseModel):
    visitor_id: str
    tenant_id: uuid.UUID
    channel_account_id: uuid.UUID
    customer_id: uuid.UUID
    conversation_id: uuid.UUID


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


def _get_redis(request: Request) -> aioredis.Redis:
    client = getattr(request.app.state, "redis", None)
    if client is None:
        client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        request.app.state.redis = client
    return client


def _jwt_secret() -> str:
    return os.environ["JWT_SECRET"]


def _jwt_alg() -> str:
    return os.environ.get("JWT_ALGORITHM", "HS256")


def _sign_widget_token(claims: WidgetTokenClaims) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": claims.visitor_id,
        "tenant_id": str(claims.tenant_id),
        "channel_account_id": str(claims.channel_account_id),
        "customer_id": str(claims.customer_id),
        "conversation_id": str(claims.conversation_id),
        "scope": "widget_visitor",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=7)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_jwt_alg())


def _decode_widget_token(authorization: str | None = Header(default=None)) -> WidgetTokenClaims:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing widget session bearer token",
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        decoded = jwt.decode(token, _jwt_secret(), algorithms=[_jwt_alg()])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired widget session token",
        ) from exc
    if decoded.get("scope") != "widget_visitor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token scope mismatch",
        )
    return WidgetTokenClaims(
        visitor_id=str(decoded["sub"]),
        tenant_id=uuid.UUID(decoded["tenant_id"]),
        channel_account_id=uuid.UUID(decoded["channel_account_id"]),
        customer_id=uuid.UUID(decoded["customer_id"]),
        conversation_id=uuid.UUID(decoded["conversation_id"]),
    )


def _enforce_origin(origin: str | None, allowed_origins: list[str]) -> dict[str, str]:
    if not origin:
        return {}
    if "*" in allowed_origins or origin in allowed_origins:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Vary": "Origin",
        }
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Origin {origin} is not in channel allowed_origins",
    )


@widget_router.options("/{path:path}")
async def widget_cors_preflight(request: Request) -> Response:
    origin = request.headers.get("origin")
    headers = (
        {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Vary": "Origin",
        }
        if origin
        else {}
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=headers)


@widget_router.post("/session", response_model=WidgetSessionResponse)
async def create_or_resume_widget_session(
    payload: WidgetSessionRequest,
    request: Request,
    response: Response,
) -> WidgetSessionResponse:
    session_factory = _get_session_factory(request)
    origin = request.headers.get("origin")

    async with system_session(session_factory) as sys_sess:
        dir_row = (
            await sys_sess.execute(
                text(
                    "SELECT tenant_id, channel_account_id "
                    "FROM resolve_tenant_by_channel_external_id('web_livechat', CAST(:skey AS text))"
                ),
                {"skey": payload.site_key},
            )
        ).first()
    if dir_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown site_key")

    tenant_id, channel_account_id = dir_row[0], dir_row[1]
    visitor_id = (payload.visitor_id or str(uuid.uuid4())).strip()
    identity_svc = IdentityResolverService()
    consent_svc = ConsentAndSuppressionService()

    async with tenant_session(session_factory, tenant_id) as session:
        acct = (
            await session.execute(
                text(
                    "SELECT display_name, config FROM channel_accounts "
                    "WHERE tenant_id = :tid AND id = :aid AND status = 'active'"
                ),
                {"tid": tenant_id, "aid": channel_account_id},
            )
        ).mappings().first()
        if acct is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Widget channel inactive"
            )

        cfg = acct["config"] or {}
        cors_headers = _enforce_origin(origin, list(cfg.get("allowed_origins") or []))
        for k, v in cors_headers.items():
            response.headers[k] = v

        business_name = str(cfg.get("business_name") or acct["display_name"])
        prechat = payload.prechat_opt_in
        display_name = (prechat.display_name if prechat and prechat.display_name else f"Visitor {visitor_id[:6]}")

        if prechat and prechat.email:
            customer_id, _ = await identity_svc.resolve_or_create_customer(
                session, tenant_id, IdentityType.EMAIL, prechat.email, display_name
            )
        elif prechat and prechat.whatsapp_phone:
            customer_id, _ = await identity_svc.resolve_or_create_customer(
                session, tenant_id, IdentityType.PHONE_E164, prechat.whatsapp_phone, display_name
            )
        else:
            customer_id, _ = await identity_svc.resolve_or_create_customer(
                session, tenant_id, IdentityType.WEB_VISITOR_ID, visitor_id, display_name
            )

        await session.execute(
            text(
                "INSERT INTO customer_identities "
                "(id, tenant_id, customer_id, identity_type, identity_value, confidence_level) "
                "VALUES (:id, :tid, :cid, 'web_visitor_id', :vid, 'deterministic') "
                "ON CONFLICT (tenant_id, identity_type, identity_value) DO NOTHING"
            ),
            {"id": uuid.uuid4(), "tid": tenant_id, "cid": customer_id, "vid": visitor_id},
        )

        if prechat and prechat.marketing_opt_in:
            client_ip = request.client.host if request.client else None
            if prechat.whatsapp_phone:
                await consent_svc.record_opt_in(
                    session=session,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    channel_type="whatsapp",
                    proof_source="web_livechat_prechat",
                    disclosed_business_name=business_name,
                    proof_ip=client_ip,
                    normalized_address=prechat.whatsapp_phone,
                )
            if prechat.email:
                await consent_svc.record_opt_in(
                    session=session,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    channel_type="email",
                    proof_source="web_livechat_prechat",
                    disclosed_business_name=business_name,
                    proof_ip=client_ip,
                    normalized_address=prechat.email,
                )

        conv_id = (
            await session.execute(
                text(
                    "SELECT id FROM conversations "
                    "WHERE tenant_id = :tid AND customer_id = :cid "
                    "AND channel_account_id = :aid AND status != 'resolved' "
                    "ORDER BY updated_at DESC LIMIT 1"
                ),
                {"tid": tenant_id, "cid": customer_id, "aid": channel_account_id},
            )
        ).scalar_one_or_none()
        if conv_id is None:
            conv_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO conversations ("
                    "id, tenant_id, customer_id, channel_account_id, channel_type, "
                    "external_thread_id, conversation_mode, status, channel_policy_state"
                    ") VALUES ("
                    ":id, :tid, :cid, :aid, :ctype, :thread, :mode, :status, CAST(:policy AS jsonb)"
                    ")"
                ),
                {
                    "id": conv_id,
                    "tid": tenant_id,
                    "cid": customer_id,
                    "aid": channel_account_id,
                    "ctype": ChannelType.WEB_LIVECHAT.value,
                    "thread": visitor_id,
                    "mode": ConversationMode.B2C_SUPPORT.value,
                    "status": ConversationStatus.OPEN.value,
                    "policy": json.dumps(
                        {
                            "page_url": payload.page_url,
                            "sku": payload.sku,
                            "cart_value_usd": payload.cart_value_usd,
                            "utm_params": payload.utm_params,
                        }
                    ),
                },
            )

        msg_rows = (
            await session.execute(
                text(
                    "SELECT id, direction, sender_type, body_text, created_at "
                    "FROM messages WHERE tenant_id = :tid AND conversation_id = :cid "
                    "AND direction != 'internal_note' ORDER BY source_timestamp ASC LIMIT 100"
                ),
                {"tid": tenant_id, "cid": conv_id},
            )
        ).mappings().all()

    token = _sign_widget_token(
        WidgetTokenClaims(
            visitor_id=visitor_id,
            tenant_id=tenant_id,
            channel_account_id=channel_account_id,
            customer_id=customer_id,
            conversation_id=conv_id,
        )
    )
    return WidgetSessionResponse(
        session_token=token,
        visitor_id=visitor_id,
        customer_id=customer_id,
        conversation_id=conv_id,
        business_name=business_name,
        messages=[WidgetMessageItem(**dict(r)) for r in msg_rows],
    )


async def _validate_account_cors(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID, origin: str | None, response: Response
) -> None:
    cfg = (
        await session.execute(
            text("SELECT config FROM channel_accounts WHERE tenant_id = :tid AND id = :aid"),
            {"tid": tenant_id, "aid": account_id},
        )
    ).scalar_one_or_none()
    allowed = list((cfg or {}).get("allowed_origins") or [])
    for k, v in _enforce_origin(origin, allowed).items():
        response.headers[k] = v


@widget_router.get("/messages")
async def list_widget_messages(
    request: Request,
    response: Response,
    claims: WidgetTokenClaims = Depends(_decode_widget_token),
) -> dict[str, list[dict[str, Any]]]:
    session_factory = _get_session_factory(request)
    async with tenant_session(session_factory, claims.tenant_id) as session:
        await _validate_account_cors(
            session, claims.tenant_id, claims.channel_account_id, request.headers.get("origin"), response
        )
        rows = (
            await session.execute(
                text(
                    "SELECT id, direction, sender_type, body_text, created_at "
                    "FROM messages WHERE tenant_id = :tid AND conversation_id = :cid "
                    "AND direction != 'internal_note' ORDER BY source_timestamp ASC LIMIT 100"
                ),
                {"tid": claims.tenant_id, "cid": claims.conversation_id},
            )
        ).mappings().all()
    return {
        "items": [
            {
                "id": str(r["id"]),
                "direction": r["direction"],
                "sender_type": r["sender_type"],
                "body_text": r["body_text"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@widget_router.post("/messages", status_code=status.HTTP_201_CREATED)
async def send_widget_message(
    payload: WidgetSendMessageRequest,
    request: Request,
    response: Response,
    claims: WidgetTokenClaims = Depends(_decode_widget_token),
) -> dict[str, Any]:
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    msg_id = uuid.uuid4()
    now_ts = datetime.now(timezone.utc)

    async with tenant_session(session_factory, claims.tenant_id) as session:
        await _validate_account_cors(
            session, claims.tenant_id, claims.channel_account_id, request.headers.get("origin"), response
        )
        await session.execute(
            text(
                "INSERT INTO messages ("
                "id, tenant_id, conversation_id, customer_id, channel_type, "
                "external_message_id, external_sender_id, direction, sender_type, "
                "sender_id, content_type, body_text, channel_metadata, delivery_status, source_timestamp"
                ") VALUES ("
                ":id, :tid, :conv_id, :cid, 'web_livechat', :emid, :vid, 'inbound', 'customer', "
                ":sid, 'text', :btext, CAST(:meta AS jsonb), 'delivered', :ts"
                ")"
            ),
            {
                "id": msg_id,
                "tid": claims.tenant_id,
                "conv_id": claims.conversation_id,
                "cid": claims.customer_id,
                "emid": f"wcin_{msg_id}",
                "vid": claims.visitor_id,
                "sid": str(claims.customer_id),
                "btext": payload.body_text,
                "meta": json.dumps({"page_url": payload.page_url, "sku": payload.sku}),
                "ts": now_ts,
            },
        )
        await session.execute(
            text(
                "UPDATE conversations SET last_inbound_at = :ts, last_message_preview = :preview, "
                "unread_count = unread_count + 1, updated_at = now() "
                "WHERE tenant_id = :tid AND id = :conv_id"
            ),
            {
                "tid": claims.tenant_id,
                "conv_id": claims.conversation_id,
                "ts": now_ts,
                "preview": payload.body_text[:180],
            },
        )

    await redis.publish(
        f"inbox:events:{claims.tenant_id}",
        json.dumps(
            {
                "event": "message.created",
                "conversation_id": str(claims.conversation_id),
                "message_id": str(msg_id),
                "channel_type": ChannelType.WEB_LIVECHAT.value,
            }
        ),
    )
    return {
        "id": str(msg_id),
        "direction": "inbound",
        "sender_type": "customer",
        "body_text": payload.body_text,
        "created_at": now_ts.isoformat(),
    }


@widget_router.post("/identify")
async def identify_widget_visitor(
    payload: WidgetIdentifyRequest,
    request: Request,
    response: Response,
    claims: WidgetTokenClaims = Depends(_decode_widget_token),
) -> dict[str, Any]:
    if abs(int(time.time()) - payload.timestamp) > 300:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Stale widget identify timestamp",
        )

    session_factory = _get_session_factory(request)
    crypto = EnvelopeCryptoService()
    identity_svc = IdentityResolverService()

    async with tenant_session(session_factory, claims.tenant_id) as session:
        await _validate_account_cors(
            session, claims.tenant_id, claims.channel_account_id, request.headers.get("origin"), response
        )
        cred = (
            await session.execute(
                text(
                    "SELECT ciphertext, nonce, auth_tag, key_version FROM channel_credentials "
                    "WHERE tenant_id = :tid AND channel_account_id = :aid "
                    "AND credential_type = 'widget_hmac_secret' LIMIT 1"
                ),
                {"tid": claims.tenant_id, "aid": claims.channel_account_id},
            )
        ).mappings().first()
        if cred is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Widget identify secret not configured",
            )

        hmac_secret = await crypto.decrypt_secret(
            session=session,
            tenant_id=claims.tenant_id,
            account_id=claims.channel_account_id,
            cred_type="widget_hmac_secret",
            ciphertext=bytes(cred["ciphertext"]),
            nonce=bytes(cred["nonce"]),
            auth_tag=bytes(cred["auth_tag"]),
            key_version=int(cred["key_version"]),
        )
        canonical = (
            f"{claims.visitor_id}:{payload.email or ''}:{payload.phone_e164 or ''}:"
            f"{payload.external_customer_id or ''}:{payload.timestamp}"
        )
        expected_sig = hmac.new(
            hmac_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(payload.hmac_signature, expected_sig):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid HMAC signature",
            )

        display_name = payload.display_name or payload.email or payload.phone_e164 or claims.visitor_id
        if payload.email:
            target_cid, _ = await identity_svc.resolve_or_create_customer(
                session, claims.tenant_id, IdentityType.EMAIL, payload.email, display_name
            )
        elif payload.phone_e164:
            target_cid, _ = await identity_svc.resolve_or_create_customer(
                session, claims.tenant_id, IdentityType.PHONE_E164, payload.phone_e164, display_name
            )
        elif payload.external_customer_id:
            target_cid, _ = await identity_svc.resolve_or_create_customer(
                session,
                claims.tenant_id,
                IdentityType.SHOPIFY_CUSTOMER_ID,
                payload.external_customer_id,
                display_name,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one identifier is required",
            )

        merged = False
        if target_cid != claims.customer_id:
            await identity_svc.merge_customers(
                session=session,
                tenant_id=claims.tenant_id,
                source_customer_id=claims.customer_id,
                target_customer_id=target_cid,
                performed_by="widget_hmac_identify",
                reason="authenticated_storefront_identify",
            )
            merged = True

    upgraded_token = _sign_widget_token(
        WidgetTokenClaims(
            visitor_id=claims.visitor_id,
            tenant_id=claims.tenant_id,
            channel_account_id=claims.channel_account_id,
            customer_id=target_cid,
            conversation_id=claims.conversation_id,
        )
    )
    return {
        "session_token": upgraded_token,
        "customer_id": str(target_cid),
        "merged": merged,
    }
```

Modify `apps/backend/src/erp_core/api_main.py` to mount `widget_router`:

```python
from erp_core.channels.widget_router import widget_router

app.include_router(widget_router)
```

Create `apps/widget/package.json`:

```json
{
  "name": "@erp/widget",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite --port 3001",
    "typecheck": "tsc --noEmit",
    "build": "tsc --noEmit && vite build"
  },
  "devDependencies": {
    "typescript": "^5.6.3",
    "vite": "^5.4.11"
  }
}
```

Create `apps/widget/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "lib": ["ES2020", "DOM"]
  },
  "include": ["src"]
}
```

Create `apps/widget/vite.config.ts`:

```ts
import { resolve } from "node:path";
import { defineConfig } from "vite";

export default defineConfig({
  build: {
    lib: {
      entry: resolve(__dirname, "src/index.ts"),
      name: "ErpLiveChat",
      formats: ["iife"],
      fileName: () => "livechat-widget.iife.js",
    },
    minify: "esbuild",
    sourcemap: false,
    outDir: "dist",
    emptyOutDir: true,
  },
});
```

Create `apps/widget/src/api.ts`:

```ts
export interface WidgetMessage {
  id: string;
  direction: string;
  sender_type: string;
  body_text: string | null;
  created_at: string;
}

export interface PreChatOptIn {
  display_name?: string;
  email?: string;
  whatsapp_phone?: string;
  marketing_opt_in: boolean;
}

export interface WidgetSessionResponse {
  session_token: string;
  visitor_id: string;
  customer_id: string;
  conversation_id: string;
  business_name: string;
  messages: WidgetMessage[];
}

export interface IdentifyPayload {
  email?: string;
  phone_e164?: string;
  external_customer_id?: string;
  display_name?: string;
  timestamp: number;
  hmac_signature: string;
}

export class WidgetApiClient {
  constructor(private readonly apiBase: string) {}

  async createSession(body: {
    site_key: string;
    visitor_id?: string | null;
    page_url: string;
    sku?: string;
    prechat_opt_in?: PreChatOptIn;
  }): Promise<WidgetSessionResponse> {
    const resp = await fetch(`${this.apiBase}/api/v1/widget/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      throw new Error(`Widget session error: ${resp.status}`);
    }
    return (await resp.json()) as WidgetSessionResponse;
  }

  async listMessages(token: string): Promise<WidgetMessage[]> {
    const resp = await fetch(`${this.apiBase}/api/v1/widget/messages`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) {
      throw new Error(`Widget list error: ${resp.status}`);
    }
    const data = (await resp.json()) as { items: WidgetMessage[] };
    return data.items;
  }

  async sendMessage(
    token: string,
    bodyText: string,
    pageUrl: string,
    sku?: string
  ): Promise<WidgetMessage> {
    const resp = await fetch(`${this.apiBase}/api/v1/widget/messages`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        body_text: bodyText,
        page_url: pageUrl,
        sku,
      }),
    });
    if (!resp.ok) {
      throw new Error(`Widget send error: ${resp.status}`);
    }
    return (await resp.json()) as WidgetMessage;
  }

  async identify(
    token: string,
    payload: IdentifyPayload
  ): Promise<{ session_token: string; customer_id: string; merged: boolean }> {
    const resp = await fetch(`${this.apiBase}/api/v1/widget/identify`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      throw new Error(`Widget identify error: ${resp.status}`);
    }
    return (await resp.json()) as {
      session_token: string;
      customer_id: string;
      merged: boolean;
    };
  }
}
```

Create `apps/widget/src/widget-app.ts`:

```ts
import {
  IdentifyPayload,
  PreChatOptIn,
  WidgetApiClient,
  WidgetMessage,
} from "./api";

export interface WidgetConfig {
  siteKey: string;
  apiBase: string;
  businessName?: string;
  sku?: string;
}

const WIDGET_STYLES = `
  :host { all: initial; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  .launcher { position: fixed; right: 20px; bottom: 20px; z-index: 2147483000; width: 56px; height: 56px;
    border-radius: 9999px; border: none; background: #0f172a; color: #fff; cursor: pointer;
    box-shadow: 0 10px 25px rgba(15, 23, 42, 0.25); font-size: 20px; }
  .panel { position: fixed; right: 20px; bottom: 88px; z-index: 2147483000; width: 340px; max-height: 480px;
    display: none; flex-direction: column; background: #ffffff; border-radius: 14px;
    border: 1px solid #e2e8f0; box-shadow: 0 18px 40px rgba(15, 23, 42, 0.22); overflow: hidden; }
  .panel.open { display: flex; }
  .header { background: #0f172a; color: #ffffff; padding: 14px 16px; display: flex;
    justify-content: space-between; align-items: center; font-size: 14px; font-weight: 600; }
  .close-btn { background: transparent; border: none; color: #cbd5e1; cursor: pointer; font-size: 18px; }
  .body { padding: 12px; overflow-y: auto; flex: 1; background: #f8fafc; display: flex; flex-direction: column; gap: 8px; min-height: 220px; }
  .msg { max-width: 82%; padding: 8px 11px; border-radius: 10px; font-size: 13px; line-height: 1.4; }
  .msg.inbound { align-self: flex-end; background: #2563eb; color: #ffffff; }
  .msg.outbound { align-self: flex-start; background: #ffffff; color: #0f172a; border: 1px solid #e2e8f0; }
  .prechat { display: flex; flex-direction: column; gap: 8px; padding: 12px; background: #ffffff; border-bottom: 1px solid #e2e8f0; }
  .input { width: 100%; box-sizing: border-box; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 13px; }
  .optin { display: flex; align-items: flex-start; gap: 6px; font-size: 11px; color: #475569; }
  .footer { display: flex; gap: 6px; padding: 10px; border-top: 1px solid #e2e8f0; background: #ffffff; }
  .send-btn { background: #2563eb; color: #ffffff; border: none; border-radius: 8px; padding: 8px 12px; font-size: 13px; cursor: pointer; }
`;

export class LiveChatWidgetApp {
  private readonly api: WidgetApiClient;
  private readonly shadow: ShadowRoot;
  private readonly storageKey: string;
  private sessionToken: string | null = null;
  private visitorId: string | null = null;
  private businessName: string;
  private messages: WidgetMessage[] = [];
  private isOpen = false;
  private pollTimer: number | null = null;

  private panelEl!: HTMLDivElement;
  private bodyEl!: HTMLDivElement;
  private prechatEl!: HTMLDivElement;
  private inputEl!: HTMLInputElement;

  constructor(private readonly config: WidgetConfig) {
    this.api = new WidgetApiClient(config.apiBase.replace(/\/$/, ""));
    this.storageKey = `erp_livechat_${config.siteKey}`;
    this.businessName = config.businessName ?? "Support";
    this.visitorId = localStorage.getItem(`${this.storageKey}_vid`);

    const host = document.createElement("div");
    host.id = "erp-livechat-host";
    document.body.appendChild(host);
    this.shadow = host.attachShadow({ mode: "open" });
    this.renderShell();
  }

  private renderShell(): void {
    const style = document.createElement("style");
    style.textContent = WIDGET_STYLES;

    const launcher = document.createElement("button");
    launcher.className = "launcher";
    launcher.type = "button";
    launcher.setAttribute("aria-label", "Open LiveChat");
    launcher.textContent = "Chat";
    launcher.addEventListener("click", () => void this.toggle());

    this.panelEl = document.createElement("div");
    this.panelEl.className = "panel";

    const header = document.createElement("div");
    header.className = "header";
    const titleSpan = document.createElement("span");
    titleSpan.textContent = this.businessName;
    const closeBtn = document.createElement("button");
    closeBtn.className = "close-btn";
    closeBtn.type = "button";
    closeBtn.textContent = "x";
    closeBtn.addEventListener("click", () => this.close());
    header.append(titleSpan, closeBtn);

    this.prechatEl = document.createElement("div");
    this.prechatEl.className = "prechat";
    const emailInput = document.createElement("input");
    emailInput.className = "input";
    emailInput.placeholder = "Work Email (for quote follow-up)";
    const waInput = document.createElement("input");
    waInput.className = "input";
    waInput.placeholder = "WhatsApp Number (+1...)";
    const optinLabel = document.createElement("label");
    optinLabel.className = "optin";
    const optinCheckbox = document.createElement("input");
    optinCheckbox.type = "checkbox";
    optinCheckbox.checked = false; // Explicitly NEVER pre-checked per GDPR/WhatsApp rules
    const optinText = document.createElement("span");
    optinText.textContent = `I agree to receive order & quote updates from ${this.businessName}.`;
    optinLabel.append(optinCheckbox, optinText);

    const startBtn = document.createElement("button");
    startBtn.className = "send-btn";
    startBtn.type = "button";
    startBtn.textContent = "Start Chat";
    startBtn.addEventListener("click", () => {
      const prechat: PreChatOptIn = {
        email: emailInput.value.trim() || undefined,
        whatsapp_phone: waInput.value.trim() || undefined,
        marketing_opt_in: optinCheckbox.checked,
      };
      void this.ensureSession(prechat);
    });
    this.prechatEl.append(emailInput, waInput, optinLabel, startBtn);

    this.bodyEl = document.createElement("div");
    this.bodyEl.className = "body";

    const footer = document.createElement("form");
    footer.className = "footer";
    this.inputEl = document.createElement("input");
    this.inputEl.className = "input";
    this.inputEl.placeholder = "Type a message...";
    const sendBtn = document.createElement("button");
    sendBtn.className = "send-btn";
    sendBtn.type = "submit";
    sendBtn.textContent = "Send";
    footer.append(this.inputEl, sendBtn);
    footer.addEventListener("submit", (ev) => {
      ev.preventDefault();
      void this.handleSend();
    });

    this.panelEl.append(header, this.prechatEl, this.bodyEl, footer);
    this.shadow.append(style, launcher, this.panelEl);
  }

  async open(): Promise<void> {
    this.isOpen = true;
    this.panelEl.classList.add("open");
    if (!this.sessionToken) {
      await this.ensureSession();
    }
    this.startPolling();
  }

  close(): void {
    this.isOpen = false;
    this.panelEl.classList.remove("open");
    if (this.pollTimer !== null) {
      window.clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  async toggle(): Promise<void> {
    if (this.isOpen) {
      this.close();
    } else {
      await this.open();
    }
  }

  private async ensureSession(prechat?: PreChatOptIn): Promise<void> {
    const resp = await this.api.createSession({
      site_key: this.config.siteKey,
      visitor_id: this.visitorId,
      page_url: window.location.href,
      sku: this.config.sku,
      prechat_opt_in: prechat,
    });
    this.sessionToken = resp.session_token;
    this.visitorId = resp.visitor_id;
    this.businessName = resp.business_name;
    localStorage.setItem(`${this.storageKey}_vid`, resp.visitor_id);
    if (prechat && (prechat.email || prechat.whatsapp_phone)) {
      this.prechatEl.style.display = "none";
    }
    this.messages = resp.messages;
    this.renderMessages();
  }

  private async handleSend(): Promise<void> {
    const text = this.inputEl.value.trim();
    if (!text) return;
    if (!this.sessionToken) {
      await this.ensureSession();
    }
    if (!this.sessionToken) return;
    this.inputEl.value = "";
    const msg = await this.api.sendMessage(
      this.sessionToken,
      text,
      window.location.href,
      this.config.sku
    );
    this.messages.push(msg);
    this.renderMessages();
  }

  async identify(payload: IdentifyPayload): Promise<void> {
    if (!this.sessionToken) {
      await this.ensureSession();
    }
    if (!this.sessionToken) return;
    const upgraded = await this.api.identify(this.sessionToken, payload);
    this.sessionToken = upgraded.session_token;
  }

  private startPolling(): void {
    if (this.pollTimer !== null) return;
    this.pollTimer = window.setInterval(async () => {
      if (!this.sessionToken || !this.isOpen) return;
      this.messages = await this.api.listMessages(this.sessionToken);
      this.renderMessages();
    }, 4000);
  }

  private renderMessages(): void {
    this.bodyEl.textContent = "";
    for (const msg of this.messages) {
      const bubble = document.createElement("div");
      bubble.className = `msg ${msg.direction === "inbound" ? "inbound" : "outbound"}`;
      bubble.textContent = msg.body_text ?? "";
      this.bodyEl.appendChild(bubble);
    }
    this.bodyEl.scrollTop = this.bodyEl.scrollHeight;
  }
}
```

Create `apps/widget/src/index.ts`:

```ts
import { IdentifyPayload } from "./api";
import { LiveChatWidgetApp, WidgetConfig } from "./widget-app";

let activeInstance: LiveChatWidgetApp | null = null;

export const ErpLiveChat = {
  init(config: WidgetConfig): LiveChatWidgetApp {
    if (!activeInstance) {
      activeInstance = new LiveChatWidgetApp(config);
    }
    return activeInstance;
  },
  open(): void {
    void activeInstance?.open();
  },
  close(): void {
    activeInstance?.close();
  },
  identify(payload: IdentifyPayload): Promise<void> {
    if (!activeInstance) {
      return Promise.resolve();
    }
    return activeInstance.identify(payload);
  },
};

declare global {
  interface Window {
    ErpLiveChat?: typeof ErpLiveChat;
  }
}

window.ErpLiveChat = ErpLiveChat;

const currentScript = document.currentScript as HTMLScriptElement | null;
if (currentScript?.dataset.siteKey && currentScript?.dataset.apiBase) {
  ErpLiveChat.init({
    siteKey: currentScript.dataset.siteKey,
    apiBase: currentScript.dataset.apiBase,
    businessName: currentScript.dataset.businessName,
    sku: currentScript.dataset.sku,
  });
}
```

Create `apps/widget/shopify-theme-extension/blocks/livechat-embed.liquid`:

```liquid
{% comment %}
  Shopify Online Store 2.0 App Embed Block (target: body)
  Replaces closed Shopify Inbox with self-hosted Shadow DOM LiveChat Widget.
{% endcomment %}
<script
  src="{{ block.settings.widget_cdn_url | escape }}"
  data-site-key="{{ block.settings.site_key | escape }}"
  data-api-base="{{ block.settings.api_base | escape }}"
  data-business-name="{{ shop.name | escape }}"
  {% if product %}
    data-sku="{{ product.selected_or_first_available_variant.sku | escape }}"
  {% endif %}
  defer
></script>

{% if customer and block.settings.customer_identify_hmac != blank %}
<script>
  window.addEventListener("load", function () {
    if (window.ErpLiveChat) {
      window.ErpLiveChat.identify({
        email: {{ customer.email | json }},
        external_customer_id: String({{ customer.id | json }}),
        display_name: {{ customer.name | json }},
        timestamp: {{ 'now' | date: '%s' | plus: 0 }},
        hmac_signature: {{ block.settings.customer_identify_hmac | json }}
      });
    }
  });
</script>
{% endif %}

{% schema %}
{
  "name": "Omnichannel LiveChat",
  "target": "body",
  "settings": [
    {
      "type": "text",
      "id": "site_key",
      "label": "Workspace Site Key"
    },
    {
      "type": "url",
      "id": "api_base",
      "label": "Backend API Base URL"
    },
    {
      "type": "url",
      "id": "widget_cdn_url",
      "label": "Widget Bundle URL"
    },
    {
      "type": "text",
      "id": "customer_identify_hmac",
      "label": "Optional Customer Identify HMAC"
    }
  ]
}
{% endschema %}
```

- [ ] **Step 4: Run backend test and widget build + `< 35KB` gzipped size assertion**

Run:
```bash
cd apps/backend && uv run pytest tests/test_web_livechat.py -v
cd ../widget && pnpm install && pnpm build
node -e 'const fs=require("fs"),zlib=require("zlib"); const gz=zlib.gzipSync(fs.readFileSync("dist/livechat-widget.iife.js")); console.log("gzipped_bytes:", gz.length); if(gz.length >= 35840) process.exit(1);'
```
Expected: `pytest` passes (`1 passed`), `pnpm build` succeeds with zero TypeScript errors, and `gzipped_bytes` is `< 35840` (around 3,000–6,000 bytes).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/channels/adapters/web_livechat.py apps/backend/src/erp_core/channels/widget_router.py apps/backend/src/erp_core/api_main.py apps/backend/tests/test_web_livechat.py apps/widget/
git commit -m "feat(channels,widget): add Web LiveChat backend, CORS/HMAC identify, and <35KB Shadow DOM widget"
```

---

### Task 2.8: Unified Inbox REST & SSE API (`apps/backend/src/erp_core/channels/inbox_router.py`)

**Files:**
- Create: `apps/backend/src/erp_core/channels/inbox_schemas.py`
- Create: `apps/backend/src/erp_core/channels/inbox_service.py`
- Create: `apps/backend/src/erp_core/channels/inbox_router.py`
- Modify: `apps/backend/src/erp_core/api_main.py`
- Test: `apps/backend/tests/test_inbox_api.py`

**Interfaces:**
- Consumes: `erp_core.shared.db.tenant_session`, `erp_core.shared.enums` (`ChannelType`, `ConversationMode`, `ConversationStatus`, `MessageDirection`, `MessageSenderType`, `MessageContentType`, `DeliveryStatus`, `AutonomyLevel`, `UserRole`), `erp_core.shared.pagination.PaginatedResponse`, `erp_core.tenancy.dependencies` (`CurrentPrincipal`, `require_roles`), `erp_core.tenancy.audit.record_audit_event`, `erp_core.identity.service.IdentityResolverService`, `erp_core.channels.adapters.whatsapp.WhatsAppAdapter`, `erp_core.channels.models` (`Conversation`, `Message`, `ChannelAccount`).
- Produces: `inbox_router` (`GET /api/v1/inbox/conversations`, `GET /api/v1/inbox/conversations/{id}/messages`, `POST /api/v1/inbox/conversations/{id}/messages`, `PATCH /api/v1/inbox/conversations/{id}`, `GET /api/v1/inbox/stream`), `InboxService`, `TranslationHook`.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/test_inbox_api.py`:

```python
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.inbox_router import _require_inbox_access, inbox_router
from erp_core.shared.enums import ChannelType, ConversationStatus, UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal


@pytest.mark.asyncio
async def test_inbox_filters_sorting_notes_outbound_sse_and_tenant_404() -> None:
    migrator_engine = create_async_engine(os.environ["DATABASE_MIGRATOR_URL"], future=True)
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    acct_wa = uuid.uuid4()
    acct_email = uuid.uuid4()
    cust_a = uuid.uuid4()
    conv_wa = uuid.uuid4()
    conv_email = uuid.uuid4()

    active_principal = CurrentPrincipal(
        user_id=user_a,
        tenant_id=tenant_a,
        role=UserRole.AGENT,
        email="agent_a@factory.test",
    )

    enqueued_tasks: list[dict] = []

    async def _fake_enqueue(req_dict: dict) -> None:
        enqueued_tasks.append(req_dict)

    app = FastAPI()
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.outbound_enqueue_fn = _fake_enqueue
    app.include_router(inbox_router)
    app.dependency_overrides[_require_inbox_access] = lambda: active_principal

    try:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) VALUES "
                    "(:ta, :sa, 'Inbox A'), (:tb, :sb, 'Inbox B')"
                ),
                {
                    "ta": tenant_a,
                    "sa": f"inb-a-{tenant_a.hex[:8]}",
                    "tb": tenant_b,
                    "sb": f"inb-b-{tenant_b.hex[:8]}",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status, rate_limit_profile) VALUES "
                    "(:awa, :ta, 'whatsapp_cloud', :ext_wa, 'WA', 'active', '{\"mps\": 80}'::jsonb), "
                    "(:aem, :ta, 'email', :ext_em, 'Email', 'active', '{\"mps\": 50}'::jsonb)"
                ),
                {
                    "awa": acct_wa,
                    "aem": acct_email,
                    "ta": tenant_a,
                    "ext_wa": f"pn_{acct_wa.hex[:8]}",
                    "ext_em": f"sales-{tenant_a.hex[:6]}@inbound.example.com",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO customers "
                    "(id, tenant_id, customer_type, display_name, primary_phone_e164, iana_timezone) VALUES "
                    "(:cid, :ta, 'b2b_contact', 'Hans Mueller', '+491701234567', 'Europe/Berlin')"
                ),
                {"cid": cust_a, "ta": tenant_a},
            )
            now = datetime.now(timezone.utc)
            await conn.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, tenant_id, customer_id, channel_account_id, channel_type, external_thread_id, "
                    "conversation_mode, status, unread_count, csw_expires_at, sla_due_at, updated_at) VALUES "
                    "(:cwa, :ta, :cid, :awa, 'whatsapp_cloud', '+491701234567', 'b2c_support', 'open', 2, :csw, :sla1, :u1), "
                    "(:cem, :ta, :cid, :aem, 'email', 'hans@example.de', 'b2b_inquiry', 'pending_human', 0, NULL, :sla2, :u2)"
                ),
                {
                    "cwa": conv_wa,
                    "cem": conv_email,
                    "ta": tenant_a,
                    "cid": cust_a,
                    "awa": acct_wa,
                    "aem": acct_email,
                    "csw": now + timedelta(hours=12),
                    "sla1": now + timedelta(hours=6),
                    "sla2": now + timedelta(hours=1),
                    "u1": now,
                    "u2": now - timedelta(minutes=10),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO messages "
                    "(id, tenant_id, conversation_id, customer_id, channel_type, external_message_id, "
                    "direction, sender_type, content_type, body_text, delivery_status, source_timestamp) VALUES "
                    "(:m1, :ta, :cwa, :cid, 'whatsapp_cloud', 'wamid.in.1', 'inbound', 'customer', 'text', 'Need LED price', 'delivered', :ts)"
                ),
                {"m1": uuid.uuid4(), "ta": tenant_a, "cwa": conv_wa, "cid": cust_a, "ts": now},
            )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Default sorting (updated_at DESC) + customer local timezone enrichment
            all_resp = await client.get("/api/v1/inbox/conversations?tab=all")
            assert all_resp.status_code == 200
            all_items = all_resp.json()["items"]
            assert [i["id"] for i in all_items] == [str(conv_wa), str(conv_email)]
            assert all_items[0]["customer"]["buyer_local_time"]["iana_timezone"] == "Europe/Berlin"

            # 2. Tab filters: unread, pending_human, sla_urgent, channel_type
            unread_resp = await client.get("/api/v1/inbox/conversations?tab=unread")
            assert [i["id"] for i in unread_resp.json()["items"]] == [str(conv_wa)]

            pending_resp = await client.get("/api/v1/inbox/conversations?tab=pending_human")
            assert [i["id"] for i in pending_resp.json()["items"]] == [str(conv_email)]

            sla_resp = await client.get("/api/v1/inbox/conversations?tab=sla_urgent")
            assert [i["id"] for i in sla_resp.json()["items"]] == [str(conv_email), str(conv_wa)]

            ch_resp = await client.get(
                f"/api/v1/inbox/conversations?channel_type={ChannelType.EMAIL.value}"
            )
            assert [i["id"] for i in ch_resp.json()["items"]] == [str(conv_email)]

            # 3. Reading messages resets unread_count to 0
            msgs_resp = await client.get(f"/api/v1/inbox/conversations/{conv_wa}/messages")
            assert msgs_resp.status_code == 200
            assert len(msgs_resp.json()["items"]) == 1
            unread_after = await client.get("/api/v1/inbox/conversations?tab=unread")
            assert unread_after.json()["total"] == 0

            # 4. Internal note persists WITHOUT enqueueing outbound delivery
            note_resp = await client.post(
                f"/api/v1/inbox/conversations/{conv_wa}/messages",
                json={
                    "direction": "internal_note",
                    "body_text": "Verify warehouse stock before quoting",
                },
            )
            assert note_resp.status_code == 201
            assert note_resp.json()["queued_for_delivery"] is False
            assert len(enqueued_tasks) == 0

            # 5. Outbound message with translate_to hook enqueues delivery
            out_resp = await client.post(
                f"/api/v1/inbox/conversations/{conv_wa}/messages",
                json={
                    "direction": "outbound",
                    "body_text": "We can ship 5000m within 7 days.",
                    "translate_to": "de",
                },
            )
            assert out_resp.status_code == 201
            assert out_resp.json()["queued_for_delivery"] is True
            assert out_resp.json()["translated_preview"] is not None
            assert len(enqueued_tasks) == 1
            assert enqueued_tasks[0]["recipient_id"] == "+491701234567"

            # 6. PATCH conversation assigns user & resolves status
            patch_resp = await client.patch(
                f"/api/v1/inbox/conversations/{conv_email}",
                json={
                    "status": ConversationStatus.OPEN.value,
                    "assigned_user_id": str(user_a),
                },
            )
            assert patch_resp.status_code == 200
            assert patch_resp.json()["status"] == "open"
            assert patch_resp.json()["assigned_user_id"] == str(user_a)

            # 7. SSE stream receives tenant-scoped Redis Pub/Sub event after handshake
            received_chunks: list[str] = []
            published = False
            async with client.stream(
                "GET", "/api/v1/inbox/stream?max_events=1"
            ) as sse_resp:
                assert sse_resp.status_code == 200
                async for line in sse_resp.aiter_lines():
                    if line:
                        received_chunks.append(line)
                        if not published and line == "event: connected":
                            published = True
                            await redis.publish(
                                f"inbox:events:{tenant_a}",
                                json.dumps(
                                    {
                                        "event": "conversation.updated",
                                        "conversation_id": str(conv_wa),
                                    }
                                ),
                            )
            assert any("conversation.updated" in line for line in received_chunks)

            # 8. Cross-tenant isolation: Tenant B receives 404 on Tenant A conversation
            active_principal = CurrentPrincipal(
                user_id=user_b,
                tenant_id=tenant_b,
                role=UserRole.ADMIN,
                email="admin_b@other.test",
            )
            cross_resp = await client.get(f"/api/v1/inbox/conversations/{conv_wa}/messages")
            assert cross_resp.status_code == 404
    finally:
        async with migrator_engine.begin() as conn:
            for tbl in (
                "audit_events",
                "messages",
                "conversations",
                "customers",
                "channel_accounts",
            ):
                await conn.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id IN (:ta, :tb)"),
                    {"ta": tenant_a, "tb": tenant_b},
                )
            await conn.execute(
                text("DELETE FROM tenants WHERE id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
        await redis.aclose()
        await runtime_engine.dispose()
        await migrator_engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/test_inbox_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'erp_core.channels.inbox_router'`.

- [ ] **Step 3: Write `inbox_schemas.py`, `inbox_service.py`, `inbox_router.py`, and mount in `api_main.py`**

Create `apps/backend/src/erp_core/channels/inbox_schemas.py`:

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.identity.schemas import CustomerSummaryRead
from erp_core.shared.enums import (
    AutonomyLevel,
    ChannelType,
    ConversationMode,
    ConversationStatus,
    DeliveryStatus,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)

InboxTabFilter = Literal["all", "unread", "pending_human", "sla_urgent"]


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    customer_id: UUID
    channel_account_id: UUID
    channel_type: ChannelType
    external_thread_id: str | None = None
    subject: str | None = None
    conversation_mode: ConversationMode
    status: ConversationStatus
    assigned_user_id: UUID | None = None
    ai_autonomy_override: AutonomyLevel | None = None
    csw_expires_at: datetime | None = None
    fep_expires_at: datetime | None = None
    human_agent_window_expires_at: datetime | None = None
    sla_due_at: datetime | None = None
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    last_message_preview: str | None = None
    unread_count: int
    updated_at: datetime
    customer_name: str | None = None
    country_code: str | None = None
    iana_timezone: str | None = None
    customer: CustomerSummaryRead | None = None


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    channel_type: ChannelType
    external_message_id: str | None = None
    direction: MessageDirection
    sender_type: MessageSenderType
    sender_id: str | None = None
    content_type: MessageContentType
    body_text: str | None = None
    body_sanitized_html: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_status: DeliveryStatus
    channel_msg_cost: Decimal = Decimal("0.000000")
    source_timestamp: datetime
    created_at: datetime


class SendInboxMessageRequest(BaseModel):
    direction: Literal["outbound", "internal_note"] = "outbound"
    content_type: MessageContentType = MessageContentType.TEXT
    body_text: str | None = None
    body_html: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_components: list[dict[str, Any]] = Field(default_factory=list)
    translate_to: str | None = None
    target_language: str | None = None
    translate_before_send: bool = False
    use_human_agent_tag: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SendInboxMessageResponse(BaseModel):
    message: MessageRead
    translated_preview: str | None = None
    queued_for_delivery: bool


class UpdateConversationRequest(BaseModel):
    status: ConversationStatus | None = None
    assigned_user_id: UUID | None = None
    conversation_mode: ConversationMode | None = None
    ai_autonomy_override: AutonomyLevel | None = None
    mark_read: bool | None = None
```

Create `apps/backend/src/erp_core/channels/inbox_service.py`:

```python
from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import OutboundMessageRequest
from erp_core.channels.adapters.whatsapp import WhatsAppAdapter
from erp_core.channels.inbox_schemas import (
    ConversationRead,
    InboxTabFilter,
    MessageRead,
    SendInboxMessageRequest,
    SendInboxMessageResponse,
    UpdateConversationRequest,
)
from erp_core.channels.models import ChannelAccount, Conversation, Message
from erp_core.identity.service import IdentityResolverService
from erp_core.shared.enums import (
    AutonomyLevel,
    ChannelType,
    ConversationMode,
    ConversationStatus,
    DeliveryStatus,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal


class TranslationHook(Protocol):
    async def translate_preview(self, text: str, target_lang: str) -> str:
        raise NotImplementedError


class DefaultTranslationPreviewHook:
    """Deterministic preview hook replaced or wrapped by Plan 04 AI translation router."""

    async def translate_preview(self, text: str, target_lang: str) -> str:
        return f"[{target_lang.lower()}] {text}"


class InboxService:
    """Unified omnichannel inbox query, outbound dispatch, and SSE event broadcaster."""

    def __init__(
        self,
        *,
        identity_service: IdentityResolverService | None = None,
        translation_hook: TranslationHook | None = None,
    ) -> None:
        self._identity_service = identity_service or IdentityResolverService()
        self._translator = translation_hook or DefaultTranslationPreviewHook()

    async def list_conversations(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        tab: InboxTabFilter = "all",
        channel_type: ChannelType | None = None,
        assigned_user_id: uuid.UUID | None = None,
        q: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[ConversationRead]:
        stmt = select(Conversation).where(Conversation.tenant_id == tenant_id)
        if tab == "unread":
            stmt = stmt.where(Conversation.unread_count > 0)
        elif tab == "pending_human":
            stmt = stmt.where(Conversation.status == ConversationStatus.PENDING_HUMAN.value)
        elif tab == "sla_urgent":
            stmt = stmt.where(
                Conversation.sla_due_at.is_not(None),
                Conversation.status != ConversationStatus.RESOLVED.value,
            )

        if channel_type is not None:
            stmt = stmt.where(Conversation.channel_type == channel_type.value)
        if assigned_user_id is not None:
            stmt = stmt.where(Conversation.assigned_user_id == assigned_user_id)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Conversation.subject.ilike(pattern),
                    Conversation.last_message_preview.ilike(pattern),
                )
            )

        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()

        if tab == "sla_urgent":
            ordered = stmt.order_by(
                Conversation.sla_due_at.asc().nulls_last(),
                Conversation.updated_at.desc(),
            )
        else:
            ordered = stmt.order_by(Conversation.updated_at.desc())

        convs = (
            await session.execute(ordered.limit(limit).offset(offset))
        ).scalars().all()

        cust_ids = list({c.customer_id for c in convs})
        summaries = await self._identity_service.get_customer_summaries(
            session, tenant_id, cust_ids
        )

        items = []
        for c in convs:
            cust_summary = summaries.get(c.customer_id)
            items.append(
                ConversationRead(
                    id=c.id,
                    tenant_id=c.tenant_id,
                    customer_id=c.customer_id,
                    channel_account_id=c.channel_account_id,
                    channel_type=ChannelType(c.channel_type),
                    external_thread_id=c.external_thread_id,
                    subject=c.subject,
                    conversation_mode=ConversationMode(c.conversation_mode),
                    status=ConversationStatus(c.status),
                    assigned_user_id=c.assigned_user_id,
                    ai_autonomy_override=(
                        AutonomyLevel(c.ai_autonomy_override)
                        if c.ai_autonomy_override
                        else None
                    ),
                    csw_expires_at=c.csw_expires_at,
                    fep_expires_at=c.fep_expires_at,
                    human_agent_window_expires_at=c.human_agent_window_expires_at,
                    sla_due_at=c.sla_due_at,
                    last_inbound_at=c.last_inbound_at,
                    last_outbound_at=c.last_outbound_at,
                    last_message_preview=c.last_message_preview,
                    unread_count=c.unread_count,
                    updated_at=c.updated_at,
                    customer_name=cust_summary.display_name if cust_summary else None,
                    country_code=cust_summary.country_code if cust_summary else None,
                    iana_timezone=(
                        cust_summary.buyer_local_time.iana_timezone if cust_summary else None
                    ),
                    customer=cust_summary,
                )
            )
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def list_messages(
        self,
        session: AsyncSession,
        redis: Redis,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        mark_read: bool = True,
    ) -> PaginatedResponse[MessageRead]:
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id == conversation_id,
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise LookupError("Conversation not found")

        stmt = select(Message).where(
            Message.tenant_id == tenant_id,
            Message.conversation_id == conversation_id,
        )
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        rows = (
            await session.execute(
                stmt.order_by(Message.source_timestamp.asc(), Message.created_at.asc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()

        if mark_read and conv.unread_count > 0:
            conv.unread_count = 0
            await session.flush()
            await redis.publish(
                f"inbox:events:{tenant_id}",
                json.dumps(
                    {
                        "event": "conversation.updated",
                        "conversation_id": str(conversation_id),
                        "unread_count": 0,
                    }
                ),
            )

        items = [MessageRead.model_validate(m) for m in rows]
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )

    async def send_message(
        self,
        session: AsyncSession,
        redis: Redis,
        tenant_id: uuid.UUID,
        principal: CurrentPrincipal,
        conversation_id: uuid.UUID,
        req: SendInboxMessageRequest,
        *,
        outbound_enqueue_fn: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> SendInboxMessageResponse:
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id == conversation_id,
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise LookupError("Conversation not found")

        acct = (
            await session.execute(
                select(ChannelAccount).where(
                    ChannelAccount.tenant_id == tenant_id,
                    ChannelAccount.id == conv.channel_account_id,
                )
            )
        ).scalar_one()

        target_lang = req.translate_to or (
            req.target_language if req.translate_before_send else None
        )
        translated_preview: str | None = None
        if target_lang and req.body_text:
            translated_preview = await self._translator.translate_preview(
                req.body_text, target_lang
            )

        meta = dict(req.metadata)
        if translated_preview is not None:
            meta["translate_to"] = target_lang
            meta["translated_preview"] = translated_preview

        now_ts = datetime.now(timezone.utc)
        msg_id = uuid.uuid4()

        if req.direction == MessageDirection.INTERNAL_NOTE.value:
            msg = Message(
                id=msg_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                customer_id=conv.customer_id,
                channel_type=conv.channel_type,
                direction=MessageDirection.INTERNAL_NOTE.value,
                sender_type=MessageSenderType.HUMAN_AGENT.value,
                sender_id=str(principal.user_id),
                content_type=req.content_type.value,
                body_text=req.body_text,
                body_sanitized_html=req.body_html,
                channel_metadata=meta,
                delivery_status=DeliveryStatus.DELIVERED.value,
                channel_msg_cost=Decimal("0.000000"),
                source_timestamp=now_ts,
            )
            session.add(msg)
            await session.flush()
            await redis.publish(
                f"inbox:events:{tenant_id}",
                json.dumps(
                    {
                        "event": "message.created",
                        "conversation_id": str(conversation_id),
                        "message_id": str(msg_id),
                        "direction": "internal_note",
                    }
                ),
            )
            return SendInboxMessageResponse(
                message=MessageRead.model_validate(msg),
                translated_preview=translated_preview,
                queued_for_delivery=False,
            )

        summaries = await self._identity_service.get_customer_summaries(
            session, tenant_id, [conv.customer_id]
        )
        cust_summary = summaries.get(conv.customer_id)
        recipient_id = (
            conv.external_thread_id
            or (cust_summary.primary_phone_e164 if cust_summary else None)
            or (cust_summary.primary_email if cust_summary else None)
            or ""
        )

        if conv.channel_type in (
            ChannelType.WHATSAPP_CLOUD.value,
            ChannelType.WHATSAPP_COEXISTENCE.value,
        ):
            decision = await WhatsAppAdapter().evaluate_send_policy(
                session=session,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                customer_id=conv.customer_id,
                recipient_address=recipient_id,
                content_type=req.content_type,
                template_category=meta.get("template_category"),
                now_utc=now_ts,
            )
            if not decision.allowed:
                raise PermissionError(decision.block_reason or "ERR_WHATSAPP_POLICY_BLOCKED")

        msg = Message(
            id=msg_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            customer_id=conv.customer_id,
            channel_type=conv.channel_type,
            direction=MessageDirection.OUTBOUND.value,
            sender_type=MessageSenderType.HUMAN_AGENT.value,
            sender_id=str(principal.user_id),
            content_type=req.content_type.value,
            body_text=req.body_text,
            body_sanitized_html=req.body_html,
            channel_metadata=meta,
            delivery_status=DeliveryStatus.QUEUED.value,
            channel_msg_cost=Decimal("0.000000"),
            source_timestamp=now_ts,
        )
        session.add(msg)
        conv.last_outbound_at = now_ts
        conv.last_message_preview = (req.body_text or "")[:180]
        conv.unread_count = 0
        conv.updated_at = now_ts
        await session.flush()

        outbound_req = OutboundMessageRequest(
            tenant_id=tenant_id,
            channel_account_id=conv.channel_account_id,
            channel_type=ChannelType(conv.channel_type),
            external_account_id=acct.external_account_id,
            recipient_id=recipient_id,
            conversation_id=conversation_id,
            message_id=msg_id,
            content_type=req.content_type,
            body_text=req.body_text,
            body_html=req.body_html,
            template_name=req.template_name,
            template_language=req.template_language,
            template_components=req.template_components,
            subject=conv.subject,
            metadata=meta,
        )
        req_dict = outbound_req.model_dump(mode="json")
        if outbound_enqueue_fn is not None:
            await outbound_enqueue_fn(req_dict)
        else:
            await redis.xadd("stream:outbound:messages", {"payload": json.dumps(req_dict)})

        await redis.publish(
            f"inbox:events:{tenant_id}",
            json.dumps(
                {
                    "event": "message.created",
                    "conversation_id": str(conversation_id),
                    "message_id": str(msg_id),
                    "direction": "outbound",
                }
            ),
        )
        return SendInboxMessageResponse(
            message=MessageRead.model_validate(msg),
            translated_preview=translated_preview,
            queued_for_delivery=True,
        )

    async def update_conversation(
        self,
        session: AsyncSession,
        redis: Redis,
        tenant_id: uuid.UUID,
        principal: CurrentPrincipal,
        conversation_id: uuid.UUID,
        req: UpdateConversationRequest,
    ) -> ConversationRead:
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id == conversation_id,
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise LookupError("Conversation not found")

        before = {
            "status": conv.status,
            "assigned_user_id": str(conv.assigned_user_id) if conv.assigned_user_id else None,
            "ai_autonomy_override": conv.ai_autonomy_override,
        }
        if req.status is not None:
            conv.status = req.status.value
        if req.assigned_user_id is not None:
            conv.assigned_user_id = req.assigned_user_id
        if req.conversation_mode is not None:
            conv.conversation_mode = req.conversation_mode.value
        if req.ai_autonomy_override is not None:
            conv.ai_autonomy_override = req.ai_autonomy_override.value
        if req.mark_read:
            conv.unread_count = 0
        conv.updated_at = datetime.now(timezone.utc)
        await session.flush()

        after = {
            "status": conv.status,
            "assigned_user_id": str(conv.assigned_user_id) if conv.assigned_user_id else None,
            "ai_autonomy_override": conv.ai_autonomy_override,
        }
        await record_audit_event(
            session=session,
            tenant_id=tenant_id,
            actor_type="human_user",
            actor_id=str(principal.user_id),
            action="conversation.updated",
            resource_type="conversation",
            resource_id=conversation_id,
            before_state=before,
            after_state=after,
        )
        await redis.publish(
            f"inbox:events:{tenant_id}",
            json.dumps(
                {
                    "event": "conversation.updated",
                    "conversation_id": str(conversation_id),
                    "status": conv.status,
                }
            ),
        )
        summaries = await self._identity_service.get_customer_summaries(
            session, tenant_id, [conv.customer_id]
        )
        cust_summary = summaries.get(conv.customer_id)
        return ConversationRead(
            id=conv.id,
            tenant_id=conv.tenant_id,
            customer_id=conv.customer_id,
            channel_account_id=conv.channel_account_id,
            channel_type=ChannelType(conv.channel_type),
            external_thread_id=conv.external_thread_id,
            subject=conv.subject,
            conversation_mode=ConversationMode(conv.conversation_mode),
            status=ConversationStatus(conv.status),
            assigned_user_id=conv.assigned_user_id,
            ai_autonomy_override=(
                AutonomyLevel(conv.ai_autonomy_override)
                if conv.ai_autonomy_override
                else None
            ),
            csw_expires_at=conv.csw_expires_at,
            fep_expires_at=conv.fep_expires_at,
            human_agent_window_expires_at=conv.human_agent_window_expires_at,
            sla_due_at=conv.sla_due_at,
            last_inbound_at=conv.last_inbound_at,
            last_outbound_at=conv.last_outbound_at,
            last_message_preview=conv.last_message_preview,
            unread_count=conv.unread_count,
            updated_at=conv.updated_at,
            customer_name=cust_summary.display_name if cust_summary else None,
            country_code=cust_summary.country_code if cust_summary else None,
            iana_timezone=(
                cust_summary.buyer_local_time.iana_timezone if cust_summary else None
            ),
            customer=cust_summary,
        )
```

Create `apps/backend/src/erp_core/channels/inbox_router.py`:

```python
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.inbox_schemas import (
    ConversationRead,
    InboxTabFilter,
    MessageRead,
    SendInboxMessageRequest,
    SendInboxMessageResponse,
    UpdateConversationRequest,
)
from erp_core.channels.inbox_service import InboxService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

inbox_router = APIRouter(prefix="/api/v1/inbox", tags=["inbox"])
_require_inbox_access = require_roles(
    UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT
)


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


def _get_redis(request: Request) -> aioredis.Redis:
    client = getattr(request.app.state, "redis", None)
    if client is None:
        client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        request.app.state.redis = client
    return client


@inbox_router.get("/conversations", response_model=PaginatedResponse[ConversationRead])
async def list_inbox_conversations(
    request: Request,
    tab: InboxTabFilter | None = Query(default=None),
    filter: InboxTabFilter | None = Query(default=None),
    channel_type: ChannelType | None = Query(default=None),
    channel: ChannelType | None = Query(default=None),
    assigned_user_id: UUID | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: CurrentPrincipal = Depends(_require_inbox_access),
) -> PaginatedResponse[ConversationRead]:
    session_factory = _get_session_factory(request)
    service = InboxService()
    effective_tab: InboxTabFilter = tab or filter or "all"
    effective_channel = channel_type or channel
    async with tenant_session(session_factory, principal.tenant_id) as session:
        return await service.list_conversations(
            session=session,
            tenant_id=principal.tenant_id,
            tab=effective_tab,
            channel_type=effective_channel,
            assigned_user_id=assigned_user_id,
            q=q,
            limit=limit,
            offset=offset,
        )


@inbox_router.get(
    "/conversations/{conversation_id}/messages",
    response_model=PaginatedResponse[MessageRead],
)
async def list_conversation_messages(
    conversation_id: UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    mark_read: bool = Query(default=True),
    principal: CurrentPrincipal = Depends(_require_inbox_access),
) -> PaginatedResponse[MessageRead]:
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    service = InboxService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            return await service.list_messages(
                session=session,
                redis=redis,
                tenant_id=principal.tenant_id,
                conversation_id=conversation_id,
                limit=limit,
                offset=offset,
                mark_read=mark_read,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc


@inbox_router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SendInboxMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_conversation_message(
    conversation_id: UUID,
    payload: SendInboxMessageRequest,
    request: Request,
    principal: CurrentPrincipal = Depends(_require_inbox_access),
) -> SendInboxMessageResponse:
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    enqueue_fn = getattr(request.app.state, "outbound_enqueue_fn", None)
    service = InboxService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            return await service.send_message(
                session=session,
                redis=redis,
                tenant_id=principal.tenant_id,
                principal=principal,
                conversation_id=conversation_id,
                req=payload,
                outbound_enqueue_fn=enqueue_fn,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc


@inbox_router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationRead,
)
async def patch_conversation(
    conversation_id: UUID,
    payload: UpdateConversationRequest,
    request: Request,
    principal: CurrentPrincipal = Depends(_require_inbox_access),
) -> ConversationRead:
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    service = InboxService()
    async with tenant_session(session_factory, principal.tenant_id) as session:
        try:
            return await service.update_conversation(
                session=session,
                redis=redis,
                tenant_id=principal.tenant_id,
                principal=principal,
                conversation_id=conversation_id,
                req=payload,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc


@inbox_router.get("/stream")
@inbox_router.get("/events", include_in_schema=False)
async def stream_inbox_events(
    request: Request,
    max_events: int | None = Query(default=None, ge=1),
    principal: CurrentPrincipal = Depends(_require_inbox_access),
) -> StreamingResponse:
    """Stream tenant-isolated real-time inbox events via Redis Pub/Sub SSE."""
    redis = _get_redis(request)
    channel_name = f"inbox:events:{principal.tenant_id}"

    async def _event_generator() -> AsyncIterator[str]:
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel_name)
        yield f"event: connected\ndata: {json.dumps({'tenant_id': str(principal.tenant_id)})}\n\n"
        emitted = 0
        try:
            while True:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    raw_data = msg["data"]
                    data_str = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
                    parsed = json.loads(data_str)
                    event_name = parsed.get("event", "message")
                    yield f"event: {event_name}\ndata: {data_str}\n\n"
                    emitted += 1
                    if max_events is not None and emitted >= max_events:
                        break
                else:
                    await asyncio.sleep(0.05)
        finally:
            await pubsub.unsubscribe(channel_name)
            await pubsub.aclose()

    return StreamingResponse(_event_generator(), media_type="text/event-stream")
```

Modify `apps/backend/src/erp_core/api_main.py` to mount `inbox_router`:

```python
from erp_core.channels.inbox_router import inbox_router

app.include_router(inbox_router)
```

- [ ] **Step 4: Run test, OpenAPI schema export, and `import-linter` boundary check**

Run:
```bash
cd apps/backend
uv run pytest tests/test_inbox_api.py -v
uv run pytest tests/test_channels_migration_rls.py tests/test_rate_limiter.py tests/test_webhook_idempotency.py tests/test_identity_graph.py tests/test_whatsapp_adapter.py tests/test_email_and_b2b_parser.py tests/test_web_livechat.py tests/test_inbox_api.py -v
uv run python scripts/export_openapi.py > ../../contracts/openapi.json
cd ../.. && pnpm --filter web openapi-ts
cd apps/backend && uv run lint-imports --config ../../infra/importlinter.ini
```
Expected: All Plan 02 test suites pass without skips, `contracts/openapi.json` and `apps/web/src/lib/api-client/` update cleanly, and `import-linter` reports `0 broken contracts`.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/erp_core/channels/inbox_schemas.py apps/backend/src/erp_core/channels/inbox_service.py apps/backend/src/erp_core/channels/inbox_router.py apps/backend/src/erp_core/api_main.py apps/backend/tests/test_inbox_api.py contracts/openapi.json apps/web/src/lib/api-client/
git commit -m "feat(inbox): add Unified Inbox REST/SSE API, translation hook, and sync OpenAPI client"
```
