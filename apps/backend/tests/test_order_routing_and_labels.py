from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.parse
import uuid as uuidlib
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import AsyncClient
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_auth_rbac import _register_owner

from erp_core.orders.carrier_label_service import (
    THERMAL_LABEL_HEIGHT_PT,
    THERMAL_LABEL_WIDTH_PT,
    CarrierBookingRequest,
    FourPxAdapter,
    LabelLine,
    SandboxCarrierAdapter,
    ThermalLabelContext,
    YunExpressAdapter,
    render_thermal_label_pdf,
)
from erp_core.shared.config import get_settings
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.security import create_access_token

ROUTING_TABLES = ("order_routing_rules", "order_routing_evaluations", "carrier_shipping_labels")

YUNEXPRESS_BODY: dict[str, Any] = {
    "Result": {
        "Success": True,
        "WayBillNumber": "YT2626000123456",
        "TrackingNumber": "YT2626000123456",
        "ShippingCost": "18.42",
    }
}

FOURPX_BODY: dict[str, Any] = {
    "resultCode": "1",
    "resultMsg": "success",
    "data": {
        "tracking_no": "4PX3000123456CN",
        "waybill_no": "4PX3000123456CN",
        "freight": "16.35",
    },
}


async def _member_headers(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    role: UserRole,
) -> tuple[dict[str, str], UUID]:
    """Create a real membership row and mint an access token for it."""
    user_id, email = uuid4(), f"{role.value}-{uuid4().hex[:8]}@factory.example.com"
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, 'Routing Tester', 'hash')"
            ),
            {"uid": user_id, "email": email},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                "VALUES (:tid, :uid, :role)"
            ),
            {"tid": tenant_id, "uid": user_id, "role": role.value},
        )
    token = create_access_token(
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        email=email,
        settings=get_settings(),
    )
    return {"Authorization": f"Bearer {token}"}, user_id


async def _seed_order(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    *,
    country_code: str = "SE",
    retail_price_usd: str = "100.00",
    cost_price_base: str = "40.00",
    quantity: int = 2,
) -> dict[str, Any]:
    """Seed a customer/SKU/warehouse trio so the API can create a real order."""
    ids = {
        "customer_id": uuid4(),
        "product_id": uuid4(),
        "variant_id": uuid4(),
        "warehouse_id": uuid4(),
    }
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO warehouses (id, tenant_id, code, name, country_code, city) "
                "VALUES (:wid, :tid, :code, 'Shenzhen Main', 'CN', 'Shenzhen')"
            ),
            {"wid": ids["warehouse_id"], "tid": tenant_id, "code": f"WH-{uuid4().hex[:6]}"},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, display_name, primary_email, "
                "country_code, customer_type) "
                "VALUES (:cid, :tid, 'Nordic Buyer AB', :email, :country, 'b2b_company_rep')"
            ),
            {
                "cid": ids["customer_id"],
                "tid": tenant_id,
                "email": f"buyer-{uuid4().hex[:6]}@nordic.example.com",
                "country": country_code,
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
                " id, tenant_id, product_id, sku, cost_price_base, retail_price_usd,"
                " weight_grams, lead_time_days"
                ") VALUES (:vid, :tid, :pid, :sku, :cost, :retail, 1200, 7)"
            ),
            {
                "vid": ids["variant_id"],
                "tid": tenant_id,
                "pid": ids["product_id"],
                "sku": f"SKU-{uuid4().hex[:6]}",
                "cost": Decimal(cost_price_base),
                "retail": Decimal(retail_price_usd),
            },
        )
        await session.execute(
            text(
                "INSERT INTO inventory_balances "
                "(tenant_id, warehouse_id, variant_id, on_hand_qty, reserved_qty) "
                "VALUES (:tid, :wid, :vid, 500, 0)"
            ),
            {
                "tid": tenant_id,
                "wid": ids["warehouse_id"],
                "vid": ids["variant_id"],
            },
        )
    ids["quantity"] = quantity
    ids["unit_price_usd"] = retail_price_usd
    return ids


