from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from erp_core.channels.ecommerce.base import resolve_routing_tenant
from erp_core.channels.ecommerce.connectors_router import router as connectors_router
from erp_core.channels.ecommerce.shoplazza import (
    verify_shoplazza_oauth_hmac,
    verify_shoplazza_webhook,
)
from erp_core.channels.ecommerce.shopline import (
    LeakyBucketLimiter,
    ShoplineAdapter,
    verify_shopline_webhook,
)
from erp_core.channels.ecommerce.woocommerce import (
    WooCommerceAdapter,
    build_wc_authorize_url,
    verify_woocommerce_webhook,
)
from erp_core.shared.enums import OrderMode
from erp_core.shared.errors import register_exception_handlers


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

    notes: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Basic ")
        if request.url.path == "/wp-json/wc/v3/orders":
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
                            {"key": "_wholesale_po_number", "value": "PO-DE-2026-77"},
                            {"key": "_tracking_number", "value": "DHL-DE-4455"},
                        ],
                        "line_items": [
                            {
                                "sku": "LED-STRIP-5M-IP65",
                                "name": "5M LED Strip",
                                "quantity": 10,
                                "total": "200.00",
                            }
                        ],
                    }
                ],
            )
        if request.url.path == "/wp-json/wc/v3/orders/9021/notes":
            body = json.loads(request.content)
            notes.append(body)
            return httpx.Response(201, json={"id": 44, **body})
        if request.url.path == "/wp-json/wc/v3/orders/9021/fulfillments":
            return httpx.Response(201, json={"id": 55})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = WooCommerceAdapter(
            store_url="https://qz-lighting.com",
            consumer_key="ck_test",
            consumer_secret="cs_test",
            meta_field_map={
                "po_number": "_wholesale_po_number",
                "tracking_number": "_tracking_number",
            },
            http_client=client,
        )
        orders, cursor = await adapter.fetch_orders_updated_since(
            datetime.now(UTC) - timedelta(hours=1)
        )
        assert cursor is None
        assert len(orders) == 1
        assert orders[0].order_mode == OrderMode.B2B_WHOLESALE
        assert orders[0].b2b_company is not None
        assert orders[0].b2b_company.legal_name == "EuroLED GmbH"
        assert orders[0].channel_metadata["po_number"] == "PO-DE-2026-77"
        assert orders[0].subtotal_amount == 200

        await adapter.create_fulfillment("9021", "DHL-DE-9999", "DHL", notify_customer=True)
    assert len(notes) == 1
    assert notes[0]["customer_note"] is True
    assert "DHL-DE-9999" in str(notes[0]["note"])

    raw = b'{"id": 9021, "status": "processing"}'
    secret = "wc-wh-secret"
    signature = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    assert verify_woocommerce_webhook(raw, {"X-WC-Webhook-Signature": signature}, secret) is True
    assert verify_woocommerce_webhook(raw, {"X-WC-Webhook-Signature": "bad"}, secret) is False


