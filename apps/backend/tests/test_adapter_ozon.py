"""Zero-network unit tests for the Ozon Seller API adapter.

Every route is served by ``httpx.MockTransport``: no request leaves the process, and each
test pins the exact wire body Ozon would receive (headers, money strings, cursors).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from erp_core.catalog.sourcing_service import (
    MarketplaceImageBlob,
    MarketplaceListingPayload,
    SourcingMoqTier,
    SourcingOptionSpec,
)
from erp_core.channels.ecommerce.ozon import (
    OZON_FBO_ORDER_LIST_PATH,
    OZON_FBS_ORDER_LIST_PATH,
    OZON_FBS_POSTING_GET_PATH,
    OZON_FBS_SHIP_PATH,
    OZON_PICTURE_IMPORT_PATH,
    OZON_PRODUCT_IMPORT_INFO_PATH,
    OZON_PRODUCT_IMPORT_PATH,
    OZON_PRODUCT_INFO_LIST_PATH,
    OZON_PRODUCT_LIST_PATH,
    OZON_PRODUCT_UPDATE_PATH,
    OZON_STOCK_IMPORT_PATH,
    OzonAdapter,
    verify_ozon_webhook,
)
from erp_core.shared.enums import FulfillmentStatus, IdentityType, PaymentStatus
from erp_core.shared.errors import AppError

CLIENT_ID = "123456"
API_KEY = "ozon-api-key-secret-value"
BASE_URL = "https://ozon.test"
SINCE = datetime(2024, 5, 1, tzinfo=UTC)
WEBHOOK_SECRET = "ozon-webhook-secret-value"

Handler = Callable[[httpx.Request], httpx.Response]
Route = Callable[[httpx.Request], httpx.Response]


def _adapter(handler: Handler, **overrides: Any) -> OzonAdapter:
    kwargs: dict[str, Any] = {
        "client_id": CLIENT_ID,
        "api_key": API_KEY,
        "warehouse_id": "1020000123456789",
        "endpoint_url": BASE_URL,
        "transport": httpx.MockTransport(handler),
        "timeout_seconds": 5.0,
    }
    kwargs.update(overrides)
    return OzonAdapter(**kwargs)


def _json(payload: Any, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _body(request: httpx.Request) -> dict[str, Any]:
    raw = request.content.decode("utf-8") if request.content else "{}"
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return payload


def _assert_ozon_headers(request: httpx.Request) -> None:
    assert request.headers["Client-Id"] == CLIENT_ID
    assert request.headers["Api-Key"] == API_KEY
    assert request.headers["Content-Type"] == "application/json"
    assert API_KEY not in request.url.path


class FakeOzon:
    """Deterministic route table standing in for every Ozon endpoint."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Route] = {}
        self.fallback: Route | None = None

    def on(self, path: str, route: Route) -> FakeOzon:
        self.routes[path] = route
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        _assert_ozon_headers(request)
        route = self.routes.get(request.url.path)
        if route is None:
            if self.fallback is not None:
                return self.fallback(request)
            raise AssertionError(f"unexpected Ozon call: {request.method} {request.url.path}")
        return route(request)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def body_at(self, path: str) -> dict[str, Any]:
        for request in self.requests:
            if request.url.path == path:
                return _body(request)
        raise AssertionError(f"no request recorded for {path}")


def _posting(**overrides: Any) -> dict[str, Any]:
    posting: dict[str, Any] = {
        "posting_number": "12345-0001-1",
        "order_id": 987654321,
        "order_number": "12345-0001",
        "status": "awaiting_packaging",
        "created_at": "2024-05-01T10:00:00Z",
        "in_process_at": "2024-05-01T10:05:00Z",
        "shipment_date": "2024-05-02T09:00:00Z",
        "delivery_method": {"id": 55, "name": "Courier", "warehouse_id": 1020000123456789},
        "products": [
            {
                "sku": 4100111,
                "name": "LED Panel 40W",
                "offer_id": "LED-40W",
                "quantity": 3,
                "price": "1290.00",
                "currency_code": "RUB",
            },
            {
                "sku": 4100112,
                "name": "Mounting Kit",
                "offer_id": "MNT-KIT",
                "quantity": 1,
                "price": "99.50",
                "currency_code": "RUB",
            },
        ],
        "analytics_data": {"region": "Moscow", "is_express": False},
        "financial_data": {
            "products": [
                {
                    "product_id": 4100111,
                    "offer_id": "LED-40W",
                    "currency_code": "RUB",
                    "commission_amount": "129.005",
                    "total_discount_value": "0",
                },
                {
                    "product_id": 4100112,
                    "offer_id": "MNT-KIT",
                    "currency_code": "RUB",
                    "commission_amount": "9.995",
                    "total_discount_value": "5.00",
                },
            ],
            "posting_services": {"marketplace_service_item_deliv_to_customer": "0.00"},
        },
        "customer": {
            "customer_id": 777001,
            "name": "Ivan Petrov",
            "phone": "+79001234567",
            "email": "ivan@example.com",
        },
        "addressee": {"name": "Ivan Petrov", "country": "ru"},
    }
    posting.update(overrides)
    return posting