async def _create_order(
    async_client: AsyncClient,
    headers: dict[str, str],
    seed: dict[str, Any],
    *,
    order_mode: str = "b2c_retail",
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
                    "quantity": seed["quantity"],
                    "unit_price": seed["unit_price_usd"],
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_profit_snapshot(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    order_id: UUID,
    *,
    gross_revenue_base: str,
    net_contribution_margin_base: str,
) -> None:
    gross = Decimal(gross_revenue_base)
    net = Decimal(net_contribution_margin_base)
    assert net <= gross, "the generated column derives net margin as gross - cogs"
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
            {
                "oid": order_id,
                "tid": tenant_id,
                "gross": gross,
                "cogs": gross - net,
            },
        )


def _pdf_text(pdf_bytes: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf_bytes)).pages)


# ---------------------------------------------------------------------------
# Rule CRUD + RBAC
# ---------------------------------------------------------------------------


async def test_routing_rule_crud_and_management_role_gate(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id = await _register_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}
    warehouse_id = uuid4()
    async with tenant_session(runtime_session_factory, UUID(tenant_id)) as session:
        await session.execute(
            text(
                "INSERT INTO warehouses (id, tenant_id, code, name) "
                "VALUES (:wid, :tid, :code, 'Xiamen Hub')"
            ),
            {"wid": warehouse_id, "tid": UUID(tenant_id), "code": f"XM-{uuid4().hex[:6]}"},
        )

    created = await async_client.post(
        "/api/v1/orders/routing-rules",
        headers=headers,
        json={
            "name": "Negative margin loss guard",
            "priority": 10,
            "conditions": {"min_margin_pct": "8.00"},
            "actions": {"hold_for_review": True, "add_tags": ["anti-loss"]},
        },
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["priority"] == 10
    assert rule["conditions"]["min_margin_pct"] == "8.00"
    assert rule["actions"]["hold_for_review"] is True

    se_rule = await async_client.post(
        "/api/v1/orders/routing-rules",
        headers=headers,
        json={
            "name": "Sweden -> YunExpress",
            "priority": 100,
            "conditions": {"destination_countries": ["se"], "order_modes": ["b2c_retail"]},
            "actions": {
                "warehouse_id": str(warehouse_id),
                "carrier_code": "yunexpress",
                "service_code": "YunExpress-Expedited",
                "add_tags": ["eu-fast-lane"],
            },
        },
    )
    assert se_rule.status_code == 201, se_rule.text
    assert se_rule.json()["conditions"]["destination_countries"] == ["SE"]

    listed = await async_client.get("/api/v1/orders/routing-rules", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [item["priority"] for item in listed.json()] == [10, 100]

    patched = await async_client.patch(
        f"/api/v1/orders/routing-rules/{rule['id']}",
        headers=headers,
        json={"is_enabled": False, "priority": 5},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["is_enabled"] is False
    assert patched.json()["priority"] == 5

    invalid_warehouse = await async_client.post(
        "/api/v1/orders/routing-rules",
        headers=headers,
        json={
            "name": "Ghost warehouse",
            "conditions": {"destination_countries": ["US"]},
            "actions": {"warehouse_id": str(uuid4())},
        },
    )
    assert invalid_warehouse.status_code == 422
    assert invalid_warehouse.json()["code"] == "ERR_ROUTING_WAREHOUSE_NOT_FOUND"

    empty_rule = await async_client.post(
        "/api/v1/orders/routing-rules",
        headers=headers,
        json={"name": "No conditions"},
    )
    assert empty_rule.status_code == 422

    agent_headers, _ = await _member_headers(
        runtime_session_factory, UUID(tenant_id), UserRole.AGENT
    )
    read_ok = await async_client.get("/api/v1/orders/routing-rules", headers=agent_headers)
    assert read_ok.status_code == 200
    write_denied = await async_client.post(
        "/api/v1/orders/routing-rules",
        headers=agent_headers,
        json={
            "name": "Agent rule",
            "conditions": {"destination_countries": ["US"]},
        },
    )
    assert write_denied.status_code == 403
    delete_denied = await async_client.delete(
        f"/api/v1/orders/routing-rules/{rule['id']}", headers=agent_headers
    )
    assert delete_denied.status_code == 403

    deleted = await async_client.delete(
        f"/api/v1/orders/routing-rules/{rule['id']}", headers=headers
    )
    assert deleted.status_code == 204
    remaining = await async_client.get("/api/v1/orders/routing-rules", headers=headers)
    assert [item["id"] for item in remaining.json()] == [se_rule.json()["id"]]


# ---------------------------------------------------------------------------
# Negative margin -> hold -> manager release -> waybill
# ---------------------------------------------------------------------------


async def test_negative_margin_hold_blocks_waybill_until_manager_releases(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id_str = await _register_owner(async_client)
    tenant_id = UUID(tenant_id_str)
    headers = {"Authorization": f"Bearer {token}"}
    seed = await _seed_order(runtime_session_factory, tenant_id)
    order = await _create_order(async_client, headers, seed)
    await _insert_profit_snapshot(
        runtime_session_factory,
        tenant_id,
        UUID(order["id"]),
        gross_revenue_base="1000.00",
        net_contribution_margin_base="-250.00",
    )

    evaluated = await async_client.post(
        f"/api/v1/orders/{order['id']}/evaluate-routing", headers=headers
    )
    assert evaluated.status_code == 201, evaluated.text
    evaluation = evaluated.json()
    assert evaluation["is_held"] is True
    assert "negative" in (evaluation["hold_reason"] or "")
    assert Decimal(evaluation["estimated_margin_pct"]) == Decimal("-25.0000")
    assert evaluation["matched_rule_ids"] == []

    latest = await async_client.get(
        f"/api/v1/orders/{order['id']}/routing-evaluation", headers=headers
    )
    assert latest.status_code == 200, latest.text
    assert latest.json()["id"] == evaluation["id"]

    blocked = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=headers,
        json={"carrier_code": "sandbox_carrier", "mode": "sandbox"},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "ERR_ORDER_HELD_FOR_REVIEW"
    assert "negative" in blocked.json()["detail"]

    agent_headers, _ = await _member_headers(
        runtime_session_factory, tenant_id, UserRole.AGENT
    )
    agent_release = await async_client.post(
        f"/api/v1/orders/{order['id']}/release-hold",
        headers=agent_headers,
        json={"reason": "Agent tries to release"},
    )
    assert agent_release.status_code == 403

    manager_headers, manager_id = await _member_headers(
        runtime_session_factory, tenant_id, UserRole.MANAGER
    )
    short_reason = await async_client.post(
        f"/api/v1/orders/{order['id']}/release-hold",
        headers=manager_headers,
        json={"reason": "x"},
    )
    assert short_reason.status_code == 422

    released = await async_client.post(
        f"/api/v1/orders/{order['id']}/release-hold",
        headers=manager_headers,
        json={"reason": "Freight renegotiated with YunExpress"},
    )
    assert released.status_code == 200, released.text
    released_body = released.json()
    assert released_body["is_held"] is False
    assert released_body["hold_released_by"] == str(manager_id)
    assert released_body["hold_released_at"] is not None

    already_released = await async_client.post(
        f"/api/v1/orders/{order['id']}/release-hold",
        headers=manager_headers,
        json={"reason": "Release it twice"},
    )
    assert already_released.status_code == 409
    assert already_released.json()["code"] == "ERR_ROUTING_HOLD_NOT_ACTIVE"

    booked = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=headers,
        json={"carrier_code": "sandbox_carrier", "mode": "sandbox"},
    )
    assert booked.status_code == 201, booked.text
    label = booked.json()
    assert label["carrier_code"] == "sandbox_carrier"
    assert label["tracking_number"].startswith("SBX")
    assert label["waybill_number"].startswith("SBX")
    assert label["mode"] == "sandbox"
    assert label["label_format"] == "pdf_4x6"
    assert Decimal(label["shipping_cost_estimate"]) > 0

    pdf = await async_client.get(
        f"/api/v1/orders/{order['id']}/shipping-labels/{label['id']}/pdf",
        headers=headers,
    )
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-")
    page = PdfReader(io.BytesIO(pdf.content)).pages[0]
    assert round(float(page.mediabox.width)) == THERMAL_LABEL_WIDTH_PT
    assert round(float(page.mediabox.height)) == THERMAL_LABEL_HEIGHT_PT
    text_of_label = _pdf_text(pdf.content)
    assert label["tracking_number"] in text_of_label
    assert label["waybill_number"] in text_of_label
    assert order["order_no"] in text_of_label
    assert "Sandbox Carrier" in text_of_label
    assert "SE" in text_of_label

    second_pdf = await async_client.get(
        f"/api/v1/orders/{order['id']}/shipping-labels/{label['id']}/pdf",
        headers=headers,
    )
    assert second_pdf.content == pdf.content, "label re-render must be deterministic"

    listed_labels = await async_client.get(
        f"/api/v1/orders/{order['id']}/shipping-labels", headers=headers
    )
    assert listed_labels.status_code == 200
    assert [item["id"] for item in listed_labels.json()] == [label["id"]]

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        shipment = (
            await session.execute(
                text(
                    "SELECT tracking_number, carrier_code, actual_shipping_cost_base "
                    "FROM shipments WHERE tenant_id = :tid AND order_id = :oid"
                ),
                {"tid": tenant_id, "oid": UUID(order["id"])},
            )
        ).first()
        assert shipment is not None
        assert shipment[0] == label["tracking_number"]
        assert shipment[1] == "sandbox_carrier"
        assert Decimal(str(shipment[2])) == Decimal(str(label["shipping_cost_estimate"]))

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
    assert {
        "order.routing_evaluated",
        "order.routing_hold_released",
        "order.shipping_label_booked",
    } <= actions

    duplicate = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=headers,
        json={"carrier_code": "sandbox_carrier", "mode": "sandbox"},
    )
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()["tracking_number"] != label["tracking_number"]


async def test_hold_override_requires_manager_role_and_reason(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id_str = await _register_owner(async_client)
    tenant_id = UUID(tenant_id_str)
    headers = {"Authorization": f"Bearer {token}"}
    seed = await _seed_order(runtime_session_factory, tenant_id)
    order = await _create_order(async_client, headers, seed)
    await _insert_profit_snapshot(
        runtime_session_factory,
        tenant_id,
        UUID(order["id"]),
        gross_revenue_base="500.00",
        net_contribution_margin_base="-30.00",
    )
    evaluated = await async_client.post(
        f"/api/v1/orders/{order['id']}/evaluate-routing", headers=headers
    )
    assert evaluated.json()["is_held"] is True

    finance_headers, _ = await _member_headers(
        runtime_session_factory, tenant_id, UserRole.FINANCE
    )
    finance_override = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=finance_headers,
        json={
            "carrier_code": "sandbox_carrier",
            "mode": "sandbox",
            "override_hold": True,
            "override_reason": "Finance wants to ship anyway",
        },
    )
    assert finance_override.status_code == 403
    assert finance_override.json()["code"] == "ERR_INSUFFICIENT_ROLE"

    manager_headers, _ = await _member_headers(
        runtime_session_factory, tenant_id, UserRole.MANAGER
    )
    missing_reason = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=manager_headers,
        json={
            "carrier_code": "sandbox_carrier",
            "mode": "sandbox",
            "override_hold": True,
        },
    )
    assert missing_reason.status_code == 422

    overridden = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=manager_headers,
        json={
            "carrier_code": "sandbox_carrier",
            "mode": "sandbox",
            "override_hold": True,
            "override_reason": "Manager accepted the loss to keep the SLA",
        },
    )
    assert overridden.status_code == 201, overridden.text
    assert overridden.json()["status"] == "created"


