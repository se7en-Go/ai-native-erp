"""Recorded Shopify + WooCommerce contracts, replayed against the real PG17 runtime.

The four fixtures under `tests/fixtures/ecommerce_contracts/` are sanitized recordings:

* `shopify_orders_paid.json` / `shopify_refunds_create.json` — the raw `orders/paid` and
  `refunds/create` webhook bodies *plus* the exact `X-Shopify-Hmac-Sha256` header that was
  recorded with them, and the GraphQL order node the polling sync normalizes.
* `woo_order_completed.json` / `woo_order_refunded.json` — the WooCommerce REST v3 webhook
  deliveries with their `X-WC-Webhook-Signature`.

Nothing here re-signs anything at test time: the assertions compare the *recorded* header
against an HMAC computed from the recorded raw body, so a fixture that drifts from its
signature fails the suite instead of silently re-signing itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.crm.portal_models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.orders.routing_models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.channels.ecommerce.base import EcommerceOrderSyncService
from erp_core.channels.ecommerce.shopify import (
    ShopifyGraphqlAdapter,
    verify_shopify_webhook_hmac,
)
from erp_core.channels.ecommerce.woocommerce import (
    WooCommerceAdapter,
    verify_woocommerce_webhook,
)
from erp_core.channels.webhook_store import (
    accept_raw_webhook_event,
    set_raw_webhook_processing_state,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus

Factory = async_sessionmaker[AsyncSession]

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ecommerce_contracts"
SHOPIFY_PAID = "shopify_orders_paid.json"
SHOPIFY_REFUND = "shopify_refunds_create.json"
WOO_COMPLETED = "woo_order_completed.json"
WOO_REFUNDED = "woo_order_refunded.json"

#: The SKU every recorded line item carries; the seeded variant matches it verbatim so
#: `CatalogService.resolve_sku_for_channel` resolves through its direct-SKU fallback.
RECORDED_SKU = "LED-STRIP-5M-IP65"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _body(fixture: dict[str, Any]) -> bytes:
    return str(fixture["body_raw"]).encode("utf-8")


def _walk_values(payload: Any) -> Iterator[Any]:
    if isinstance(payload, dict):
        for value in payload.values():
            yield from _walk_values(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from _walk_values(item)
    else:
        yield payload


# ---------------------------------------------------------------------------
# Signature verification (no database)
# ---------------------------------------------------------------------------


def test_shopify_recorded_webhooks_verify_their_recorded_hmac() -> None:
    for name in (SHOPIFY_PAID, SHOPIFY_REFUND):
        fixture = _load(name)
        raw = _body(fixture)
        secret = fixture["signed_with_test_secret"]
        header = fixture["headers"]["x-shopify-hmac-sha256"]
        assert verify_shopify_webhook_hmac(raw, header, secret), name
        # A single appended byte (a proxy that re-serializes JSON) must not verify.
        assert not verify_shopify_webhook_hmac(raw + b"\n", header, secret), name
        # Nor may a wrong secret, an empty secret or a missing header.
        assert not verify_shopify_webhook_hmac(raw, header, "wrong-secret"), name
        assert not verify_shopify_webhook_hmac(raw, header, ""), name
        assert not verify_shopify_webhook_hmac(raw, "", secret), name
        assert fixture["headers"]["x-shopify-topic"] in {"orders/paid", "refunds/create"}
        assert fixture["headers"]["x-shopify-shop-domain"].endswith(".myshopify.com")


def test_woocommerce_recorded_webhooks_verify_their_recorded_signature() -> None:
    for name in (WOO_COMPLETED, WOO_REFUNDED):
        fixture = _load(name)
        raw = _body(fixture)
        secret = fixture["signed_with_test_secret"]
        headers = fixture["headers"]
        assert verify_woocommerce_webhook(raw, headers, secret), name
        assert not verify_woocommerce_webhook(raw + b" ", headers, secret), name
        assert not verify_woocommerce_webhook(raw, headers, "wrong-secret"), name
        assert not verify_woocommerce_webhook(
            raw, {"x-wc-webhook-signature": ""}, secret
        ), name
        assert headers["x-wc-webhook-topic"] in {"order.completed", "order.refunded"}


def test_recorded_rest_bodies_parse_to_decimal_money_without_float() -> None:
    """Every money field in the recorded REST bodies is an exact decimal string."""
    shopify_paid = json.loads(_load(SHOPIFY_PAID)["body_raw"])
    assert Decimal(str(shopify_paid["total_price"])) == Decimal("1338.50")
    assert Decimal(str(shopify_paid["current_total_price_set"]["shop_money"]["amount"])) == (
        Decimal("1338.50")
    )
    shopify_refund = json.loads(_load(SHOPIFY_REFUND)["body_raw"])
    refund_total = sum(
        Decimal(str(item["transactions"][0]["amount"])) for item in [shopify_refund]
    )
    assert refund_total == Decimal("1338.50")
    assert shopify_refund["restock"] is True

    woo_completed = json.loads(_load(WOO_COMPLETED)["body_raw"])
    assert Decimal(str(woo_completed["total"])) == Decimal("768.00")
    assert Decimal(str(woo_completed["line_items"][0]["total"])) == Decimal("750.00")
    # WooCommerce serializes numeric fields as JSON numbers; the adapter must never let
    # that reach a NUMERIC column as an IEEE-754 float (asserted on the DTO below).
    assert isinstance(woo_completed["line_items"][0]["price"], float)


# ---------------------------------------------------------------------------
# Normalization (no database)
# ---------------------------------------------------------------------------


def test_shopify_recorded_nodes_normalize_to_exact_decimal_money() -> None:
    paid = _load(SHOPIFY_PAID)
    order = ShopifyGraphqlAdapter._normalize_order_node(
        json.loads(paid["graphql_order_node_raw"])
    )
    assert order.external_order_id == "gid://shopify/Order/5501234567890"
    assert order.external_order_number == "#1024"
    assert order.order_mode is OrderMode.B2B_WHOLESALE
    assert order.payment_status is PaymentStatus.PAID
    assert order.fulfillment_status is FulfillmentStatus.CONFIRMED_RESERVED
    assert order.currency == "EUR"
    assert order.subtotal_amount == Decimal("1249.50")
    assert order.shipping_charged == Decimal("89.00")
    assert order.tax_collected == Decimal("0.00")
    assert order.total_amount == Decimal("1338.50")
    assert order.b2b_company is not None
    assert order.b2b_company.legal_name == "Nordlicht Lichttechnik GmbH"
    assert order.channel_metadata["po_number"] == "PO-DE-2026-77"
    assert order.channel_metadata["financial_status"] == "PAID"
    assert order.line_items[0].quantity == 2
    assert order.line_items[0].line_total == Decimal("1249.50")

    refunded = ShopifyGraphqlAdapter._normalize_order_node(
        json.loads(_load(SHOPIFY_REFUND)["graphql_order_node_raw"])
    )
    assert refunded.external_order_id == order.external_order_id
    assert refunded.payment_status is PaymentStatus.REFUNDED
    assert refunded.fulfillment_status is FulfillmentStatus.REFUNDED
    assert refunded.channel_metadata["refunded_amount"] == "1338.50"

    # No money field may degrade into a float anywhere in the normalized DTO.
    for value in _walk_values(order.model_dump(mode="python")):
        assert not isinstance(value, float), value
    assert isinstance(order.total_amount, Decimal)


def test_woocommerce_recorded_orders_normalize_money_and_b2b_company() -> None:
    adapter = WooCommerceAdapter(
        store_url="https://qz-lighting-sandbox.example.com",
        consumer_key="ck_recorded",
        consumer_secret="cs_recorded",
        meta_field_map={
            "po_number": "_wholesale_po_number",
            "tracking_number": "_tracking_number",
        },
    )
    completed = adapter._normalize_order(json.loads(_load(WOO_COMPLETED)["body_raw"]))
    assert completed.external_order_id == "9101"
    assert completed.external_order_number == "9101"
    assert completed.order_mode is OrderMode.B2B_WHOLESALE
    assert completed.payment_status is PaymentStatus.PAID
    assert completed.fulfillment_status is FulfillmentStatus.DELIVERED
    assert completed.total_amount == Decimal("768.00")
    assert completed.shipping_charged == Decimal("18.00")
    # subtotal = total - shipping - tax + discount, entirely in Decimal.
    assert completed.subtotal_amount == Decimal("750.00")
    assert completed.line_items[0].line_total == Decimal("750.00")
    assert completed.b2b_company is not None
    assert completed.b2b_company.legal_name == "Nordlicht Lichttechnik GmbH"
    assert completed.channel_metadata["wc_status"] == "completed"
    assert completed.channel_metadata["tracking_number"] == "DHL-DE-4455"

    refunded = adapter._normalize_order(json.loads(_load(WOO_REFUNDED)["body_raw"]))
    assert refunded.external_order_id == completed.external_order_id
    assert refunded.payment_status is PaymentStatus.REFUNDED
    assert refunded.fulfillment_status is FulfillmentStatus.REFUNDED
    assert refunded.channel_metadata["refunded_amount"] == "768.00"

    for value in _walk_values(completed.model_dump(mode="python")):
        assert not isinstance(value, float), value


# ---------------------------------------------------------------------------
# Replay + ingestion against the real PG17 runtime
# ---------------------------------------------------------------------------


async def test_recorded_webhook_replay_is_idempotent_in_raw_webhook_events(
    runtime_session_factory: Factory,
    migrator_session_factory: Factory,
) -> None:
    tenant_id = uuid4()
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, 'Recorded Contracts Co', 'USD')"
            ),
            {"id": tenant_id, "slug": f"recorded-{tenant_id.hex[:8]}"},
        )

    for name, channel in (
        (SHOPIFY_PAID, "shopify"),
        (WOO_COMPLETED, "woocommerce"),
    ):
        fixture = _load(name)
        raw = _body(fixture)
        # The production ingress keys idempotency on sha256(raw_body), which is global per
        # channel — so a previous run of this suite already owns that key. Clear it first.
        event_id = hashlib.sha256(raw).hexdigest()
        async with tenant_session(migrator_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "DELETE FROM raw_webhook_events "
                    "WHERE channel_type = :ctype AND external_event_id = :eid"
                ),
                {"ctype": channel, "eid": event_id},
            )
        # `accept_raw_webhook_event` is a SECURITY DEFINER function granted to the broker
        # (app_migrator) session only, exactly like the production ingress path.
        async with tenant_session(migrator_session_factory, tenant_id) as session:
            first = await accept_raw_webhook_event(
                session,
                event_id=uuid4(),
                tenant_id=tenant_id,
                channel_type=channel,
                external_event_id=event_id,
                external_account_id="recorded-account",
                headers_json=json.dumps(fixture["headers"]),
                payload_json=fixture["body_raw"],
            )
            assert first is not None, f"{name}: first delivery must be persisted"
            # A duplicate while the row is still `received` resolves to the same id: the
            # caller may re-enqueue, but ON CONFLICT DO NOTHING inserts nothing.
            replay = await accept_raw_webhook_event(
                session,
                event_id=uuid4(),
                tenant_id=tenant_id,
                channel_type=channel,
                external_event_id=event_id,
                external_account_id="recorded-account",
                headers_json=json.dumps(fixture["headers"]),
                payload_json=fixture["body_raw"],
            )
            assert replay == first, f"{name}: the duplicate must map to the stored row"
            await set_raw_webhook_processing_state(
                session,
                raw_event_id=first,
                status="processed",
                tenant_id=tenant_id,
                attempt=1,
            )
            settled_replay = await accept_raw_webhook_event(
                session,
                event_id=uuid4(),
                tenant_id=tenant_id,
                channel_type=channel,
                external_event_id=event_id,
                external_account_id="recorded-account",
                headers_json=json.dumps(fixture["headers"]),
                payload_json=fixture["body_raw"],
            )
            assert settled_replay is None, (
                f"{name}: once processed, the identical delivery is a no-op"
            )

        async with tenant_session(migrator_session_factory, tenant_id) as session:
            stored = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM raw_webhook_events "
                    "WHERE tenant_id = :tid AND channel_type = :ctype "
                    "AND external_event_id = :eid"
                ),
                {"tid": tenant_id, "ctype": channel, "eid": event_id},
            )
            row = (
                await session.execute(
                    text(
                        "SELECT signature_verified, raw_payload ->> 'id' "
                        "FROM raw_webhook_events WHERE tenant_id = :tid "
                        "AND external_event_id = :eid"
                    ),
                    {"tid": tenant_id, "eid": event_id},
                )
            ).one()
    assert int(stored or 0) == 1, "ON CONFLICT DO NOTHING keeps exactly one raw event"
    assert row[0] is True, "the recorded signature is verified before persisting"
    assert row[1]


async def _seed_storefront_tenant(
    factory: Factory, *, channel_type: str
) -> dict[str, UUID]:
    tenant_id, account_id, product_id, variant_id, warehouse_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, 'Recorded Contracts Co', 'USD')"
            ),
            {"id": tenant_id, "slug": f"recorded-{tenant_id.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO warehouses (id, tenant_id, code, name, country_code, city) "
                "VALUES (:id, :tid, 'REC-01', 'Quanzhou Main', 'CN', 'Quanzhou')"
            ),
            {"id": warehouse_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                " external_account_id, display_name) "
                "VALUES (:id, :tid, :ctype, :ext, 'Recorded storefront')"
            ),
            {
                "id": account_id,
                "tid": tenant_id,
                "ctype": channel_type,
                "ext": f"{channel_type}-{tenant_id.hex[:8]}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:id, :tid, 'SPU-RECORDED', 'led_lighting')"
            ),
            {"id": product_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO product_variants (id, tenant_id, product_id, sku, "
                " cost_price_base, base_currency, retail_price_usd, weight_grams, "
                " lead_time_days) "
                "VALUES (:id, :tid, :pid, :sku, 300.0000, 'USD', 375.00, 900, 7)"
            ),
            {"id": variant_id, "tid": tenant_id, "pid": product_id, "sku": RECORDED_SKU},
        )
        await session.execute(
            text(
                "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
                " on_hand_qty, reserved_qty) VALUES (:tid, :wid, :vid, 100, 0)"
            ),
            {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
        )
    return {
        "tenant_id": tenant_id,
        "channel_account_id": account_id,
        "variant_id": variant_id,
        "warehouse_id": warehouse_id,
    }


async def _reserved_qty(factory: Factory, tenant_id: UUID, variant_id: UUID) -> int:
    async with tenant_session(factory, tenant_id) as session:
        value = await session.scalar(
            text(
                "SELECT reserved_qty FROM inventory_balances "
                "WHERE tenant_id = :tid AND variant_id = :vid"
            ),
            {"tid": tenant_id, "vid": variant_id},
        )
    return int(value or 0)


async def test_shopify_recorded_refund_releases_the_stock_reservation(
    runtime_session_factory: Factory,
) -> None:
    seeded = await _seed_storefront_tenant(runtime_session_factory, channel_type="shopify")
    tenant_id = seeded["tenant_id"]
    paid_node = ShopifyGraphqlAdapter._normalize_order_node(
        json.loads(_load(SHOPIFY_PAID)["graphql_order_node_raw"])
    )
    refunded_node = ShopifyGraphqlAdapter._normalize_order_node(
        json.loads(_load(SHOPIFY_REFUND)["graphql_order_node_raw"])
    )
    sync = EcommerceOrderSyncService()

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        order_id, created = await sync.ingest_normalized_order(
            session,
            tenant_id=tenant_id,
            channel_account_id=seeded["channel_account_id"],
            default_warehouse_id=seeded["warehouse_id"],
            order_dto=paid_node,
        )
    assert created is True
    assert await _reserved_qty(runtime_session_factory, tenant_id, seeded["variant_id"]) == 2

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        replayed_id, created_again = await sync.ingest_normalized_order(
            session,
            tenant_id=tenant_id,
            channel_account_id=seeded["channel_account_id"],
            default_warehouse_id=seeded["warehouse_id"],
            order_dto=paid_node,
        )
    assert (replayed_id, created_again) == (order_id, False)
    assert await _reserved_qty(runtime_session_factory, tenant_id, seeded["variant_id"]) == 2

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        refunded_id, refund_created = await sync.ingest_normalized_order(
            session,
            tenant_id=tenant_id,
            channel_account_id=seeded["channel_account_id"],
            default_warehouse_id=seeded["warehouse_id"],
            order_dto=refunded_node,
        )
    assert (refunded_id, refund_created) == (order_id, False)
    assert await _reserved_qty(runtime_session_factory, tenant_id, seeded["variant_id"]) == 0

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT fulfillment_status, payment_status, channel_metadata "
                    "FROM orders WHERE tenant_id = :tid AND id = :oid"
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).one()
        ledger = (
            await session.execute(
                text(
                    "SELECT event_type, qty_delta_reserved FROM inventory_ledger "
                    "WHERE tenant_id = :tid AND reference_id = :oid "
                    "ORDER BY id"
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).all()
        receivable = (
            await session.execute(
                text(
                    "SELECT status FROM receivables_payables "
                    "WHERE tenant_id = :tid AND order_id = :oid"
                ),
                {"tid": tenant_id, "oid": order_id},
            )
        ).one()
    assert row[0] == "refunded"
    assert row[1] == "refunded"
    assert Decimal(str(row[2]["refunded_amount"])) == Decimal("1338.50")
    event_types = [item[0] for item in ledger]
    assert "reserve_order" in event_types
    assert "release_cancel" in event_types, "a refunded order must hand its stock back"
    assert int(ledger[-1][1]) == -2
    assert receivable[0] == "void"

    # Replaying the refund delivery changes nothing (idempotent release).
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await sync.ingest_normalized_order(
            session,
            tenant_id=tenant_id,
            channel_account_id=seeded["channel_account_id"],
            default_warehouse_id=seeded["warehouse_id"],
            order_dto=refunded_node,
        )
    assert await _reserved_qty(runtime_session_factory, tenant_id, seeded["variant_id"]) == 0


async def test_woocommerce_recorded_duplicate_delivery_creates_zero_double_order(
    runtime_session_factory: Factory,
) -> None:
    seeded = await _seed_storefront_tenant(
        runtime_session_factory, channel_type="woocommerce"
    )
    tenant_id = seeded["tenant_id"]
    adapter = WooCommerceAdapter(
        store_url="https://qz-lighting-sandbox.example.com",
        consumer_key="ck_recorded",
        consumer_secret="cs_recorded",
        meta_field_map={
            "po_number": "_wholesale_po_number",
            "tracking_number": "_tracking_number",
        },
    )
    completed = adapter._normalize_order(json.loads(_load(WOO_COMPLETED)["body_raw"]))
    sync = EcommerceOrderSyncService()

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        order_id, created = await sync.ingest_normalized_order(
            session,
            tenant_id=tenant_id,
            channel_account_id=seeded["channel_account_id"],
            default_warehouse_id=seeded["warehouse_id"],
            order_dto=completed,
        )
    assert created is True

    # The identical signed delivery arrives three times (Woo retries on 5xx).
    for _ in range(3):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            again_id, again_created = await sync.ingest_normalized_order(
                session,
                tenant_id=tenant_id,
                channel_account_id=seeded["channel_account_id"],
                default_warehouse_id=seeded["warehouse_id"],
                order_dto=completed,
            )
        assert (again_id, again_created) == (order_id, False)

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        orders = (
            await session.execute(
                text(
                    "SELECT order_no, order_mode, currency, total_amount, subtotal_amount, "
                    " shipping_charged, payment_status, fulfillment_status, channel_metadata "
                    "FROM orders WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            )
        ).all()
        items = (
            await session.execute(
                text(
                    "SELECT sku_snapshot, quantity, line_total FROM order_items "
                    "WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            )
        ).all()
    assert len(orders) == 1, "a replayed webhook must never create a second order"
    assert len(items) == 1
    row = orders[0]
    assert row[1] == "b2b_wholesale"
    assert row[2] == "USD"
    assert Decimal(str(row[3])) == Decimal("768.00")
    assert Decimal(str(row[4])) == Decimal("750.00")
    assert Decimal(str(row[5])) == Decimal("18.00")
    assert row[6] == "paid"
    assert row[7] == "delivered"
    assert row[8]["wc_status"] == "completed"
    assert row[8]["fx_rate_order_to_base"] == "1.000000"
    assert items[0][0] == RECORDED_SKU
    assert int(items[0][1]) == 2
    assert Decimal(str(items[0][2])) == Decimal("750.00")


@pytest.mark.parametrize(
    "fixture_name",
    [SHOPIFY_PAID, SHOPIFY_REFUND, WOO_COMPLETED, WOO_REFUNDED],
)
def test_recorded_fixture_headers_are_fully_sanitized(fixture_name: str) -> None:
    """No live token, cookie or password may ride along in a recorded header set."""
    fixture = _load(fixture_name)
    forbidden = {"authorization", "cookie", "x-shopify-access-token", "x-wc-webhook-secret"}
    lowered = {key.lower() for key in fixture["headers"]}
    assert lowered & forbidden == set()
    blob = json.dumps(fixture)
    for marker in ("ck_live_", "cs_live_", "shpat_", "access_token="):
        assert marker not in blob
    assert isinstance(fixture["body_raw"], str)
    assert datetime.fromisoformat(
        _load(SHOPIFY_PAID)["headers"]["x-shopify-triggered-at"].replace("Z", "+00:00")
    )