async def test_shopline_b2b_po_and_leaky_bucket() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sl_tok_3yr"
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
                            "b2b_company_location": {
                                "company_name": "Pacific Lighting Pty",
                                "country_code": "AU",
                            },
                            "updated_at": "2026-11-19T01:05:00Z",
                            "customer": {
                                "email": "ops@pacificled.au",
                                "first_name": "Liam",
                                "last_name": "Chen",
                            },
                            "line_items": [
                                {
                                    "sku": "LED-STRIP-5M-IP65",
                                    "title": "5M Strip",
                                    "quantity": 25,
                                    "price": "20.00",
                                }
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    bucket = LeakyBucketLimiter(redis_client=None)
    allowed, wait_ms = await bucket.acquire("shopline:store1", rate_per_sec=4.0, capacity=40)
    assert (allowed, wait_ms) == (True, 0)
    assert (await bucket.acquire("shopline:tiny", rate_per_sec=1.0, capacity=1)) == (True, 0)
    throttled, wait_ms = await bucket.acquire("shopline:tiny", rate_per_sec=1.0, capacity=1)
    assert throttled is False and wait_ms > 0

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ShoplineAdapter(
            store_domain="qz-led.myshopline.com",
            access_token="sl_tok_3yr",
            leaky_bucket=bucket,
            http_client=client,
        )
        orders, _ = await adapter.fetch_orders_updated_since(datetime.now(UTC) - timedelta(hours=1))
    assert len(orders) == 1
    assert orders[0].order_mode == OrderMode.B2B_WHOLESALE
    assert orders[0].channel_metadata["po_number"] == "PO-SL-B2B-09"
    assert orders[0].b2b_company is not None
    assert orders[0].b2b_company.legal_name == "Pacific Lighting Pty"
    assert orders[0].line_items[0].line_total == 500

    raw = b'{"id":"sl_ord_301"}'
    secret = "sl_app_secret"
    signature = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    assert verify_shopline_webhook(raw, {"X-Shopline-Hmac-Sha256": signature}, secret) is True


async def test_shoplazza_dual_hmac_and_deduplication_idempotency(
    runtime_session_factory: async_sessionmaker,
) -> None:
    secret = "shoplazza_client_secret_99"
    query = {"code": "authcode1", "shop": "qz.myshoplaza.com", "timestamp": "1763510000"}
    canonical = "code=authcode1&shop=qz.myshoplaza.com&timestamp=1763510000"
    hex_hmac = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert verify_shoplazza_oauth_hmac({**query, "hmac": hex_hmac}, secret) is True
    assert verify_shoplazza_oauth_hmac({**query, "hmac": "wrong"}, secret) is False

    raw = b'{"id":"spz_991","total_price":"99.00"}'
    body_hmac = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    assert verify_shoplazza_webhook(raw, {"X-Shoplazza-Hmac-Sha256": body_hmac}, secret) is True
    assert (
        verify_shoplazza_webhook(raw + b" ", {"X-Shoplazza-Hmac-Sha256": body_hmac}, secret)
        is False
    )

    monkeypatch_secret = os.environ.get("SHOPLAZZA_CLIENT_SECRET")
    os.environ["SHOPLAZZA_CLIENT_SECRET"] = secret
    dedup_id = f"spz-dedup-{uuid4().hex}"
    app = FastAPI()
    app.include_router(connectors_router, prefix="/api/v1")
    app.state.session_factory = runtime_session_factory
    register_exception_handlers(app)
    admin_engine = create_async_engine(
        os.environ.get(
            "DATABASE_ADMIN_URL",
            "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
        )
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            headers = {
                "X-Shoplazza-Hmac-Sha256": body_hmac,
                "X-Shoplazza-Deduplication-ID": dedup_id,
                "X-Shoplazza-Topic": "orders/create",
                "X-Shoplazza-Shop-Domain": "qz.myshoplaza.com",
            }
            first = await client.post("/api/v1/webhooks/shoplazza", content=raw, headers=headers)
            second = await client.post("/api/v1/webhooks/shoplazza", content=raw, headers=headers)
            tampered = await client.post(
                "/api/v1/webhooks/shoplazza",
                content=raw + b" ",
                headers={**headers, "X-Shoplazza-Deduplication-ID": f"{dedup_id}-x"},
            )
        assert (first.status_code, second.status_code, tampered.status_code) == (200, 200, 401)
        assert first.json()["deduplicated"] is False
        assert second.json()["deduplicated"] is True

        async with admin_engine.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM raw_webhook_events WHERE channel_type = 'shoplazza' "
                        "AND external_event_id = :eid"
                    ),
                    {"eid": dedup_id},
                )
            ).scalar_one()
        assert count == 1
        async with runtime_session_factory() as session:
            assert await resolve_routing_tenant(session, "shoplazza", "qz.myshoplaza.com") is None
    finally:
        if monkeypatch_secret is None:
            os.environ.pop("SHOPLAZZA_CLIENT_SECRET", None)
        else:
            os.environ["SHOPLAZZA_CLIENT_SECRET"] = monkeypatch_secret
        async with admin_engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM raw_webhook_events WHERE channel_type = 'shoplazza' "
                    "AND external_event_id = :eid"
                ),
                {"eid": dedup_id},
            )
        await admin_engine.dispose()


@pytest.mark.parametrize(
    ("platform", "verifier"),
    [("woocommerce", verify_woocommerce_webhook), ("shopline", verify_shopline_webhook)],
)
def test_storefront_webhooks_require_a_non_empty_secret(platform: str, verifier) -> None:
    raw = b'{"id":1}'
    signature = base64.b64encode(b"\x00" * 32).decode()
    header = {
        "woocommerce": "X-WC-Webhook-Signature",
        "shopline": "X-Shopline-Hmac-Sha256",
    }[platform]
    assert verifier(raw, {header: signature}, "") is False