async def test_live_mode_refuses_carriers_without_a_live_contract(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id_str = await _register_owner(async_client)
    tenant_id = UUID(tenant_id_str)
    headers = {"Authorization": f"Bearer {token}"}
    seed = await _seed_order(runtime_session_factory, tenant_id)
    order = await _create_order(async_client, headers, seed)

    sandbox_live = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=headers,
        json={"carrier_code": "sandbox_carrier", "mode": "live"},
    )
    assert sandbox_live.status_code == 422
    assert sandbox_live.json()["code"] == "ERR_SANDBOX_CARRIER_LIVE_MODE"

    dhl_live = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=headers,
        json={"carrier_code": "dhl", "mode": "live"},
    )
    assert dhl_live.status_code == 422
    assert dhl_live.json()["code"] == "ERR_CARRIER_LIVE_MODE_UNSUPPORTED"

    # The same carriers stay bookable in sandbox mode.
    dhl_sandbox = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=headers,
        json={"carrier_code": "dhl", "mode": "sandbox"},
    )
    assert dhl_sandbox.status_code == 201, dhl_sandbox.text
    assert dhl_sandbox.json()["mode"] == "sandbox"


# ---------------------------------------------------------------------------
# Rule matching / recommendation + fallback margin math
# ---------------------------------------------------------------------------


