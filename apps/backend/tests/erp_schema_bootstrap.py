"""Test-only schema bridge for Plan 03 while Alembic revision `0002` is still in flight.

`0003_dual_mode_erp_and_cvp.py` declares `down_revision = "0002_channels_identity_inbox"`
and takes foreign keys onto the Plan 02 tables (`channel_accounts`, `b2b_companies`,
`customers`). Until Grok's `0002` lands on `main` those tables do not exist, so a plain
`alembic upgrade head` cannot reach `0003`.

This module lets the Plan 03 tests run against a real PostgreSQL 17 cluster *now* without
duplicating any DDL:

1. It creates the three Plan 02 prerequisite tables exactly as specified in
   `docs/superpowers/plans/2026-11-19-02-omnichannel-inbox-and-p0-channels.md`
   (same columns, constraints, indexes, `ENABLE`/`FORCE ROW LEVEL SECURITY` and policies).
2. It executes the real `upgrade()` body of `0003_dual_mode_erp_and_cvp.py` through an
   Alembic `Operations` context bound to the live connection, so every table, check
   constraint, generated column, index and RLS policy under test is the production DDL.

Once `0002` is merged, `ensure_erp_schema()` becomes a no-op because the tables it would
create already exist; the authoritative path stays `alembic upgrade head`.
"""

from __future__ import annotations

import importlib.util
from functools import partial
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
import structlog
from alembic import op
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

logger = structlog.get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REVISION_0003_PATH = _REPO_ROOT / "apps/backend/alembic/versions/0003_dual_mode_erp_and_cvp.py"

CHANNEL_TYPE_CHECK = (
    "channel_type IN ("
    "'whatsapp_cloud', 'whatsapp_coexistence', 'email', 'web_livechat', "
    "'shopify', 'woocommerce', 'shopline', 'shoplazza', 'tiktok_shop', "
    "'made_in_china_email', 'alibaba_email', 'messenger', 'instagram_dm', "
    "'telegram', 'amazon_sp', 'ebay', 'mercado_libre', 'temu', "
    "'walmart', 'shopee', 'lazada'"
    ")"
)

PLAN02_PREREQ_TABLES = ("channel_accounts", "b2b_companies", "customers", "customer_identities")


def _load_revision_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_erp_revision_0003", _REVISION_0003_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Unable to load Alembic revision from {_REVISION_0003_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _enable_force_rls(table_name: str) -> None:
    """Mirror the Plan 02 / Plan 03 fail-closed policy shape for the stand-in tables."""
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


def _drop_legacy_probe_tables() -> None:
    """Remove the throw-away tables the Plan 01 GDPR test fabricates when no schema exists.

    `test_audit_and_gdpr.py` creates local `customers` / `customer_identities` / `orders` /
    `order_profit_snapshots` stand-ins only while the authoritative schema is absent. On a
    clean database it therefore runs first and leaves probe tables behind that would block
    the real `0003` DDL (`orders` there has no `order_mode`, `order_profit_snapshots` has
    `net_profit_base` instead of the generated CVP column). Once `0003` is being applied
    those probe tables must give way.
    """
    bind = op.get_bind()
    order_mode_present = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'orders' "
            "AND column_name = 'order_mode'"
        )
    ).scalar()
    orders_present = bind.execute(
        sa.text("SELECT to_regclass('public.orders') IS NOT NULL")
    ).scalar()
    if orders_present and not order_mode_present:
        op.execute(
            "DROP TABLE IF EXISTS order_profit_snapshots, orders, "
            "customer_identities, customers CASCADE"
        )
        logger.info("erp_schema_bootstrap.dropped_legacy_probe_tables")


def _create_plan02_stub_tables() -> None:
    """Create the three Plan 02 tables `0003` depends on, per the Plan 02 spec."""
    _drop_legacy_probe_tables()

    existing = {
        row[0]
        for row in op.get_bind().execute(
            sa.text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = ANY(:names)"
            ),
            {"names": list(PLAN02_PREREQ_TABLES)},
        )
    }

    if "channel_accounts" not in existing:
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
            sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
            sa.Column(
                "rate_limit_profile",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text('\'{"mps": 80, "coexistence": false}\'::jsonb'),
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
            "idx_channel_accounts_tenant_type", "channel_accounts", ["tenant_id", "channel_type"]
        )
        _enable_force_rls("channel_accounts")
        logger.info("erp_schema_bootstrap.created_stub", table="channel_accounts")

    if "b2b_companies" not in existing:
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
                "credit_tier", sa.Text(), nullable=False, server_default=sa.text("'prepaid'")
            ),
            sa.Column(
                "default_incoterm", sa.Text(), nullable=False, server_default=sa.text("'FOB'")
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
            "idx_b2b_companies_tenant_domain", "b2b_companies", ["tenant_id", "website_domain"]
        )
        op.execute(
            "CREATE INDEX idx_b2b_companies_legal_name_trgm "
            "ON b2b_companies USING gin (legal_name gin_trgm_ops)"
        )
        _enable_force_rls("b2b_companies")
        logger.info("erp_schema_bootstrap.created_stub", table="b2b_companies")

    if "customers" not in existing:
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
                "customer_type", sa.Text(), nullable=False, server_default=sa.text("'b2c_consumer'")
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
                "preferred_language", sa.Text(), nullable=False, server_default=sa.text("'en'")
            ),
            sa.Column("iana_timezone", sa.Text(), nullable=True),
            sa.Column("timezone_source", sa.Text(), nullable=True),
            sa.Column(
                "lifecycle_stage", sa.Text(), nullable=False, server_default=sa.text("'lead'")
            ),
            sa.Column("ltv_usd", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
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
                "timezone_source IS NULL OR timezone_source IN "
                "('phone_prefix', 'shipping_country', 'manual')",
                name="ck_customers_timezone_source",
            ),
        )
        op.create_index(
            "idx_customers_tenant_created", "customers", ["tenant_id", sa.text("created_at DESC")]
        )
        op.create_index(
            "idx_customers_tenant_email",
            "customers",
            ["tenant_id", "primary_email"],
            postgresql_where=sa.text("primary_email IS NOT NULL"),
        )
        _enable_force_rls("customers")
        logger.info("erp_schema_bootstrap.created_stub", table="customers")

    if "customer_identities" not in existing:
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
        logger.info("erp_schema_bootstrap.created_stub", table="customer_identities")


def _apply_schema(connection: Connection, *, apply_0003: bool) -> None:
    """Always reconcile the Plan 02 stand-ins; apply the real 0003 DDL when still missing."""
    with Operations.context(MigrationContext.configure(connection)):
        _create_plan02_stub_tables()
        if apply_0003:
            _load_revision_module().upgrade()
    if apply_0003:
        logger.info("erp_schema_bootstrap.applied_revision", revision="0003")


async def _table_exists(connection: object, table: str) -> bool:
    result = await connection.execute(  # type: ignore[attr-defined]
        sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"}
    )
    return bool(result.scalar_one())


async def ensure_erp_schema(migrator_url: str) -> None:
    """Idempotently materialise the `0003` schema (plus Plan 02 stand-ins) on a live PG17."""
    engine = create_async_engine(migrator_url, echo=False)
    try:
        async with engine.begin() as connection:
            apply_0003 = not await _table_exists(connection, "products")
            await connection.run_sync(partial(_apply_schema, apply_0003=apply_0003))
    finally:
        await engine.dispose()