# --------------------------------------------------------------------------- construction


def test_repr_and_errors_never_leak_the_api_key() -> None:
    adapter = _adapter(lambda request: _json({}))

    assert API_KEY not in repr(adapter)
    assert CLIENT_ID in repr(adapter)
    assert adapter.fulfillment_mode == "fbs"
    assert adapter.warehouse_id == "1020000123456789"


def test_rejects_an_unknown_fulfillment_mode() -> None:
    with pytest.raises(AppError) as excinfo:
        _adapter(lambda request: _json({}), fulfillment_mode="dbs")

    assert excinfo.value.code == "ERR_OZON_FULFILLMENT_MODE"
    assert excinfo.value.status_code == 422


async def test_api_errors_redact_the_api_key_and_map_status_codes() -> None:
    fake = FakeOzon()
    fake.on(
        OZON_FBS_ORDER_LIST_PATH,
        lambda request: httpx.Response(500, text=f"boom key={API_KEY}"),
    )
    adapter = _adapter(fake.handle)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_OZON_API_ERROR"
    assert excinfo.value.status_code == 502
    assert API_KEY not in excinfo.value.detail
    assert "[redacted]" in excinfo.value.detail


@pytest.mark.parametrize(
    ("status_code", "expected_code", "expected_status"),
    [(401, "ERR_OZON_AUTH_FAILED", 502), (403, "ERR_OZON_AUTH_FAILED", 502),
     (429, "ERR_OZON_RATE_LIMITED", 429)],
)
async def test_credential_and_rate_outcomes_are_typed(
    status_code: int, expected_code: str, expected_status: int
) -> None:
    fake = FakeOzon()
    fake.on(
        OZON_FBS_ORDER_LIST_PATH,
        lambda request: httpx.Response(status_code, text="denied"),
    )

    with pytest.raises(AppError) as excinfo:
        await _adapter(fake.handle).fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == expected_code
    assert excinfo.value.status_code == expected_status
    assert API_KEY not in excinfo.value.detail


async def test_transport_failure_maps_to_unreachable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(AppError) as excinfo:
        await _adapter(boom).fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_OZON_UNREACHABLE"
    assert excinfo.value.status_code == 502


# ----------------------------------------------------------------------- order ingestion


async def test_fetch_orders_normalizes_money_statuses_and_cursor() -> None:
    fake = FakeOzon()
    fake.on(
        OZON_FBS_ORDER_LIST_PATH,
        lambda request: _json({"result": {"postings": [_posting()], "has_next": True}}),
    )
    adapter = _adapter(fake.handle)

    orders, cursor = await adapter.fetch_orders_updated_since(SINCE, cursor="0")

    body = fake.body_at(OZON_FBS_ORDER_LIST_PATH)
    assert body["dir"] == "ASC"
    assert body["filter"]["since"] == "2024-05-01T00:00:00Z"
    assert body["limit"] == 1000
    assert body["offset"] == 0
    assert body["with"] == {"analytics_data": True, "financial_data": True}
    assert cursor == "1"

    assert len(orders) == 1
    order = orders[0]
    assert order.external_order_id == "12345-0001-1"
    assert order.external_order_number == "12345-0001"
    assert order.currency == "RUB"
    # 3 x 1290.00 + 1 x 99.50, less the 5.00 line discount reported by financial_data.
    assert order.subtotal_amount == Decimal("3969.50")
    assert order.discount_amount == Decimal("5.00")
    assert order.total_amount == Decimal("3964.50")
    # 129.005 + 9.995 = 139.000 -> exact 2dp Decimal, ROUND_HALF_UP.
    assert order.platform_commission_base == Decimal("139.00")
    assert order.channel_metadata["platform_commission_base"] == "139.00"
    assert "refunded_amount" not in order.channel_metadata
    assert order.channel_metadata["ozon_status"] == "awaiting_packaging"
    assert order.channel_metadata["ozon_fulfillment_mode"] == "fbs"
    assert order.fulfillment_status == FulfillmentStatus.CONFIRMED_RESERVED
    assert order.payment_status == PaymentStatus.PAID
    assert order.customer.email == "ivan@example.com"
    assert order.customer.external_identity_type == IdentityType.EMAIL
    assert order.customer.country_code == "RU"
    assert [line.external_sku for line in order.line_items] == ["LED-40W", "MNT-KIT"]
    assert order.line_items[0].line_total == Decimal("3870.00")
    assert order.updated_at == datetime(2024, 5, 1, 10, 5, tzinfo=UTC)