async def test_rule_scope_routes_order_and_floor_holds_negative_margin(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_id_str = await _register_owner(async_client)
    tenant_id = UUID(tenant_id_str)
    headers = {"Authorization": f"Bearer {token}"}
    seed = await _seed_order(
        runtime_session_factory,
        tenant_id,
        country_code="SE",
        retail_price_usd="100.00",
        cost_price_base="40.00",
    )
    warehouse_id = seed["warehouse_id"]
    rule = await async_client.post(
        "/api/v1/orders/routing-rules",
        headers=headers,
        json={
            "name": "Sweden express lane",
            "priority": 10,
            "conditions": {
                "destination_countries": ["SE"],
                "order_modes": ["b2c_retail"],
                "max_weight_kg": "25.00",
            },
            "actions": {
                "warehouse_id": str(warehouse_id),
                "carrier_code": "yunexpress",
                "service_code": "YunExpress-Expedited",
                "add_tags": ["eu-fast-lane", "priority"],
            },
        },
    )
    assert rule.status_code == 201, rule.text
    rule_id = rule.json()["id"]

    order = await _create_order(async_client, headers, seed)
    evaluated = await async_client.post(
        f"/api/v1/orders/{order['id']}/evaluate-routing", headers=headers
    )
    assert evaluated.status_code == 201, evaluated.text
    body = evaluated.json()
    # Cost 40 vs price 100 per unit -> +60% contribution margin from the order's own
    # landed cost, so no snapshot row is required to route the order.
    assert Decimal(body["estimated_margin_pct"]) == Decimal("60.0000")
    assert body["matched_rule_ids"] == [rule_id]
    assert body["recommended_carrier"] == "yunexpress"
    assert body["recommended_service"] == "YunExpress-Expedited"
    assert body["recommended_warehouse_id"] == str(warehouse_id)
    assert body["applied_tags"] == ["eu-fast-lane", "priority"]
    assert body["is_held"] is False
    assert body["hold_reason"] is None

    # A floor rule above the order's margin holds it even with a positive margin.
    floor_rule = await async_client.post(
        "/api/v1/orders/routing-rules",
        headers=headers,
        json={
            "name": "Minimum 70% contribution margin",
            "priority": 5,
            "conditions": {
                "destination_countries": ["SE"],
                "min_margin_pct": "70.00",
            },
            "actions": {"hold_for_review": True, "add_tags": ["margin-floor"]},
        },
    )
    assert floor_rule.status_code == 201, floor_rule.text
    re_evaluated = await async_client.post(
        f"/api/v1/orders/{order['id']}/evaluate-routing", headers=headers
    )
    assert re_evaluated.status_code == 201, re_evaluated.text
    held = re_evaluated.json()
    assert held["is_held"] is True
    assert "below the 70% floor" in (held["hold_reason"] or "")
    assert set(held["matched_rule_ids"]) == {rule_id, floor_rule.json()["id"]}
    assert "margin-floor" in held["applied_tags"]

    # A destination that matches nothing at all leaves the order unrouted.
    other_seed = await _seed_order(runtime_session_factory, tenant_id, country_code="US")
    other_order = await _create_order(async_client, headers, other_seed)
    unmatched = await async_client.post(
        f"/api/v1/orders/{other_order['id']}/evaluate-routing", headers=headers
    )
    assert unmatched.status_code == 201, unmatched.text
    assert unmatched.json()["matched_rule_ids"] == []
    assert unmatched.json()["recommended_carrier"] is None
    assert unmatched.json()["is_held"] is False

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        evaluations = await session.scalar(
            text(
                "SELECT count(*) FROM order_routing_evaluations "
                "WHERE tenant_id = :tid AND order_id = :oid"
            ),
            {"tid": tenant_id, "oid": UUID(order["id"])},
        )
    assert evaluations == 2, "each evaluation is appended, never overwritten"


# ---------------------------------------------------------------------------
# Adapter request contracts (mocked httpx.MockTransport)
# ---------------------------------------------------------------------------


def _booking_request(
    *,
    carrier_code: str,
    credentials: dict[str, str] | None = None,
    mode: str = "live",
) -> CarrierBookingRequest:
    return CarrierBookingRequest(
        tenant_id=uuid4(),
        order_id=uuid4(),
        order_no="ORD-2026-0007",
        carrier_code=carrier_code,
        service_code="YunExpress-Expedited",
        mode=mode,
        destination_country="SE",
        destination_city="Stockholm",
        recipient_name="Nordic Buyer AB",
        declared_weight_kg=Decimal("2.400"),
        currency="USD",
        lines=(LabelLine(sku="SKU-LED-01", quantity=2),),
        sequence=0,
        credentials=credentials or {},
    )


async def test_yunexpress_adapter_builds_signed_request_and_parses_waybill() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=YUNEXPRESS_BODY)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = YunExpressAdapter(
            http_client=client, base_url="https://yune.test/lms/api/waybill"
        )
        result = await adapter.book(
            _booking_request(
                carrier_code="yunexpress",
                credentials={"customer_code": "YUN-001", "api_secret": "s3cr3t-value"},
            )
        )

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://yune.test/lms/api/waybill"
    assert request.method == "POST"
    expected_auth = "Basic " + base64.b64encode(b"YUN-001&s3cr3t-value").decode()
    assert request.headers["authorization"] == expected_auth
    payload = json.loads(request.content)
    assert payload["customerCode"] == "YUN-001"
    assert payload["orderNumber"] == "ORD-2026-0007"
    assert payload["shippingMethodCode"] == "YunExpress-Expedited"
    assert payload["shipTo"] == {
        "CountryCode": "SE",
        "City": "Stockholm",
        "Name": "Nordic Buyer AB",
    }
    assert payload["parcels"] == [{"SKU": "SKU-LED-01", "Quantity": 2}]
    assert payload["weight"] == "2.400"
    assert payload["labelSize"] == "4x6"

    assert result.waybill_number == "YT2626000123456"
    assert result.tracking_number == "YT2626000123456"
    assert result.shipping_cost_estimate == Decimal("18.42")
    assert result.pdf_bytes.startswith(b"%PDF-")
    assert "YT2626000123456" in _pdf_text(result.pdf_bytes)
    # The api secret must never be echoed back in the adapter result.
    assert "s3cr3t-value" not in json.dumps(result.raw_response)


