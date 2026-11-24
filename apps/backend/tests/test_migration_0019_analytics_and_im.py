"""PG17 migration + RLS tests for 0019_analytics_im_sourcing.

Covers the cross-channel daily sales analytics view (`vw_tenant_channel_sales_analytics`),
the `marketplace_buyer_id` identity type, and the new `sourcing_drafts` JSONB columns,
including fail-closed behaviour and tenant isolation through the SECURITY INVOKER view.

Runs against the local PostgreSQL 17 migrator/runtime roles on 5432
(`DATABASE_MIGRATOR_URL` / `DATABASE_RUNTIME_URL`), never against the PgBouncer port.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_migrator_engine,
    create_runtime_engine,
    create_session_factory,
    tenant_session,
)

_VIEW = "vw_tenant_channel_sales_analytics"

_EXPECTED_VIEW_COLUMNS: tuple[str, ...] = (
    "tenant_id",
    "channel_account_id",
    "channel_type",
    "channel_label",
    "metric_date",
    "order_count",
    "gmv_base",
    "refunded_amount_base",
    "cogs_base",
    "platform_commission_base",
    "ad_spend_base",
    "net_margin_base",
    "impressions",
    "clicks",
    "external_sessions",
)

_METRIC_DATE = date(2026, 11, 20)


@pytest.fixture
async def migrator_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_migrator_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def runtime_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_runtime_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()


def _load_migration():  # type: ignore[no-untyped-def]
    path = Path(__file__).resolve().parents[1] / (
        "alembic/versions/0019_analytics_im_sourcing.py"
    )
    spec = spec_from_file_location("revision_0019", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _identity_constraint_def(conn: Connection) -> str:
    definition = conn.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_customer_identities_type' "
            "AND conrelid = 'public.customer_identities'::regclass"
        )
    )
    assert definition is not None
    return str(definition)


def _view_reloptions(conn: Connection) -> list[str]:
    options = conn.scalar(
        text(
            "SELECT reloptions FROM pg_class "
            "WHERE relname = :view AND relnamespace = 'public'::regnamespace"
        ),
        {"view": _VIEW},
    )
    return [str(option) for option in (options or [])]


def _view_columns(conn: Connection) -> tuple[str, ...]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :view "
            "ORDER BY ordinal_position"
        ),
        {"view": _VIEW},
    )
    return tuple(row[0] for row in rows.fetchall())


def _sourcing_draft_columns(conn: Connection) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'sourcing_drafts' "
            "AND column_name = ANY(:names)"
        ),
        {"names": ["localized_copies", "marketplace_category_attrs"]},
    )
    return {row[0] for row in rows.fetchall()}


@pytest.mark.asyncio
async def test_0019_round_trip_in_rolled_back_transaction(
    migrator_engine: AsyncEngine,
) -> None:
    migration = _load_migration()

    def round_trip(conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            assert migration.revision == "0019_analytics_im_sourcing"
            assert migration.down_revision == "0018_marketplace_channels"
            # alembic_version.version_num is VARCHAR(32).
            assert len(migration.revision) <= 32

            # --- pre-state (head) -------------------------------------------------
            assert "marketplace_buyer_id" in _identity_constraint_def(conn)
            assert _view_columns(conn) == _EXPECTED_VIEW_COLUMNS
            assert _view_reloptions(conn) == ["security_invoker=true"]
            assert _sourcing_draft_columns(conn) == {
                "localized_copies",
                "marketplace_category_attrs",
            }
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.role_table_grants "
                        "WHERE table_name = :view AND grantee = 'app_runtime' "
                        "AND privilege_type = 'SELECT'"
                    ),
                    {"view": _VIEW},
                )
                == 1
            )

            # --- downgrade -------------------------------------------------------
            migration.downgrade()
            assert (
                conn.scalar(text(f"SELECT to_regclass('public.{_VIEW}')")) is None
            )
            assert _sourcing_draft_columns(conn) == set()
            downgraded_def = _identity_constraint_def(conn)
            assert "marketplace_buyer_id" not in downgraded_def
            for legacy_type in (
                "email",
                "phone_e164",
                "whatsapp_waid",
                "whatsapp_bsuid",
                "ig_psid",
                "fb_psid",
                "shopify_customer_id",
                "tiktok_buyer_id",
                "amazon_buyer_email",
                "web_visitor_id",
            ):
                assert legacy_type in downgraded_def

            # --- upgrade ---------------------------------------------------------
            migration.upgrade()
            assert conn.scalar(text(f"SELECT to_regclass('public.{_VIEW}')::text")) == _VIEW
            assert _view_columns(conn) == _EXPECTED_VIEW_COLUMNS
            assert _view_reloptions(conn) == ["security_invoker=true"]
            assert _sourcing_draft_columns(conn) == {
                "localized_copies",
                "marketplace_category_attrs",
            }
            assert "marketplace_buyer_id" in _identity_constraint_def(conn)
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.role_table_grants "
                        "WHERE table_name = :view AND grantee = 'app_runtime' "
                        "AND privilege_type = 'SELECT'"
                    ),
                    {"view": _VIEW},
                )
                == 1
            )

    async with migrator_engine.connect() as conn:
        try:
            await conn.run_sync(round_trip)
        finally:
            await conn.rollback()


@pytest.mark.asyncio
async def test_0019_fail_closed_and_cross_channel_rls_isolation(
    runtime_engine: AsyncEngine,
) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    customer_a = uuid4()
    channel_account_a = uuid4()
    order_a = uuid4()
    draft_a = uuid4()
    runtime_factory = create_session_factory(runtime_engine)

    for tid in (tenant_a, tenant_b):
        async with tenant_session(runtime_factory, tid) as setup_sess:
            await setup_sess.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, base_currency) "
                    "VALUES (:id, :slug, :name, 'USD')"
                ),
                {
                    "id": tid,
                    "slug": f"p1-0019-{tid.hex[:8]}",
                    "name": f"P1 0019 {tid.hex[:8]}",
                },
            )

    try:
        # Tenant A: shopee order + profit snapshot + daily traffic + IM identity + draft.
        async with tenant_session(runtime_factory, tenant_a) as session_a:
            await session_a.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, display_name, country_code) "
                    "VALUES (:id, :tid, 'Nguyen Van A', 'SG')"
                ),
                {"id": customer_a, "tid": tenant_a},
            )
            await session_a.execute(
                text(
                    "INSERT INTO customer_identities (tenant_id, customer_id, identity_type, "
                    "identity_value, confidence_level, metadata) "
                    "VALUES (:tid, :cid, 'marketplace_buyer_id', 'shopee-buyer-778899', "
                    "'deterministic', '{\"marketplace\": \"shopee\"}'::jsonb)"
                ),
                {"tid": tenant_a, "cid": customer_a},
            )
            await session_a.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status) "
                    "VALUES (:id, :tid, 'shopee', 'shopee-sg-001', 'Shopee SG Store', 'active')"
                ),
                {"id": channel_account_a, "tid": tenant_a},
            )
            await session_a.execute(
                text(
                    "INSERT INTO orders (id, tenant_id, order_no, order_mode, "
                    "channel_account_id, external_order_id, customer_id, currency, "
                    "fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status, "
                    "payment_status, channel_metadata, created_at) VALUES "
                    "(:oid, :tid, 'SHOPEE-0001', 'b2c_retail', :ca, 'SP-778899', :cid, "
                    "'USD', 7.200000, 1000.00, 1000.00, 'delivered', 'paid', "
                    "'{\"refunded_amount\": \"50.00\", \"platform_commission_base\": "
                    "\"n/a\"}'::jsonb, :created_at)"
                ),
                {
                    "oid": order_a,
                    "tid": tenant_a,
                    "ca": channel_account_a,
                    "cid": customer_a,
                    "created_at": datetime(2026, 11, 20, 10, 30, tzinfo=UTC),
                },
            )
            await session_a.execute(
                text(
                    "INSERT INTO order_profit_snapshots (order_id, tenant_id, base_currency, "
                    "gross_revenue_base, cogs_base, platform_commission_base, "
                    "ad_attribution_cost_base) VALUES "
                    "(:oid, :tid, 'USD', 7200.00, 2000.00, 90.00, 30.00)"
                ),
                {"oid": order_a, "tid": tenant_a},
            )
            await session_a.execute(
                text(
                    "INSERT INTO channel_daily_metrics (tenant_id, channel_account_id, "
                    "metric_date, impressions, clicks, ad_spend_base, external_sessions, source) "
                    "VALUES (:tid, :ca, :metric_date, 5000, 120, 12.50, 300, 'shopee_api')"
                ),
                {"tid": tenant_a, "ca": channel_account_a, "metric_date": _METRIC_DATE},
            )
            await session_a.execute(
                text(
                    "INSERT INTO sourcing_drafts (id, tenant_id, source_platform, raw_title, "
                    "optimized_title_en, suggested_sku, localized_copies, "
                    "marketplace_category_attrs) VALUES "
                    "(:id, :tid, '1688', 'XJ-400Pro 防水射灯', 'XJ-400Pro IP66 LED Floodlight', "
                    "'XJ-400PRO-SG', '{\"en\": {\"title\": \"XJ-400Pro IP66\"}, "
                    "\"zh-CN\": {\"title\": \"XJ-400Pro 防水射灯\"}}'::jsonb, "
                    "'{\"Shopee\": {\"category_id\": 100012, \"attr_set\": \"IS\"}}'::jsonb)"
                ),
                {"id": draft_a, "tid": tenant_a},
            )

            draft_row = (
                await session_a.execute(
                    text(
                        "SELECT localized_copies->>'en' AS en_copy, "
                        "marketplace_category_attrs->'Shopee'->>'category_id' AS shopee_cat "
                        "FROM sourcing_drafts WHERE id = :id"
                    ),
                    {"id": draft_a},
                )
            ).one()
            assert '"title": "XJ-400Pro IP66"' in draft_row.en_copy
            assert draft_row.shopee_cat == "100012"

        # An unknown identity type is still rejected by the widened CHECK.  A failing
        # statement aborts its transaction, so this runs in its own rolled-back session.
        with pytest.raises(IntegrityError):
            async with tenant_session(runtime_factory, tenant_a) as session_a_bad:
                await session_a_bad.execute(
                    text(
                        "INSERT INTO customer_identities (tenant_id, customer_id, "
                        "identity_type, identity_value, confidence_level) "
                        "VALUES (:tid, :cid, 'bogus_identity_type', 'x-1', 'deterministic')"
                    ),
                    {"tid": tenant_a, "cid": customer_a},
                )

        # 1. Fail-closed: a bare runtime session (no app.current_tenant_id) sees nothing
        #    through the SECURITY INVOKER view and cannot write a sourcing draft.
        async with runtime_factory() as bare_session:
            view_count = (
                await bare_session.execute(text(f"SELECT count(*) FROM {_VIEW}"))
            ).scalar_one()
            assert view_count == 0

        async with runtime_factory() as bare_write_session:
            with pytest.raises(DBAPIError):
                async with bare_write_session.begin():
                    await bare_write_session.execute(
                        text(
                            "INSERT INTO sourcing_drafts (id, tenant_id, source_platform, "
                            "raw_title, optimized_title_en, suggested_sku) VALUES "
                            "(:id, :tid, '1688', 'Leak', 'Leak', 'LEAK-1')"
                        ),
                        {"id": uuid4(), "tid": tenant_a},
                    )

        # 2. Tenant B sees zero rows of Tenant A everywhere, including the analytics view.
        async with tenant_session(runtime_factory, tenant_b) as session_b:
            for source in (_VIEW, "orders", "order_profit_snapshots",
                           "channel_daily_metrics", "customer_identities", "sourcing_drafts"):
                count = (
                    await session_b.execute(text(f"SELECT count(*) FROM {source}"))
                ).scalar_one()
                assert count == 0, f"Tenant B leaked rows in {source}"

        # 3. Tenant A sees exactly one aggregated row with exact Decimals and counters.
        async with tenant_session(runtime_factory, tenant_a) as session_a_read:
            rows = (
                await session_a_read.execute(
                    text(
                        "SELECT tenant_id, channel_account_id, channel_type, channel_label, "
                        "metric_date, order_count, gmv_base, refunded_amount_base, cogs_base, "
                        "platform_commission_base, ad_spend_base, net_margin_base, "
                        "impressions, clicks, external_sessions "
                        f"FROM {_VIEW} ORDER BY metric_date, channel_account_id"
                    )
                )
            ).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.tenant_id == tenant_a
            assert row.channel_account_id == channel_account_a
            assert row.channel_type == "shopee"
            assert row.channel_label == "Shopee SG Store"
            assert row.metric_date == _METRIC_DATE
            assert row.order_count == 1
            assert row.gmv_base == Decimal("7200.00")
            # 50.00 USD refunded at 7.2 base FX.
            assert row.refunded_amount_base == Decimal("360.00")
            assert row.cogs_base == Decimal("2000.00")
            assert row.platform_commission_base == Decimal("90.00")
            # order-level ad attribution (30.00) + traffic ad spend (12.50).
            assert row.ad_spend_base == Decimal("42.50")
            # snapshot net contribution margin (7200 - 2000 - 90 - 30 = 5080) minus traffic.
            assert row.net_margin_base == Decimal("5067.50")
            assert row.impressions == 5000
            assert row.clicks == 120
            assert row.external_sessions == 300
            assert isinstance(row.gmv_base, Decimal)
            assert isinstance(row.net_margin_base, Decimal)
    finally:
        admin = create_async_engine(
            os.environ.get(
                "DATABASE_ADMIN_URL",
                "postgresql+asyncpg://postgres:postgres_dev_pass@127.0.0.1:5432/erp_saas",
            )
        )
        try:
            async with admin.begin() as conn:
                for tenant_id in (tenant_a, tenant_b):
                    for statement in (
                        "DELETE FROM channel_daily_metrics WHERE tenant_id = :tid",
                        "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                        "DELETE FROM orders WHERE tenant_id = :tid",
                        "DELETE FROM customer_identities WHERE tenant_id = :tid",
                        "DELETE FROM customers WHERE tenant_id = :tid",
                        "DELETE FROM sourcing_drafts WHERE tenant_id = :tid",
                        "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                    ):
                        await conn.execute(text(statement), {"tid": tenant_id})
                await conn.execute(
                    text("DELETE FROM tenants WHERE id = ANY(:ids)"),
                    {"ids": [tenant_a, tenant_b]},
                )
        finally:
            await admin.dispose()
