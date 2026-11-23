"""Zero-network unit tests for the Walmart Marketplace connector adapter.

Every HTTP interaction runs through ``httpx.MockTransport``; the suite pins credential
redaction, Decimal-only money handling, cursor semantics and each typed ``AppError`` path.
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
from uuid import UUID, uuid4

import httpx
import pytest

import erp_core.channels.ecommerce.walmart as walmart_module
from erp_core.catalog.sourcing_service import (
    MarketplaceListingPayload,
    MarketplaceVariantPayload,
    SourcingOptionSpec,
)
from erp_core.channels.ecommerce.base import (
    CommercePlatformAdapter,
    NormalizedOrder,
    NormalizedProduct,
)
from erp_core.channels.ecommerce.walmart import (
    WALMART_FEEDS_PATH,
    WALMART_INVENTORY_PATH,
    WALMART_ITEMS_PATH,
    WALMART_ORDERS_PATH,
    WALMART_PAGE_LIMIT,
    WALMART_PRODUCTION_BASE_URL,
    WALMART_SANDBOX_BASE_URL,
    WALMART_TOKEN_PATH,
    WalmartAdapter,
    verify_walmart_webhook,
)
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

SELLER_ID = "10001234"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret-value"
ACCESS_TOKEN = "test-access-token-value"
BASE_URL = "https://walmart.test"
SINCE = datetime(2024, 5, 1, tzinfo=UTC)
CHANNEL_ACCOUNT_ID = uuid4()

Route = Callable[[httpx.Request], httpx.Response]


class FakeWalmart:
    """Deterministic route table standing in for every Walmart Marketplace endpoint."""

    def __init__(
        self,
        routes: dict[tuple[str, str], Route] | None = None,
        *,
        token: Route | None = None,
    ) -> None:
        self.routes = routes or {}
        self.token_route: Route = token or (
            lambda _request: httpx.Response(
                200, json={"access_token": ACCESS_TOKEN, "expires_in": 900}
            )
        )
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == WALMART_TOKEN_PATH:
            self.token_calls += 1
            return self.token_route(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(
                404,
                json={"errors": [{"message": f"unrouted {request.method} {request.url.path}"}]},
            )
        return route(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def sent(self, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == path]

    def only(self, method: str, path: str) -> httpx.Request:
        matched = [
            request
            for request in self.requests
            if request.method == method and request.url.path == path
        ]
        assert len(matched) == 1, f"expected exactly one {method} {path}, got {len(matched)}"
        return matched[0]


def make_adapter(fake: FakeWalmart, **overrides: Any) -> WalmartAdapter:
    options: dict[str, Any] = {
        "seller_id": SELLER_ID,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "access_token": ACCESS_TOKEN,
        "endpoint_url": BASE_URL,
        "transport": fake.transport,
        "timeout_seconds": 5.0,
    }
    options.update(overrides)
    return WalmartAdapter(**options)


def body(request: httpx.Request) -> dict[str, Any]:
    parsed = json.loads(request.content)
    assert isinstance(parsed, dict)
    return parsed


def empty_orders(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"list": {"elements": {"order": []}, "meta": {}}})


def empty_items(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ItemResponse": [], "totalItems": 0})


def _line(
    *,
    line_number: str,
    sku: str,
    quantity: str,
    product: str,
    shipping: str = "0.00",
    tax: str = "0.00",
    currency: str = "USD",
    status: str = "Created",
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "lineNumber": line_number,
        "item": {"sku": sku, "productName": title or f"Product {sku}"},
        "orderLineQuantity": {"unitOfMeasurement": "EACH", "amount": quantity},
        "charges": {
            "charge": [
                {
                    "chargeType": "PRODUCT",
                    "chargeAmount": {"currency": currency, "amount": product},
                },
                {
                    "chargeType": "SHIPPING",
                    "chargeAmount": {"currency": currency, "amount": shipping},
                },
                {
                    "chargeType": "TAX",
                    "chargeAmount": {"currency": currency, "amount": tax},
                },
            ]
        },
        "orderLineStatuses": {
            "orderLineStatus": [{"currentStatus": {"status": status}}],
        },
    }


def order_payload(
    *,
    purchase_order_id: str = "1122334455667",
    lines: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "purchaseOrderId": purchase_order_id,
        "customerOrderId": "WEB-0042",
        "customerEmailId": "buyer@example.com",
        "orderDate": "2024-05-02T10:15:30.000Z",
        "lastModifiedDate": "2024-05-02T11:00:00.000Z",
        "orderType": "Walmart.com",
        "shipNode": "1001",
        "commission": {"amount": "3.555", "currency": "USD"},
        "shippingInfo": {
            "postalAddress": {
                "name": "Jane Buyer",
                "address1": "1 Main St",
                "city": "Bentonville",
                "state": "AR",
                "postalCode": "72712",
                "country": "US",
                "phone": "+14155550123",
            }
        },
        "orderLines": {
            "orderLine": lines
            if lines is not None
            else [
                _line(line_number="1", sku="SKU-RED-L", quantity="2", product="12.75")
            ]
        },
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------------------ construction


def test_repr_redacts_secret_material() -> None:
    adapter = WalmartAdapter(
        seller_id=SELLER_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        access_token=ACCESS_TOKEN,
        endpoint_url="https://walmart.test/",
    )

    rendered = f"{adapter!r}{adapter!s}"
    assert CLIENT_SECRET not in rendered
    assert ACCESS_TOKEN not in rendered
    assert SELLER_ID in repr(adapter)
    assert adapter.base_url == "https://walmart.test"


def test_endpoint_defaults_and_fulfillment_mode_normalisation() -> None:
    production = WalmartAdapter(
        seller_id=SELLER_ID, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
    )
    sandbox = WalmartAdapter(
        seller_id=SELLER_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        endpoint_url=WALMART_SANDBOX_BASE_URL,
        fulfillment_mode="  WFS ",
    )

    assert production.base_url == WALMART_PRODUCTION_BASE_URL
    assert production.fulfillment_mode == "standard"
    assert sandbox.base_url == WALMART_SANDBOX_BASE_URL
    assert sandbox.fulfillment_mode == "wfs"
    assert isinstance(production, CommercePlatformAdapter)


# --------------------------------------------------------------------------- OAuth2 token


async def test_client_credentials_token_is_exchanged_and_cached() -> None:
    fake = FakeWalmart({("GET", WALMART_ORDERS_PATH): empty_orders})
    adapter = make_adapter(fake, access_token=None)

    await adapter.fetch_orders_updated_since(SINCE)
    await adapter.fetch_orders_updated_since(SINCE)

    assert fake.token_calls == 1, "the access token must be cached between calls"
    token_request = fake.sent(WALMART_TOKEN_PATH)[0]
    assert token_request.method == "POST"
    assert token_request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    basic = token_request.headers["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(basic).decode() == f"{CLIENT_ID}:{CLIENT_SECRET}"
    assert token_request.content.decode() == "grant_type=client_credentials"

    order_requests = fake.sent(WALMART_ORDERS_PATH)
    assert len(order_requests) == 2
    for request in order_requests:
        assert request.headers["WM_SEC.ACCESS_TOKEN"] == ACCESS_TOKEN
        assert request.headers["WM_SVC.NAME"] == "Walmart Marketplace"
        assert len(request.headers["WM_QOS.CORRELATION_ID"]) == 32


async def test_access_token_is_refreshed_only_after_the_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(walmart_module.time, "monotonic", lambda: clock[0])
    fake = FakeWalmart({("GET", WALMART_ORDERS_PATH): empty_orders})
    adapter = make_adapter(fake, access_token=None)

    await adapter.fetch_orders_updated_since(SINCE)
    assert fake.token_calls == 1

    clock[0] = 1000.0 + 900 - 60 - 1
    await adapter.fetch_orders_updated_since(SINCE)
    assert fake.token_calls == 1

    clock[0] = 1000.0 + 900 - 60
    await adapter.fetch_orders_updated_since(SINCE)
    assert fake.token_calls == 2


async def test_caller_supplied_token_never_triggers_a_token_call() -> None:
    fake = FakeWalmart({("GET", WALMART_ORDERS_PATH): empty_orders})
    adapter = make_adapter(fake)

    await adapter.fetch_orders_updated_since(SINCE)

    assert fake.token_calls == 0
    assert fake.sent(WALMART_TOKEN_PATH) == []


async def test_token_endpoint_401_raises_auth_failed_without_leaking_secrets() -> None:
    fake = FakeWalmart(
        token=lambda _request: httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": f"bad credentials {CLIENT_SECRET} {CLIENT_ID}",
            },
        )
    )
    adapter = make_adapter(fake, access_token=None)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    error = excinfo.value
    assert (error.slug, error.code, error.status_code) == (
        "walmart-auth-failed",
        "ERR_WALMART_AUTH_FAILED",
        502,
    )
    rendered = f"{error.detail} {error} {adapter!r}"
    assert CLIENT_SECRET not in rendered
    assert ACCESS_TOKEN not in rendered
    assert fake.sent(WALMART_ORDERS_PATH) == []


async def test_token_response_without_access_token_fails_closed() -> None:
    fake = FakeWalmart(token=lambda _request: httpx.Response(200, json={"expires_in": 900}))
    adapter = make_adapter(fake, access_token=None)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_WALMART_AUTH_FAILED"
    assert "access_token" in excinfo.value.detail


async def test_token_endpoint_429_maps_to_rate_limited() -> None:
    fake = FakeWalmart(token=lambda _request: httpx.Response(429, json={"errors": []}))
    adapter = make_adapter(fake, access_token=None)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    assert (excinfo.value.slug, excinfo.value.code, excinfo.value.status_code) == (
        "walmart-rate-limited",
        "ERR_WALMART_RATE_LIMITED",
        429,
    )


async def test_unreachable_transport_maps_to_walmart_unreachable() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = make_adapter(FakeWalmart({("GET", WALMART_ORDERS_PATH): boom}))

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    assert (excinfo.value.slug, excinfo.value.code) == (
        "walmart-unreachable",
        "ERR_WALMART_UNREACHABLE",
    )
    assert CLIENT_SECRET not in excinfo.value.detail


# ------------------------------------------------------------------------------ order sync


async def test_fetch_orders_normalizes_money_customer_and_metadata() -> None:
    lines = [
        _line(
            line_number="1",
            sku="SKU-RED-L",
            quantity="2",
            product="12.75",
            shipping="4.99",
            tax="1.15",
            status="Shipped",
        ),
        # Walmart may repeat a SKU across lines: the ERP order item is per variant.
        _line(
            line_number="2",
            sku="SKU-RED-L",
            quantity="1",
            product="6.375",
            status="Shipped",
        ),
        _line(line_number="3", sku="SKU-BLUE-S", quantity="1", product="9.10", status="Cancelled"),
    ]
    fake = FakeWalmart(
        {
            ("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(
                200,
                json={
                    "list": {
                        "elements": {"order": [order_payload(lines=lines)]},
                        "meta": {},
                    }
                },
            )
        }
    )
    adapter = make_adapter(fake)

    orders, cursor = await adapter.fetch_orders_updated_since(SINCE)

    assert cursor is None
    assert len(orders) == 1
    order = orders[0]
    assert isinstance(order, NormalizedOrder)
    assert order.external_order_id == "1122334455667"
    assert order.external_order_number == "WEB-0042"
    assert order.order_mode is OrderMode.B2C_RETAIL
    assert order.currency == "USD"
    assert order.fx_rate_to_base is None, "the FX rate is locked at ingestion, never guessed"

    assert order.subtotal_amount == Decimal("28.23")  # 12.75 + 6.38 (ROUND_HALF_UP) + 9.10
    assert order.shipping_charged == Decimal("4.99")
    assert order.tax_collected == Decimal("1.15")
    assert order.discount_amount == Decimal("0.00")
    assert order.total_amount == Decimal("34.37")
    assert order.platform_commission_base == Decimal("3.56"), "3.555 rounds HALF_UP to 3.56"
    assert order.channel_metadata["platform_commission_base"] == "3.56"
    assert order.channel_metadata["refunded_amount"] == "9.10"
    assert order.channel_metadata["walmart_purchase_order_id"] == "1122334455667"
    assert order.channel_metadata["walmart_customer_order_id"] == "WEB-0042"
    assert order.channel_metadata["walmart_seller_id"] == SELLER_ID
    assert order.channel_metadata["walmart_fulfillment_mode"] == "standard"

    assert [(line.external_sku, line.quantity) for line in order.line_items] == [
        ("SKU-RED-L", 3),
        ("SKU-BLUE-S", 1),
    ]
    assert order.line_items[0].line_total == Decimal("19.13")
    assert all(isinstance(line.line_total, Decimal) for line in order.line_items)

    # One live line shipped, one cancelled: everything that will ever ship already shipped.
    assert order.fulfillment_status is FulfillmentStatus.SHIPPED
    assert order.payment_status is PaymentStatus.PAID
    assert order.customer.email == "buyer@example.com"
    assert order.customer.phone_e164 == "+14155550123"
    assert order.customer.country_code == "US"
    assert order.customer.display_name == "Jane Buyer"
    assert order.customer.external_identity_type is IdentityType.EMAIL
    assert order.updated_at == datetime(2024, 5, 2, 11, 0, tzinfo=UTC)

    request = fake.only("GET", WALMART_ORDERS_PATH)
    assert request.url.params["lastModifiedStartDate"] == "2024-05-01T00:00:00.000Z"
    assert request.url.params["limit"] == str(WALMART_PAGE_LIMIT)
    assert "nextCursor" not in request.url.params


async def test_fetch_orders_follows_walmart_next_cursor() -> None:
    def page_one(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "list": {
                    "elements": {
                        "order": [order_payload(purchase_order_id="PO-1")]
                    },
                    "meta": {"nextCursor": "cursor-abc"},
                }
            },
        )

    def page_two(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "list": {
                    "elements": {
                        "order": [order_payload(purchase_order_id="PO-2")]
                    },
                    "meta": {"nextCursor": ""},
                }
            },
        )

    fake = FakeWalmart()
    fake.routes[("GET", WALMART_ORDERS_PATH)] = page_one
    adapter = make_adapter(fake)

    first, cursor = await adapter.fetch_orders_updated_since(SINCE)
    assert [order.external_order_id for order in first] == ["PO-1"]
    assert cursor == "cursor-abc"

    fake.routes[("GET", WALMART_ORDERS_PATH)] = page_two
    second, cursor = await adapter.fetch_orders_updated_since(SINCE, cursor)
    assert [order.external_order_id for order in second] == ["PO-2"]
    assert cursor is None
    assert fake.sent(WALMART_ORDERS_PATH)[1].url.params["nextCursor"] == "cursor-abc"


async def test_fetch_orders_skips_rows_without_lines_or_purchase_order_id() -> None:
    payload = {
        "list": {
            "elements": {
                "order": [
                    {"purchaseOrderId": "NO-LINES", "orderLines": {"orderLine": []}},
                    {"customerOrderId": "NO-ID", "orderLines": order_payload()["orderLines"]},
                    "not-an-order",
                    order_payload(purchase_order_id="PO-KEEP"),
                ]
            },
            "meta": {},
        }
    }
    fake = FakeWalmart(
        {("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(200, json=payload)}
    )

    orders, cursor = await make_adapter(fake).fetch_orders_updated_since(SINCE)

    assert [order.external_order_id for order in orders] == ["PO-KEEP"]
    assert cursor is None


async def test_all_cancelled_lines_mark_the_order_refunded() -> None:
    payload = order_payload(
        lines=[
            _line(
                line_number="1",
                sku="SKU-GONE",
                quantity="1",
                product="15.00",
                status="Cancelled",
            )
        ]
    )
    fake = FakeWalmart(
        {
            ("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(
                200, json={"list": {"elements": {"order": [payload]}, "meta": {}}}
            )
        }
    )

    orders, _ = await make_adapter(fake).fetch_orders_updated_since(SINCE)

    order = orders[0]
    assert order.fulfillment_status is FulfillmentStatus.CANCELLED
    assert order.payment_status is PaymentStatus.REFUNDED
    assert order.channel_metadata["refunded_amount"] == "15.00"


async def test_partially_shipped_order_reports_partially_shipped() -> None:
    payload = order_payload(
        lines=[
            _line(line_number="1", sku="SKU-A", quantity="1", product="5.00", status="Shipped"),
            _line(line_number="2", sku="SKU-B", quantity="1", product="5.00", status="Created"),
        ]
    )
    fake = FakeWalmart(
        {
            ("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(
                200, json={"list": {"elements": {"order": [payload]}, "meta": {}}}
            )
        }
    )

    orders, _ = await make_adapter(fake).fetch_orders_updated_since(SINCE)

    assert orders[0].fulfillment_status is FulfillmentStatus.PARTIALLY_SHIPPED


async def test_delivered_lines_roll_up_to_delivered() -> None:
    payload = order_payload(
        lines=[
            _line(line_number="1", sku="SKU-A", quantity="1", product="5.00", status="Delivered")
        ]
    )
    fake = FakeWalmart(
        {
            ("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(
                200, json={"list": {"elements": {"order": [payload]}, "meta": {}}}
            )
        }
    )

    orders, _ = await make_adapter(fake).fetch_orders_updated_since(SINCE)

    assert orders[0].fulfillment_status is FulfillmentStatus.DELIVERED


async def test_explicit_unpaid_signal_downgrades_payment_status() -> None:
    payload = order_payload(paymentStatus="PENDING")
    fake = FakeWalmart(
        {
            ("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(
                200, json={"list": {"elements": {"order": [payload]}, "meta": {}}}
            )
        }
    )

    orders, _ = await make_adapter(fake).fetch_orders_updated_since(SINCE)

    assert orders[0].payment_status is PaymentStatus.UNPAID


async def test_absent_commission_is_zero_and_not_invented() -> None:
    payload = order_payload()
    del payload["commission"]
    fake = FakeWalmart(
        {
            ("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(
                200, json={"list": {"elements": {"order": [payload]}, "meta": {}}}
            )
        }
    )

    orders, _ = await make_adapter(fake).fetch_orders_updated_since(SINCE)

    assert orders[0].platform_commission_base == Decimal("0.00")
    assert "platform_commission_base" not in orders[0].channel_metadata


async def test_mixed_line_currency_is_refused() -> None:
    payload = order_payload(
        lines=[
            _line(line_number="1", sku="SKU-A", quantity="1", product="5.00", currency="USD"),
            _line(line_number="2", sku="SKU-B", quantity="1", product="5.00", currency="CAD"),
        ]
    )
    fake = FakeWalmart(
        {
            ("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(
                200, json={"list": {"elements": {"order": [payload]}, "meta": {}}}
            )
        }
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).fetch_orders_updated_since(SINCE)

    assert (excinfo.value.code, excinfo.value.status_code) == (
        "ERR_WALMART_MIXED_ORDER_CURRENCY",
        502,
    )
    assert "CAD" in excinfo.value.detail and "USD" in excinfo.value.detail


async def test_non_numeric_money_raises_instead_of_defaulting_to_zero() -> None:
    line = _line(line_number="1", sku="SKU-A", quantity="1", product="12.00")
    line["charges"]["charge"][0]["chargeAmount"]["amount"] = "not-a-number"
    payload = order_payload(lines=[line])
    fake = FakeWalmart(
        {
            ("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(
                200, json={"list": {"elements": {"order": [payload]}, "meta": {}}}
            )
        }
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).fetch_orders_updated_since(SINCE)

    assert (excinfo.value.slug, excinfo.value.code, excinfo.value.status_code) == (
        "walmart-invalid-money",
        "ERR_WALMART_INVALID_MONEY",
        502,
    )


async def test_order_sync_error_detail_is_redacted() -> None:
    fake = FakeWalmart(
        {
            ("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(
                500,
                text=f"upstream exploded with {CLIENT_SECRET} and {ACCESS_TOKEN}",
            )
        }
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).fetch_orders_updated_since(SINCE)

    error = excinfo.value
    assert (error.slug, error.code, error.status_code) == (
        "walmart-order-sync-failed",
        "ERR_WALMART_ORDER_SYNC_FAILED",
        502,
    )
    assert CLIENT_SECRET not in error.detail
    assert ACCESS_TOKEN not in error.detail
    assert "[redacted]" in error.detail


async def test_order_sync_401_maps_to_auth_failed() -> None:
    fake = FakeWalmart(
        {("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(401, json={})}
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).fetch_orders_updated_since(SINCE)

    assert (excinfo.value.code, excinfo.value.status_code) == (
        "ERR_WALMART_AUTH_FAILED",
        502,
    )


async def test_malformed_json_body_is_treated_as_an_empty_feed() -> None:
    fake = FakeWalmart(
        {
            ("GET", WALMART_ORDERS_PATH): lambda _request: httpx.Response(
                200, text="<html>maintenance</html>"
            )
        }
    )

    orders, cursor = await make_adapter(fake).fetch_orders_updated_since(SINCE)

    assert orders == []
    assert cursor is None


# --------------------------------------------------------------------------- product sync


async def test_fetch_products_paginates_on_offset_cursor() -> None:
    rows = [
        {
            "sku": "SKU-A",
            "productName": "Red Lamp",
            "price": {"amount": "19.99", "currency": "USD"},
            "publishedStatus": "PUBLISHED",
            "lifecycleStatus": "ACTIVE",
            "variantGroupId": "GRP-1",
            "gtin": "00012345600012",
            "availability": {"amount": 7},
        },
        {"productName": "no sku here"},
        {
            "sku": "SKU-B",
            "productName": "Blue Lamp",
            "price": {"amount": "9.5"},
            "publishedStatus": "UNPUBLISHED",
        },
    ]
    fake = FakeWalmart(
        {
            ("GET", WALMART_ITEMS_PATH): lambda _request: httpx.Response(
                200, json={"ItemResponse": rows, "totalItems": 5}
            )
        }
    )
    adapter = make_adapter(fake)

    products, cursor = await adapter.fetch_products_updated_since(SINCE)
    assert cursor == "3", "the cursor advances by every raw row the feed returned"

    products, cursor = await adapter.fetch_products_updated_since(SINCE, cursor)
    assert fake.sent(WALMART_ITEMS_PATH)[1].url.params["offset"] == "3"
    assert cursor is None, "3 + 3 rows exhausted the 5-row feed"

    assert products == [
        NormalizedProduct(
            external_product_id="SKU-A",
            title="Red Lamp",
            variants=[
                {
                    "sku": "SKU-A",
                    "title": "Red Lamp",
                    "price": "19.99",
                    "currency": "USD",
                    "published_status": "PUBLISHED",
                    "lifecycle_status": "ACTIVE",
                    "variant_group_id": "GRP-1",
                    "gtin": "00012345600012",
                    "available_quantity": 7,
                }
            ],
        ),
        NormalizedProduct(
            external_product_id="SKU-B",
            title="Blue Lamp",
            variants=[
                {
                    "sku": "SKU-B",
                    "title": "Blue Lamp",
                    "price": "9.50",
                    "currency": None,
                    "published_status": "UNPUBLISHED",
                    "lifecycle_status": None,
                    "variant_group_id": None,
                    "gtin": None,
                    "available_quantity": None,
                }
            ],
        ),
    ]
    assert fake.sent(WALMART_ITEMS_PATH)[0].url.params["limit"] == str(WALMART_PAGE_LIMIT)


async def test_short_product_page_without_total_ends_the_feed() -> None:
    fake = FakeWalmart(
        {("GET", WALMART_ITEMS_PATH): lambda _request: httpx.Response(200, json={})}
    )

    products, cursor = await make_adapter(fake).fetch_products_updated_since(SINCE)

    assert products == []
    assert cursor is None


async def test_invalid_product_cursor_is_rejected() -> None:
    fake = FakeWalmart({("GET", WALMART_ITEMS_PATH): empty_items})

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).fetch_products_updated_since(SINCE, "not-a-number")

    assert (excinfo.value.slug, excinfo.value.status_code) == (
        "walmart-invalid-cursor",
        422,
    )
    assert fake.sent(WALMART_ITEMS_PATH) == []


async def test_product_sync_error_is_typed_and_redacted() -> None:
    fake = FakeWalmart(
        {
            ("GET", WALMART_ITEMS_PATH): lambda _request: httpx.Response(
                503, text=f"maintenance window for {ACCESS_TOKEN}"
            )
        }
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).fetch_products_updated_since(SINCE)

    assert (excinfo.value.slug, excinfo.value.code) == (
        "walmart-product-sync-failed",
        "ERR_WALMART_PRODUCT_SYNC_FAILED",
    )
    assert ACCESS_TOKEN not in excinfo.value.detail


# --------------------------------------------------------------------------- fulfillment


def _shipment_order(line_numbers: tuple[str, ...] = ("1", "2")) -> dict[str, Any]:
    return {
        "purchaseOrderId": "PO-SHIP-1",
        "orderLines": {
            "orderLine": [
                {
                    "lineNumber": line_number,
                    "orderLineQuantity": {"unitOfMeasurement": "EACH", "amount": "2"},
                }
                for line_number in line_numbers
            ]
        },
    }


async def test_create_fulfillment_reads_lines_then_ships_every_line() -> None:
    fake = FakeWalmart(
        {
            ("GET", f"{WALMART_ORDERS_PATH}/PO-SHIP-1"): lambda _request: httpx.Response(
                200, json=_shipment_order()
            ),
            ("POST", f"{WALMART_ORDERS_PATH}/PO-SHIP-1/shipping"): lambda _request: (
                httpx.Response(200, json={"purchaseOrderId": "PO-SHIP-1"})
            ),
        }
    )
    adapter = make_adapter(fake, fulfillment_mode="mcs")

    result = await adapter.create_fulfillment(
        "PO-SHIP-1",
        "1Z999AA10123456784",
        "UPS",
        tracking_url="https://tracking.example/1Z999AA10123456784",
        fulfillment_order_id="FUL-9",
    )

    assert result["channel"] == "walmart"
    assert result["purchase_order_id"] == "PO-SHIP-1"
    assert result["tracking_number"] == "1Z999AA10123456784"
    assert result["fulfillment_mode"] == "mcs"
    assert result["line_numbers"] == ["1", "2"]

    payload = body(fake.only("POST", f"{WALMART_ORDERS_PATH}/PO-SHIP-1/shipping"))
    lines = payload["orderShipment"]["orderLines"]["orderLine"]
    assert [line["lineNumber"] for line in lines] == ["1", "2"]
    status = lines[0]["orderLineStatuses"]["orderLineStatus"][0]
    assert status["status"] == "Shipped"
    assert status["statusQuantity"] == {"unitOfMeasurement": "EACH", "amount": "2"}
    tracking = status["trackingInfo"]
    assert tracking["trackingNumber"] == "1Z999AA10123456784"
    assert tracking["trackingURL"] == "https://tracking.example/1Z999AA10123456784"
    assert tracking["carrierName"] == {"carrier": "UPS", "otherCarrier": "UPS"}
    assert isinstance(tracking["shipDateTime"], int)
    assert "PO-SHIP-1" in fake.only("GET", f"{WALMART_ORDERS_PATH}/PO-SHIP-1").url.path


async def test_unknown_carrier_falls_back_to_the_other_enum() -> None:
    fake = FakeWalmart(
        {
            ("GET", f"{WALMART_ORDERS_PATH}/PO-SHIP-2"): lambda _request: httpx.Response(
                200, json=_shipment_order(("1",))
            ),
            ("POST", f"{WALMART_ORDERS_PATH}/PO-SHIP-2/shipping"): lambda _request: (
                httpx.Response(200, json={})
            ),
        }
    )

    await make_adapter(fake).create_fulfillment("PO-SHIP-2", "TRACK-1", "Speedy  Express")

    payload = body(fake.only("POST", f"{WALMART_ORDERS_PATH}/PO-SHIP-2/shipping"))
    carrier = payload["orderShipment"]["orderLines"]["orderLine"][0]["orderLineStatuses"][
        "orderLineStatus"
    ][0]["trackingInfo"]["carrierName"]
    assert carrier == {"carrier": "Other", "otherCarrier": "Speedy Express"}


async def test_create_fulfillment_requires_a_tracking_number() -> None:
    fake = FakeWalmart()
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment("PO-1", "   ", "UPS")

    assert (excinfo.value.slug, excinfo.value.code, excinfo.value.status_code) == (
        "walmart-tracking-number-required",
        "ERR_WALMART_TRACKING_NUMBER_REQUIRED",
        422,
    )
    assert fake.requests == []


async def test_create_fulfillment_requires_an_order_id() -> None:
    fake = FakeWalmart()

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).create_fulfillment("  ", "TRACK-1", "UPS")

    assert excinfo.value.code == "ERR_WALMART_FULFILLMENT_REJECTED"
    assert fake.requests == []


async def test_create_fulfillment_rejects_an_order_without_shippable_lines() -> None:
    fake = FakeWalmart(
        {
            ("GET", f"{WALMART_ORDERS_PATH}/PO-EMPTY"): lambda _request: httpx.Response(
                200, json={"purchaseOrderId": "PO-EMPTY", "orderLines": {"orderLine": []}}
            )
        }
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).create_fulfillment("PO-EMPTY", "TRACK-1", "UPS")

    assert (excinfo.value.slug, excinfo.value.code) == (
        "walmart-fulfillment-rejected",
        "ERR_WALMART_FULFILLMENT_REJECTED",
    )
    assert fake.sent(f"{WALMART_ORDERS_PATH}/PO-EMPTY/shipping") == []


async def test_create_fulfillment_propagates_remote_rejection_without_secrets() -> None:
    fake = FakeWalmart(
        {
            ("GET", f"{WALMART_ORDERS_PATH}/PO-BAD"): lambda _request: httpx.Response(
                200, json=_shipment_order(("1",))
            ),
            ("POST", f"{WALMART_ORDERS_PATH}/PO-BAD/shipping"): lambda _request: (
                httpx.Response(400, text=f"invalid carrier for token {ACCESS_TOKEN}")
            ),
        }
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).create_fulfillment("PO-BAD", "TRACK-9", "UPS")

    error = excinfo.value
    assert (error.slug, error.code, error.status_code) == (
        "walmart-fulfillment-rejected",
        "ERR_WALMART_FULFILLMENT_REJECTED",
        502,
    )
    assert ACCESS_TOKEN not in error.detail
    assert "[redacted]" in error.detail


async def test_create_fulfillment_order_lookup_failure_is_typed() -> None:
    fake = FakeWalmart(
        {("GET", f"{WALMART_ORDERS_PATH}/PO-404"): lambda _request: httpx.Response(404, json={})}
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).create_fulfillment("PO-404", "TRACK-1", "UPS")

    assert (excinfo.value.slug, excinfo.value.code) == (
        "walmart-fulfillment-rejected",
        "ERR_WALMART_FULFILLMENT_REJECTED",
    )


# ------------------------------------------------------------------------- webhook verify


def _signature(body_bytes: bytes, secret: str, *, encoding: str) -> str:
    digest = hmac.new(secret.encode(), body_bytes, hashlib.sha256).digest()
    return digest.hex() if encoding == "hex" else base64.b64encode(digest).decode()


def test_verify_webhook_accepts_hex_signature_on_wm_sec_header() -> None:
    raw = b'{"eventType":"ORDER_UPDATES","purchaseOrderId":"123"}'

    assert verify_walmart_webhook(
        raw, {"wm-sec.signature": _signature(raw, CLIENT_SECRET, encoding="hex")}, CLIENT_SECRET
    )


def test_verify_webhook_accepts_base64_signature_on_x_walmart_header() -> None:
    raw = b'{"eventType":"ORDER_UPDATES"}'
    encoded = _signature(raw, CLIENT_SECRET, encoding="base64")

    assert verify_walmart_webhook(
        raw, {"X-Walmart-Signature": encoded}, CLIENT_SECRET
    )
    # Some relays strip the base64 padding; the digest is unchanged.
    assert verify_walmart_webhook(
        raw, {"x-walmart-signature": encoded.rstrip("=")}, CLIENT_SECRET
    )
    assert verify_walmart_webhook(
        raw, {"x-walmart-signature": f"sha256={encoded}"}, CLIENT_SECRET
    )


@pytest.mark.parametrize(
    ("raw", "headers", "secret"),
    [
        (b"{}", {"wm-sec.signature": ""}, CLIENT_SECRET),
        (b"{}", {}, CLIENT_SECRET),
        (b"", {"wm-sec.signature": "deadbeef"}, CLIENT_SECRET),
        (b"{}", {"wm-sec.signature": "deadbeef"}, ""),
        (b"{}", {"wm-sec.signature": "deadbeef"}, CLIENT_SECRET),
    ],
)
def test_verify_webhook_fails_closed(
    raw: bytes, headers: dict[str, str], secret: str
) -> None:
    assert verify_walmart_webhook(raw, headers, secret) is False


def test_verify_webhook_rejects_a_tampered_body() -> None:
    secret = CLIENT_SECRET
    signature = _signature(b'{"amount":1}', secret, encoding="hex")

    assert not verify_walmart_webhook(
        b'{"amount":2}', {"wm-sec.signature": signature}, secret
    )


def test_adapter_verify_webhook_uses_the_configured_client_secret() -> None:
    raw = b'{"eventType":"ORDER_UPDATES"}'
    adapter = WalmartAdapter(
        seller_id=SELLER_ID, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
    )

    assert adapter.verify_webhook(
        raw, {"x-walmart-signature": _signature(raw, CLIENT_SECRET, encoding="hex")}, ""
    )
    assert not adapter.verify_webhook(
        raw, {"x-walmart-signature": _signature(raw, "other-secret", encoding="hex")}, ""
    )
    assert adapter.verify_webhook(
        raw, {"x-walmart-signature": _signature(raw, "explicit", encoding="hex")}, "explicit"
    )


# ---------------------------------------------------------------------- listing publish


def listing_payload(**overrides: Any) -> MarketplaceListingPayload:
    values: dict[str, Any] = {
        "channel_account_id": CHANNEL_ACCOUNT_ID,
        "channel_type": "walmart",
        "external_sku": "OMNI-LAMP-01",
        "title": "Omni LED Desk Lamp",
        "description": "A dimmable LED desk lamp.\nSecond paragraph.",
        "bullet_points": ["Dimmable", "USB-C powered"],
        "price_usd": Decimal("19.99"),
        "quantity": 25,
        "images": [
            "https://cdn.example/lamp-main.jpg",
            "https://cdn.example/lamp-side.jpg",
            "http://insecure.example/dropped.jpg",
        ],
        "category": "LED Lighting",
    }
    values.update(overrides)
    return MarketplaceListingPayload(**values)


async def test_publish_listing_submits_an_mp_item_feed() -> None:
    fake = FakeWalmart(
        {("POST", WALMART_FEEDS_PATH): lambda _request: httpx.Response(202, json={"feedId": "F-1"})}
    )
    adapter = make_adapter(fake)

    receipt = await adapter.publish_listing(listing_payload())

    request = fake.only("POST", WALMART_FEEDS_PATH)
    assert request.url.params["feedType"] == "MP_ITEM"
    feed = body(request)
    assert feed["MPItemFeedHeader"] == {
        "version": "3.0",
        "feedType": "MP_ITEM",
        "locale": "en",
    }
    orderable = feed["MPItem"][0]["Orderable"]
    assert orderable["sku"] == "OMNI-LAMP-01"
    assert orderable["productName"] == "Omni LED Desk Lamp"
    assert orderable["price"] == {"currency": "USD", "amount": "19.99"}
    shelf = orderable["children"]["Shelf"]
    assert shelf["longDescription"] == "A dimmable LED desk lamp.\nSecond paragraph."
    assert shelf["shortDescription"] == "Dimmable"
    assert shelf["keyFeatures"] == {"keyFeature": ["Dimmable", "USB-C powered"]}
    assert shelf["mainImageUrl"] == "https://cdn.example/lamp-main.jpg"
    assert shelf["productSecondaryImageURL"] == {
        "productImageUrl": ["https://cdn.example/lamp-side.jpg"]
    }, "non-https image URLs are dropped"

    assert receipt.external_product_id == "OMNI-LAMP-01"
    assert receipt.external_sku == "OMNI-LAMP-01"
    assert receipt.status == "submitted"
    assert receipt.raw_response["feedId"] == "F-1"
    assert receipt.raw_response["itemCount"] == 1
    assert [variant.external_sku for variant in receipt.variant_receipts] == ["OMNI-LAMP-01"]


async def test_publish_listing_groups_variants_options_and_images() -> None:
    payload = listing_payload(
        options=[SourcingOptionSpec(name="Color", values=["Red", "Blue"])],
        variants=[
            MarketplaceVariantPayload(
                local_sku="OMNI-LAMP-01-RED",
                external_sku="OMNI-LAMP-01-RED",
                option_values={"Color": "Red"},
                price_usd=Decimal("19.99"),
                quantity=10,
                image_url="https://cdn.example/lamp-red.jpg",
            ),
            MarketplaceVariantPayload(
                local_sku="OMNI-LAMP-01-BLUE",
                external_sku="OMNI-LAMP-01-BLUE",
                option_values={"Color": "Blue"},
                price_usd=Decimal("21.5"),
                quantity=15,
                image_url="https://cdn.example/lamp-blue.jpg",
            ),
        ],
    )
    fake = FakeWalmart(
        {("POST", WALMART_FEEDS_PATH): lambda _request: httpx.Response(202, json={"feedId": "F-2"})}
    )

    receipt = await make_adapter(fake).publish_listing(payload)

    items = body(fake.only("POST", WALMART_FEEDS_PATH))["MPItem"]
    assert len(items) == 2
    first, second = items[0]["Orderable"], items[1]["Orderable"]
    assert first["sku"] == "OMNI-LAMP-01-RED"
    assert first["price"]["amount"] == "19.99"
    assert first["variantGroupId"] == "OMNI-LAMP-01"
    assert first["variantAttributeNames"] == {"variantAttributeName": ["Color"]}
    assert first["isPrimaryVariant"] == "Yes"
    assert first["children"]["Shelf"]["mainImageUrl"] == "https://cdn.example/lamp-red.jpg"
    assert second["sku"] == "OMNI-LAMP-01-BLUE"
    assert second["price"]["amount"] == "21.50"
    assert second["isPrimaryVariant"] == "No"
    assert second["children"]["Shelf"]["mainImageUrl"] == "https://cdn.example/lamp-blue.jpg"

    assert [variant.external_sku for variant in receipt.variant_receipts] == [
        "OMNI-LAMP-01-RED",
        "OMNI-LAMP-01-BLUE",
    ]
    assert all(variant.external_variant_id is None for variant in receipt.variant_receipts)


async def test_publish_listing_rejects_an_empty_sku() -> None:
    fake = FakeWalmart()

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).publish_listing(listing_payload(external_sku="  "))

    assert (excinfo.value.slug, excinfo.value.code, excinfo.value.status_code) == (
        "walmart-listing-rejected",
        "ERR_WALMART_LISTING_REJECTED",
        422,
    )
    assert fake.requests == []


async def test_publish_listing_refuses_float_prices() -> None:
    payload = MarketplaceListingPayload.model_construct(
        channel_account_id=CHANNEL_ACCOUNT_ID,
        channel_type="walmart",
        external_sku="OMNI-LAMP-01",
        title="Omni LED Desk Lamp",
        description="desc",
        bullet_points=[],
        price_usd=19.99,
        quantity=1,
        images=[],
        category="LED Lighting",
        hs_code=None,
        moq_tiers=[],
        options=[],
        variants=[],
        image_blobs=[],
    )
    fake = FakeWalmart()

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).publish_listing(payload)

    assert (excinfo.value.slug, excinfo.value.code, excinfo.value.status_code) == (
        "walmart-invalid-money",
        "ERR_WALMART_INVALID_MONEY",
        422,
    )
    assert fake.requests == []


async def test_publish_listing_remote_rejection_is_typed_and_redacted() -> None:
    fake = FakeWalmart(
        {
            ("POST", WALMART_FEEDS_PATH): lambda _request: httpx.Response(
                400, text=f"feed rejected for app secret {CLIENT_SECRET}"
            )
        }
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).publish_listing(listing_payload())

    error = excinfo.value
    assert (error.slug, error.code, error.status_code) == (
        "walmart-listing-rejected",
        "ERR_WALMART_LISTING_REJECTED",
        502,
    )
    assert CLIENT_SECRET not in error.detail
    assert "[redacted]" in error.detail


async def test_publish_listing_without_a_feed_id_fails_closed() -> None:
    fake = FakeWalmart(
        {("POST", WALMART_FEEDS_PATH): lambda _request: httpx.Response(200, json={})}
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).publish_listing(listing_payload())

    assert (excinfo.value.slug, excinfo.value.code) == (
        "walmart-listing-rejected",
        "ERR_WALMART_LISTING_REJECTED",
    )
    assert "feedId" in excinfo.value.detail


# ---------------------------------------------------------------------- inventory push


async def test_push_inventory_level_clamps_negative_quantity() -> None:
    fake = FakeWalmart(
        {
            ("PUT", WALMART_INVENTORY_PATH): lambda _request: httpx.Response(
                200, json={"sku": "SKU-A"}
            )
        }
    )
    adapter = make_adapter(fake)

    result = await adapter.push_inventory_level(
        external_sku=" SKU-A ",
        quantity=-4,
        external_product_id="PROD-1",
        external_variant_id="VAR-1",
        external_inventory_item_id="INV-1",
        location_id="LOC-1",
        warehouse_id="WH-1",
    )

    request = fake.only("PUT", WALMART_INVENTORY_PATH)
    assert request.url.params["sku"] == "SKU-A"
    assert body(request) == {
        "sku": "SKU-A",
        "quantity": {"unit": "EACH", "amount": 0},
    }
    assert result["channel"] == "walmart"
    assert result["external_sku"] == "SKU-A"
    assert result["quantity"] == 0
    assert result["external_inventory_item_id"] == "INV-1"
    assert result["location_id"] == "LOC-1"
    assert result["warehouse_id"] == "WH-1"
    assert result["raw"] == {"sku": "SKU-A"}


async def test_push_inventory_level_coerces_string_quantities() -> None:
    fake = FakeWalmart(
        {("PUT", WALMART_INVENTORY_PATH): lambda _request: httpx.Response(200, json={})}
    )

    result = await make_adapter(fake).push_inventory_level(
        external_sku="SKU-A", quantity="7"  # type: ignore[arg-type]
    )

    assert result["quantity"] == 7
    assert body(fake.only("PUT", WALMART_INVENTORY_PATH))["quantity"]["amount"] == 7


async def test_push_inventory_level_requires_a_sku() -> None:
    fake = FakeWalmart()

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).push_inventory_level(external_sku="  ", quantity=1)

    assert (excinfo.value.slug, excinfo.value.code, excinfo.value.status_code) == (
        "walmart-inventory-sync-failed",
        "ERR_WALMART_INVENTORY_SYNC",
        422,
    )
    assert fake.requests == []


async def test_push_inventory_level_error_is_typed_and_redacted() -> None:
    fake = FakeWalmart(
        {
            ("PUT", WALMART_INVENTORY_PATH): lambda _request: httpx.Response(
                400, text=f"invalid sku, token {ACCESS_TOKEN}"
            )
        }
    )

    with pytest.raises(AppError) as excinfo:
        await make_adapter(fake).push_inventory_level(external_sku="SKU-A", quantity=1)

    error = excinfo.value
    assert (error.slug, error.code, error.status_code) == (
        "walmart-inventory-sync-failed",
        "ERR_WALMART_INVENTORY_SYNC",
        502,
    )
    assert ACCESS_TOKEN not in error.detail
    assert "[redacted]" in error.detail


async def test_push_inventory_level_forwards_ship_node_metadata() -> None:
    fake = FakeWalmart(
        {("PUT", WALMART_INVENTORY_PATH): lambda _request: httpx.Response(204)}
    )

    result = await make_adapter(fake).push_inventory_level(
        external_sku="SKU-A", quantity=3, location_id="WFS-NODE-1"
    )

    assert result["quantity"] == 3
    assert result["location_id"] == "WFS-NODE-1"
    assert result["warehouse_id"] is None
    assert result["raw"] == {}


def test_listing_payload_channel_account_id_is_a_uuid() -> None:
    assert isinstance(listing_payload().channel_account_id, UUID)