async def test_yunexpress_adapter_maps_http_failure_to_structured_app_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream boom s3cr3t-value")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = YunExpressAdapter(http_client=client, base_url="https://yune.test/api")
        with pytest.raises(AppError) as exc_info:
            await adapter.book(
                _booking_request(
                    carrier_code="yunexpress",
                    credentials={"customer_code": "YUN-001", "api_secret": "s3cr3t-value"},
                )
            )
    error = exc_info.value
    assert error.status_code == 502
    assert error.code == "ERR_CARRIER_UPSTREAM"
    assert "s3cr3t-value" not in error.detail


async def test_yunexpress_adapter_requires_live_credentials() -> None:
    adapter = YunExpressAdapter(base_url="https://yune.test/api")
    with pytest.raises(AppError) as exc_info:
        await adapter.book(_booking_request(carrier_code="yunexpress", credentials={}))
    assert exc_info.value.code == "ERR_CARRIER_UPSTREAM"
    assert "YUNEXPRESS_CUSTOMER_CODE" in exc_info.value.detail


async def test_fourpx_adapter_signs_outbound_create_and_parses_tracking() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=FOURPX_BODY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = FourPxAdapter(
            http_client=client, base_url="https://4px.test/router/api/service"
        )
        result = await adapter.book(
            _booking_request(
                carrier_code="4px",
                credentials={"app_key": "px-app-key", "app_secret": "px-app-secret"},
            )
        )

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://4px.test/router/api/service"
    form = {
        key: values[0]
        for key, values in urllib.parse.parse_qs(
            request.content.decode("utf-8"), keep_blank_values=True
        ).items()
    }
    assert form["method"] == "fu.wms.outbound.create"
    assert form["app_key"] == "px-app-key"
    assert form["format"] == "json"
    unsigned = {key: value for key, value in form.items() if key != "sign"}
    expected_sign = hashlib.md5(  # noqa: S324 - mirrors the carrier's MD5 contract
        ("px-app-secret"
        + "".join(f"{key}{unsigned[key]}" for key in sorted(unsigned) if unsigned[key])
        + "px-app-secret").encode()
    ).hexdigest().upper()
    assert form["sign"] == expected_sign
    parcel = json.loads(form["data"])
    assert parcel["ref_no"] == "ORD-2026-0007"
    assert parcel["to_country_code"] == "SE"
    assert parcel["items"] == [{"sku_code": "SKU-LED-01", "qty": 2}]
    # The app secret only ever enters the signature, never the request body.
    assert "px-app-secret" not in request.content.decode("utf-8")

    assert result.tracking_number == "4PX3000123456CN"
    assert result.waybill_number == "4PX3000123456CN"
    assert result.shipping_cost_estimate == Decimal("16.35")
    assert result.pdf_bytes.startswith(b"%PDF-")


