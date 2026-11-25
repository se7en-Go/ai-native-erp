"""Zero-network unit tests for the eBay Sell API connector adapter."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

import erp_core.channels.ecommerce.ebay as ebay_module
from erp_core.channels.ecommerce.ebay import EbayConnectorAdapter, EbayListingReceipt
from erp_core.shared.enums import FulfillmentStatus, IdentityType, PaymentStatus
from erp_core.shared.errors import AppError

APP_ID = "test-app-id"
CERT_ID = "test-cert-id-secret"
REFRESH_TOKEN = "test-refresh-token-secret"
ACCESS_TOKEN = "v^1.1#i^1#test-access-token-secret"
SINCE = datetime(2024, 5, 1, tzinfo=UTC)

TOKEN_PATH = "/identity/v1/oauth2/token"
ORDERS_PATH = "/sell/fulfillment/v1/order"
INVENTORY_ITEM_PATH = "/sell/inventory/v1/inventory_item"
OFFER_PATH = "/sell/inventory/v1/offer"
BULK_PATH = "/sell/inventory/v1/bulk_update_price_quantity"

Route = Callable[[httpx.Request], httpx.Response]


class FakeEbay:
    """Deterministic route table standing in for every eBay endpoint."""

    def __init__(
        self,
        routes: dict[tuple[str, str], Route] | None = None,
        *,
        token: Route | None = None,
    ) -> None:
        self.routes = routes or {}
        self.token_route: Route = token or (
            lambda _request: httpx.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "expires_in": 7200,
                    "token_type": "Application Access Token",
                },
            )
        )
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == TOKEN_PATH:
            self.token_calls += 1
            return self.token_route(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(
                404, json={"errors": [{"message": f"unrouted {request.method} {request.url.path}"}]}
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


def make_adapter(fake: FakeEbay, **overrides: Any) -> EbayConnectorAdapter:
    return EbayConnectorAdapter(
        seller_username="best-seller",
        app_id=APP_ID,
        cert_id=CERT_ID,
        refresh_token=REFRESH_TOKEN,
        transport=fake.transport,
        **overrides,
    )


def body(request: httpx.Request) -> dict[str, Any]:
    parsed = json.loads(request.content)
    assert isinstance(parsed, dict)
    return parsed


def form(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))


def empty_orders(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"orders": [], "total": 0, "limit": 50, "offset": 0})


# ----------------------------------------------------------------------- OAuth2 exchange


async def test_oauth2_refresh_token_exchange_uses_basic_auth_and_form_scope() -> None:
    fake = FakeEbay({("GET", ORDERS_PATH): empty_orders})
    adapter = make_adapter(fake)

    await adapter.fetch_orders_updated_since(SINCE)
    await adapter.fetch_orders_updated_since(SINCE)

    assert fake.token_calls == 1, "the access token must be cached between calls"
    token_request = fake.sent(TOKEN_PATH)[0]
    assert token_request.method == "POST"
    assert token_request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    basic = token_request.headers["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(basic).decode() == f"{APP_ID}:{CERT_ID}"

    payload = form(token_request)
    assert payload["grant_type"] == "refresh_token"
    assert payload["refresh_token"] == REFRESH_TOKEN
    assert payload["scope"].split() == [
        "https://api.ebay.com/oauth/api_scope",
        "https://api.ebay.com/oauth/api_scope/sell.inventory",
        "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    ]

    order_requests = fake.sent(ORDERS_PATH)
    assert len(order_requests) == 2
    assert all(
        request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}" for request in order_requests
    )


async def test_access_token_is_refreshed_only_after_the_sixty_second_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(ebay_module.time, "monotonic", lambda: clock[0])
    fake = FakeEbay({("GET", ORDERS_PATH): empty_orders})
    adapter = make_adapter(fake)

    await adapter.fetch_orders_updated_since(SINCE)
    assert fake.token_calls == 1

    clock[0] = 1000.0 + 7200 - 60 - 1  # one second inside the buffer window
    await adapter.fetch_orders_updated_since(SINCE)
    assert fake.token_calls == 1

    clock[0] = 1000.0 + 7200 - 60  # buffer exhausted -> refresh
    await adapter.fetch_orders_updated_since(SINCE)
    assert fake.token_calls == 2


async def test_token_endpoint_401_raises_auth_failed_without_leaking_secrets() -> None:
    fake = FakeEbay(
        token=lambda _request: httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": f"bad credentials for {CERT_ID} with {REFRESH_TOKEN}",
            },
        )
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    error = excinfo.value
    assert (error.slug, error.code, error.status_code) == (
        "ebay-auth-failed",
        "ERR_EBAY_AUTH_FAILED",
        502,
    )
    rendered = f"{error.detail} {error} {adapter!r}"
    for secret in (CERT_ID, REFRESH_TOKEN, ACCESS_TOKEN):
        assert secret not in rendered
    assert fake.sent(ORDERS_PATH) == []


async def test_token_endpoint_429_maps_to_rate_limited() -> None:
    fake = FakeEbay(token=lambda _request: httpx.Response(429, json={"errors": []}))
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    assert (excinfo.value.slug, excinfo.value.code, excinfo.value.status_code) == (
        "ebay-rate-limited",
        "ERR_EBAY_RATE_LIMITED",
        429,
    )


async def test_sell_endpoint_401_maps_to_auth_failed() -> None:
    fake = FakeEbay(
        {("GET", ORDERS_PATH): lambda _request: httpx.Response(401, json={"errors": []})}
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_EBAY_AUTH_FAILED"


def test_sandbox_and_production_base_urls() -> None:
    sandbox = EbayConnectorAdapter(
        seller_username="seller",
        app_id=APP_ID,
        cert_id=CERT_ID,
        refresh_token=REFRESH_TOKEN,
        sandbox=True,
    )
    production = EbayConnectorAdapter(
        seller_username="seller",
        app_id=APP_ID,
        cert_id=CERT_ID,
        refresh_token=REFRESH_TOKEN,
    )
    overridden = EbayConnectorAdapter(
        seller_username="seller",
        app_id=APP_ID,
        cert_id=CERT_ID,
        refresh_token=REFRESH_TOKEN,
        sandbox=True,
        api_base_url="https://ebay-gateway.internal/",
    )

    assert sandbox.base_url == "https://api.sandbox.ebay.com"
    assert production.base_url == "https://api.ebay.com"
    assert overridden.base_url == "https://ebay-gateway.internal"
    assert CERT_ID not in repr(production)
    assert REFRESH_TOKEN not in repr(production)


# ------------------------------------------------------------------------------ order sync

PAGE_ONE: dict[str, Any] = {
    "total": 3,
    "limit": 50,
    "offset": 0,
    "orders": [
        {
            "orderId": "12-12345-67890",
            "orderFulfillmentStatus": "FULFILLED",
            "orderPaymentStatus": "PAID",
            "lastModifiedDate": "2024-05-02T10:15:30.000Z",
            "sellerId": "best-seller",
            "pricingSummary": {
                "priceSubtotal": {"value": "25.50", "currency": "USD"},
                "deliveryCost": {"value": "4.99", "currency": "USD"},
                "tax": {"value": "2.35", "currency": "USD"},
                "total": {"value": "32.84", "currency": "USD"},
            },
            "buyer": {
                "username": "buyer_one",
                "buyerRegistrationAddress": {
                    "email": "buyer_one@example.com",
                    "countryCode": "US",
                    "primaryPhone": {"phoneNumber": "+14155550123"},
                },
            },
            "lineItems": [
                {
                    "lineItemId": "1",
                    "sku": "SKU-RED-L",
                    "title": "Red Lamp",
                    "quantity": 2,
                    "lineItemCost": {"value": "12.75", "currency": "USD"},
                },
                {
                    "lineItemId": "2",
                    "legacyItemId": "285000000001",
                    "title": "Spare Bulb",
                    "quantity": 1,
                    "total": {"value": "3.00", "currency": "USD"},
                },
            ],
        },
        {
            "orderId": "12-99999-11111",
            "orderFulfillmentStatus": "NOT_STARTED",
            "cancelStatus": {"cancelState": "CANCELED"},
            "lastModifiedDate": "2024-05-02T11:00:00.000Z",
            "pricingSummary": {
                "priceSubtotal": {"value": "9.10", "currency": "USD"},
            },
            "buyer": {"username": "buyer_two"},
            "lineItems": [
                {"sku": "SKU-BLUE-S", "quantity": 1, "lineItemCost": {"value": "9.10"}},
            ],
        },
    ],
}

PAGE_TWO: dict[str, Any] = {
    "total": 3,
    "limit": 50,
    "offset": 2,
    "orders": [
        {
            "orderId": "12-22222-33333",
            "orderFulfillmentStatus": "IN_PROGRESS",
            "lastModifiedDate": "2024-05-03T08:00:00.000Z",
            "pricingSummary": {
                "priceSubtotal": {"value": "40.00", "currency": "USD"},
                "deliveryCost": {"value": "0.00", "currency": "USD"},
                "tax": {"value": "0.00", "currency": "USD"},
                "total": {"value": "40.00", "currency": "USD"},
            },
            "buyer": {"username": "buyer_three"},
            "lineItems": [
                {"sku": "SKU-GREEN-M", "quantity": 4, "lineItemCost": {"value": "40.00"}}
            ],
        }
    ],
}


def _orders_route(request: httpx.Request) -> httpx.Response:
    offset = request.url.params.get("offset")
    return httpx.Response(200, json=PAGE_TWO if offset == "2" else PAGE_ONE)


async def test_fetch_orders_normalizes_decimal_amounts_and_paginates_by_offset() -> None:
    fake = FakeEbay({("GET", ORDERS_PATH): _orders_route})
    adapter = make_adapter(fake)

    orders, cursor = await adapter.fetch_orders_updated_since(SINCE)
    assert cursor == "2"

    first_request = fake.sent(ORDERS_PATH)[0]
    assert first_request.url.params["filter"] == "lastmodifieddate:[2024-05-01T00:00:00.000Z..]"
    assert first_request.url.params["limit"] == "50"
    assert first_request.url.params["offset"] == "0"

    assert [order.external_order_id for order in orders] == ["12-12345-67890", "12-99999-11111"]
    shipped = orders[0]
    assert shipped.subtotal_amount == Decimal("25.50")
    assert str(shipped.subtotal_amount) == "25.50"
    assert shipped.shipping_charged == Decimal("4.99")
    assert shipped.tax_collected == Decimal("2.35")
    assert shipped.total_amount == Decimal("32.84")
    assert isinstance(shipped.total_amount, Decimal)
    assert shipped.fx_rate_to_base is None
    assert shipped.currency == "USD"
    assert shipped.fulfillment_status is FulfillmentStatus.SHIPPED
    assert shipped.payment_status is PaymentStatus.PAID
    assert shipped.updated_at == datetime(2024, 5, 2, 10, 15, 30, tzinfo=UTC)
    assert [(line.external_sku, line.quantity, line.line_total) for line in shipped.line_items] == [
        ("SKU-RED-L", 2, Decimal("12.75")),
        ("285000000001", 1, Decimal("3.00")),
    ]
    assert shipped.customer.email == "buyer_one@example.com"
    assert shipped.customer.phone_e164 == "+14155550123"
    assert shipped.customer.country_code == "US"
    assert shipped.customer.display_name == "buyer_one"
    assert shipped.customer.external_identity_type is IdentityType.EMAIL
    assert shipped.channel_metadata["ebay_order_id"] == "12-12345-67890"
    assert shipped.channel_metadata["ebay_fulfillment_status"] == "FULFILLED"

    cancelled = orders[1]
    assert cancelled.fulfillment_status is FulfillmentStatus.CANCELLED
    assert cancelled.payment_status is PaymentStatus.REFUNDED
    # No ``total`` in pricingSummary -> derived strictly from the Decimal components.
    assert cancelled.total_amount == Decimal("9.10")
    assert cancelled.shipping_charged == Decimal("0.00")
    assert cancelled.customer.email is None
    assert cancelled.customer.external_identity_type is IdentityType.WEB_VISITOR_ID

    page_two, next_cursor = await adapter.fetch_orders_updated_since(SINCE, cursor)
    assert fake.sent(ORDERS_PATH)[1].url.params["offset"] == "2"
    assert next_cursor is None
    assert [order.external_order_id for order in page_two] == ["12-22222-33333"]
    assert page_two[0].fulfillment_status is FulfillmentStatus.CONFIRMED_RESERVED
    assert page_two[0].line_items[0].quantity == 4
    assert page_two[0].line_items[0].line_total == Decimal("40.00")


async def test_fetch_orders_rejects_a_non_numeric_cursor() -> None:
    fake = FakeEbay({("GET", ORDERS_PATH): _orders_route})
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE, "page-token")

    assert excinfo.value.code == "ERR_EBAY_INVALID_CURSOR"
    assert fake.sent(ORDERS_PATH) == []


# ---------------------------------------------------------------------------- product sync


async def test_fetch_products_maps_inventory_items_and_advances_offset() -> None:
    payload = {
        "total": 2,
        "inventoryItems": [
            {
                "sku": "SKU-RED-L",
                "condition": "NEW",
                "product": {
                    "title": "Red Lamp",
                    "imageUrls": ["https://img.example/red.jpg"],
                    "ean": ["0194252345678"],
                },
                "availability": {"shipToLocationAvailability": {"quantity": 7}},
            },
            {
                "sku": "SKU-BLUE-S",
                "product": {"title": "Blue Lamp"},
                "availability": {"shipToLocationAvailability": {"quantity": 0}},
            },
        ],
    }
    fake = FakeEbay(
        {
            ("GET", INVENTORY_ITEM_PATH): lambda _request: httpx.Response(200, json=payload),
        }
    )
    adapter = make_adapter(fake)

    products, cursor = await adapter.fetch_products_updated_since(SINCE, "50")

    request = fake.only("GET", INVENTORY_ITEM_PATH)
    assert request.url.params["limit"] == "50"
    assert request.url.params["offset"] == "50"
    assert [product.external_product_id for product in products] == ["SKU-RED-L", "SKU-BLUE-S"]
    assert products[0].title == "Red Lamp"
    assert products[0].variants[0]["quantity"] == 7
    assert products[0].variants[0]["images"] == ["https://img.example/red.jpg"]
    assert products[1].variants[0]["quantity"] == 0
    assert cursor is None


# --------------------------------------------------------------------- listing publication


def _publish_routes(
    *,
    offer_status: int = 201,
    offer_payload: dict[str, Any] | None = None,
    publish_status: int = 200,
    publish_payload: dict[str, Any] | None = None,
    inventory_status: int = 204,
    inventory_payload: dict[str, Any] | None = None,
) -> dict[tuple[str, str], Route]:
    return {
        ("PUT", f"{INVENTORY_ITEM_PATH}/SKU-RED-L"): lambda _request: httpx.Response(
            inventory_status, json=inventory_payload
        ),
        ("POST", OFFER_PATH): lambda _request: httpx.Response(
            offer_status,
            json=offer_payload if offer_payload is not None else {"offerId": "offer-1"},
        ),
        (
            "POST",
            f"{OFFER_PATH}/offer-1/publish",
        ): lambda _request: httpx.Response(
            publish_status,
            json=publish_payload if publish_payload is not None else {"listingId": "1105567890"},
        ),
    }


async def test_publish_listing_runs_three_steps_with_exact_decimal_price() -> None:
    fake = FakeEbay(_publish_routes())
    adapter = make_adapter(fake)

    receipt = await adapter.publish_listing(
        sku="SKU-RED-L",
        title="Industrial Red LED Work Lamp " * 5,
        description="Heavy duty red lamp.",
        bullet_points=["1200 lumens", "IP65 rated"],
        price_usd=Decimal("19.989"),
        quantity=0,
        images=["https://img.example/red.jpg"],
        category="Lighting",
        hs_code="9405.40",
        moq_tiers=[{"min_quantity": 12, "unit_price": "18.50"}],
    )

    assert isinstance(receipt, EbayListingReceipt)
    assert (receipt.external_product_id, receipt.external_sku) == ("1105567890", "SKU-RED-L")
    assert (receipt.offer_id, receipt.listing_id, receipt.status) == (
        "offer-1",
        "1105567890",
        "PUBLISHED",
    )
    assert receipt.raw_response == {"listingId": "1105567890"}

    inventory_request = fake.only("PUT", f"{INVENTORY_ITEM_PATH}/SKU-RED-L")
    assert inventory_request.headers["Content-Language"] == "en-US"
    inventory_body = body(inventory_request)
    assert len(inventory_body["product"]["title"]) == 80
    description = inventory_body["product"]["description"]
    assert description.startswith("Heavy duty red lamp.")
    assert "- 1200 lumens" in description
    assert "- HS code: 9405.40" in description
    assert "- MOQ 12: 18.50 per unit" in description
    assert inventory_body["product"]["imageUrls"] == ["https://img.example/red.jpg"]
    assert inventory_body["condition"] == "NEW"
    assert inventory_body["availability"]["shipToLocationAvailability"]["quantity"] == 1

    offer_request = fake.only("POST", OFFER_PATH)
    assert offer_request.headers["Content-Language"] == "en-US"
    offer_body = body(offer_request)
    assert offer_body["sku"] == "SKU-RED-L"
    assert offer_body["marketplaceId"] == "EBAY_US"
    assert offer_body["format"] == "FIXED_PRICE"
    assert offer_body["availableQuantity"] == 1
    assert offer_body["categoryId"] == "20706"
    assert offer_body["merchantLocationKey"] == "default"
    assert offer_body["pricingSummary"]["price"] == {"value": "19.99", "currency": "USD"}
    assert isinstance(offer_body["pricingSummary"]["price"]["value"], str)

    publish_request = fake.only("POST", f"{OFFER_PATH}/offer-1/publish")
    assert publish_request.headers["Content-Language"] == "en-US"


async def test_publish_listing_reuses_the_existing_offer_id_on_409() -> None:
    fake = FakeEbay(
        _publish_routes(
            offer_status=409,
            offer_payload={
                "errors": [
                    {
                        "errorId": 25002,
                        "message": "Offer entity already exists.",
                        "parameters": [{"name": "offerId", "value": "offer-1"}],
                    }
                ]
            },
        )
    )
    adapter = make_adapter(fake)

    receipt = await adapter.publish_listing(
        sku="SKU-RED-L",
        title="Red Lamp",
        description="Heavy duty red lamp.",
        bullet_points=[],
        price_usd=Decimal("19.99"),
        quantity=3,
        images=[],
    )

    assert receipt.offer_id == "offer-1"
    assert receipt.listing_id == "1105567890"
    assert fake.only("POST", f"{OFFER_PATH}/offer-1/publish").method == "POST"


def _failing_routes(step: str) -> dict[tuple[str, str], Route]:
    """Publish routes in which exactly one step fails (echoing every secret back)."""
    leaking = {
        "errors": [{"message": "echo", "longMessage": f"{CERT_ID} {REFRESH_TOKEN} {ACCESS_TOKEN}"}]
    }

    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=leaking)

    def reject_server_side(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=leaking)

    def publish_without_listing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"offerId": "offer-1"})

    routes = _publish_routes()
    targets: dict[str, tuple[tuple[str, str], Route]] = {
        "inventory": (("PUT", f"{INVENTORY_ITEM_PATH}/SKU-RED-L"), reject),
        "offer": (("POST", OFFER_PATH), reject),
        "publish": (("POST", f"{OFFER_PATH}/offer-1/publish"), reject_server_side),
        "publish-missing-listing": (
            ("POST", f"{OFFER_PATH}/offer-1/publish"),
            publish_without_listing,
        ),
    }
    key, route = targets[step]
    routes[key] = route
    return routes


@pytest.mark.parametrize("step", ["inventory", "offer", "publish", "publish-missing-listing"])
async def test_publish_listing_failure_raises_rejected_without_leaking_secrets(
    step: str,
) -> None:
    fake = FakeEbay(_failing_routes(step))
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(
            sku="SKU-RED-L",
            title="Red Lamp",
            description="Heavy duty red lamp.",
            bullet_points=[],
            price_usd=Decimal("19.99"),
            quantity=2,
            images=[],
        )

    error = excinfo.value
    assert (error.slug, error.code, error.status_code) == (
        "ebay-listing-rejected",
        "ERR_EBAY_LISTING_REJECTED",
        502,
    )
    rendered = f"{error.detail} {error}"
    for secret in (CERT_ID, REFRESH_TOKEN, ACCESS_TOKEN):
        assert secret not in rendered


async def test_publish_listing_rejects_an_empty_sku() -> None:
    fake = FakeEbay({})
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(
            sku="   ",
            title="Red Lamp",
            description="x",
            bullet_points=[],
            price_usd=Decimal("1.00"),
            quantity=1,
            images=[],
        )

    assert excinfo.value.code == "ERR_EBAY_LISTING_REJECTED"
    assert fake.requests == []


async def test_publish_listing_refuses_a_float_price() -> None:
    fake = FakeEbay(_publish_routes())
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(
            sku="SKU-RED-L",
            title="Red Lamp",
            description="x",
            bullet_points=[],
            price_usd=0.1 + 0.2,  # pragma: no cover - rejected before any HTTP call
            quantity=1,
            images=[],
        )

    assert excinfo.value.code == "ERR_EBAY_INVALID_MONEY"
    assert fake.requests == []


# --------------------------------------------------------------------- inventory quantity


async def test_push_inventory_level_posts_bulk_update_price_quantity() -> None:
    fake = FakeEbay(
        {
            ("POST", BULK_PATH): lambda _request: httpx.Response(
                200, json={"responses": [{"statusCode": 200, "sku": "SKU-RED-L"}]}
            )
        }
    )
    adapter = make_adapter(fake)

    result = await adapter.push_inventory_level(external_sku="SKU-RED-L", quantity=-5)

    request = fake.only("POST", BULK_PATH)
    assert body(request) == {
        "requests": [
            {"sku": "SKU-RED-L", "shipToLocationAvailability": {"quantity": 0}},
        ]
    }
    assert result["responses"][0]["statusCode"] == 200


async def test_push_inventory_level_rejects_per_sku_failures() -> None:
    fake = FakeEbay(
        {
            ("POST", BULK_PATH): lambda _request: httpx.Response(
                200,
                json={"responses": [{"statusCode": 404, "sku": "SKU-RED-L", "errors": []}]},
            )
        }
    )
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(external_sku="SKU-RED-L", quantity=4)

    assert excinfo.value.code == "ERR_EBAY_INVENTORY_UPDATE_REJECTED"


# -------------------------------------------------------------------------- fulfillment


async def test_create_fulfillment_posts_shipping_fulfillment() -> None:
    fake = FakeEbay(
        {
            ("POST", f"{ORDERS_PATH}/12-12345-67890/shipping_fulfillment"): (
                lambda _request: httpx.Response(201, json={"fulfillmentId": "ful-1"})
            )
        }
    )
    adapter = make_adapter(fake)

    result = await adapter.create_fulfillment(
        external_order_id="12-12345-67890",
        tracking_number="1Z999AA10123456784",
        carrier_company="UPS",
    )

    request = fake.only("POST", f"{ORDERS_PATH}/12-12345-67890/shipping_fulfillment")
    payload = body(request)
    assert payload["shippingCarrierCode"] == "UPS"
    assert payload["trackingNumber"] == "1Z999AA10123456784"
    assert payload["lineItems"] == []
    assert payload["shippedDate"].endswith("Z")
    shipped_at = datetime.strptime(payload["shippedDate"], "%Y-%m-%dT%H:%M:%S.%fZ")
    assert shipped_at.tzinfo is None  # Z suffix stripped by strptime; value is UTC
    assert result == {"fulfillmentId": "ful-1", "fulfillment_id": "ful-1"}


async def test_create_fulfillment_extracts_location_header_and_populates_line_items() -> None:
    fake = FakeEbay(
        {
            ("GET", f"{ORDERS_PATH}/12-55555-77777"): lambda _request: httpx.Response(
                200,
                json={
                    "orderId": "12-55555-77777",
                    "lineItems": [{"lineItemId": "li-1", "quantity": 2}],
                },
            ),
            ("POST", f"{ORDERS_PATH}/12-55555-77777/shipping_fulfillment"): (
                lambda _request: httpx.Response(
                    201,
                    headers={
                        "Location": (
                            "https://api.ebay.com/sell/fulfillment/v1/order/"
                            "12-55555-77777/shipping_fulfillment/ful-loc-99"
                        )
                    },
                    content=b"",
                )
            ),
        }
    )
    adapter = make_adapter(fake)

    result = await adapter.create_fulfillment(
        external_order_id="12-55555-77777",
        tracking_number="1Z999AA10123456784",
        carrier_company="UPS",
    )

    request = fake.only("POST", f"{ORDERS_PATH}/12-55555-77777/shipping_fulfillment")
    assert body(request)["lineItems"] == [{"lineItemId": "li-1", "quantity": 2}]
    assert result["fulfillment_id"] == "ful-loc-99"
    assert result["fulfillmentId"] == "ful-loc-99"


async def test_ebay_order_normalization_refunds_and_cancellation_variants() -> None:
    fake = FakeEbay(
        {
            ("GET", ORDERS_PATH): lambda _request: httpx.Response(
                200,
                json={
                    "total": 3,
                    "orders": [
                        {
                            "orderId": "ebay-cancelled",
                            "cancelStatus": {"cancelState": "cancelled"},
                            "lastModifiedDate": "2024-05-02T11:00:00.000Z",
                            "pricingSummary": {"priceSubtotal": {"value": "10.00"}},
                            "lineItems": [
                                {"sku": "SKU-1", "quantity": 1, "lineItemCost": {"value": "10.00"}}
                            ],
                        },
                        {
                            "orderId": "ebay-full-refund",
                            "orderFulfillmentStatus": "FULFILLED",
                            "orderPaymentStatus": "FULLY_REFUNDED",
                            "lastModifiedDate": "2024-05-02T11:00:00.000Z",
                            "pricingSummary": {"total": {"value": "20.00"}},
                            "lineItems": [
                                {"sku": "SKU-2", "quantity": 1, "lineItemCost": {"value": "20.00"}}
                            ],
                        },
                        {
                            "orderId": "ebay-partial-refund",
                            "orderFulfillmentStatus": "FULFILLED",
                            "orderPaymentStatus": "PARTIALLY_REFUNDED",
                            "lastModifiedDate": "2024-05-02T11:00:00.000Z",
                            "pricingSummary": {"total": {"value": "30.00"}},
                            "paymentSummary": {"refunds": [{"amount": {"value": "5.00"}}]},
                            "lineItems": [
                                {"sku": "SKU-3", "quantity": 1, "lineItemCost": {"value": "30.00"}}
                            ],
                        },
                    ],
                },
            )
        }
    )
    orders, _ = await make_adapter(fake).fetch_orders_updated_since(SINCE)
    by_id = {o.external_order_id: o for o in orders}
    assert by_id["ebay-cancelled"].fulfillment_status is FulfillmentStatus.CANCELLED
    assert by_id["ebay-cancelled"].payment_status is PaymentStatus.REFUNDED
    assert by_id["ebay-full-refund"].fulfillment_status is FulfillmentStatus.REFUNDED
    assert by_id["ebay-full-refund"].payment_status is PaymentStatus.REFUNDED
    assert by_id["ebay-partial-refund"].fulfillment_status is FulfillmentStatus.SHIPPED
    assert by_id["ebay-partial-refund"].payment_status is PaymentStatus.PAID
    assert by_id["ebay-partial-refund"].channel_metadata["refunded_amount"] == "5.00"


async def test_create_fulfillment_requires_a_tracking_number() -> None:
    fake = FakeEbay({})
    adapter = make_adapter(fake)

    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment(
            external_order_id="12-12345-67890",
            tracking_number=" ",
            carrier_company="UPS",
        )

    assert excinfo.value.code == "ERR_EBAY_TRACKING_NUMBER_REQUIRED"
    assert fake.requests == []


# --------------------------------------------------------------------------- webhooks


def test_verify_webhook_accepts_hex_and_base64_signatures_only() -> None:
    adapter = make_adapter(FakeEbay({}))
    raw = b'{"notificationId":"n-1"}'
    secret = "ebay-webhook-secret"
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    hex_signature = digest.hex()
    base64_signature = base64.b64encode(digest).decode()

    assert adapter.verify_webhook(raw, {"X-EBAY-SIGNATURE": hex_signature}, secret)
    assert adapter.verify_webhook(raw, {"x-ebay-signature": hex_signature.upper()}, secret)
    assert adapter.verify_webhook(raw, {"X-EBAY-SIGNATURE": base64_signature}, secret)
    assert adapter.verify_webhook(raw, {"X-EBAY-SIGNATURE": base64_signature.rstrip("=")}, secret)

    assert not adapter.verify_webhook(raw + b" ", {"X-EBAY-SIGNATURE": hex_signature}, secret)
    assert not adapter.verify_webhook(raw, {"X-EBAY-SIGNATURE": "deadbeef"}, secret)
    assert not adapter.verify_webhook(raw, {}, secret)
    assert not adapter.verify_webhook(raw, {"X-EBAY-SIGNATURE": hex_signature}, "")
    assert not adapter.verify_webhook(b"", {"X-EBAY-SIGNATURE": hex_signature}, secret)
