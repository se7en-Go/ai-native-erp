from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from erp_core.ai_engine.numeric_guard import ToolGroundedNumericGuard
from erp_core.ai_engine.tools import ErpToolRegistry
from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_runtime_engine,
    create_session_factory,
    tenant_session,
)
from erp_core.shared.enums import Incoterm


@pytest.fixture
async def runtime_engine() -> AsyncEngine:
    engine = create_runtime_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_erp_tools_and_numeric_guard_blocks_fabricated_price(
    runtime_engine: AsyncEngine,
) -> None:
    tenant_id = uuid4()
    product_id = uuid4()
    variant_id = uuid4()
    warehouse_id = uuid4()
    customer_id = uuid4()
    order_id = uuid4()
    shipment_id = uuid4()
    rp_id = uuid4()
    runtime_factory = create_session_factory(runtime_engine)

    async with tenant_session(runtime_factory, tenant_id) as setup_sess:
        await setup_sess.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'LED Co', :slug)"),
            {"id": tenant_id, "slug": f"led-{tenant_id.hex[:8]}"},
        )
        await setup_sess.execute(
            text("""
            UPDATE tenants SET ai_policy_settings =
                '{"quote_pricing": {"usd_to_currency": {"CNY": "7.2"},
                                    "incoterm_surcharge_pct": {"FOB": "0"}}}'::jsonb
            WHERE id = :id
        """),
            {"id": tenant_id},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2b_contact', 'Hans Weber')"
            ),
            {"id": customer_id, "tid": tenant_id},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category, hs_code, certifications) "
                "VALUES (:id, :tid, 'SPU-XJ400', 'Floodlight', '9405429000', ARRAY['CE','RoHS'])"
            ),
            {"id": product_id, "tid": tenant_id},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO product_variants "
                "(id, tenant_id, product_id, sku, cost_price_base, base_currency, "
                "min_margin_floor_pct, "
                "retail_price_usd, b2b_moq_tiers, weight_grams, lead_time_days) "
                "VALUES (:id, :tid, :pid, 'XJ-400Pro-EU', 68.0000, 'CNY', 15.00, 29.00, "
                '\'[{"min_qty": 50, "unit_price_usd": "18.50"}, {"min_qty": 500, '
                '"unit_price_usd": "14.20"}]\'::jsonb, '
                "3200, 7)"
            ),
            {"id": variant_id, "tid": tenant_id, "pid": product_id},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
                "on_hand_qty, reserved_qty) "
                "VALUES (:tid, :wid, :vid, 1200, 150)"
            ),
            {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO orders "
                "(id, tenant_id, order_no, order_mode, customer_id, currency, fx_rate_to_base, "
                "subtotal_amount, total_amount, fulfillment_status, payment_status) "
                "VALUES (:id, :tid, 'ORD-2026-8801', 'b2c_retail', :cid, 'USD', 7.200000, "
                "58.00, 58.00, 'shipped', 'paid')"
            ),
            {"id": order_id, "tid": tenant_id, "cid": customer_id},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO shipments "
                "(id, tenant_id, order_id, tracking_provider, tracking_number, carrier_code, "
                "standardized_status, sub_status, last_event_at, tracking_events) "
                "VALUES (:id, :tid, :oid, '17track', 'LP123456789CN', 'yunexpress', "
                "'InTransit', 'InTransit_Customs', :ts, "
                '\'[{"time": "2026-11-18T10:00:00Z", "location": "Frankfurt", '
                '"description": "Arrived at customs"}]\'::jsonb)'
            ),
            {"id": shipment_id, "tid": tenant_id, "oid": order_id, "ts": datetime.now(UTC)},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO order_profit_snapshots "
                "(order_id, tenant_id, base_currency, gross_revenue_base, cogs_base, "
                "platform_commission_base, payment_gateway_fee_base, shipping_and_duty_base, "
                "channel_msg_cost_base) "
                "VALUES (:oid, :tid, 'CNY', 417.60, 136.00, 20.00, 12.00, 45.00, 0.00)"
            ),
            {"oid": order_id, "tid": tenant_id},
        )
        await setup_sess.execute(
            text(
                "INSERT INTO receivables_payables "
                "(id, tenant_id, order_id, direction, milestone_label, due_amount, paid_amount, "
                "currency, status) "
                "VALUES (:id, :tid, :oid, 'receivable', 'b2b_30_deposit', 2130.00, 0.00, 'USD', "
                "'open')"
            ),
            {"id": rp_id, "tid": tenant_id, "oid": order_id},
        )

    try:
        registry = ErpToolRegistry()
        guard = ToolGroundedNumericGuard()

        async with tenant_session(runtime_factory, tenant_id) as session:
            # 1. Live SKU pricing & available stock (1200 - 150 = 1050)
            sku_res = await registry.get_live_sku_pricing_and_stock(
                session, tenant_id, sku="XJ-400Pro-EU", qty=500, incoterm=Incoterm.FOB
            )
            assert sku_res.ok is True
            assert sku_res.tool_call_id.startswith("tc_")
            assert sku_res.data["applied_unit_price_usd"] == "14.20"
            assert sku_res.data["available_qty"] == 1050
            assert "cost_price_base" not in sku_res.data

            # 2. Live 17TRACK order tracking
            trk_res = await registry.get_live_order_tracking(
                session, tenant_id, order_no="ORD-2026-8801"
            )
            assert trk_res.ok is True
            assert trk_res.data["shipments"][0]["tracking_number"] == "LP123456789CN"
            assert trk_res.data["shipments"][0]["standardized_status"] == "InTransit"

            # 3. Quote matrix & margin floor detection
            ok_quote = await registry.calc_b2b_quote_matrix(
                session,
                tenant_id,
                items=[{"sku": "XJ-400Pro-EU", "qty": 500}],
                currency="USD",
                incoterm=Incoterm.FOB,
            )
            assert ok_quote.ok is True
            assert ok_quote.data["total_amount"] == "7100.00"
            assert ok_quote.data["below_margin_floor"] is False

            low_quote = await registry.calc_b2b_quote_matrix(
                session,
                tenant_id,
                items=[{"sku": "XJ-400Pro-EU", "qty": 500, "requested_unit_price": "10.00"}],
                currency="USD",
                incoterm=Incoterm.FOB,
            )
            assert low_quote.ok is True
            assert low_quote.data["below_margin_floor"] is True

            # 4. Open receivables match
            rec_res = await registry.match_open_receivables(
                session, tenant_id, amount=Decimal("2130.00"), currency="USD"
            )
            assert rec_res.ok is True
            assert len(rec_res.data["candidates"]) == 1
            assert rec_res.data["candidates"][0]["rp_id"] == str(rp_id)

            # 5. Read-only analytics SQL allows safe view and blocks DML/raw credential tables
            view_res = await registry.query_readonly_analytics_sql(
                session,
                tenant_id,
                "SELECT order_mode, SUM(order_count) AS orders_total FROM "
                "vw_tenant_order_profit_summary GROUP BY order_mode",
            )
            assert view_res.ok is True
            assert Decimal(view_res.data["rows"][0]["orders_total"]) == 1

            blocked_dml = await registry.query_readonly_analytics_sql(
                session,
                tenant_id,
                "DELETE FROM orders WHERE order_no = 'ORD-2026-8801'",
            )
            assert blocked_dml.ok is False
            assert blocked_dml.error_code == "ERR_UNSAFE_ANALYTICS_SQL"

            blocked_table = await registry.query_readonly_analytics_sql(
                session,
                tenant_id,
                "SELECT sku, cost_price_base FROM product_variants",
            )
            assert blocked_table.ok is False
            assert blocked_table.error_code == "ERR_DISALLOWED_ANALYTICS_RELATION"

            # 6. Numeric Guard allows grounded $14.20 + 1050 units, BLOCKS fabricated $12.99
            good_reply = (
                "For 500 units of XJ-400Pro-EU (FOB), the live unit price is $14.20 "
                "and we currently have 1,050 units in stock."
            )
            good_guard = guard.verify(good_reply, [sku_res])
            assert good_guard.passed is True
            assert good_guard.allow_l2_auto_send is True
            assert sku_res.tool_call_id in good_guard.matched_tool_call_ids

            hallucinated_reply = good_reply.replace("$14.20", "$12.99")
            bad_guard = guard.verify(hallucinated_reply, [sku_res])
            assert bad_guard.passed is False
            assert bad_guard.allow_l2_auto_send is False
            assert "12.99" in bad_guard.ungrounded_tokens
            assert not guard.verify(good_reply.replace("$14.20", "$500"), [sku_res]).passed
            assert not guard.verify(good_reply.replace("$14.20", "€14.20"), [sku_res]).passed
            assert not guard.verify(
                good_reply.replace("XJ-400Pro-EU", "NOT-THIS-SKU"), [sku_res]
            ).passed
            assert not guard.verify(good_reply + " Ships in nine days.", [sku_res]).passed
            assert not guard.verify(good_reply + " MOQ: 991.", [sku_res]).passed
            for claim in (
                {"source_tool_call_id": sku_res.tool_call_id, "price": "12.99"},
                {"source_tool_call_id": trk_res.tool_call_id, "stock": 1050},
                {"price": "14.20"},
            ):
                assert not guard.verify("", [sku_res, trk_res], [claim]).passed
            assert guard.verify(
                "",
                [sku_res],
                [
                    {
                        "source_tool_call_id": sku_res.tool_call_id,
                        "price": "14.20",
                        "sku": "XJ-400Pro-EU",
                        "stock": 1050,
                    }
                ],
            ).passed
            assert not guard.verify("The tracking number is AA000000000CN.", [trk_res]).passed
            assert not (
                await registry.get_live_sku_pricing_and_stock(
                    session,
                    tenant_id,
                    "XJ-400Pro-EU",
                    500,
                    Incoterm.DDP,
                )
            ).ok
            assert not (
                await registry.calc_b2b_quote_matrix(
                    session,
                    tenant_id,
                    [{"sku": "XJ-400Pro-EU", "qty": 500}],
                    "EUR",
                    Incoterm.FOB,
                )
            ).ok
            for sql in (
                "SELECT set_config('app.current_tenant_id', '', true) FROM "
                "vw_tenant_order_profit_summary",
                "SELECT * FROM vw_tenant_order_profit_summary, tenant_keys",
                "SELECT * FROM pg_catalog.vw_tenant_order_profit_summary",
            ):
                assert not (await registry.query_readonly_analytics_sql(session, tenant_id, sql)).ok

        async with tenant_session(runtime_factory, uuid4()) as other:
            assert not (
                await registry.get_live_order_tracking(
                    other,
                    tenant_id,
                    "ORD-2026-8801",
                )
            ).ok
            assert not (
                await registry.get_live_sku_pricing_and_stock(
                    other,
                    tenant_id,
                    "XJ-400Pro-EU",
                )
            ).ok
    finally:
        admin = create_async_engine(
            os.environ.get(
                "DATABASE_ADMIN_URL",
                "postgresql+asyncpg://postgres:postgres_dev_pass@127.0.0.1:5432/erp_saas",
            )
        )
        try:
            async with admin.begin() as conn:
                for table in (
                    "receivables_payables",
                    "order_profit_snapshots",
                    "shipments",
                    "orders",
                    "inventory_balances",
                    "product_variants",
                    "products",
                    "customers",
                ):
                    await conn.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id = :tid"), {"tid": tenant_id}
                    )
                await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        finally:
            await admin.dispose()