async def test_fourpx_adapter_rejects_upstream_error_envelope() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultCode": "9", "resultMsg": "bad request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = FourPxAdapter(http_client=client, base_url="https://4px.test/api")
        with pytest.raises(AppError) as exc_info:
            await adapter.book(
                _booking_request(
                    carrier_code="4px",
                    credentials={"app_key": "k", "app_secret": "s"},
                )
            )
    assert exc_info.value.code == "ERR_CARRIER_UPSTREAM"


async def test_sandbox_adapter_is_deterministic_and_renders_4x6_pdf() -> None:
    adapter = SandboxCarrierAdapter()
    request = _booking_request(carrier_code="sandbox_carrier", mode="sandbox")
    first = await adapter.book(request)
    second = await adapter.book(request)
    assert first.tracking_number == second.tracking_number
    assert first.waybill_number == second.waybill_number
    assert first.pdf_bytes == second.pdf_bytes

    page = PdfReader(io.BytesIO(first.pdf_bytes)).pages[0]
    assert round(float(page.mediabox.width)) == THERMAL_LABEL_WIDTH_PT
    assert round(float(page.mediabox.height)) == THERMAL_LABEL_HEIGHT_PT
    text_of_label = "\n".join(p.extract_text() or "" for p in (page,))
    assert first.tracking_number in text_of_label
    assert "ORD-2026-0007" in text_of_label
    assert "SKU-LED-01" in text_of_label
    assert "2.400 kg" in text_of_label

    # A different sequence yields a different waybill (re-bookings never collide).
    other = await adapter.book(replace(request, sequence=1))
    assert other.waybill_number != first.waybill_number


