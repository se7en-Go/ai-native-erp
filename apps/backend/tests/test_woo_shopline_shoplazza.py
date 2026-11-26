from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from erp_core.channels.ecommerce.base import resolve_routing_tenant
from erp_core.channels.ecommerce.connectors_router import (
    WooAuthCallbackPayload,
    _normalize_route_candidates,
)
from erp_core.channels.ecommerce.connectors_router import (
    router as connectors_router,
)
from erp_core.channels.ecommerce.shoplazza import (
    ShoplazzaAdapter,
    verify_shoplazza_oauth_hmac,
    verify_shoplazza_webhook,
)
from erp_core.channels.ecommerce.shopline import (
    LeakyBucketLimiter,
    ShoplineAdapter,
    verify_shopline_webhook,
)
from erp_core.channels.ecommerce.tiktok_shop import TikTokShopCoreAdapter
from erp_core.channels.ecommerce.woocommerce import (
    WooCommerceAdapter,
    build_wc_authorize_url,
    normalize_store_url,
    verify_woocommerce_webhook,
)
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus
from erp_core.shared.errors import register_exception_handlers
from erp_core.worker_main import _extract_storefront_account_candidates


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


async def test_woo_shopline_shoplazza_tiktok_shopify_parity() -> None:
    # WooCommerce: no double-count, status mappings, fulfillment_id + variation lookup
    woo_calls: list[httpx.Request] = []

    def woo_handler(request: httpx.Request) -> httpx.Response:
        woo_calls.append(request)
        if request.url.path == "/wp-json/wc/v3/products/501/variations":
            return httpx.Response(200, json=[{"id": 601, "sku": "WOO-VAR"}])
        if request.url.path == "/wp-json/wc/v3/products/501/variations/601":
            return httpx.Response(200, json={"id": 601, "stock_quantity": 9})
        if request.url.path == "/wp-json/wc/v3/orders/101/notes":
            return httpx.Response(201, json={"id": 1})
        if request.url.path == "/wp-json/wc/v3/orders/101/fulfillments":
            return httpx.Response(201, json={"id": 77})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(woo_handler)) as client:
        woo = WooCommerceAdapter("https://woo.example.com", "ck", "cs", http_client=client)
        woo_completed = woo._normalize_order(
            {
                "id": 1,
                "status": "completed",
                "total": "100.00",
                "refunded_amount": "10.00",
                "refunds": [{"id": 1, "total": "-10.00"}],
                "date_modified_gmt": "2026-11-23T08:00:00",
                "line_items": [{"sku": "SKU-1", "quantity": 1, "total": "100.00"}],
            }
        )
        assert woo_completed.fulfillment_status == FulfillmentStatus.DELIVERED
        assert woo_completed.payment_status == PaymentStatus.PAID
        assert woo_completed.channel_metadata["refunded_amount"] == "10.00"

        woo_partial = woo._normalize_order(
            {
                "id": 2,
                "status": "partially-refunded",
                "total": "100.00",
                "refunded_amount": "20.00",
                "date_modified_gmt": "2026-11-23T08:00:00",
                "line_items": [{"sku": "SKU-1", "quantity": 1, "total": "100.00"}],
            }
        )
        assert woo_partial.payment_status == PaymentStatus.PAID

        woo_cancelled = woo._normalize_order(
            {
                "id": 3,
                "status": "cancelled",
                "total": "100.00",
                "date_paid_gmt": "2026-11-23T07:00:00",
                "date_modified_gmt": "2026-11-23T08:00:00",
                "line_items": [{"sku": "SKU-1", "quantity": 1, "total": "100.00"}],
            }
        )
        assert woo_cancelled.fulfillment_status == FulfillmentStatus.CANCELLED
        assert woo_cancelled.payment_status == PaymentStatus.REFUNDED

        woo_ful = await woo.create_fulfillment(
            "101", "TRK-1", "DHL", tracking_url="https://track.test/1"
        )
        assert woo_ful["fulfillment_id"] == "77"
        woo_ful_req = json.loads(
            next(r for r in woo_calls if r.url.path.endswith("/fulfillments")).content
        )
        assert woo_ful_req["tracking_url"] == "https://track.test/1"

        woo_inv = await woo.push_inventory_level(
            external_sku="WOO-VAR", quantity=9, external_product_id="501"
        )
        assert woo_inv["variant_id"] == "601"

    # SHOPLINE & Shoplazza: options synthesis, inventory tracking, omit null tracking_url
    sl_bodies: list[dict[str, object]] = []

    def sl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/products/products.json"):
            body = json.loads(request.content)
            sl_bodies.append(body)
            return httpx.Response(
                201,
                json={"product": {"id": "sl-p1", "variants": [{"id": "v1", "sku": "S-1"}]}},
            )
        if request.url.path.endswith("/fulfillments.json"):
            body = json.loads(request.content)
            sl_bodies.append(body)
            return httpx.Response(201, json={"fulfillment": {"id": "sl-f1"}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(sl_handler)) as client:
        sl = ShoplineAdapter("qz.myshopline.com", "tok", http_client=client)
        sl_partial = sl._normalize_order(
            {
                "id": "sl-p",
                "financial_status": "partially_refunded",
                "total_price": "50.00",
                "total_refunded": "10.00",
                "updated_at": "2026-11-23T08:00:00Z",
                "line_items": [{"sku": "S-1", "quantity": 1, "price": "50.00"}],
            }
        )
        assert sl_partial.payment_status == PaymentStatus.PAID
        sl_can = sl._normalize_order(
            {
                "id": "sl-c",
                "financial_status": "paid",
                "cancelled_at": "2026-11-23T09:00:00Z",
                "total_price": "50.00",
                "updated_at": "2026-11-23T08:00:00Z",
                "line_items": [{"sku": "S-1", "quantity": 1, "price": "50.00"}],
            }
        )
        assert sl_can.fulfillment_status == FulfillmentStatus.CANCELLED
        assert sl_can.payment_status == PaymentStatus.REFUNDED

        await sl.publish_listing(
            sku="S-1",
            title="Strip",
            description="LED",
            bullet_points=[],
            price_usd=Decimal("10.00"),
            variants=[{"external_sku": "S-1", "option_values": {"Color": "Warm"}}],
        )
        assert sl_bodies[0]["product"]["options"] == [{"name": "Color", "values": ["Warm"]}]
        assert sl_bodies[0]["product"]["variants"][0]["inventory_tracker"] is True

        sl_ful = await sl.create_fulfillment("sl-1", "TRK-SL", "UPS", tracking_url=None)
        assert sl_ful["fulfillment_id"] == "sl-f1"
        assert "tracking_url" not in sl_bodies[1]

    sz_bodies: list[dict[str, object]] = []

    def sz_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/products"):
            body = json.loads(request.content)
            sz_bodies.append(body)
            return httpx.Response(
                201,
                json={"product": {"id": "sz-p1", "variants": [{"id": "v1", "sku": "Z-1"}]}},
            )
        if request.url.path.endswith("/fulfillments"):
            body = json.loads(request.content)
            sz_bodies.append(body)
            return httpx.Response(201, json={"id": "sz-f1"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(sz_handler)) as client:
        sz = ShoplazzaAdapter("qz.myshoplaza.com", "tok", http_client=client)
        sz_pp = sz._normalize_order(
            {
                "id": "sz-1",
                "financial_status": "partially_paid",
                "fulfillment_status": "partial",
                "total_price": "80.00",
                "updated_at": "2026-11-23T08:00:00Z",
                "line_items": [{"sku": "Z-1", "quantity": 1, "price": "80.00"}],
            }
        )
        assert sz_pp.payment_status == PaymentStatus.PARTIALLY_PAID
        assert sz_pp.fulfillment_status == FulfillmentStatus.PARTIALLY_SHIPPED

        await sz.publish_listing(
            sku="Z-1",
            title="Lamp",
            description="LED",
            bullet_points=[],
            price_usd=Decimal("10.00"),
            variants=[{"external_sku": "Z-1", "option_values": {"Watt": "20W"}}],
        )
        assert sz_bodies[0]["product"]["options"] == [{"name": "Watt", "values": ["20W"]}]
        assert sz_bodies[0]["product"]["variants"][0]["inventory_tracking"] is True

        sz_ful = await sz.create_fulfillment("sz-1", "TRK-SZ", "DHL", tracking_url=None)
        assert sz_ful["fulfillment_id"] == "sz-f1"
        assert "tracking_url" not in sz_bodies[1]

    # TikTok Shop: auto-resolve package_id in create_fulfillment + UNPAID/REFUNDED normalization
    def tt_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/order/202309/orders":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"orders": [{"id": "TT-1", "packages": [{"id": "pkg-auto"}]}]},
                },
            )
        if request.url.path == "/fulfillment/202309/packages/pkg-auto/ship":
            return httpx.Response(200, json={"code": 0, "data": {"shipped": True}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(tt_handler)) as client:
        tt = TikTokShopCoreAdapter("ak", "as", "tok", "cipher", http_client=client)
        tt_unpaid = tt._normalize_order(
            {
                "id": "tt-u",
                "status": "unpaid",
                "update_time": 1763884800,
                "payment": {"total_amount": "40.00"},
                "line_items": [{"seller_sku": "SKU-1", "quantity": 1, "sale_price": "40.00"}],
            }
        )
        assert tt_unpaid.fulfillment_status == FulfillmentStatus.PENDING_PAYMENT
        assert tt_unpaid.payment_status == PaymentStatus.UNPAID

        tt_ful = await tt.create_fulfillment("TT-1", "TRK-TT", "USPS")
        assert tt_ful["fulfillment_id"] == "pkg-auto"
        assert tt_ful["package_id"] == "pkg-auto"


async def test_woocommerce_real_world_fallback_modified_after_local_scheme_and_ping() -> None:
    seen_requests: list[tuple[str, str, dict[str, object] | None, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen_requests.append((request.method, request.url.path, body, str(request.url.params)))
        if request.method == "GET" and request.url.path == "/wp-json/wc/v3/orders":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/wp-json/wc/v3/orders/42/notes":
            return httpx.Response(201, json={"id": 888, "note": (body or {}).get("note")})
        if request.method == "POST" and request.url.path == "/wp-json/wc/v3/orders/42/fulfillments":
            return httpx.Response(404, json={"code": "rest_no_route"})
        if request.method == "PUT" and request.url.path == "/wp-json/wc/v3/orders/42":
            return httpx.Response(200, json={"id": 42, "status": "completed"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        woo = WooCommerceAdapter(
            "https://woo-fallback.example.com", "ck", "cs", http_client=client
        )
        empty = woo._normalize_order(
            {
                "id": 41,
                "status": "processing",
                "total": "16.90",
                "billing": {"email": " ", "phone": ""},
                "date_modified_gmt": "2026-11-26T01:00:00",
                "line_items": [{"sku": "WOO-SKU", "quantity": 1, "total": "16.90"}],
            }
        )
        assert empty.customer.email is None
        assert empty.customer.phone_e164 is None

        orders, _ = await woo.fetch_orders_updated_since(datetime(2026, 11, 26, 0, 0, tzinfo=UTC))
        assert orders == []
        assert "modified_after=" in seen_requests[0][3]
        assert "after=" not in seen_requests[0][3].replace("modified_after=", "")

        res = await woo.create_fulfillment(
            "42", "DHL-WOO-1", "DHL", tracking_url="https://track.example/1"
        )
        assert res["fulfillment_id"] == "wc-42-888"
        assert res["status"] == "completed"
        put_req = next(item for item in seen_requests if item[0] == "PUT")
        assert put_req[2] is not None
        assert put_req[2]["status"] == "completed"
        meta_keys = {entry["key"]: entry["value"] for entry in put_req[2]["meta_data"]}  # type: ignore[index]
        assert meta_keys["_tracking_number"] == "DHL-WOO-1"
        assert meta_keys["_tracking_provider"] == "DHL"
        assert meta_keys["_wc_shipment_tracking_items"][0]["tracking_number"] == "DHL-WOO-1"

    assert (
        normalize_store_url("http://erp_woo_wordpress:80", allow_local=True)
        == "http://erp_woo_wordpress:80"
    )
    with pytest.raises(Exception) as unsafe_err:
        normalize_store_url("http://127.0.0.1:8088", allow_local=False)
    assert getattr(unsafe_err.value, "code", None) == "ERR_WOOCOMMERCE_UNSAFE_HOST"

    assert _normalize_route_candidates("https://woo.example.com/") == [
        "https://woo.example.com/",
        "woo.example.com",
    ]
    assert _extract_storefront_account_candidates(
        "",
        {"shop_domain": "http://127.0.0.1:8088/"},
        {},
    ) == ["http://127.0.0.1:8088/", "127.0.0.1:8088"]

    app = FastAPI()
    app.include_router(connectors_router, prefix="/api/v1")
    app.state.session_factory = lambda: None  # ping path does not query DB
    register_exception_handlers(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as api_client:
        ping = await api_client.post(
            "/api/v1/webhooks/woocommerce",
            content=b"webhook_id=7",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert ping.status_code == 200
        assert ping.json() == {"code": 200, "status": "ping_ok", "deduplicated": False}

        bad_json = await api_client.post(
            "/api/v1/webhooks/woocommerce",
            content=b'{"id": 7}',
            headers={"Content-Type": "application/json"},
        )
        assert bad_json.status_code == 401


async def test_woocommerce_publish_listing_uploads_image_blobs_to_wp_media() -> None:
    calls: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        if request.method == "POST" and request.url.path == "/wp-json/wp/v2/media":
            calls.append((request.method, request.url.path, headers, None))
            assert headers["authorization"] == (
                "Basic " + base64.b64encode(b"woo_admin:app-pass").decode()
            )
            assert 'filename="led_high_bay.png"' in headers["content-disposition"]
            assert headers["content-type"] == "image/png"
            assert request.content == b"\x89PNG\r\n\x1a\nfake-png"
            return httpx.Response(201, json={"id": 901, "source_url": "https://woo.example.com/led.png"})
        if request.method == "POST" and request.url.path == "/wp-json/wc/v3/products":
            body = json.loads(request.content)
            calls.append((request.method, request.url.path, headers, body))
            assert body["images"] == [{"id": 901, "alt": "100W LED"}]
            return httpx.Response(201, json={"id": 501, "status": "publish", **body})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        woo = WooCommerceAdapter(
            "https://woo.example.com",
            "ck_test",
            "cs_test",
            wp_username="woo_admin",
            wp_app_password="app-pass",
            http_client=client,
        )
        receipt = await woo.publish_listing(
            sku="WOO-IMG-100W",
            title="100W Industrial LED High Bay",
            description="Die-cast aluminum UFO fixture.",
            bullet_points=["IP65"],
            price_usd=Decimal("16.90"),
            quantity=10,
            image_blobs=[
                {
                    "filename": "../led high bay.png",
                    "mime_type": "image/png",
                    "data": b"\x89PNG\r\n\x1a\nfake-png",
                    "alt_text": "100W LED",
                }
            ],
        )
    assert receipt.external_product_id == "501"
    assert [c[1] for c in calls] == ["/wp-json/wp/v2/media", "/wp-json/wc/v3/products"]

    payload = WooAuthCallbackPayload(
        store_url="https://shop.example.com",
        consumer_key="ck_1",
        consumer_secret="cs_1",
        wp_username="woo_admin",
        wp_app_password="app-pass-value",
    )
    assert payload.wp_username == "woo_admin"
    assert payload.wp_app_password == "app-pass-value"