async def test_commission_rounds_half_up_to_two_decimals() -> None:
    posting = _posting(
        financial_data={
            "products": [
                {"offer_id": "LED-40W", "currency_code": "RUB", "commission_amount": "1.005"}
            ]
        }
    )
    fake = FakeOzon()
    fake.on(OZON_FBS_ORDER_LIST_PATH, lambda request: _json({"result": {"postings": [posting]}}))

    orders, cursor = await _adapter(fake.handle).fetch_orders_updated_since(SINCE)

    assert orders[0].platform_commission_base == Decimal("1.01")
    assert orders[0].channel_metadata["platform_commission_base"] == "1.01"
    assert isinstance(orders[0].platform_commission_base, Decimal)
    assert cursor is None


async def test_cancelled_posting_is_refunded_end_to_end() -> None:
    posting = _posting(
        status="cancelled",
        customer={},
        addressee={"name": "Anonymous"},
        financial_data={"products": []},
    )
    fake = FakeOzon()
    fake.on(OZON_FBS_ORDER_LIST_PATH, lambda request: _json({"result": {"postings": [posting]}}))

    orders, _ = await _adapter(fake.handle).fetch_orders_updated_since(SINCE)

    order = orders[0]
    assert order.fulfillment_status == FulfillmentStatus.CANCELLED
    assert order.payment_status == PaymentStatus.REFUNDED
    assert order.channel_metadata["refunded_amount"] == str(order.total_amount)
    assert order.platform_commission_base == Decimal("0.00")
    # Without buyer contact the adapter keys the synthetic buyer off the posting number.
    assert order.customer.external_identity_type == IdentityType.WEB_VISITOR_ID
    assert order.customer.external_customer_id == "ozon-posting:12345-0001-1"
    assert order.customer.display_name == "Anonymous"


async def test_uppercase_cancelled_refunded_and_partial_refunds() -> None:
    cancelled_upper = _posting(posting_number="OZ-CANCEL-UP", status="CANCELLED")
    refunded_status = _posting(posting_number="OZ-REFUND-ST", status="REFUNDED")
    partial_refund = _posting(
        posting_number="OZ-PARTIAL", status="delivered", refunded_amount="250.00"
    )
    full_refund = _posting(
        posting_number="OZ-FULL", status="delivered", refund_amount="3964.50"
    )
    fake = FakeOzon()
    fake.on(
        OZON_FBS_ORDER_LIST_PATH,
        lambda request: _json(
            {
                "result": {
                    "postings": [
                        cancelled_upper,
                        refunded_status,
                        partial_refund,
                        full_refund,
                    ]
                }
            }
        ),
    )

    orders, _ = await _adapter(fake.handle).fetch_orders_updated_since(SINCE)
    by_id = {order.external_order_id: order for order in orders}

    assert by_id["OZ-CANCEL-UP"].fulfillment_status is FulfillmentStatus.CANCELLED
    assert by_id["OZ-CANCEL-UP"].payment_status is PaymentStatus.REFUNDED
    assert by_id["OZ-CANCEL-UP"].channel_metadata["refunded_amount"] == "3964.50"

    assert by_id["OZ-REFUND-ST"].fulfillment_status is FulfillmentStatus.REFUNDED
    assert by_id["OZ-REFUND-ST"].payment_status is PaymentStatus.REFUNDED

    assert by_id["OZ-PARTIAL"].fulfillment_status is FulfillmentStatus.DELIVERED
    assert by_id["OZ-PARTIAL"].payment_status is PaymentStatus.PAID
    assert by_id["OZ-PARTIAL"].channel_metadata["refunded_amount"] == "250.00"

    assert by_id["OZ-FULL"].fulfillment_status is FulfillmentStatus.REFUNDED
    assert by_id["OZ-FULL"].payment_status is PaymentStatus.REFUNDED


async def test_fbo_mode_uses_the_ozon_warehouse_endpoint() -> None:
    fake = FakeOzon()
    fake.on(
        OZON_FBO_ORDER_LIST_PATH,
        lambda request: _json({"result": {"postings": [_posting()], "has_next": False}}),
    )
    adapter = _adapter(fake.handle, fulfillment_mode="FBO")

    orders, cursor = await adapter.fetch_orders_updated_since(SINCE, cursor="2000")

    assert fake.paths() == [OZON_FBO_ORDER_LIST_PATH]
    assert fake.body_at(OZON_FBO_ORDER_LIST_PATH)["offset"] == 2000
    assert adapter.fulfillment_mode == "fbo"
    assert len(orders) == 1
    assert orders[0].channel_metadata["ozon_fulfillment_mode"] == "fbo"
    assert cursor is None


async def test_postings_without_lines_are_skipped_but_the_cursor_advances() -> None:
    fake = FakeOzon()
    fake.on(
        OZON_FBS_ORDER_LIST_PATH,
        lambda request: _json(
            {
                "result": {
                    "postings": [_posting(products=[], posting_number="x-1")],
                    "has_next": True,
                }
            }
        ),
    )

    orders, cursor = await _adapter(fake.handle).fetch_orders_updated_since(SINCE)

    assert orders == []
    assert cursor == "1"


