"""Zero-network unit tests for the Mercado Libre connector adapter."""

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
    MarketplaceVariantPayload,
    SourcingMoqTier,
    SourcingOptionSpec,
)
from erp_core.channels.ecommerce.mercado_libre import (
    MERCADO_LIBRE_DEFAULT_CATEGORY_ID,
    MercadoLibreAdapter,
    verify_mercado_libre_webhook,
)
from erp_core.shared.enums import FulfillmentStatus, PaymentStatus
from erp_core.shared.errors import AppError

SELLER_ID = "123456789"
ACCESS_TOKEN = "APP_USR-test-access-token-secret"
REFRESH_TOKEN = "TG-test-refresh-token-secret"
WEBHOOK_SECRET = "ml-webhook-secret"
SINCE = datetime(2024, 5, 1, tzinfo=UTC)

ORDERS_SEARCH_PATH = "/orders/search"
ITEMS_PATH = "/items"
SHIPMENTS_PATH = "/shipments"
ITEMS_SEARCH_PATH = f"/users/{SELLER_ID}/items/search"

Route = Callable[[httpx.Request], httpx.Response]


class FakeMercadoLibre:
    """Deterministic route table standing in for every Mercado Libre endpoint."""

    def __init__(self, routes: dict[tuple[str, str], Route] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(
                404, json={"message": f"unrouted {request.method} {request.url.path}"}
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


def make_adapter(fake: FakeMercadoLibre, **overrides: Any) -> MercadoLibreAdapter:
    return MercadoLibreAdapter(
        seller_id=SELLER_ID,
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
        transport=fake.transport,
        **overrides,
    )


def body(request: httpx.Request) -> dict[str, Any]:
    parsed = json.loads(request.content)
    assert isinstance(parsed, dict)
    return parsed


def empty_orders(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"results": [], "paging": {"total": 0, "offset": 0, "limit": 50}}
    )


def order_payload(**overrides: Any) -> dict[str, Any]:
    """A paid MXN order with two lines, a shipment and a buyer profile."""
    payload: dict[str, Any] = {
        "id": 2000012345678,
        "status": "paid",
        "status_detail": None,
        "date_created": "2024-05-02T10:15:30.000-04:00",
        "last_updated": "2024-05-02T11:00:00.000-04:00",
        "currency_id": "MXN",
        "order_items": [
            {
                "item": {"id": "MLM1001", "title": "Lámpara LED", "seller_sku": "SKU-RED-L"},
                "quantity": 2,
                "unit_price": 12.75,
                "sale_fee": 0.125,
            },
            {
                "item": {"id": "MLM1002", "title": "Bulb", "seller_custom_field": "SKU-BULB"},
                "quantity": 1,
                "unit_price": 3.00,
                "sale_fee": 1.535,
            },
        ],
        "payments": [
            {"id": 1, "status": "approved", "transaction_amount": 32.84, "currency_id": "MXN"}
        ],
        "shipping": {"id": 412345678},
        "shipping_cost": 4.99,
        "taxes": {"amount": 2.35},
        "total_amount": 32.84,
        "buyer": {
            "id": 987,
            "nickname": "BUYER_ONE",
            "email": "Buyer_One@Example.com",
            "phone": {"area_code": "55", "number": "1234-5678"},
            "billing_info": {"name": "Ada", "last_name": "Lovelace"},
        },
        "tags": ["paid", "not_delivered"],
    }
    payload.update(overrides)
    return payload


def listing_payload(**overrides: Any) -> MarketplaceListingPayload:
    base: dict[str, Any] = {
        "channel_account_id": uuid4(),
        "channel_type": "mercado_libre",
        "external_sku": "SKU-PARENT",
        "title": "LED Panel Light 40W " * 5,
        "description": "Wall-mounted LED panel for workshops.",
        "bullet_points": ["Energy saving", "5-year warranty"],
        "price_usd": Decimal("19.999"),
        "quantity": 7,
        "images": ["https://cdn.example.com/a.jpg", "http://insecure.example.com/b.jpg"],
        "category": "LED Lighting",
        "hs_code": "9405.40",
        "moq_tiers": [SourcingMoqTier(min_qty=10, unit_price_usd=Decimal("17.50"))],
        "options": [SourcingOptionSpec(name="Color", values=["Red", "Blue"])],
        "variants": [
            MarketplaceVariantPayload(
                local_sku="SKU-PARENT-RED",
                external_sku="SKU-RED",
                option_values={"Color": "Red", "Size": "L"},
                price_usd=Decimal("19.999"),
                quantity=5,
                image_url="https://cdn.example.com/red.jpg",
            ),
            MarketplaceVariantPayload(
                local_sku="SKU-PARENT-BLUE",
                external_sku="SKU-BLUE",
                option_values={"Color": "Blue"},
                price_usd=Decimal("21.5"),
                quantity=2,
            ),
        ],
    }
    base.update(overrides)
    return MarketplaceListingPayload(**base)


def created_item(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "id": "MLM2001",
            "status": "active",
            "variations": [
                {"id": 111, "seller_custom_field": "SKU-RED"},
                {"id": 222, "seller_custom_field": "SKU-BLUE"},
            ],
        },
    )


def description_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "MLM2001", "plain_text": "ok"})


def publish_routes() -> dict[tuple[str, str], Route]:
    return {
        ("POST", ITEMS_PATH): created_item,
        ("PUT", f"{ITEMS_PATH}/MLM2001/description"): description_ok,
    }


