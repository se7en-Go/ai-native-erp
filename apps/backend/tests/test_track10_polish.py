"""Track 10: B2B portal reorder, batch 4x6 labels, scan-to-ship and 2 new automations.

Every test runs against the real PostgreSQL 17 cluster (`erp_test_t10`) as the
non-superuser `app_runtime` role with FORCE RLS, so the assertions cover the actual
`scan_dispatch_events` policy, the widened `automation_rules` CHECK vocabulary and the
`b2b_portal_tokens` magic-link resolution — never SQLite and never an in-memory mock.

The three surfaces covered here:

1. `POST /api/v1/portal/{token}/reorder` — magic-link-only reorder that re-prices live
   `product_variants` into a brand new **draft** quotation with zero cost/margin leakage.
2. `POST /api/v1/orders/shipping-labels/batch` + `/batch-pdf` + `/scan-dispatch` — a
   warehouse print run that skips held/unpaid orders instead of failing, one merged 4x6
   PDF, and a barcode scan that either releases the parcel or leaves 409 evidence.
3. `negative_review_ticket` / `high_adoption_faq_extract` — two new automation templates
   with dry-run parity and per-entity de-duplication.
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_auth_rbac import _register_owner

import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.crm.portal_models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.orders.routing_models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.ai_engine.automation_service import (
    DEFAULT_NEGATIVE_REVIEW_KEYWORDS,
    DEFAULT_RULE_TEMPLATES,
    FAQ_PROMOTION_URI_PREFIX,
    AutomationService,
)
from erp_core.crm.portal_service import PORTAL_TOKEN_PREFIX, hash_portal_token
from erp_core.orders.carrier_label_service import (
    THERMAL_LABEL_HEIGHT_PT,
    THERMAL_LABEL_WIDTH_PT,
)
from erp_core.shared.db import tenant_session

Factory = async_sessionmaker[AsyncSession]

PI_TEMPLATE: dict[str, str] = {
    "company_legal_name": "Quanzhou Smart LED Lighting Co., Ltd.",
    "company_address": "88 Export Avenue, Quanzhou, Fujian, China",
    "contact_phone": "+86 595 8888 0000",
    "contact_email": "export@qzled.example.com",
    "website": "https://qzled.example.com",
    "tax_or_reg_no": "91350500MA2XXXXX1A",
    "bank_beneficiary_name": "Quanzhou Smart LED Lighting Co., Ltd.",
    "bank_name": "Bank of China, Quanzhou Branch",
    "bank_account_no": "4060 1234 5678 9012 345",
    "bank_swift_code": "BKCHCNBJ73A",
    "bank_address": "Quanzhou, Fujian, China",
    "default_terms_notes": "30% T/T deposit, 70% balance before shipment.",
}


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _owner(async_client: AsyncClient) -> tuple[dict[str, str], UUID, UUID]:
    """Register a fresh tenant + owner and return `(headers, tenant_id, user_id)`."""
    token, _refresh, tenant_id_str = await _register_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}
    me = await async_client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, UUID(tenant_id_str), UUID(me.json()["user_id"])


async def _seed_catalog(factory: Factory, tenant_id: UUID) -> dict[str, Any]:
    """Customer + product + variant + warehouse, ready for `POST /api/v1/orders`."""
    ids: dict[str, Any] = {
        "customer_id": uuid4(),
        "product_id": uuid4(),
        "variant_id": uuid4(),
        "warehouse_id": uuid4(),
    }
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO warehouses (id, tenant_id, code, name, country_code, city) "
                "VALUES (:wid, :tid, :code, 'Quanzhou Main', 'CN', 'Quanzhou')"
            ),
            {"wid": ids["warehouse_id"], "tid": tenant_id, "code": f"WH-{uuid4().hex[:6]}"},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, display_name, primary_email, "
                "country_code, customer_type) "
                "VALUES (:cid, :tid, 'Berlin Lighting GmbH', :email, 'DE', 'b2b_company_rep')"
            ),
            {
                "cid": ids["customer_id"],
                "tid": tenant_id,
                "email": f"buyer-{uuid4().hex[:6]}@berlin-lighting.example",
            },
        )
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:pid, :tid, :spu, 'led_lighting')"
            ),
            {"pid": ids["product_id"], "tid": tenant_id, "spu": f"SPU-{uuid4().hex[:6]}"},
        )
        await session.execute(
            text(
                "INSERT INTO product_variants ("
                " id, tenant_id, product_id, sku, cost_price_base, base_currency, "
                " retail_price_usd, b2b_moq_tiers, weight_grams, lead_time_days) "
                "VALUES (:vid, :tid, :pid, :sku, 40.0000, 'CNY', 18.50, "
                " CAST(:tiers AS jsonb), 900, 7)"
            ),
            {
                "vid": ids["variant_id"],
                "tid": tenant_id,
                "pid": ids["product_id"],
                "sku": f"LED-STRIP-{uuid4().hex[:6].upper()}",
                "tiers": json.dumps(
                    [
                        {"min_qty": 50, "unit_price_usd": "12.0000"},
                        {"min_qty": 500, "unit_price_usd": "8.5000"},
                    ]
                ),
            },
        )
        await session.execute(
            text(
                "INSERT INTO inventory_balances "
                "(tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty) "
                "VALUES (:tid, :wid, :vid, 500, 0)"
            ),
            {"tid": tenant_id, "wid": ids["warehouse_id"], "vid": ids["variant_id"]},
        )
    return ids


async def _create_order(
    async_client: AsyncClient,
    headers: dict[str, str],
    seed: dict[str, Any],
    *,
    order_mode: str = "b2c_retail",
    quantity: int = 2,
) -> dict[str, Any]:
    response = await async_client.post(
        "/api/v1/orders",
        headers=headers,
        json={
            "order_mode": order_mode,
            "customer_id": str(seed["customer_id"]),
            "warehouse_id": str(seed["warehouse_id"]),
            "currency": "USD",
            "items": [
                {
                    "variant_id": str(seed["variant_id"]),
                    "quantity": quantity,
                    "unit_price": "18.50",
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_profit_snapshot(
    factory: Factory,
    tenant_id: UUID,
    order_id: UUID,
    *,
    gross_revenue_base: str,
    net_contribution_margin_base: str,
) -> None:
    gross = Decimal(gross_revenue_base)
    net = Decimal(net_contribution_margin_base)
    assert net <= gross, "net margin is generated as gross - cogs"
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO order_profit_snapshots ("
                " order_id, tenant_id, base_currency, gross_revenue_base, cogs_base,"
                " platform_commission_base, payment_gateway_fee_base,"
                " shipping_and_duty_base, channel_msg_cost_base, ad_attribution_cost_base,"
                " fx_gain_loss_base"
                ") VALUES ("
                " :oid, :tid, 'USD', :gross, :cogs, 0, 0, 0, 0, 0, 0)"
            ),
            {"oid": order_id, "tid": tenant_id, "gross": gross, "cogs": gross - net},
        )


def _pages_text(pdf_bytes: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [page.extract_text() or "" for page in reader.pages]


async def _scan_events(factory: Factory, tenant_id: UUID) -> list[dict[str, Any]]:
    async with tenant_session(factory, tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT order_id, scanned_code, tracking_number, outcome, "
                    "carrier_shipping_label_id FROM scan_dispatch_events "
                    "WHERE tenant_id = :tid ORDER BY dispatched_at, id"
                ),
                {"tid": tenant_id},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Batch waybills: skip matrix + reuse
# ---------------------------------------------------------------------------


async def test_batch_label_booking_skips_held_and_unpaid_orders_and_reuses_labels(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    headers, tenant_id, _user_id = await _owner(async_client)
    seed = await _seed_catalog(runtime_session_factory, tenant_id)
    shippable = await _create_order(async_client, headers, seed)
    unpaid_balance = await _create_order(
        async_client, headers, seed, order_mode="b2b_wholesale"
    )
    held = await _create_order(async_client, headers, seed)
    await _insert_profit_snapshot(
        runtime_session_factory,
        tenant_id,
        UUID(held["id"]),
        gross_revenue_base="100.00",
        net_contribution_margin_base="-50.00",
    )
    evaluated = await async_client.post(
        f"/api/v1/orders/{held['id']}/evaluate-routing", headers=headers
    )
    assert evaluated.status_code == 201, evaluated.text
    assert evaluated.json()["is_held"] is True

    response = await async_client.post(
        "/api/v1/orders/shipping-labels/batch",
        headers=headers,
        json={
            "order_ids": [shippable["id"], unpaid_balance["id"], held["id"]],
            "carrier_code": "sandbox_carrier",
            "mode": "sandbox",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["booked"]) == 1
    assert body["booked"][0]["order_id"] == shippable["id"]
    assert body["booked"][0]["reused"] is False
    assert body["booked"][0]["tracking_number"].startswith("SBX")
    assert Decimal(body["booked"][0]["shipping_cost_estimate"]) > Decimal("0")
    skipped = {row["order_id"]: row["reason_code"] for row in body["skipped"]}
    assert skipped == {
        unpaid_balance["id"]: "balance_unpaid",
        held["id"]: "held_for_review",
    }
    assert all(row["reason"] for row in body["skipped"])

    # Second run reuses the already-booked waybill instead of buying another one.
    again = await async_client.post(
        "/api/v1/orders/shipping-labels/batch",
        headers=headers,
        json={
            "order_ids": [shippable["id"]],
            "carrier_code": "sandbox_carrier",
            "mode": "sandbox",
        },
    )
    assert again.status_code == 201, again.text
    reused = again.json()["booked"][0]
    assert reused["reused"] is True
    assert reused["label_id"] == body["booked"][0]["label_id"]

    missing = await async_client.post(
        "/api/v1/orders/shipping-labels/batch",
        headers=headers,
        json={
            "order_ids": [str(uuid4())],
            "carrier_code": "sandbox_carrier",
            "mode": "sandbox",
        },
    )
    assert missing.status_code == 201, missing.text
    assert missing.json()["booked"] == []
    assert missing.json()["skipped"][0]["reason_code"] == "order_not_found"

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        label_count = await session.scalar(
            text(
                "SELECT COUNT(*) FROM carrier_shipping_labels WHERE tenant_id = :tid "
                "AND order_id = :oid"
            ),
            {"tid": tenant_id, "oid": UUID(shippable["id"])},
        )
    assert int(label_count or 0) == 1


async def test_batch_label_pdf_merges_4x6_pages_in_request_order(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    headers, tenant_id, _user_id = await _owner(async_client)
    seed = await _seed_catalog(runtime_session_factory, tenant_id)
    first = await _create_order(async_client, headers, seed)
    second = await _create_order(async_client, headers, seed)

    labels: list[dict[str, Any]] = []
    for order in (first, second):
        booked = await async_client.post(
            f"/api/v1/orders/{order['id']}/shipping-label",
            headers=headers,
            json={"carrier_code": "sandbox_carrier", "mode": "sandbox"},
        )
        assert booked.status_code == 201, booked.text
        labels.append(booked.json())

    # Request the labels in reverse order to prove page order follows the request.
    response = await async_client.post(
        "/api/v1/orders/shipping-labels/batch-pdf",
        headers=headers,
        json={"label_ids": [labels[1]["id"], labels[0]["id"]]},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")

    reader = PdfReader(io.BytesIO(response.content))
    assert len(reader.pages) == 2
    for page in reader.pages:
        assert round(float(page.mediabox.width)) == THERMAL_LABEL_WIDTH_PT
        assert round(float(page.mediabox.height)) == THERMAL_LABEL_HEIGHT_PT
    texts = _pages_text(response.content)
    assert labels[1]["tracking_number"] in texts[0]
    assert labels[0]["tracking_number"] in texts[1]
    assert second["order_no"] in texts[0]
    assert first["order_no"] in texts[1]

    # `order_ids` selects each order's newest label, no ids required.
    by_order = await async_client.post(
        "/api/v1/orders/shipping-labels/batch-pdf",
        headers=headers,
        json={"order_ids": [first["id"], second["id"]]},
    )
    assert by_order.status_code == 200, by_order.text
    by_order_texts = _pages_text(by_order.content)
    assert len(by_order_texts) == 2
    assert first["order_no"] in by_order_texts[0]
    assert second["order_no"] in by_order_texts[1]

    empty = await async_client.post(
        "/api/v1/orders/shipping-labels/batch-pdf", headers=headers, json={}
    )
    assert empty.status_code == 422

    unknown = await async_client.post(
        "/api/v1/orders/shipping-labels/batch-pdf",
        headers=headers,
        json={"label_ids": [str(uuid4())]},
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "ERR_SHIPPING_LABELS_NOT_FOUND"


# ---------------------------------------------------------------------------
# Scan-to-ship
# ---------------------------------------------------------------------------


async def test_scan_dispatch_releases_parcel_and_deducts_inventory(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    headers, tenant_id, user_id = await _owner(async_client)
    seed = await _seed_catalog(runtime_session_factory, tenant_id)
    order = await _create_order(async_client, headers, seed, quantity=3)
    booked = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=headers,
        json={"carrier_code": "sandbox_carrier", "mode": "sandbox"},
    )
    assert booked.status_code == 201, booked.text
    label = booked.json()

    scanned = await async_client.post(
        "/api/v1/orders/scan-dispatch",
        headers=headers,
        json={"code": label["tracking_number"]},
    )
    assert scanned.status_code == 200, scanned.text
    body = scanned.json()
    assert body["order_id"] == order["id"]
    assert body["order_no"] == order["order_no"]
    assert body["tracking_number"] == label["tracking_number"]
    assert body["status"] == "shipped"

    events = await _scan_events(runtime_session_factory, tenant_id)
    assert len(events) == 1
    assert events[0]["outcome"] == "dispatched"
    assert events[0]["scanned_code"] == label["tracking_number"]
    assert events[0]["carrier_shipping_label_id"] == UUID(label["id"])

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        balance = (
            await session.execute(
                text(
                    "SELECT on_hand_qty, reserved_qty FROM inventory_balances "
                    "WHERE tenant_id = :tid AND variant_id = :vid"
                ),
                {"tid": tenant_id, "vid": seed["variant_id"]},
            )
        ).one()
        assert int(balance[0]) == 497, "the scanned parcel leaves the on-hand balance"
        assert int(balance[1]) == 0, "the reservation is released by the shipment"
        actions = set(
            (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE tenant_id = :tid AND resource_id = :oid"
                    ),
                    {"tid": tenant_id, "oid": UUID(order["id"])},
                )
            ).scalars()
        )
        actor = await session.scalar(
            text(
                "SELECT dispatched_by FROM scan_dispatch_events "
                "WHERE tenant_id = :tid AND order_id = :oid"
            ),
            {"tid": tenant_id, "oid": UUID(order["id"])},
        )
    assert "order.scan_dispatched" in actions
    assert UUID(str(actor)) == user_id

    # Re-scanning a shipped parcel is evidence only: never a downgrade, never a double
    # stock deduct (the ledger key is the order reference).
    rescan = await async_client.post(
        "/api/v1/orders/scan-dispatch",
        headers=headers,
        json={"code": order["order_no"]},
    )
    assert rescan.status_code == 200, rescan.text
    assert rescan.json()["status"] == "shipped"
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        on_hand = await session.scalar(
            text(
                "SELECT on_hand_qty FROM inventory_balances "
                "WHERE tenant_id = :tid AND variant_id = :vid"
            ),
            {"tid": tenant_id, "vid": seed["variant_id"]},
        )
    assert int(on_hand or 0) == 497, "a replayed scan must not deduct stock twice"
    assert len(await _scan_events(runtime_session_factory, tenant_id)) == 2


async def test_scan_dispatch_blocks_unpaid_b2b_balance_with_durable_evidence(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    headers, tenant_id, _user_id = await _owner(async_client)
    seed = await _seed_catalog(runtime_session_factory, tenant_id)
    order = await _create_order(
        async_client, headers, seed, order_mode="b2b_wholesale"
    )

    blocked = await async_client.post(
        "/api/v1/orders/scan-dispatch",
        headers=headers,
        json={"code": order["order_no"]},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "ERR_BALANCE_PAYMENT_REQUIRED"

    # The refusal itself is the audit trail: the row survives the rolled-back request.
    events = await _scan_events(runtime_session_factory, tenant_id)
    assert len(events) == 1
    assert events[0]["outcome"] == "blocked_balance"
    assert events[0]["scanned_code"] == order["order_no"]

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        status = await session.scalar(
            text("SELECT fulfillment_status FROM orders WHERE tenant_id = :tid AND id = :oid"),
            {"tid": tenant_id, "oid": UUID(order["id"])},
        )
    assert status != "shipped"


async def test_scan_dispatch_blocks_negative_margin_hold(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    headers, tenant_id, _user_id = await _owner(async_client)
    seed = await _seed_catalog(runtime_session_factory, tenant_id)
    order = await _create_order(async_client, headers, seed)
    await _insert_profit_snapshot(
        runtime_session_factory,
        tenant_id,
        UUID(order["id"]),
        gross_revenue_base="100.00",
        net_contribution_margin_base="-20.00",
    )
    evaluated = await async_client.post(
        f"/api/v1/orders/{order['id']}/evaluate-routing", headers=headers
    )
    assert evaluated.status_code == 201, evaluated.text
    assert evaluated.json()["is_held"] is True

    blocked = await async_client.post(
        "/api/v1/orders/scan-dispatch",
        headers=headers,
        json={"code": order["order_no"]},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "ERR_ORDER_HELD_FOR_REVIEW"
    assert "anti-loss" in blocked.json()["detail"]
    events = await _scan_events(runtime_session_factory, tenant_id)
    assert [row["outcome"] for row in events] == ["blocked_margin_hold"]


async def test_scan_dispatch_rejects_unknown_code_and_inactive_order(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    headers, tenant_id, _user_id = await _owner(async_client)
    seed = await _seed_catalog(runtime_session_factory, tenant_id)

    unknown = await async_client.post(
        "/api/v1/orders/scan-dispatch",
        headers=headers,
        json={"code": "NOT-A-REAL-CODE"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "ERR_SCAN_CODE_NOT_FOUND"
    assert await _scan_events(runtime_session_factory, tenant_id) == []

    blank = await async_client.post(
        "/api/v1/orders/scan-dispatch", headers=headers, json={"code": "   "}
    )
    assert blank.status_code == 422

    order = await _create_order(async_client, headers, seed)
    cancelled = await async_client.post(
        f"/api/v1/orders/{order['id']}/cancel",
        headers=headers,
        json={
            "warehouse_id": str(seed["warehouse_id"]),
            "reason": "buyer cancel",
        },
    )
    assert cancelled.status_code in {200, 201}, cancelled.text
    inactive = await async_client.post(
        "/api/v1/orders/scan-dispatch",
        headers=headers,
        json={"code": order["order_no"]},
    )
    assert inactive.status_code == 409
    assert inactive.json()["code"] == "ERR_ORDER_INACTIVE"


# ---------------------------------------------------------------------------
# One-click reorder (magic link only)
# ---------------------------------------------------------------------------


async def _seed_portal_quote(
    factory: Factory,
    tenant_id: UUID,
    user_id: UUID,
    *,
    fx_locked_until: datetime | None = None,
) -> dict[str, Any]:
    """Customer/opportunity/quote/quote_items + a plaintext magic link token."""
    seed = await _seed_catalog(factory, tenant_id)
    opportunity_id, quote_id = uuid4(), uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "UPDATE tenants SET ai_policy_settings = CAST(:settings AS jsonb) "
                "WHERE id = :tid"
            ),
            {"tid": tenant_id, "settings": json.dumps({"pi_template": PI_TEMPLATE})},
        )
        await session.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, customer_id, title, stage, "
                "owner_user_id) VALUES (:id, :tid, :cid, '500 pcs LED strip FOB Xiamen', "
                "'quoting', :uid)"
            ),
            {
                "id": opportunity_id,
                "tid": tenant_id,
                "cid": seed["customer_id"],
                "uid": user_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO quotes (id, tenant_id, opportunity_id, quote_no, version, "
                " is_proforma_invoice, incoterm, port_of_loading, port_of_destination, "
                " payment_terms, base_currency, quote_currency, fx_rate_snapshot, "
                " fx_locked_until, freight_fee, insurance_fee, customs_duty_estimate, "
                " total_amount, status) "
                "VALUES (:id, :tid, :oid, :qno, 1, true, 'FOB', 'Xiamen, China', "
                " 'Hamburg, Germany', '30% T/T deposit, 70% before shipment', 'CNY', 'USD', "
                " 7.200000, :locked, 200.00, 0.00, 0.00, 4450.00, 'sent')"
            ),
            {
                "id": quote_id,
                "tid": tenant_id,
                "oid": opportunity_id,
                "qno": f"PI-202611-{uuid4().hex[:6].upper()}",
                "locked": fx_locked_until or (datetime.now(UTC) + timedelta(days=10)),
            },
        )
        await session.execute(
            text(
                "INSERT INTO quote_items (tenant_id, quote_id, variant_id, sku_snapshot, "
                " description_i18n, quantity, moq_tier_applied, unit_price_quote_ccy, "
                " unit_cost_base_snapshot, line_total_quote_ccy) "
                "VALUES (:tid, :qid, :vid, :sku, '5M IP65 Smart LED Strip', 500, 500, "
                " 8.5000, 42.0000, 4250.00)"
            ),
            {
                "tid": tenant_id,
                "qid": quote_id,
                "vid": seed["variant_id"],
                "sku": f"LED-Q-{uuid4().hex[:6].upper()}",
            },
        )
        plain_token = f"{PORTAL_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        await session.execute(
            text(
                "INSERT INTO b2b_portal_tokens (id, tenant_id, quote_id, created_by_user_id, "
                " token_prefix, token_hash) VALUES (:id, :tid, :qid, :uid, :prefix, :hash)"
            ),
            {
                "id": uuid4(),
                "tid": tenant_id,
                "qid": quote_id,
                "uid": user_id,
                "prefix": plain_token[:16],
                "hash": hash_portal_token(plain_token),
            },
        )
    return {**seed, "opportunity_id": opportunity_id, "quote_id": quote_id, "token": plain_token}


def _walk_keys(payload: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            keys.add(key)
            keys |= _walk_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            keys |= _walk_keys(item)
    return keys


async def test_portal_reorder_reprices_live_and_returns_a_draft_quote(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    _headers, tenant_id, user_id = await _owner(async_client)
    seeded = await _seed_portal_quote(runtime_session_factory, tenant_id, user_id)

    response = await async_client.post(
        f"/api/v1/portal/{seeded['token']}/reorder", json={}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["new_quote_no"].startswith("PI-REO-")
    assert body["status"] == "draft"
    assert body["currency"] == "USD"
    # 500 pcs at the live 500-tier price (8.5000) + the source freight (200.00).
    assert Decimal(body["total_amount"]) == Decimal("4450.00")
    leaked = _walk_keys(body) & {
        "unit_cost_base_snapshot",
        "unit_cost",
        "cost_price_base",
        "margin_pct",
        "min_margin_floor_pct",
        "estimated_margin_pct",
        "net_contribution_margin_base",
        "reasoning_summary",
    }
    assert leaked == set(), f"buyer payload leaked internal cost fields: {leaked}"

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        quote_row = (
            await session.execute(
                text(
                    "SELECT id, quote_no, version, status, opportunity_id, total_amount, "
                    "fx_rate_snapshot, quote_currency, base_currency, fx_locked_until "
                    "FROM quotes WHERE tenant_id = :tid AND id = :qid"
                ),
                {"tid": tenant_id, "qid": UUID(body["new_quote_id"])},
            )
        ).one()
        items = (
            await session.execute(
                text(
                    "SELECT quantity, unit_price_quote_ccy, line_total_quote_ccy, "
                    "moq_tier_applied, unit_cost_base_snapshot FROM quote_items "
                    "WHERE tenant_id = :tid AND quote_id = :qid"
                ),
                {"tid": tenant_id, "qid": UUID(body["new_quote_id"])},
            )
        ).all()
        audit = (
            await session.execute(
                text(
                    "SELECT action, actor_type, actor_id, after_state FROM audit_events "
                    "WHERE tenant_id = :tid AND resource_id = :qid"
                ),
                {"tid": tenant_id, "qid": UUID(body["new_quote_id"])},
            )
        ).one()
    assert quote_row[2] == 1
    assert quote_row[3] == "draft"
    assert quote_row[4] == seeded["opportunity_id"]
    assert Decimal(str(quote_row[6])) == Decimal("7.200000")
    assert quote_row[7] == "USD"
    assert quote_row[8] == "CNY"
    assert quote_row[9] > datetime.now(UTC)
    assert len(items) == 1
    assert int(items[0][0]) == 500
    assert Decimal(str(items[0][1])) == Decimal("8.5000")
    assert Decimal(str(items[0][2])) == Decimal("4250.00")
    assert int(items[0][3]) == 500
    assert Decimal(str(items[0][4])) == Decimal("40.0000")
    assert audit[0] == "b2b.portal.reorder"
    assert audit[1] == "webhook"
    assert audit[2] == f"portal:{audit[3]['portal_token_id']}"
    assert audit[3]["source_quote_id"] == str(seeded["quote_id"])


async def test_portal_reorder_reprices_from_live_variant_tiers_and_validates_quantities(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    _headers, tenant_id, user_id = await _owner(async_client)
    seeded = await _seed_portal_quote(runtime_session_factory, tenant_id, user_id)
    sku = None
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        sku = await session.scalar(
            text(
                "SELECT sku_snapshot FROM quote_items WHERE tenant_id = :tid AND quote_id = :qid"
            ),
            {"tid": tenant_id, "qid": seeded["quote_id"]},
        )
        # The vendor re-priced the 50+ tier after the original PI was sent.
        await session.execute(
            text(
                "UPDATE product_variants SET b2b_moq_tiers = CAST(:tiers AS jsonb) "
                "WHERE tenant_id = :tid AND id = :vid"
            ),
            {
                "tid": tenant_id,
                "vid": seeded["variant_id"],
                "tiers": json.dumps(
                    [
                        {"min_qty": 50, "unit_price_usd": "11.0000"},
                        {"min_qty": 500, "unit_price_usd": "9.0000"},
                    ]
                ),
            },
        )
    assert sku

    response = await async_client.post(
        f"/api/v1/portal/{seeded['token']}/reorder",
        json={
            "notes": "Please repeat the same strip for our spring promo.",
            "requested_quantities": {sku: 60},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    # 60 pcs now uses the *live* 50-tier price (11.0000): 660.00 + 200.00 freight.
    assert Decimal(body["total_amount"]) == Decimal("860.00")

    unknown_line = await async_client.post(
        f"/api/v1/portal/{seeded['token']}/reorder",
        json={"requested_quantities": {"NOT-A-LINE": 5}},
    )
    assert unknown_line.status_code == 422
    assert unknown_line.json()["code"] == "ERR_PORTAL_REORDER_UNKNOWN_LINE"

    bad_quantity = await async_client.post(
        f"/api/v1/portal/{seeded['token']}/reorder",
        json={"requested_quantities": {sku: 0}},
    )
    assert bad_quantity.status_code == 422
    assert bad_quantity.json()["code"] == "ERR_VALIDATION"

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await session.execute(
            text(
                "UPDATE b2b_portal_tokens SET revoked_at = now() "
                "WHERE tenant_id = :tid AND quote_id = :qid"
            ),
            {"tid": tenant_id, "qid": seeded["quote_id"]},
        )
    revoked = await async_client.post(
        f"/api/v1/portal/{seeded['token']}/reorder", json={}
    )
    assert revoked.status_code == 401
    assert revoked.json()["code"] == "ERR_INVALID_PORTAL_TOKEN"


async def test_portal_reorder_rejects_a_tampered_magic_link(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    _headers, tenant_id, user_id = await _owner(async_client)
    seeded = await _seed_portal_quote(runtime_session_factory, tenant_id, user_id)
    tampered = seeded["token"][:-4] + "AAAA"
    response = await async_client.post(f"/api/v1/portal/{tampered}/reorder", json={})
    assert response.status_code == 401
    assert response.json()["code"] == "ERR_INVALID_PORTAL_TOKEN"


# ---------------------------------------------------------------------------
# Two new automation templates
# ---------------------------------------------------------------------------


async def _seed_inbox_thread(
    factory: Factory,
    tenant_id: UUID,
    *,
    body_text: str,
    channel_type: str = "email",
) -> dict[str, UUID]:
    """One channel account + conversation + inbound customer message."""
    account_id, conversation_id, message_id, customer_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, display_name, primary_email, "
                "country_code, customer_type) "
                "VALUES (:cid, :tid, 'Anna Weber', :email, 'DE', 'b2c_consumer')"
            ),
            {"cid": customer_id, "tid": tenant_id, "email": f"anna-{uuid4().hex[:6]}@example.de"},
        )
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name) "
                "VALUES (:aid, :tid, :ctype, :ext, 'Support inbox')"
            ),
            {
                "aid": account_id,
                "tid": tenant_id,
                "ctype": channel_type,
                "ext": f"acct-{uuid4().hex[:8]}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO conversations (id, tenant_id, customer_id, channel_account_id, "
                "channel_type, status) VALUES (:id, :tid, :cid, :aid, :ctype, 'open')"
            ),
            {
                "id": conversation_id,
                "tid": tenant_id,
                "cid": customer_id,
                "aid": account_id,
                "ctype": channel_type,
            },
        )
        await session.execute(
            text(
                "INSERT INTO messages (id, tenant_id, conversation_id, customer_id, "
                " channel_type, direction, sender_type, content_type, body_text, "
                " source_timestamp) "
                "VALUES (:id, :tid, :conv, :cid, :ctype, 'inbound', 'customer', 'text', "
                " :body, now())"
            ),
            {
                "id": message_id,
                "tid": tenant_id,
                "conv": conversation_id,
                "cid": customer_id,
                "ctype": channel_type,
                "body": body_text,
            },
        )
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "customer_id": customer_id,
    }


async def _insert_notification_channel(factory: Factory, tenant_id: UUID) -> None:
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO notification_channels (tenant_id, name, provider, webhook_url, "
                " min_priority, is_enabled) "
                "VALUES (:tid, 'Ops Feishu', 'feishu', "
                " 'http://127.0.0.1:9997/hook/track10', 'P2', true)"
            ),
            {"tid": tenant_id},
        )


async def test_negative_review_ticket_matches_complaints_and_dedupes_per_message(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    _headers, tenant_id, _user_id = await _owner(async_client)
    complaint = await _seed_inbox_thread(
        runtime_session_factory,
        tenant_id,
        body_text=(
            "The strip arrived broken and the 1 star review is already posted. "
            "差评，灯带破损，我要退款。"
        ),
    )
    await _seed_inbox_thread(
        runtime_session_factory,
        tenant_id,
        body_text="Thanks, the reorder arrived on time. Great quality!",
    )
    await _insert_notification_channel(runtime_session_factory, tenant_id)

    automation = AutomationService()
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        dry = await automation.evaluate_rules(session, tenant_id, dry_run=True)
        dry_item = next(
            item for item in dry.rules if item.template_code == "negative_review_ticket"
        )
        assert dry_item.matched_count == 1
        assert dry_item.proposals_created == 0
        assert dry_item.notifications_queued == 0
        assert "dry_run" in (dry_item.note or "")
        preview = dry_item.items_preview[0]
        assert preview["message_id"] == str(complaint["message_id"])
        assert "broken" in preview["matched_keywords"]
        assert "差评" in preview["matched_keywords"]
        assert preview["severity"] == "P0"
        proposals_after_dry = await session.scalar(
            text("SELECT COUNT(*) FROM ai_proposals WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
    assert int(proposals_after_dry or 0) == 0, "a dry run must never write a proposal"

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        live = await automation.evaluate_rules(session, tenant_id)
        live_item = next(
            item for item in live.rules if item.template_code == "negative_review_ticket"
        )
        assert live_item.matched_count == 1
        assert live_item.proposals_created == 1
        assert live_item.notifications_queued == 1
        proposal = (
            await session.execute(
                text(
                    "SELECT capability, proposal_type, status, target_entity_type, "
                    " target_entity_id, proposed_payload FROM ai_proposals "
                    "WHERE tenant_id = :tid AND proposed_payload ->> 'rule' = "
                    " 'negative_review_ticket'"
                ),
                {"tid": tenant_id},
            )
        ).one()
        delivery = (
            await session.execute(
                text(
                    "SELECT priority, event_type, body FROM notification_deliveries "
                    "WHERE tenant_id = :tid AND event_type = "
                    " 'automation.negative_review_ticket'"
                ),
                {"tid": tenant_id},
            )
        ).one()
    assert proposal[0] == "cs_support"
    # The `ai_proposals` CHECK constraint only allows the seven locked proposal types.
    assert proposal[1] == "follow_up_suggestion"
    assert proposal[2] == "pending"
    assert proposal[3] == "message"
    assert proposal[4] == complaint["message_id"]
    payload = proposal[5]
    assert payload["category"] == "service_ticket"
    expected_key = f"{tenant_id}:negative_review_ticket:{complaint['message_id']}"
    assert payload["idempotency_key"] == expected_key
    assert payload["recommended_action"] == "open_service_ticket_and_reply"
    assert payload["conversation_id"] == str(complaint["conversation_id"])
    assert payload["grounding"]["source"] == "sql"
    assert delivery[0] == "P0", "a 1-star/差评 hit escalates the digest to P0"
    assert delivery[1] == "automation.negative_review_ticket"
    assert "complaint" in delivery[2]
    assert "Anna Weber" in delivery[2]

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        second = await automation.evaluate_rules(session, tenant_id)
        second_item = next(
            item for item in second.rules if item.template_code == "negative_review_ticket"
        )
        assert second_item.matched_count == 1
        assert second_item.proposals_created == 0, "one pending ticket per message, ever"
        count = await session.scalar(
            text(
                "SELECT COUNT(*) FROM ai_proposals WHERE tenant_id = :tid "
                "AND proposed_payload ->> 'rule' = 'negative_review_ticket'"
            ),
            {"tid": tenant_id},
        )
    assert int(count or 0) == 1


async def test_negative_review_keywords_are_configurable_per_tenant(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    _headers, tenant_id, _user_id = await _owner(async_client)
    await _seed_inbox_thread(
        runtime_session_factory,
        tenant_id,
        body_text="The parcel smells like a wet dog and arrived late.",
    )
    automation = AutomationService()
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        seeded = await automation.seed_default_rules(session, tenant_id)
        rule = next(
            r for r in seeded.rules if r.template_code == "negative_review_ticket"
        )
        assert rule.config_json["keywords"] == list(DEFAULT_NEGATIVE_REVIEW_KEYWORDS)
        await automation.update_rule(
            session,
            tenant_id,
            rule.id,
            config_json={"keywords": ["wet dog"]},
        )
        report = await automation.evaluate_rules(session, tenant_id)
        item = next(
            r for r in report.rules if r.template_code == "negative_review_ticket"
        )
    assert item.matched_count == 1
    assert item.items_preview[0]["matched_keywords"] == ["wet dog"]


async def test_high_adoption_faq_extract_promotes_approved_replies_once(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    _headers, tenant_id, _user_id = await _owner(async_client)
    thread = await _seed_inbox_thread(
        runtime_session_factory,
        tenant_id,
        body_text="Do you ship the 5M LED strip to Germany with a 2-year warranty?",
    )
    approved_id, weak_id = uuid4(), uuid4()
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO ai_proposals (id, tenant_id, capability, proposal_type, "
                " conversation_id, proposed_payload, citations, confidence_score, status, "
                " reviewed_at) "
                "VALUES (:id, :tid, 'cs_support', 'reply_draft', :conv, "
                " CAST(:payload AS jsonb), '[]'::jsonb, 0.940, 'approved', now())"
            ),
            {
                "id": approved_id,
                "tid": tenant_id,
                "conv": thread["conversation_id"],
                "payload": json.dumps(
                    {
                        "body_text": (
                            "Yes — we ship 5M LED strip to Germany with a 2-year warranty "
                            "and a 30-day DOA replacement policy."
                        ),
                        "channel_type": "email",
                    }
                ),
            },
        )
        await session.execute(
            text(
                "INSERT INTO ai_proposals (id, tenant_id, capability, proposal_type, "
                " conversation_id, proposed_payload, citations, confidence_score, status, "
                " reviewed_at) "
                "VALUES (:id, :tid, 'cs_support', 'reply_draft', :conv, "
                " CAST(:payload AS jsonb), '[]'::jsonb, 0.410, 'approved', now())"
            ),
            {
                "id": weak_id,
                "tid": tenant_id,
                "conv": thread["conversation_id"],
                "payload": json.dumps({"body_text": "Not sure, let me check."}),
            },
        )

    automation = AutomationService()
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        dry = await automation.evaluate_rules(session, tenant_id, dry_run=True)
        dry_item = next(
            item for item in dry.rules if item.template_code == "high_adoption_faq_extract"
        )
        assert dry_item.matched_count == 1, "only the high-confidence reply qualifies"
        assert dry_item.proposals_created == 0
        assert dry_item.items_preview[0]["proposal_id"] == str(approved_id)
        assert "warranty" in dry_item.items_preview[0]["answer"]

        live = await automation.evaluate_rules(session, tenant_id)
        live_item = next(
            item for item in live.rules if item.template_code == "high_adoption_faq_extract"
        )
        assert live_item.matched_count == 1
        assert live_item.proposals_created == 1
        extraction = (
            await session.execute(
                text(
                    "SELECT capability, proposal_type, status, target_entity_type, "
                    " target_entity_id, proposed_payload FROM ai_proposals "
                    "WHERE tenant_id = :tid AND proposed_payload ->> 'rule' = "
                    " 'high_adoption_faq_extract'"
                ),
                {"tid": tenant_id},
            )
        ).one()
        assert extraction[0] == "cs_support"
        assert extraction[1] == "crm_entity_extract"
        assert extraction[2] == "pending"
        assert extraction[3] == "ai_proposal"
        assert extraction[4] == approved_id
        payload = extraction[5]
        assert payload["category"] == "faq_extract"
        assert payload["question"].startswith("Do you ship the 5M LED strip")
        assert payload["answer"].startswith("Yes — we ship 5M LED strip")
        assert payload["promotion_marker"] == f"{FAQ_PROMOTION_URI_PREFIX}{approved_id}"
        assert payload["idempotency_key"] == (
            f"{tenant_id}:high_adoption_faq_extract:{approved_id}"
        )

        second = await automation.evaluate_rules(session, tenant_id)
        second_item = next(
            item for item in second.rules if item.template_code == "high_adoption_faq_extract"
        )
        assert second_item.proposals_created == 0, "one extraction per source proposal"

        # Promoting the proposal into the KB (logical `proposal:<id>` link) stops the scan.
        await session.execute(
            text(
                "INSERT INTO kb_documents (tenant_id, title, source_type, source_uri, "
                " doc_hash) VALUES (:tid, 'Warranty FAQ', 'faq_json', :uri, :hash)"
            ),
            {
                "tid": tenant_id,
                "uri": f"{FAQ_PROMOTION_URI_PREFIX}{approved_id}",
                "hash": hashlib.sha256(str(approved_id).encode()).hexdigest(),
            },
        )
        # The pending extraction proposal is still in the review queue; clear it so the
        # scan sees "already promoted" rather than "already proposed".
        await session.execute(
            text(
                "DELETE FROM ai_proposals WHERE tenant_id = :tid "
                "AND proposed_payload ->> 'rule' = 'high_adoption_faq_extract'"
            ),
            {"tid": tenant_id},
        )
        promoted = await automation.evaluate_rules(session, tenant_id, dry_run=True)
        promoted_item = next(
            item
            for item in promoted.rules
            if item.template_code == "high_adoption_faq_extract"
        )
    assert promoted_item.matched_count == 0


async def test_seeding_covers_seven_templates_and_the_check_constraint_rejects_others(
    async_client: AsyncClient,
    runtime_session_factory: Factory,
) -> None:
    _headers, tenant_id, _user_id = await _owner(async_client)
    automation = AutomationService()
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        seeded = await automation.seed_default_rules(session, tenant_id)
        assert seeded.created == 7
        assert {rule.template_code for rule in seeded.rules} == set(DEFAULT_RULE_TEMPLATES)
        by_code = {rule.template_code: rule for rule in seeded.rules}
        assert by_code["negative_review_ticket"].priority == "P1"
        assert by_code["negative_review_ticket"].autonomy_level == "L1_proposal"
        assert by_code["high_adoption_faq_extract"].priority == "P2"
        assert by_code["high_adoption_faq_extract"].config_json["min_confidence"] == "0.900"

    with pytest.raises(DBAPIError):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO automation_rules (tenant_id, template_code, name, "
                    " autonomy_level, priority) "
                    "VALUES (:tid, 'made_up_template', 'Nope', 'L1_proposal', 'P1')"
                ),
                {"tid": tenant_id},
            )


# ---------------------------------------------------------------------------
# RLS / schema invariants
# ---------------------------------------------------------------------------


async def test_scan_dispatch_events_are_force_rls_tenant_isolated(
    runtime_session_factory: Factory,
) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    user_a, customer_a = uuid4(), uuid4()
    order_a, label_a = uuid4(), uuid4()
    for tenant_id, tag in ((tenant_a, "a"), (tenant_b, "b")):
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, base_currency) "
                    "VALUES (:id, :slug, :name, 'USD')"
                ),
                {"id": tenant_id, "slug": f"track10-{tag}-{tenant_id.hex[:8]}", "name": tag},
            )
    async with tenant_session(runtime_session_factory, tenant_a) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, 'Track 10 Owner', 'hash')"
            ),
            {"id": user_a, "email": f"{user_a.hex}@track10.example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, display_name, primary_email, "
                "country_code, customer_type) "
                "VALUES (:id, :tid, 'RLS Buyer', :email, 'DE', 'b2c_consumer')"
            ),
            {"id": customer_a, "tid": tenant_a, "email": f"{customer_a.hex}@track10.example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, "
                " currency, fx_rate_to_base, subtotal_amount, total_amount, "
                " fulfillment_status, payment_status, channel_metadata) "
                "VALUES (:id, :tid, 'ORD-TRACK10-RLS', 'b2c_retail', :cid, 'USD', 1.000000, "
                " 10.00, 10.00, 'confirmed_reserved', 'paid', '{}'::jsonb)"
            ),
            {"id": order_a, "tid": tenant_a, "cid": customer_a},
        )
        await session.execute(
            text(
                "INSERT INTO carrier_shipping_labels (id, tenant_id, order_id, carrier_code, "
                " service_code, tracking_number, waybill_number, mode) "
                "VALUES (:id, :tid, :oid, 'sandbox_carrier', 'Sandbox-Standard', "
                " 'SBX-RLS-0001', 'SBX-RLS-0001', 'sandbox')"
            ),
            {"id": label_a, "tid": tenant_a, "oid": order_a},
        )
        await session.execute(
            text(
                "INSERT INTO scan_dispatch_events (tenant_id, order_id, "
                " carrier_shipping_label_id, scanned_code, tracking_number, outcome, "
                " dispatched_by) VALUES (:tid, :oid, :lid, 'SBX-RLS-0001', 'SBX-RLS-0001', "
                " 'dispatched', :uid)"
            ),
            {"tid": tenant_a, "oid": order_a, "lid": label_a, "uid": user_a},
        )

    async with tenant_session(runtime_session_factory, tenant_b) as session:
        # No WHERE clause at all: FORCE RLS alone must return zero rows.
        visible = await session.scalar(text("SELECT COUNT(*) FROM scan_dispatch_events"))
        assert int(visible or 0) == 0
        default_outcome = await session.scalar(
            text(
                "SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef "
                "WHERE adrelid = 'scan_dispatch_events'::regclass "
                "AND adnum = (SELECT attnum FROM pg_attribute "
                "WHERE attrelid = 'scan_dispatch_events'::regclass AND attname = 'outcome')"
            )
        )
        assert "dispatched" in str(default_outcome)
        rls = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'scan_dispatch_events'"
                )
            )
        ).one()
    assert (rls[0], rls[1]) == (True, True)

    async with tenant_session(runtime_session_factory, tenant_a) as session:
        owned = await session.scalar(text("SELECT COUNT(*) FROM scan_dispatch_events"))
        assert int(owned or 0) == 1

    with pytest.raises(DBAPIError):
        async with tenant_session(runtime_session_factory, tenant_a) as session:
            await session.execute(
                text(
                    "INSERT INTO scan_dispatch_events (tenant_id, order_id, scanned_code, "
                    " outcome) VALUES (:tid, :oid, 'SBX-RLS-0002', 'not_an_outcome')"
                ),
                {"tid": tenant_a, "oid": order_a},
            )