async def test_an_empty_posting_page_ends_paging_without_replaying() -> None:
    fake = FakeOzon()
    fake.on(
        OZON_FBS_ORDER_LIST_PATH,
        lambda request: _json({"result": {"postings": [], "has_next": True}}),
    )

    orders, cursor = await _adapter(fake.handle).fetch_orders_updated_since(
        SINCE, cursor="999"
    )

    assert orders == []
    assert cursor is None


async def test_invalid_cursor_fails_closed_without_calling_ozon() -> None:
    fake = FakeOzon()

    with pytest.raises(AppError) as excinfo:
        await _adapter(fake.handle).fetch_orders_updated_since(SINCE, cursor="last-page-token")

    assert excinfo.value.code == "ERR_OZON_INVALID_CURSOR"
    assert excinfo.value.status_code == 422
    assert fake.requests == []


async def test_non_numeric_money_from_ozon_is_refused() -> None:
    posting = _posting()
    posting["products"] = [
        {"sku": 1, "name": "Bad", "offer_id": "BAD-1", "quantity": 1, "price": "n/a"}
    ]
    fake = FakeOzon()
    fake.on(OZON_FBS_ORDER_LIST_PATH, lambda request: _json({"result": {"postings": [posting]}}))

    with pytest.raises(AppError) as excinfo:
        await _adapter(fake.handle).fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_OZON_INVALID_MONEY"


# -------------------------------------------------------------------- product ingestion


async def test_fetch_products_pages_then_filters_by_update_time() -> None:
    fake = FakeOzon()
    fake.on(
        OZON_PRODUCT_LIST_PATH,
        lambda request: _json(
            {"result": {"items": [{"product_id": 11}, {"product_id": 12}], "last_id": "page-2"}}
        ),
    )
    fake.on(
        OZON_PRODUCT_INFO_LIST_PATH,
        lambda request: _json(
            {
                "items": [
                    {
                        "id": 11,
                        "name": "LED Panel 40W",
                        "offer_id": "LED-40W",
                        "updated_at": "2024-05-02T00:00:00Z",
                        "sources": [
                            {
                                "sku": 4100111,
                                "offer_id": "LED-40W",
                                "price": "1290.00",
                                "currency_code": "RUB",
                                "stock": 7,
                            }
                        ],
                    },
                    {
                        "id": 12,
                        "name": "Stale Panel",
                        "offer_id": "LED-STALE",
                        "updated_at": "2024-04-01T00:00:00Z",
                        "sources": [],
                    },
                ]
            }
        ),
    )

    products, cursor = await _adapter(fake.handle).fetch_products_updated_since(
        SINCE, cursor=""
    )

    assert fake.body_at(OZON_PRODUCT_LIST_PATH) == {
        "filter": {"visibility": "ALL"},
        "last_id": "",
        "limit": 1000,
    }
    assert fake.body_at(OZON_PRODUCT_INFO_LIST_PATH) == {"product_id": ["11", "12"]}
    assert cursor == "page-2"
    assert [product.external_product_id for product in products] == ["11"]
    assert products[0].title == "LED Panel 40W"
    assert products[0].variants == [
        {
            "external_sku": "LED-40W",
            "external_variant_id": "4100111",
            "price": Decimal("1290.00"),
            "currency": "RUB",
            "quantity": 7,
        }
    ]


async def test_fetch_products_returns_early_on_an_empty_page() -> None:
    fake = FakeOzon()
    fake.on(OZON_PRODUCT_LIST_PATH, lambda request: _json({"result": {"items": []}}))

    products, cursor = await _adapter(fake.handle).fetch_products_updated_since(SINCE)

    assert products == []
    assert cursor is None
    assert fake.paths() == [OZON_PRODUCT_LIST_PATH]


# ------------------------------------------------------------------------ fulfillment


async def test_create_fulfillment_resolves_posting_products_then_ships() -> None:
    fake = FakeOzon()
    fake.on(
        OZON_FBS_POSTING_GET_PATH,
        lambda request: _json(
            {
                "result": {
                    "posting_number": "12345-0001-1",
                    "products": [
                        {"sku": 4100111, "quantity": 3},
                        {"sku": 4100112, "quantity": 0},
                    ],
                }
            }
        ),
    )
    fake.on(OZON_FBS_SHIP_PATH, lambda request: _json({"result": ["12345-0001-1"]}))
    adapter = _adapter(fake.handle)

    result = await adapter.create_fulfillment(
        "12345-0001-1", "RU123456789", "CDEK", tracking_url="https://track.test/1"
    )

    assert fake.body_at(OZON_FBS_POSTING_GET_PATH)["posting_number"] == "12345-0001-1"
    ship_body = fake.body_at(OZON_FBS_SHIP_PATH)
    assert ship_body["posting_number"] == "12345-0001-1"
    package = ship_body["packages"][0]
    assert package["products"] == [{"product_id": 4100111, "quantity": 3}]
    assert package["tracking_number"] == "RU123456789"
    assert package["carrier"] == "CDEK"
    assert package["tracking_url"] == "https://track.test/1"
    assert "package_id" not in package
    assert result["channel"] == "ozon"
    assert result["status"] == "shipped"
    assert result["fulfillment_id"] == "12345-0001-1"
    assert result["tracking_number"] == "RU123456789"
    assert result["raw"] == {"result": ["12345-0001-1"]}