def test_thermal_label_render_is_byte_identical_for_identical_context() -> None:
    ctx = ThermalLabelContext(
        carrier_code="dhl",
        service_code="DHL-Express-Worldwide",
        order_no="ORD-2026-9999",
        tracking_number="DHL123456789",
        waybill_number="DHLWB123456789",
        destination_country="DE",
        destination_city="Berlin",
        recipient_name="Berlin Lighting GmbH",
        weight_kg=Decimal("7.250"),
        currency="USD",
        shipping_cost_estimate=Decimal("61.73"),
        lines=(LabelLine(sku="SKU-A", quantity=3), LabelLine(sku="SKU-B", quantity=1)),
        mode="sandbox",
    )
    assert render_thermal_label_pdf(ctx) == render_thermal_label_pdf(ctx)


# ---------------------------------------------------------------------------
# Tenant isolation (FORCE RLS)
# ---------------------------------------------------------------------------


async def test_routing_and_label_tables_are_force_rls_tenant_isolated(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token_a, _, tenant_a_str = await _register_owner(async_client)
    tenant_a = UUID(tenant_a_str)
    headers_a = {"Authorization": f"Bearer {token_a}"}
    rule = await async_client.post(
        "/api/v1/orders/routing-rules",
        headers=headers_a,
        json={
            "name": "Tenant A only",
            "conditions": {"destination_countries": ["SE"]},
            "actions": {"carrier_code": "4px"},
        },
    )
    assert rule.status_code == 201, rule.text

    token_b, _, tenant_b_str = await _register_owner(async_client)
    tenant_b = UUID(tenant_b_str)
    headers_b = {"Authorization": f"Bearer {token_b}"}

    foreign_rules = await async_client.get("/api/v1/orders/routing-rules", headers=headers_b)
    assert foreign_rules.status_code == 200
    assert foreign_rules.json() == []

    cross_tenant_patch = await async_client.patch(
        f"/api/v1/orders/routing-rules/{rule.json()['id']}",
        headers=headers_b,
        json={"priority": 1},
    )
    assert cross_tenant_patch.status_code == 404

    # Direct runtime reads without a tenant GUC see zero rows on every new table.
    async with system_session(runtime_session_factory) as session:
        for table in ROUTING_TABLES:
            rows = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar()
            assert rows == 0, f"{table} leaked rows without app.current_tenant_id"

    # Cross-tenant INSERT is rejected by the RESTRICTIVE RLS policy itself.
    with pytest.raises(DBAPIError) as exc_info:
        async with tenant_session(runtime_session_factory, tenant_b) as session:
            await session.execute(
                text(
                    "INSERT INTO order_routing_rules (tenant_id, name) "
                    "VALUES (:tid, 'smuggled rule')"
                ),
                {"tid": tenant_a},
            )
    assert "row-level security" in str(exc_info.value).lower()

    # Tenant B cannot evaluate (or book) tenant A's order.
    seed = await _seed_order(runtime_session_factory, tenant_a)
    order = await _create_order(async_client, headers_a, seed)
    foreign_eval = await async_client.post(
        f"/api/v1/orders/{order['id']}/evaluate-routing", headers=headers_b
    )
    assert foreign_eval.status_code == 404
    foreign_booking = await async_client.post(
        f"/api/v1/orders/{order['id']}/shipping-label",
        headers=headers_b,
        json={"carrier_code": "sandbox_carrier", "mode": "sandbox"},
    )
    assert foreign_booking.status_code == 404
    missing_label = await async_client.get(
        f"/api/v1/orders/{order['id']}/shipping-labels/{uuidlib.uuid4()}/pdf",
        headers=headers_a,
    )
    assert missing_label.status_code == 404
    assert missing_label.json()["code"] == "ERR_SHIPPING_LABEL_NOT_FOUND"