# ---------------------------------------------------------------------------- constructor


def test_constructor_rejects_a_blank_seller_id() -> None:
    with pytest.raises(AppError) as excinfo:
        MercadoLibreAdapter(seller_id="  ", access_token=ACCESS_TOKEN)

    assert (excinfo.value.code, excinfo.value.status_code) == (
        "ERR_MERCADO_LIBRE_SELLER_ID_REQUIRED",
        422,
    )


def test_constructor_rejects_a_blank_access_token() -> None:
    with pytest.raises(AppError) as excinfo:
        MercadoLibreAdapter(seller_id=SELLER_ID, access_token="")

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_ACCESS_TOKEN_REQUIRED"


@pytest.mark.parametrize("mode", ["express", "me2", "drop-ship"])
def test_constructor_rejects_an_unknown_fulfillment_mode(mode: str) -> None:
    with pytest.raises(AppError) as excinfo:
        MercadoLibreAdapter(
            seller_id=SELLER_ID, access_token=ACCESS_TOKEN, fulfillment_mode=mode
        )

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_FULFILLMENT_MODE_INVALID"
    assert mode in excinfo.value.detail
    assert "standard" in excinfo.value.detail


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_constructor_rejects_a_non_positive_timeout(timeout: float) -> None:
    with pytest.raises(AppError) as excinfo:
        MercadoLibreAdapter(
            seller_id=SELLER_ID, access_token=ACCESS_TOKEN, timeout_seconds=timeout
        )

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_TIMEOUT_INVALID"


def test_endpoint_currency_and_mode_defaults_and_repr_redaction() -> None:
    default = MercadoLibreAdapter(seller_id=SELLER_ID, access_token=ACCESS_TOKEN)
    brazil = MercadoLibreAdapter(
        seller_id=SELLER_ID, access_token=ACCESS_TOKEN, site_id="mlb", fulfillment_mode="FULL"
    )
    managed = MercadoLibreAdapter(
        seller_id=SELLER_ID, access_token=ACCESS_TOKEN, fulfillment_mode="full_managed"
    )
    assert managed.fulfillment_mode == "full"
    overridden = MercadoLibreAdapter(
        seller_id=SELLER_ID,
        access_token=ACCESS_TOKEN,
        endpoint_url="https://ml-gateway.internal/",
    )

    assert default.endpoint_url == "https://api.mercadolibre.com"
    assert (default.site_id, default.currency_id) == ("MLM", "MXN")
    assert (default.fulfillment_mode, default.currency_id) == ("standard", "MXN")
    assert (brazil.site_id, brazil.currency_id, brazil.fulfillment_mode) == (
        "MLB",
        "BRL",
        "full",
    )
    assert overridden.endpoint_url == "https://ml-gateway.internal"
    rendered = f"{default!r} {default} {brazil!r}"
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered


# ------------------------------------------------------------------------------ order sync


async def test_fetch_orders_sends_seller_window_params_and_bearer_token() -> None:
    fake = FakeMercadoLibre({("GET", ORDERS_SEARCH_PATH): empty_orders})
    adapter = make_adapter(fake)

    orders, cursor = await adapter.fetch_orders_updated_since(SINCE)

    assert (orders, cursor) == ([], None)
    request = fake.only("GET", ORDERS_SEARCH_PATH)
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    params = request.url.params
    assert params["seller"] == SELLER_ID
    assert params["order.date_last_updated.from"] == "2024-05-01T00:00:00.000-00:00"
    assert (params["limit"], params["offset"], params["sort"]) == ("50", "0", "date_desc")


async def test_fetch_orders_normalizes_amounts_commission_and_customer() -> None:
    fake = FakeMercadoLibre(
        {
            ("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(
                200, json={"results": [order_payload()]}
            )
        }
    )
    adapter = make_adapter(fake)

    orders, _cursor = await adapter.fetch_orders_updated_since(SINCE)

    assert len(orders) == 1
    order = orders[0]
    assert order.external_order_id == "2000012345678"
    assert order.external_order_number == "2000012345678"
    assert order.currency == "MXN"
    assert order.fx_rate_to_base is None
    assert order.subtotal_amount == Decimal("28.50")
    assert order.shipping_charged == Decimal("4.99")
    assert order.tax_collected == Decimal("2.35")
    assert order.total_amount == Decimal("32.84")
    # 0.125 + 1.535 -> ROUND_HALF_UP -> 0.13 + 1.54
    assert order.platform_commission_base == Decimal("1.67")
    assert order.channel_metadata["platform_commission_base"] == "1.67"
    assert "refunded_amount" not in order.channel_metadata
    assert order.fulfillment_status is FulfillmentStatus.CONFIRMED_RESERVED
    assert order.payment_status is PaymentStatus.PAID
    assert order.channel_metadata["mercado_libre_shipment_id"] == "412345678"
    assert order.channel_metadata["mercado_libre_order_status"] == "paid"
    assert order.channel_metadata["mercado_libre_tags"] == ["not_delivered", "paid"]
    assert order.customer.email == "buyer_one@example.com"
    assert order.customer.phone_e164 == "+5512345678"
    assert order.customer.display_name == "Ada Lovelace"
    assert order.customer.external_customer_id == "987"
    assert order.line_items[0].external_sku == "SKU-RED-L"
    assert order.line_items[0].quantity == 2
    assert order.line_items[0].line_total == Decimal("25.50")
    assert order.line_items[1].external_sku == "SKU-BULB"
    assert order.updated_at == datetime(2024, 5, 2, 15, 0, tzinfo=UTC)
    assert not isinstance(order.total_amount, float)


