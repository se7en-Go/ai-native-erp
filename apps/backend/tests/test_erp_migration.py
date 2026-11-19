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
        # Tenants, customers and every ERP row are FORCE-RLS protected, so seeding runs
        # through the same fail-closed runtime path production uses (`app_runtime` behind
        # PgBouncer with a transaction-scoped `app.current_tenant_id`).
        for tid, cid in [(tenant_a, customer_a), (tenant_b, customer_b)]:
            async with tenant_session(rt_factory, tid) as seed:
                await seed.execute(
                    text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                    {"id": tid, "name": f"t-{tid.hex[:8]}", "slug": f"t-{tid.hex[:8]}"},
                )
                await seed.execute(
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
                    VALUES (
                        :pid, :tid, 'SPU-LED-5M', 'led_strip',
                        '{"en": {"title": "LED Strip"}}'::jsonb
                    )
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
                    INSERT INTO inventory_balances (
                        tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty
                    ) VALUES (:tid, :wid, :vid, 10, 4)
                    """
                ),
                {"tid": tenant_a, "wid": warehouse_a, "vid": variant_a},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO orders (
                        id, tenant_id, order_no, order_mode, customer_id, currency,
                        fx_rate_to_base, subtotal_amount, total_amount,
                        fulfillment_status, payment_status
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
                    text(
                        "SELECT net_contribution_margin_base FROM order_profit_snapshots "
                        "WHERE order_id = :oid"
                    ),
                    {"oid": order_a},
                )
            ).scalar_one()
            # 720.00 - 200.00 - 36.00 - 21.60 - 65.00 - 2.40 - 50.00 + 5.00 = 350.00
            assert Decimal(str(snap)) == Decimal("350.00")

        # Verify CHECK (on_hand_qty >= reserved_qty) fails cleanly
        # `tenant_session` is an async context manager, so it cannot be merged into the
        # same `with` header as the synchronous `pytest.raises` block.
        with pytest.raises(IntegrityError):  # noqa: SIM117
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
                await session_b.execute(
                    text("SELECT COUNT(*) FROM orders WHERE id = :oid"), {"oid": order_a}
                )
            ).scalar_one()
            count_snaps = (
                await session_b.execute(
                    text("SELECT COUNT(*) FROM order_profit_snapshots WHERE order_id = :oid"),
                    {"oid": order_a},
                )
            ).scalar_one()
            assert count_orders == 0
            assert count_snaps == 0

        # Verify fail-closed when app.current_tenant_id is empty/unset
        async with rt_factory() as raw_session, raw_session.begin():
            await raw_session.execute(
                text("SELECT set_config('app.current_tenant_id', '', true)")
            )
            count_products = (
                await raw_session.execute(text("SELECT COUNT(*) FROM products"))
            ).scalar_one()
            assert count_products == 0
    finally:
        # Cleanup mirrors the seed path: FORCE RLS means only the tenant's own runtime
        # session may delete its rows, and `app_migrator` would silently filter them out.
        for tid in (tenant_a, tenant_b):
            async with tenant_session(rt_factory, tid) as cleanup:
                await cleanup.execute(
                    text("DELETE FROM order_profit_snapshots WHERE tenant_id = :tid"), {"tid": tid}
                )
                await cleanup.execute(
                    text("DELETE FROM orders WHERE tenant_id = :tid"), {"tid": tid}
                )
                await cleanup.execute(
                    text("DELETE FROM inventory_balances WHERE tenant_id = :tid"), {"tid": tid}
                )
                await cleanup.execute(
                    text("DELETE FROM product_variants WHERE tenant_id = :tid"), {"tid": tid}
                )
                await cleanup.execute(
                    text("DELETE FROM products WHERE tenant_id = :tid"), {"tid": tid}
                )
                await cleanup.execute(
                    text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tid}
                )
                await cleanup.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid})
        await mig_engine.dispose()
        await rt_engine.dispose()