async def test_create_fulfillment_with_package_id_skips_the_posting_lookup() -> None:
    fake = FakeOzon()
    fake.on(OZON_FBS_SHIP_PATH, lambda request: _json({"result": ["12345-0001-1"]}))

    result = await _adapter(fake.handle).create_fulfillment(
        "12345-0001-1", "RU1", "CDEK", fulfillment_order_id="PKG-77"
    )

    assert fake.paths() == [OZON_FBS_SHIP_PATH]
    package = fake.body_at(OZON_FBS_SHIP_PATH)["packages"][0]
    assert package["package_id"] == "PKG-77"
    assert "products" not in package
    assert result["fulfillment_id"] == "PKG-77"


async def test_create_fulfillment_refuses_fbo_and_blank_tracking() -> None:
    fbo = _adapter(lambda request: _json({}), fulfillment_mode="fbo")
    with pytest.raises(AppError) as fbo_error:
        await fbo.create_fulfillment("12345-0001-1", "RU1", "Ozon")
    assert fbo_error.value.code == "ERR_OZON_FULFILLMENT_UNSUPPORTED"

    fbs = _adapter(lambda request: _json({}))
    with pytest.raises(AppError) as tracking_error:
        await fbs.create_fulfillment("12345-0001-1", "  ", "CDEK")
    assert tracking_error.value.code == "ERR_OZON_FULFILLMENT_REJECTED"


async def test_create_fulfillment_refuses_a_posting_without_products() -> None:
    fake = FakeOzon()
    fake.on(OZON_FBS_POSTING_GET_PATH, lambda request: _json({"result": {"products": []}}))

    with pytest.raises(AppError) as excinfo:
        await _adapter(fake.handle).create_fulfillment("12345-0001-1", "RU1", "CDEK")

    assert excinfo.value.code == "ERR_OZON_POSTING_PRODUCTS_MISSING"


# ---------------------------------------------------------------------------- webhooks