async def test_fetch_orders_maps_a_cancelled_order_to_refunded_with_full_refund() -> None:
    canceled = order_payload(
        status="cancelled",
        payments=[{"id": 2, "status": "approved", "transaction_amount": 32.84}],
    )
    fake = FakeMercadoLibre(
        {("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(200, json={"results": [canceled]})}
    )
    adapter = make_adapter(fake)

    orders, _cursor = await adapter.fetch_orders_updated_since(SINCE)

    order = orders[0]
    assert order.fulfillment_status is FulfillmentStatus.CANCELLED
    assert order.payment_status is PaymentStatus.REFUNDED
    assert order.channel_metadata["refunded_amount"] == "32.84"


async def test_fetch_orders_sums_only_refunded_payments_for_partial_refunds() -> None:
    partial = order_payload(
        status="partially_refunded",
        payments=[
            {"id": 3, "status": "refunded", "transaction_amount": 10.005},
            {"id": 4, "status": "approved", "transaction_amount": 22.835},
        ],
    )
    fake = FakeMercadoLibre(
        {("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(200, json={"results": [partial]})}
    )
    adapter = make_adapter(fake)

    orders, _cursor = await adapter.fetch_orders_updated_since(SINCE)

    order = orders[0]
    assert order.payment_status is PaymentStatus.PAID
    assert order.fulfillment_status is FulfillmentStatus.CONFIRMED_RESERVED
    assert order.channel_metadata["refunded_amount"] == "10.01"


async def test_fetch_orders_handles_refunded_and_transaction_amount_refunded() -> None:
    tx_refunded = order_payload(
        id=2000012345679,
        status="partially_refunded",
        payments=[
            {
                "id": 5,
                "status": "partially_refunded",
                "transaction_amount": 32.84,
                "transaction_amount_refunded": 5.50,
            }
        ],
    )
    top_full_refund = order_payload(
        id=2000012345680,
        status="paid",
        payments=[{"id": 6, "status": "approved", "transaction_amount": 32.84}],
        refunded_amount=32.84,
    )
    explicit_refunded = order_payload(id=2000012345681, status="refunded", payments=[])
    fake = FakeMercadoLibre(
        {
            ("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(
                200, json={"results": [tx_refunded, top_full_refund, explicit_refunded]}
            )
        }
    )
    orders, _ = await make_adapter(fake).fetch_orders_updated_since(SINCE)
    by_id = {o.external_order_id: o for o in orders}

    assert by_id["2000012345679"].payment_status is PaymentStatus.PAID
    assert by_id["2000012345679"].channel_metadata["refunded_amount"] == "5.50"

    assert by_id["2000012345680"].payment_status is PaymentStatus.REFUNDED
    assert by_id["2000012345680"].fulfillment_status is FulfillmentStatus.REFUNDED
    assert by_id["2000012345680"].channel_metadata["refunded_amount"] == "32.84"

    assert by_id["2000012345681"].payment_status is PaymentStatus.REFUNDED
    assert by_id["2000012345681"].fulfillment_status is FulfillmentStatus.REFUNDED


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("shipped", FulfillmentStatus.SHIPPED), ("delivered", FulfillmentStatus.DELIVERED)],
)
async def test_fetch_orders_reads_fulfillment_state_from_order_tags(
    tag: str, expected: FulfillmentStatus
) -> None:
    tagged = order_payload(tags=[tag])
    fake = FakeMercadoLibre(
        {("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(200, json={"results": [tagged]})}
    )
    adapter = make_adapter(fake)

    orders, _cursor = await adapter.fetch_orders_updated_since(SINCE)

    assert orders[0].fulfillment_status is expected


async def test_fetch_orders_skips_rows_without_line_items_but_still_advances_cursor() -> None:
    rows = [order_payload(), {"id": 999, "status": "paid", "order_items": []}]
    fake = FakeMercadoLibre(
        {
            ("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(
                200, json={"results": rows, "paging": {"total": 5}}
            )
        }
    )
    adapter = make_adapter(fake)

    orders, cursor = await adapter.fetch_orders_updated_since(SINCE)

    assert [order.external_order_id for order in orders] == ["2000012345678"]
    assert cursor == "2"


async def test_fetch_orders_cursor_follows_paging_total_then_stops() -> None:
    fake = FakeMercadoLibre(
        {
            ("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(
                200,
                json={"results": [order_payload()], "paging": {"total": 5, "offset": 2}},
            )
        }
    )
    adapter = make_adapter(fake)

    _orders, cursor = await adapter.fetch_orders_updated_since(SINCE, cursor="2")

    assert fake.only("GET", ORDERS_SEARCH_PATH).url.params["offset"] == "2"
    assert cursor == "3"


async def test_fetch_orders_without_paging_uses_a_short_page_as_end_of_feed() -> None:
    fake = FakeMercadoLibre(
        {("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(200, json={"results": []})}
    )
    adapter = make_adapter(fake)

    _orders, cursor = await adapter.fetch_orders_updated_since(SINCE)

    assert cursor is None


async def test_fetch_orders_rejects_a_non_numeric_cursor() -> None:
    fake = FakeMercadoLibre({})
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE, cursor="page-two")

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_INVALID_CURSOR"
    assert fake.requests == []


async def test_fetch_orders_401_maps_to_auth_failed_without_leaking_tokens() -> None:
    fake = FakeMercadoLibre(
        {
            ("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(
                401, json={"message": f"invalid token {ACCESS_TOKEN} / {REFRESH_TOKEN}"}
            )
        }
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    error = excinfo.value
    assert (error.slug, error.code, error.status_code) == (
        "mercado-libre-auth-failed",
        "ERR_MERCADO_LIBRE_AUTH_FAILED",
        502,
    )
    rendered = f"{error.detail} {error} {adapter!r}"
    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered


async def test_fetch_orders_429_maps_to_rate_limited() -> None:
    fake = FakeMercadoLibre(
        {("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(429, json={"message": "slow"})}
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    assert (excinfo.value.code, excinfo.value.status_code) == (
        "ERR_MERCADO_LIBRE_RATE_LIMITED",
        429,
    )


async def test_fetch_orders_500_maps_to_order_sync_failed_with_redaction() -> None:
    fake = FakeMercadoLibre(
        {
            ("GET", ORDERS_SEARCH_PATH): lambda _r: httpx.Response(
                500, json={"message": f"boom for {ACCESS_TOKEN}"}
            )
        }
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_ORDER_SYNC_FAILED"
    assert "HTTP 500" in excinfo.value.detail
    assert ACCESS_TOKEN not in excinfo.value.detail


async def test_fetch_orders_transport_failure_maps_to_unreachable() -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    fake = FakeMercadoLibre({("GET", ORDERS_SEARCH_PATH): explode})
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_UNREACHABLE"


# --------------------------------------------------------------------------- product sync


def item_body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "MLM1001",
        "title": "Lámpara LED",
        "seller_custom_field": "SKU-RED-L",
        "price": 12.755,
        "available_quantity": 9,
        "last_updated": "2024-05-10T12:00:00.000Z",
        "pictures": [
            {"secure_url": "https://cdn.example.com/main.jpg", "url": "http://cdn.example.com/main.jpg"}
        ],
        "variations": [
            {
                "id": 501,
                "seller_custom_field": "SKU-RED-L",
                "price": 12.755,
                "available_quantity": 4,
                "attribute_combinations": [
                    {"name": "Color", "value_name": "Red"},
                    {"name": "Size", "value_name": "L"},
                ],
                "pictures": [{"secure_url": "https://cdn.example.com/red.jpg"}],
            },
            {
                "id": 502,
                "seller_custom_field": "SKU-BLUE-S",
                "price": 13.5,
                "available_quantity": 5,
                "attribute_combinations": [{"name": "Color", "value_name": "Blue"}],
            },
        ],
    }
    payload.update(overrides)
    return payload


def items_search(results: list[str], total: int | None = None) -> Route:
    def route(_request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = {"results": results}
        if total is not None:
            payload["paging"] = {"total": total, "offset": 0, "limit": 50}
        return httpx.Response(200, json=payload)

    return route


def items_multiget(bodies: list[dict[str, Any]]) -> Route:
    return lambda _request: httpx.Response(
        200, json=[{"code": 200, "body": body} for body in bodies]
    )


async def test_fetch_products_walks_item_search_then_multiget() -> None:
    fake = FakeMercadoLibre(
        {
            ("GET", ITEMS_SEARCH_PATH): items_search(["MLM1001"]),
            ("GET", ITEMS_PATH): items_multiget([item_body()]),
        }
    )
    adapter = make_adapter(fake)

    products, cursor = await adapter.fetch_products_updated_since(SINCE)

    search = fake.only("GET", ITEMS_SEARCH_PATH)
    assert search.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert search.url.params["status"] == "active"
    assert search.url.params["sort"] == "last_updated_desc"
    assert search.url.params["limit"] == "50"
    assert fake.only("GET", ITEMS_PATH).url.params["ids"] == "MLM1001"
    assert cursor is None

    assert len(products) == 1
    product = products[0]
    assert (product.external_product_id, product.title) == ("MLM1001", "Lámpara LED")
    assert [variant["sku"] for variant in product.variants] == ["SKU-RED-L", "SKU-BLUE-S"]
    assert [variant["quantity"] for variant in product.variants] == [4, 5]
    assert product.variants[0]["external_variant_id"] == "501"
    assert product.variants[0]["option_values"] == {"Color": "Red", "Size": "L"}
    assert product.variants[0]["images"] == ["https://cdn.example.com/red.jpg"]
    assert product.variants[0]["price"] == "12.76"
    assert product.variants[1]["price"] == "13.50"


async def test_fetch_products_falls_back_to_one_variant_for_an_item_without_variations() -> None:
    plain = item_body(variations=[], last_updated="2024-05-11T00:00:00.000Z")
    fake = FakeMercadoLibre(
        {
            ("GET", ITEMS_SEARCH_PATH): items_search(["MLM1001"]),
            ("GET", ITEMS_PATH): items_multiget([plain]),
        }
    )
    adapter = make_adapter(fake)

    products, _cursor = await adapter.fetch_products_updated_since(SINCE)

    variants = products[0].variants
    assert len(variants) == 1
    assert variants[0]["sku"] == "SKU-RED-L"
    assert variants[0]["quantity"] == 9
    assert variants[0]["external_variant_id"] is None
    assert variants[0]["images"] == ["https://cdn.example.com/main.jpg"]


async def test_fetch_products_chunks_multiget_at_twenty_ids() -> None:
    ids = [f"MLM{1000 + index}" for index in range(25)]
    fake = FakeMercadoLibre(
        {
            ("GET", ITEMS_SEARCH_PATH): items_search(ids, total=25),
            ("GET", ITEMS_PATH): lambda request: httpx.Response(
                200,
                json=[
                    {"code": 200, "body": item_body(id=item_id)}
                    for item_id in request.url.params["ids"].split(",")
                ],
            ),
        }
    )
    adapter = make_adapter(fake)

    products, cursor = await adapter.fetch_products_updated_since(SINCE)

    calls = fake.sent(ITEMS_PATH)
    assert [len(call.url.params["ids"].split(",")) for call in calls] == [20, 5]
    assert len(products) == 25
    assert cursor is None


async def test_fetch_products_drops_items_older_than_since_and_failed_multiget_rows() -> None:
    stale = item_body(id="MLM-STALE", last_updated="2024-04-01T00:00:00.000Z")
    fresh = item_body(id="MLM-FRESH", last_updated="2024-05-20T00:00:00.000Z")

    def multiget(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"code": 200, "body": stale},
                {"code": 200, "body": fresh},
                {"code": 404, "body": {"message": "item not found"}},
            ],
        )

    fake = FakeMercadoLibre(
        {
            ("GET", ITEMS_SEARCH_PATH): items_search(
                ["MLM-STALE", "MLM-FRESH", "MLM-GONE"], total=3
            ),
            ("GET", ITEMS_PATH): multiget,
        }
    )
    adapter = make_adapter(fake)

    products, cursor = await adapter.fetch_products_updated_since(SINCE)

    assert [product.external_product_id for product in products] == ["MLM-FRESH"]
    assert cursor is None


async def test_fetch_products_keeps_items_without_a_last_updated_field() -> None:
    undated = item_body()
    undated.pop("last_updated")
    fake = FakeMercadoLibre(
        {
            ("GET", ITEMS_SEARCH_PATH): items_search(["MLM1001"]),
            ("GET", ITEMS_PATH): items_multiget([undated]),
        }
    )
    adapter = make_adapter(fake)

    products, _cursor = await adapter.fetch_products_updated_since(SINCE)

    assert [product.external_product_id for product in products] == ["MLM1001"]


async def test_fetch_products_500_maps_to_product_sync_failed() -> None:
    fake = FakeMercadoLibre(
        {
            ("GET", ITEMS_SEARCH_PATH): lambda _r: httpx.Response(
                500, json={"message": "internal"}
            )
        }
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_products_updated_since(SINCE)

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_PRODUCT_SYNC_FAILED"


async def test_fetch_products_403_maps_to_auth_failed() -> None:
    fake = FakeMercadoLibre(
        {("GET", ITEMS_SEARCH_PATH): lambda _r: httpx.Response(403, json={"message": "nope"})}
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_products_updated_since(SINCE)

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_AUTH_FAILED"


# ---------------------------------------------------------------------------- fulfillment

ORDER_PATH = "/orders/2000012345678"


async def test_create_fulfillment_resolves_the_shipment_from_the_order() -> None:
    fake = FakeMercadoLibre(
        {
            ("GET", ORDER_PATH): lambda _r: httpx.Response(
                200, json={"id": 2000012345678, "shipping": {"id": 412345678}}
            ),
            ("PUT", f"{SHIPMENTS_PATH}/412345678"): lambda _r: httpx.Response(
                200, json={"id": 412345678, "status": "shipped"}
            ),
        }
    )
    adapter = make_adapter(fake)

    result = await adapter.create_fulfillment(
        "2000012345678", "MEX123456789", "DHL", tracking_url="https://track.example.com/1"
    )

    sent = body(fake.only("PUT", f"{SHIPMENTS_PATH}/412345678"))
    assert sent == {
        "tracking_number": "MEX123456789",
        "tracking_method": "DHL",
        "status": "shipped",
        "tracking_url": "https://track.example.com/1",
    }
    assert result["channel"] == "mercado_libre"
    assert result["external_order_id"] == "2000012345678"
    assert result["external_shipment_id"] == "412345678"
    assert result["fulfillment_id"] == "412345678"
    assert result["tracking_number"] == "MEX123456789"
    assert result["carrier"] == "DHL"
    assert result["raw"] == {"id": 412345678, "status": "shipped"}


async def test_create_fulfillment_uses_a_supplied_shipment_id_without_order_lookup() -> None:
    fake = FakeMercadoLibre(
        {
            ("PUT", f"{SHIPMENTS_PATH}/999"): lambda _r: httpx.Response(200, json={"id": 999}),
        }
    )
    adapter = make_adapter(fake)

    result = await adapter.create_fulfillment(
        "2000012345678",
        "MEX1",
        "FedEx",
        fulfillment_order_id="999",
        notify_customer=False,
    )

    assert fake.sent(ORDER_PATH) == []
    assert result["external_shipment_id"] == "999"
    assert result["notify_customer"] is False


@pytest.mark.parametrize(
    ("order_id", "tracking", "code"),
    [
        ("   ", "MEX1", "ERR_MERCADO_LIBRE_ORDER_ID_REQUIRED"),
        ("2000012345678", "  ", "ERR_MERCADO_LIBRE_TRACKING_NUMBER_REQUIRED"),
    ],
)
async def test_create_fulfillment_validates_order_and_tracking(
    order_id: str, tracking: str, code: str
) -> None:
    fake = FakeMercadoLibre({})
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment(order_id, tracking, "DHL")

    assert excinfo.value.code == code
    assert fake.requests == []


async def test_create_fulfillment_refuses_to_fake_tracking_for_full_fulfillment() -> None:
    fake = FakeMercadoLibre({})
    adapter = make_adapter(fake, fulfillment_mode="full")

    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment("2000012345678", "MEX1", "DHL")

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_FULL_FULFILLMENT_TRACKING_UNSUPPORTED"
    assert excinfo.value.status_code == 422
    assert fake.requests == []


async def test_create_fulfillment_fails_closed_when_the_order_has_no_shipment() -> None:
    fake = FakeMercadoLibre(
        {("GET", ORDER_PATH): lambda _r: httpx.Response(200, json={"id": 2000012345678})}
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment("2000012345678", "MEX1", "DHL")

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_SHIPMENT_NOT_FOUND"
    assert fake.sent(SHIPMENTS_PATH) == []


async def test_create_fulfillment_rejection_carries_a_redacted_detail() -> None:
    fake = FakeMercadoLibre(
        {
            ("GET", ORDER_PATH): lambda _r: httpx.Response(
                200, json={"shipping": {"id": 412345678}}
            ),
            ("PUT", f"{SHIPMENTS_PATH}/412345678"): lambda _r: httpx.Response(
                400, json={"message": f"tracking rejected for {ACCESS_TOKEN}"}
            ),
        }
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment("2000012345678", "MEX1", "DHL")

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_FULFILLMENT_REJECTED"
    assert ACCESS_TOKEN not in excinfo.value.detail
    assert "[redacted]" in excinfo.value.detail


async def test_create_fulfillment_order_lookup_failure_is_a_fulfillment_rejection() -> None:
    fake = FakeMercadoLibre(
        {("GET", ORDER_PATH): lambda _r: httpx.Response(404, json={"message": "missing"})}
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment("2000012345678", "MEX1", "DHL")

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_FULFILLMENT_REJECTED"


# -------------------------------------------------------------------------------- webhooks


def signed(body_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()


def test_webhook_accepts_hex_base64_and_composite_signatures() -> None:
    raw = b'{"_id":"abc","resource":"/orders/2000012345678","topic":"orders_v2"}'
    digest = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).digest()
    hex_digest = digest.hex()
    b64_digest = base64.b64encode(digest).decode()

    assert verify_mercado_libre_webhook(raw, {"x-signature": hex_digest}, WEBHOOK_SECRET)
    assert verify_mercado_libre_webhook(raw, {"x-meli-signature": hex_digest}, WEBHOOK_SECRET)
    assert verify_mercado_libre_webhook(raw, {"X-Signature": hex_digest.upper()}, WEBHOOK_SECRET)
    assert verify_mercado_libre_webhook(raw, {"x-signature": b64_digest}, WEBHOOK_SECRET)
    assert verify_mercado_libre_webhook(
        raw, {"x-signature": b64_digest.rstrip("=")}, WEBHOOK_SECRET
    )
    assert verify_mercado_libre_webhook(
        raw, {"x-signature": f"ts=1716000000,v1={hex_digest}"}, WEBHOOK_SECRET
    )


def test_webhook_fails_closed_on_missing_or_wrong_material() -> None:
    raw = b'{"_id":"abc"}'

    assert not verify_mercado_libre_webhook(raw, {}, WEBHOOK_SECRET)
    assert not verify_mercado_libre_webhook(raw, {"x-signature": ""}, WEBHOOK_SECRET)
    assert not verify_mercado_libre_webhook(raw, {"x-signature": signed(raw)}, "")
    assert not verify_mercado_libre_webhook(b"", {"x-signature": signed(b"")}, WEBHOOK_SECRET)
    assert not verify_mercado_libre_webhook(
        raw, {"x-signature": signed(b"other-body")}, WEBHOOK_SECRET
    )
    assert not verify_mercado_libre_webhook(
        raw, {"x-signature": signed(raw, "wrong-secret")}, WEBHOOK_SECRET
    )


def test_adapter_verify_webhook_delegates_to_the_module_helper() -> None:
    raw = b'{"_id":"abc"}'
    adapter = MercadoLibreAdapter(seller_id=SELLER_ID, access_token=ACCESS_TOKEN)

    assert adapter.verify_webhook(raw, {"x-signature": signed(raw)}, WEBHOOK_SECRET)
    assert not adapter.verify_webhook(raw, {"x-signature": "deadbeef"}, WEBHOOK_SECRET)


# -------------------------------------------------------------------------------- listing


async def test_publish_listing_creates_a_multi_variation_item_and_description() -> None:
    fake = FakeMercadoLibre(publish_routes())
    adapter = make_adapter(fake)
    payload = listing_payload()

    receipt = await adapter.publish_listing(payload)

    created = body(fake.only("POST", ITEMS_PATH))
    assert len(created["title"]) == 60
    assert created["category_id"] == MERCADO_LIBRE_DEFAULT_CATEGORY_ID
    assert created["price"] == "20.00"
    assert created["currency_id"] == "MXN"
    assert created["available_quantity"] == 7
    assert created["condition"] == "new"
    assert created["buying_mode"] == "buy_it_now"
    assert created["pictures"] == [
        {"source": "https://cdn.example.com/a.jpg"},
        {"source": "https://cdn.example.com/red.jpg"},
    ]
    assert created["variations"] == [
        {
            "seller_custom_field": "SKU-RED",
            "available_quantity": 5,
            "price": "20.00",
            "attribute_combinations": [
                {"name": "Color", "value_name": "Red"},
                {"name": "Size", "value_name": "L"},
            ],
        },
        {
            "seller_custom_field": "SKU-BLUE",
            "available_quantity": 2,
            "price": "21.50",
            "attribute_combinations": [{"name": "Color", "value_name": "Blue"}],
        },
    ]
    description = body(fake.only("PUT", f"{ITEMS_PATH}/MLM2001/description"))
    assert description["plain_text"].startswith("Wall-mounted LED panel")
    assert "- Energy saving" in description["plain_text"]
    assert "- HS code: 9405.40" in description["plain_text"]
    assert "- MOQ 10: 17.50 USD per unit" in description["plain_text"]

    assert receipt.external_product_id == "MLM2001"
    assert receipt.external_sku == "SKU-PARENT"
    assert receipt.status == "active"
    assert [
        (item.external_sku, item.external_variant_id, item.external_inventory_item_id)
        for item in receipt.variant_receipts
    ] == [("SKU-RED", "111", "111"), ("SKU-BLUE", "222", "222")]
    assert receipt.raw_response["description_uploaded"] is True


async def test_publish_listing_keeps_a_real_mercado_libre_category_id() -> None:
    fake = FakeMercadoLibre(publish_routes())
    adapter = make_adapter(fake)

    await adapter.publish_listing(listing_payload(category="mlb1648", variants=[]))

    assert body(fake.only("POST", ITEMS_PATH))["category_id"] == "MLB1648"


async def test_publish_listing_without_variants_returns_the_item_level_receipt() -> None:
    fake = FakeMercadoLibre(
        {
            ("POST", ITEMS_PATH): lambda _r: httpx.Response(
                201, json={"id": "MLM2002", "status": "under_review"}
            ),
            ("PUT", f"{ITEMS_PATH}/MLM2002/description"): description_ok,
        }
    )
    adapter = make_adapter(fake)

    receipt = await adapter.publish_listing(listing_payload(variants=[], options=[]))

    assert "variations" not in body(fake.only("POST", ITEMS_PATH))
    assert receipt.external_product_id == "MLM2002"
    assert receipt.status == "under_review"
    assert receipt.variant_receipts[0].external_sku == "SKU-PARENT"
    assert receipt.variant_receipts[0].external_inventory_item_id == "MLM2002"


async def test_publish_listing_survives_a_description_upload_rejection() -> None:
    routes = publish_routes()
    routes[("PUT", f"{ITEMS_PATH}/MLM2001/description")] = lambda _r: httpx.Response(
        400, json={"message": "description too long"}
    )
    fake = FakeMercadoLibre(routes)
    adapter = make_adapter(fake)

    receipt = await adapter.publish_listing(listing_payload())

    assert receipt.external_product_id == "MLM2001"
    assert receipt.raw_response["description_uploaded"] is False


async def test_publish_listing_skips_the_description_call_for_an_empty_description() -> None:
    fake = FakeMercadoLibre(publish_routes())
    adapter = make_adapter(fake)

    receipt = await adapter.publish_listing(
        listing_payload(description="", bullet_points=[], moq_tiers=[], hs_code=None, category="")
    )

    assert fake.sent(f"{ITEMS_PATH}/MLM2001/description") == []
    assert receipt.raw_response["description_uploaded"] is False


async def test_publish_listing_remote_rejection_is_a_typed_error_without_token_leakage() -> None:
    fake = FakeMercadoLibre(
        {
            ("POST", ITEMS_PATH): lambda _r: httpx.Response(
                400, json={"message": f"invalid access token {ACCESS_TOKEN}"}
            )
        }
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(listing_payload())

    assert (excinfo.value.code, excinfo.value.status_code) == (
        "ERR_MERCADO_LIBRE_LISTING_REJECTED",
        502,
    )
    assert ACCESS_TOKEN not in excinfo.value.detail
    assert fake.sent(f"{ITEMS_PATH}/MLM2001/description") == []


async def test_publish_listing_requires_the_created_item_id() -> None:
    fake = FakeMercadoLibre({("POST", ITEMS_PATH): lambda _r: httpx.Response(201, json={})})
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(listing_payload())

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_LISTING_REJECTED"
    assert "SKU-PARENT" in excinfo.value.detail


@pytest.mark.parametrize(
    "overrides", [{"external_sku": "  "}, {"title": ""}], ids=["blank-sku", "blank-title"]
)
async def test_publish_listing_validates_sku_and_title(overrides: dict[str, Any]) -> None:
    fake = FakeMercadoLibre({})
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(listing_payload(**overrides))

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_LISTING_INVALID"
    assert fake.requests == []


async def test_publish_listing_refuses_a_zero_price() -> None:
    fake = FakeMercadoLibre({})
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(listing_payload(price_usd=Decimal("0")))

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_LISTING_INVALID"
    assert fake.requests == []


async def test_publish_listing_refuses_an_ieee754_float_price() -> None:
    fake = FakeMercadoLibre({})
    adapter = make_adapter(fake)
    # `model_copy(update=...)` bypasses validation exactly like a sloppy caller would.
    payload = listing_payload().model_copy(update={"price_usd": 19.99})

    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(payload)

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_INVALID_MONEY"
    assert fake.requests == []


async def test_publish_listing_refuses_image_blobs_without_https_urls() -> None:
    fake = FakeMercadoLibre({})
    adapter = make_adapter(fake)
    payload = listing_payload(
        images=[],
        variants=[],
        image_blobs=[
            MarketplaceImageBlob(filename="a.jpg", mime_type="image/jpeg", data=b"\x89PNG")
        ],
    )

    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(payload)

    assert (excinfo.value.code, excinfo.value.status_code) == (
        "ERR_MERCADO_LIBRE_IMAGE_BLOB_UNSUPPORTED",
        422,
    )
    assert fake.requests == []


# ------------------------------------------------------------------------------- inventory


async def test_push_inventory_level_updates_the_item_quantity_and_clamps_negatives() -> None:
    fake = FakeMercadoLibre(
        {
            ("PUT", f"{ITEMS_PATH}/MLM2001"): lambda _r: httpx.Response(
                200, json={"id": "MLM2001", "available_quantity": 0}
            )
        }
    )
    adapter = make_adapter(fake)

    result = await adapter.push_inventory_level(
        external_sku=" SKU-RED-L ", quantity=-3, external_product_id="MLM2001"
    )

    assert body(fake.only("PUT", f"{ITEMS_PATH}/MLM2001")) == {"available_quantity": 0}
    assert result["channel"] == "mercado_libre"
    assert result["external_sku"] == "SKU-RED-L"
    assert result["quantity"] == 0
    assert result["external_product_id"] == "MLM2001"
    assert result["external_variant_id"] is None
    assert result["site_id"] == "MLM"


@pytest.mark.parametrize(
    "overrides",
    [
        {"external_variant_id": "111"},
        {"external_inventory_item_id": "111"},
    ],
    ids=["variant-id", "inventory-item-id"],
)
async def test_push_inventory_level_targets_a_variation(overrides: dict[str, Any]) -> None:
    fake = FakeMercadoLibre(
        {
            ("PUT", f"{ITEMS_PATH}/MLM2001/variations/111"): lambda _r: httpx.Response(
                200, json={"id": 111, "available_quantity": 12}
            )
        }
    )
    adapter = make_adapter(fake)

    result = await adapter.push_inventory_level(
        external_sku="SKU-RED",
        quantity=12,
        external_product_id="MLM2001",
        location_id="warehouse-a",
        warehouse_id="wms-1",
        **overrides,
    )

    assert body(fake.only("PUT", f"{ITEMS_PATH}/MLM2001/variations/111")) == {
        "available_quantity": 12
    }
    assert result["quantity"] == 12
    assert result["external_variant_id"] == "111"


async def test_push_inventory_level_keeps_the_item_path_when_inventory_id_is_the_item() -> None:
    fake = FakeMercadoLibre(
        {
            ("PUT", f"{ITEMS_PATH}/MLM2001"): lambda _r: httpx.Response(
                200, json={"id": "MLM2001"}
            )
        }
    )
    adapter = make_adapter(fake)

    result = await adapter.push_inventory_level(
        external_sku="SKU-RED-L",
        quantity=4,
        external_product_id="MLM2001",
        external_inventory_item_id="MLM2001",
    )

    assert fake.only("PUT", f"{ITEMS_PATH}/MLM2001")
    assert result["external_variant_id"] is None


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"external_sku": " ", "quantity": 1, "external_product_id": "MLM2001"},
            "ERR_MERCADO_LIBRE_INVENTORY_SYNC",
        ),
        (
            {"external_sku": "SKU-RED", "quantity": 1},
            "ERR_MERCADO_LIBRE_INVENTORY_SYNC",
        ),
    ],
    ids=["blank-sku", "missing-item-id"],
)
async def test_push_inventory_level_validates_its_inputs(
    kwargs: dict[str, Any], code: str
) -> None:
    fake = FakeMercadoLibre({})
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(**kwargs)

    assert (excinfo.value.code, excinfo.value.status_code) == (code, 422)
    assert fake.requests == []


async def test_push_inventory_level_remote_rejection_is_typed() -> None:
    fake = FakeMercadoLibre(
        {
            ("PUT", f"{ITEMS_PATH}/MLM2001"): lambda _r: httpx.Response(
                400, json={"message": "item closed"}
            )
        }
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(
            external_sku="SKU-RED-L", quantity=4, external_product_id="MLM2001"
        )

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_INVENTORY_SYNC"
    assert "HTTP 400" in excinfo.value.detail


async def test_push_inventory_level_429_maps_to_rate_limited() -> None:
    fake = FakeMercadoLibre(
        {("PUT", f"{ITEMS_PATH}/MLM2001"): lambda _r: httpx.Response(429, json={})}
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(
            external_sku="SKU-RED-L", quantity=4, external_product_id="MLM2001"
        )

    assert excinfo.value.code == "ERR_MERCADO_LIBRE_RATE_LIMITED"
