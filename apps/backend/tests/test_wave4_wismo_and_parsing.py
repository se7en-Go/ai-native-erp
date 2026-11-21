"""Wave 4 AI guardrail regressions: WISMO buyer isolation + quantity/SKU parsing.

Two layers:

* Real PostgreSQL 17 (runtime role + forced RLS) coverage for the WISMO ownership
  boundary: a buyer may only ever read tracking for their own order, and an explicit
  order number they do not own must produce no evidence at all (no fallback to a
  different order of theirs).
* Pure unit coverage of the deterministic parsers (``_parse_quantity`` and
  ``_match_skus``) that feed those tools.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from erp_core.ai_engine.proposal_generator import AiProposalGeneratorService, _parse_quantity
from erp_core.ai_engine.tools import ErpToolRegistry
from erp_core.shared.config import get_settings
from erp_core.shared.db import create_runtime_engine, create_session_factory, tenant_session

ORDER_A_LATEST = "ORD-W4-A-2002"
ORDER_A_OLDER = "ORD-W4-A-1001"
ORDER_B_PRIVATE = "ORD-W4-B-9001"


@pytest.fixture
async def runtime_engine() -> AsyncEngine:
    engine = create_runtime_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_two_buyers(session: AsyncSession, tenant_id: UUID) -> dict[str, UUID]:
    customer_a = uuid4()
    customer_b = uuid4()
    await session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Wave4 LED Co', :slug)"),
        {"id": tenant_id, "slug": f"wave4-{tenant_id.hex[:8]}"},
    )
    for customer_id, name in ((customer_a, "Buyer A"), (customer_b, "Buyer B")):
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2b_contact', :name)"
            ),
            {"id": customer_id, "tid": tenant_id, "name": name},
        )
    orders: dict[str, UUID] = {}
    created_at = datetime.now(UTC)
    for index, (order_no, customer_id) in enumerate(
        (
            (ORDER_A_OLDER, customer_a),
            (ORDER_A_LATEST, customer_a),
            (ORDER_B_PRIVATE, customer_b),
        )
    ):
        order_id = uuid4()
        orders[order_no] = order_id
        await session.execute(
            text(
                "INSERT INTO orders "
                "(id, tenant_id, order_no, order_mode, customer_id, currency, fx_rate_to_base, "
                "subtotal_amount, total_amount, fulfillment_status, payment_status, created_at) "
                "VALUES (:id, :tid, :order_no, 'b2b_wholesale', :cid, 'USD', 7.200000, "
                "100.00, 100.00, 'shipped', 'paid', :created_at)"
            ),
            {
                "id": order_id,
                "tid": tenant_id,
                "order_no": order_no,
                "cid": customer_id,
                # Distinct, ordered timestamps so "latest order" is deterministic.
                "created_at": created_at - timedelta(days=10 - index),
            },
        )
        await session.execute(
            text(
                "INSERT INTO shipments "
                "(id, tenant_id, order_id, tracking_provider, tracking_number, carrier_code, "
                "standardized_status, sub_status, last_event_at, tracking_events) "
                "VALUES (:id, :tid, :oid, '17track', :tracking_no, 'dhl', 'InTransit', "
                "'InTransit_Customs', :ts, '[]'::jsonb)"
            ),
            {
                "id": uuid4(),
                "tid": tenant_id,
                "oid": order_id,
                "tracking_no": f"TRK-{order_no[-4:]}",
                "ts": created_at,
            },
        )
    return {"customer_a": customer_a, "customer_b": customer_b, **orders}


async def _cleanup_tenant(tenant_id: UUID) -> None:
    admin = create_async_engine(
        os.environ.get(
            "DATABASE_ADMIN_URL",
            "postgresql+asyncpg://postgres:postgres_dev_pass@127.0.0.1:5432/erp_saas",
        )
    )
    try:
        async with admin.begin() as conn:
            for table in (
                "shipments",
                "orders",
                "customers",
                "product_variants",
                "products",
            ):
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :tid"), {"tid": tenant_id}
                )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
    finally:
        await admin.dispose()


@pytest.mark.asyncio
async def test_wismo_never_discloses_another_buyers_order(runtime_engine: AsyncEngine) -> None:
    tenant_id = uuid4()
    runtime_factory = create_session_factory(runtime_engine)
    registry = ErpToolRegistry()
    generator = AiProposalGeneratorService(tool_registry=registry)

    async with tenant_session(runtime_factory, tenant_id) as setup:
        seeded = await _seed_two_buyers(setup, tenant_id)
    customer_a = seeded["customer_a"]
    customer_b = seeded["customer_b"]

    try:
        async with tenant_session(runtime_factory, tenant_id) as session:
            # Buyer A explicitly asks about Buyer B's order number.
            buyer_message = f"Hi, where is my order {ORDER_B_PRIVATE}? Please send tracking."
            matched = await generator._match_order_no(session, tenant_id, customer_a, buyer_message)
            # No match, and crucially NO fallback to Buyer A's own latest order.
            assert matched is None

            stolen = await registry.get_live_order_tracking(
                session, tenant_id, ORDER_B_PRIVATE, customer_id=customer_a
            )
            assert stolen.ok is False
            assert stolen.error_code == "ERR_ORDER_NOT_FOUND"
            assert stolen.data == {}

            # Buyer B is the owner: the very same lookup succeeds for them.
            owned = await registry.get_live_order_tracking(
                session, tenant_id, ORDER_B_PRIVATE, customer_id=customer_b
            )
            assert owned.ok is True
            assert owned.data["order_no"] == ORDER_B_PRIVATE
            assert owned.data["shipments"][0]["tracking_number"] == "TRK-9001"

            # Internal staff callers (no customer scope) keep full tenant visibility.
            staff = await registry.get_live_order_tracking(session, tenant_id, ORDER_B_PRIVATE)
            assert staff.ok is True
            assert staff.data["order_no"] == ORDER_B_PRIVATE

            # End-to-end: asking for someone else's order yields zero order evidence.
            claimed_evidence = await generator._collect_evidence(
                session, tenant_id, customer_a, f"Any update on {ORDER_B_PRIVATE}?"
            )
            assert [
                item for item in claimed_evidence if item["tool_name"] == "get_live_order_tracking"
            ] == []

            tracking_numbers = str(claimed_evidence)
            assert "TRK-9001" not in tracking_numbers
            assert ORDER_B_PRIVATE not in tracking_numbers

        async with tenant_session(runtime_factory, tenant_id) as session:
            # Buyer A asking about their own order reads cleanly.
            own_message = f"Could you track {ORDER_A_OLDER} for me?"
            matched_own = await generator._match_order_no(
                session, tenant_id, customer_a, own_message
            )
            assert matched_own == ORDER_A_OLDER

            own_tracking = await registry.get_live_order_tracking(
                session, tenant_id, ORDER_A_OLDER, customer_id=customer_a
            )
            assert own_tracking.ok is True
            assert own_tracking.data["order_no"] == ORDER_A_OLDER
            assert own_tracking.data["fulfillment_status"] == "shipped"
            assert own_tracking.data["shipments"][0]["tracking_number"] == "TRK-1001"

            own_evidence = await generator._collect_evidence(
                session, tenant_id, customer_a, own_message
            )
            assert any(
                item["tool_name"] == "get_live_order_tracking"
                and item["data"]["order_no"] == ORDER_A_OLDER
                for item in own_evidence
            )

            # No explicit order number: the customer's latest own order is used.
            implicit = await generator._match_order_no(
                session, tenant_id, customer_a, "Where is my order? Any tracking?"
            )
            assert implicit == ORDER_A_LATEST

            # A different, non-existent order number never falls back either.
            unknown = await generator._match_order_no(
                session, tenant_id, customer_a, "Track ORD-W4-Z-0000 please"
            )
            assert unknown is None
    finally:
        await _cleanup_tenant(tenant_id)


@pytest.mark.parametrize(
    ("buyer_text", "expected_qty"),
    [
        ("Do you have LED-COB-24V-5M in stock? Price for 1000 pcs?", 1000),
        ("5M strip, 200 rolls", 200),
        ("need led strip 5M 1000pcs", 1000),
        ("1.5 meters sample", 1),
        ("led-cob-24v-5m 300 pcs", 300),
        ("LED-COB-24V-5M 5000米", 5000),
        ("5M strip only, please advise", 1),
        ("200 pcs of LED-COB-24V-5M", 200),
        ("Need 300 rolls of LED-COB-24V-5M", 300),
        ("Quote for 2,500 pieces of SKU LED-COB-24V-10M please", 2500),
    ],
)
def test_parse_quantity_prefers_count_units_over_length_specs(
    buyer_text: str, expected_qty: int
) -> None:
    assert _parse_quantity(buyer_text) == expected_qty


def test_parse_quantity_never_reads_the_fraction_of_a_decimal_length() -> None:
    # `1.5 meters` must not silently become 5 units of product.
    assert _parse_quantity("1.5 meters sample") == 1
    assert _parse_quantity("2.75 meters of strip") == 1
    # A whole-number length still parses when no count unit is present.
    assert _parse_quantity("I need 12 meters for a sample") == 12


@pytest.mark.asyncio
async def test_match_skus_returns_nothing_without_a_catalog_term() -> None:
    generator = AiProposalGeneratorService(tool_registry=ErpToolRegistry())
    # The early return must happen before any SQL is issued, so a stub session is safe.
    stub_session = cast(AsyncSession, object())
    tenant_id = uuid4()

    for message in (
        "Where is my order? Please send the tracking number.",
        "Hello, any update on my delivery?",
        "Thanks, talk soon.",
        "",
        "我的订单发货了吗？",
    ):
        assert await generator._match_skus(stub_session, tenant_id, message) == []

    # Sanity check: real catalog terms are still extracted (no DB needed).
    terms: list[str] = generator._search_terms("Price for led strip LED-COB-24V-5M in 500 pcs?")
    assert "LED-COB-24V-5M" in terms
    assert "led" in terms


@pytest.mark.asyncio
async def test_match_skus_prefers_exact_sku_over_partial_matches(
    runtime_engine: AsyncEngine,
) -> None:
    tenant_id = uuid4()
    runtime_factory = create_session_factory(runtime_engine)
    generator = AiProposalGeneratorService(tool_registry=ErpToolRegistry())
    product_id = uuid4()

    async with tenant_session(runtime_factory, tenant_id) as setup:
        await setup.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Wave4 SKU Co', :slug)"),
            {"id": tenant_id, "slug": f"wave4sku-{tenant_id.hex[:8]}"},
        )
        await setup.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:id, :tid, 'SPU-COB', 'LED Strip')"
            ),
            {"id": product_id, "tid": tenant_id},
        )
        for sku in ("COB-24V-5M-EXTRA-LONG", "LED-COB-24V-5M"):
            await setup.execute(
                text(
                    "INSERT INTO product_variants "
                    "(id, tenant_id, product_id, sku, cost_price_base, base_currency, "
                    "retail_price_usd, weight_grams, lead_time_days) "
                    "VALUES (:id, :tid, :pid, :sku, 5.0000, 'CNY', 9.00, 200, 7)"
                ),
                {"id": uuid4(), "tid": tenant_id, "pid": product_id, "sku": sku},
            )

    try:
        async with tenant_session(runtime_factory, tenant_id) as session:
            matched = await generator._match_skus(
                session, tenant_id, "Price for led-cob-24v-5m in 500 pcs?"
            )
            # Lowercase buyer input resolves to the exact catalog SKU first.
            assert matched[0] == "LED-COB-24V-5M"
            assert (
                await generator._match_skus(session, tenant_id, "Where is my order? Any tracking?")
                == []
            )
    finally:
        await _cleanup_tenant(tenant_id)