def _signature(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_webhook_accepts_a_valid_signature_and_fails_closed() -> None:
    body = json.dumps({"event": "posting_sent", "posting_number": "12345-0001-1"}).encode()
    signature = _signature(body)

    assert verify_ozon_webhook(body, {"x-ozon-signature": signature}, WEBHOOK_SECRET)
    assert verify_ozon_webhook(body, {"X-Ozon-Signature": signature.upper()}, WEBHOOK_SECRET)
    assert not verify_ozon_webhook(body, {"x-ozon-signature": _signature(b"other")}, WEBHOOK_SECRET)
    assert not verify_ozon_webhook(body, {}, WEBHOOK_SECRET)
    assert not verify_ozon_webhook(body, {"x-ozon-signature": signature}, "")
    assert not verify_ozon_webhook(b"", {"x-ozon-signature": signature}, WEBHOOK_SECRET)


def test_adapter_verify_webhook_delegates_to_the_module_helper() -> None:
    body = b'{"event":"posting_sent"}'
    adapter = _adapter(lambda request: _json({}))

    assert adapter.verify_webhook(body, {"x-ozon-signature": _signature(body)}, WEBHOOK_SECRET)
    assert not adapter.verify_webhook(
        body, {"x-ozon-signature": _signature(body, "wrong")}, WEBHOOK_SECRET
    )


# ------------------------------------------------------------------ listing publication


def _payload(**overrides: Any) -> MarketplaceListingPayload:
    payload: dict[str, Any] = {
        "channel_account_id": uuid4(),
        "channel_type": "ozon",
        "external_sku": "LED-40W",
        "title": "LED Panel 40W",
        "description": "Industrial LED panel with 5 year warranty.",
        "bullet_points": ["IP65 rated", "Dimmable"],
        "price_usd": Decimal("129.005"),
        "quantity": 25,
        "images": ["https://cdn.example.com/led-40w.jpg"],
        "category": "LED Lighting",
        "hs_code": "9405.40",
        "moq_tiers": [SourcingMoqTier(min_qty=10, unit_price_usd=Decimal("119.00"))],
    }
    payload.update(overrides)
    return MarketplaceListingPayload(**payload)


def _import_info(offer_id: str, product_id: int, status: str = "imported") -> dict[str, Any]:
    return {
        "result": {
            "task_id": 55,
            "items": [
                {
                    "offer_id": offer_id,
                    "product_id": product_id,
                    "status": status,
                    "errors": [],
                }
            ],
        }
    }


async def test_publish_listing_single_variant_pushes_stock_and_copy() -> None:
    fake = FakeOzon()
    fake.on(OZON_PRODUCT_IMPORT_PATH, lambda request: _json({"result": {"task_id": 55}}))
    fake.on(OZON_PRODUCT_IMPORT_INFO_PATH, lambda request: _json(_import_info("LED-40W", 4100111)))
    fake.on(OZON_PRODUCT_UPDATE_PATH, lambda request: _json({"result": True}))
    fake.on(
        OZON_STOCK_IMPORT_PATH,
        lambda request: _json({"result": {"updated": True}}),
    )

    receipt = await _adapter(fake.handle).publish_listing(_payload())

    items = fake.body_at(OZON_PRODUCT_IMPORT_PATH)["items"]
    assert items == [
        {
            "offer_id": "LED-40W",
            "name": "LED Panel 40W",
            "price": "129.01",
            "currency_code": "USD",
            "vat": "0",
            "images": ["https://cdn.example.com/led-40w.jpg"],
            "primary_image": "https://cdn.example.com/led-40w.jpg",
        }
    ]
    assert fake.body_at(OZON_PRODUCT_IMPORT_INFO_PATH) == {"task_id": 55}

    update_items = fake.body_at(OZON_PRODUCT_UPDATE_PATH)["items"]
    assert update_items[0]["offer_id"] == "LED-40W"
    description = update_items[0]["description"]
    assert "Industrial LED panel" in description
    assert "- IP65 rated" in description
    assert "Category: LED Lighting" in description
    assert "HS code: 9405.40" in description
    assert "MOQ 10: 119.00 per unit" in description

    assert fake.body_at(OZON_STOCK_IMPORT_PATH) == {
        "stocks": [
            {
                "offer_id": "LED-40W",
                "product_id": 4100111,
                "stock": 25,
                "warehouse_id": 1020000123456789,
            }
        ]
    }
    assert receipt.external_product_id == "4100111"
    assert receipt.external_sku == "LED-40W"
    assert receipt.status == "imported"
    assert receipt.raw_response["stock_pushed"] is True
    assert [vr.external_sku for vr in receipt.variant_receipts] == ["LED-40W"]


async def test_publish_listing_multi_variant_maps_options_and_prices() -> None:
    fake = FakeOzon()
    fake.on(
        OZON_PRODUCT_IMPORT_PATH,
        lambda request: _json({"result": {"task_id": 56}}),
    )
    fake.on(
        OZON_PRODUCT_IMPORT_INFO_PATH,
        lambda request: _json(
            {
                "result": {
                    "task_id": 56,
                    "items": [
                        {"offer_id": "LED-40W-WHT", "product_id": 1, "status": "imported"},
                        {"offer_id": "LED-40W-BLK", "product_id": 2, "status": "pending"},
                    ],
                }
            }
        ),
    )
    fake.on(OZON_PRODUCT_UPDATE_PATH, lambda request: _json({"result": True}))
    fake.on(OZON_STOCK_IMPORT_PATH, lambda request: _json({"result": {"updated": True}}))

    payload = _payload(
        options=[SourcingOptionSpec(name="Color", values=["White", "Black"])],
        variants=[
            {
                "local_sku": "LED-40W-WHT-LOCAL",
                "external_sku": "LED-40W-WHT",
                "option_values": {"Color": "White"},
                "price_usd": Decimal("129.00"),
                "quantity": 10,
                "image_url": "https://cdn.example.com/white.jpg",
            },
            {
                "local_sku": "LED-40W-BLK-LOCAL",
                "external_sku": "LED-40W-BLK",
                "option_values": {"Color": "Black"},
                "price_usd": Decimal("134.00"),
                "quantity": 0,
                "image_url": None,
            },
        ],
    )

    receipt = await _adapter(fake.handle).publish_listing(payload)

    items = fake.body_at(OZON_PRODUCT_IMPORT_PATH)["items"]
    assert [item["offer_id"] for item in items] == ["LED-40W-WHT", "LED-40W-BLK"]
    assert [item["price"] for item in items] == ["129.00", "134.00"]
    assert items[0]["name"] == "LED Panel 40W (White)"
    assert items[0]["primary_image"] == "https://cdn.example.com/white.jpg"
    assert items[1]["primary_image"] == "https://cdn.example.com/led-40w.jpg"

    description = fake.body_at(OZON_PRODUCT_UPDATE_PATH)["items"][0]["description"]
    assert "Option Color: White, Black" in description
    assert "Variant LED-40W-WHT: Color=White" in description

    # Only the in-stock variant is pushed; the zero-quantity one keeps its current stock.
    assert fake.body_at(OZON_STOCK_IMPORT_PATH) == {
        "stocks": [
            {
                "offer_id": "LED-40W-WHT",
                "product_id": 1,
                "stock": 10,
                "warehouse_id": 1020000123456789,
            }
        ]
    }
    assert [vr.external_sku for vr in receipt.variant_receipts] == ["LED-40W-WHT", "LED-40W-BLK"]
    assert receipt.external_product_id == "1"
    assert receipt.status == "imported"


async def test_publish_listing_uploads_image_blobs_as_data_uris() -> None:
    blob = MarketplaceImageBlob(
        filename="white.png", mime_type="image/png", data=b"\x89PNG\r\n\x1a\nbinary"
    )
    fake = FakeOzon()
    fake.on(OZON_PRODUCT_IMPORT_PATH, lambda request: _json({"result": {"task_id": 57}}))
    fake.on(
        OZON_PRODUCT_IMPORT_INFO_PATH,
        lambda request: _json(
            {
                "result": {
                    "task_id": 57,
                    "items": [
                        {"offer_id": "LED-40W-A", "product_id": 21, "status": "imported"},
                        {"offer_id": "LED-40W-B", "product_id": 22, "status": "imported"},
                    ],
                }
            }
        ),
    )
    fake.on(OZON_PRODUCT_UPDATE_PATH, lambda request: _json({"result": True}))
    fake.on(OZON_PICTURE_IMPORT_PATH, lambda request: _json({"result": {"pictures": []}}))
    fake.on(OZON_STOCK_IMPORT_PATH, lambda request: _json({"result": {"updated": True}}))

    payload = _payload(
        image_blobs=[blob],
        variants=[
            {
                "local_sku": "A",
                "external_sku": "LED-40W-A",
                "option_values": {},
                "price_usd": Decimal("10.00"),
                "quantity": 1,
            },
            {
                "local_sku": "B",
                "external_sku": "LED-40W-B",
                "option_values": {},
                "price_usd": Decimal("10.00"),
                "quantity": 1,
            },
        ],
    )

    await _adapter(fake.handle).publish_listing(payload)

    picture_calls = [
        _body(request)
        for request in fake.requests
        if request.url.path == OZON_PICTURE_IMPORT_PATH
    ]
    assert [call["product_id"] for call in picture_calls] == [21, 22]
    expected_uri = "data:image/png;base64," + base64.b64encode(blob.data).decode("ascii")
    assert picture_calls[0]["images"] == [expected_uri]


async def test_publish_listing_without_a_warehouse_skips_stock_but_still_publishes() -> None:
    fake = FakeOzon()
    fake.on(OZON_PRODUCT_IMPORT_PATH, lambda request: _json({"result": {"task_id": 58}}))
    fake.on(OZON_PRODUCT_IMPORT_INFO_PATH, lambda request: _json(_import_info("LED-40W", 4100111)))
    fake.on(OZON_PRODUCT_UPDATE_PATH, lambda request: _json({"result": True}))

    receipt = await _adapter(fake.handle, warehouse_id="").publish_listing(_payload())

    assert OZON_STOCK_IMPORT_PATH not in fake.paths()
    assert receipt.raw_response["stock_pushed"] is False


async def test_publish_listing_surfaces_remote_rejections_as_typed_errors() -> None:
    rejected = FakeOzon()
    rejected.on(OZON_PRODUCT_IMPORT_PATH, lambda request: _json({"result": {"task_id": 59}}))
    rejected.on(
        OZON_PRODUCT_IMPORT_INFO_PATH,
        lambda request: _json(
            {
                "result": {
                    "task_id": 59,
                    "items": [
                        {
                            "offer_id": "LED-40W",
                            "status": "failed",
                            "errors": [{"code": "ITEM_DESCRIPTION_TOO_LONG"}],
                        }
                    ],
                }
            }
        ),
    )

    with pytest.raises(AppError) as rejected_error:
        await _adapter(rejected.handle).publish_listing(_payload())
    assert rejected_error.value.code == "ERR_OZON_LISTING_REJECTED"
    assert rejected_error.value.status_code == 502
    assert "ITEM_DESCRIPTION_TOO_LONG" in rejected_error.value.detail

    unchecked = FakeOzon()
    unchecked.on(OZON_PRODUCT_IMPORT_PATH, lambda request: _json({"items": []}))
    with pytest.raises(AppError) as task_error:
        await _adapter(unchecked.handle).publish_listing(_payload())
    assert task_error.value.code == "ERR_OZON_LISTING_REJECTED"

    unconfirmed = FakeOzon()
    unconfirmed.on(OZON_PRODUCT_IMPORT_PATH, lambda request: _json({"result": {"task_id": 60}}))
    unconfirmed.on(
        OZON_PRODUCT_IMPORT_INFO_PATH,
        lambda request: _json(
            {
                "result": {
                    "task_id": 60,
                    "items": [{"offer_id": "LED-40W", "status": "pending"}],
                }
            }
        ),
    )
    with pytest.raises(AppError) as unconfirmed_error:
        await _adapter(unconfirmed.handle).publish_listing(_payload())
    assert unconfirmed_error.value.code == "ERR_OZON_LISTING_UNCONFIRMED"


async def test_publish_listing_rejects_float_prices_and_blank_identity() -> None:
    adapter = _adapter(lambda request: _json({}))

    with pytest.raises(AppError) as float_error:
        await adapter.publish_listing(_payload().model_copy(update={"price_usd": 129.0}))
    assert float_error.value.code == "ERR_OZON_INVALID_MONEY"
    assert float_error.value.status_code == 422

    with pytest.raises(AppError) as blank_error:
        await adapter.publish_listing(_payload(external_sku="   "))
    assert blank_error.value.code == "ERR_OZON_LISTING_REJECTED"

    with pytest.raises(AppError) as zero_error:
        await adapter.publish_listing(_payload(price_usd=Decimal("0")))
    assert zero_error.value.code == "ERR_OZON_LISTING_REJECTED"


# --------------------------------------------------------------------- inventory push


async def test_push_inventory_level_clamps_and_reports_the_wire_body() -> None:
    fake = FakeOzon()
    fake.on(OZON_STOCK_IMPORT_PATH, lambda request: _json({"result": {"updated": True}}))
    adapter = _adapter(fake.handle)

    result = await adapter.push_inventory_level(
        external_sku=" LED-40W ",
        quantity=-4,
        external_product_id="4100111",
    )

    assert fake.body_at(OZON_STOCK_IMPORT_PATH) == {
        "stocks": [
            {
                "offer_id": "LED-40W",
                "stock": 0,
                "warehouse_id": 1020000123456789,
                "product_id": 4100111,
            }
        ]
    }
    assert result["channel"] == "ozon"
    assert result["external_sku"] == "LED-40W"
    assert result["quantity"] == 0
    assert result["product_id"] == "4100111"
    assert result["warehouse_id"] == "1020000123456789"
    assert isinstance(result["raw"], dict)


async def test_push_inventory_level_honours_an_explicit_warehouse_and_location() -> None:
    fake = FakeOzon()
    fake.on(OZON_STOCK_IMPORT_PATH, lambda request: _json({"result": {"updated": True}}))

    result = await _adapter(fake.handle, warehouse_id="").push_inventory_level(
        external_sku="LED-40W",
        quantity=12,
        external_variant_id="999",
        location_id=" WH-9 ",
        warehouse_id=" 2000000000001 ",
    )

    stock_row = fake.body_at(OZON_STOCK_IMPORT_PATH)["stocks"][0]
    assert stock_row["warehouse_id"] == 2000000000001
    assert stock_row["stock"] == 12
    assert stock_row["product_id"] == 999
    assert result["location_id"] == "WH-9"
    assert result["variant_id"] == "999"


async def test_push_inventory_level_requires_a_usable_warehouse() -> None:
    adapter = _adapter(lambda request: _json({}), warehouse_id="")

    with pytest.raises(AppError) as missing:
        await adapter.push_inventory_level(external_sku="LED-40W", quantity=3)
    assert missing.value.code == "ERR_OZON_INVENTORY_SYNC"
    assert missing.value.status_code == 422

    with pytest.raises(AppError) as non_numeric:
        await adapter.push_inventory_level(
            external_sku="LED-40W", quantity=3, warehouse_id="main-warehouse"
        )
    assert non_numeric.value.code == "ERR_OZON_INVENTORY_SYNC"

    with pytest.raises(AppError) as blank_sku:
        await adapter.push_inventory_level(external_sku="   ", quantity=3, warehouse_id="1")
    assert blank_sku.value.code == "ERR_OZON_INVENTORY_SYNC"


async def test_push_inventory_level_refuses_an_unapplied_update() -> None:
    fake = FakeOzon()
    fake.on(
        OZON_STOCK_IMPORT_PATH,
        lambda request: _json({"result": {"updated": False, "errors": ["sku not found"]}}),
    )

    with pytest.raises(AppError) as excinfo:
        await _adapter(fake.handle).push_inventory_level(
            external_sku="LED-40W", quantity=5, warehouse_id="1020000123456789"
        )

    assert excinfo.value.code == "ERR_OZON_INVENTORY_SYNC"
    assert excinfo.value.status_code == 502
    assert "sku not found" in excinfo.value.detail


async def test_push_inventory_level_refuses_list_result_with_updated_false() -> None:
    fake = FakeOzon()
    fake.on(
        OZON_STOCK_IMPORT_PATH,
        lambda request: _json(
            {"result": [{"offer_id": "LED-40W", "updated": False, "errors": ["bad warehouse"]}]}
        ),
    )

    with pytest.raises(AppError) as excinfo:
        await _adapter(fake.handle).push_inventory_level(
            external_sku="LED-40W", quantity=5, warehouse_id="1020000123456789"
        )

    assert excinfo.value.code == "ERR_OZON_INVENTORY_SYNC"
    assert excinfo.value.status_code == 502
    assert "bad warehouse" in excinfo.value.detail
