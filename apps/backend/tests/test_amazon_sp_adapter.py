"""Zero-network unit tests for the Amazon SP-API connector (``httpx.MockTransport``).

Every assertion about money checks both the numeric value and that the value is a
``decimal.Decimal`` (never a ``float``), because a float regression would silently corrupt
order revenue, tax and CVP margins downstream.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from erp_core.channels.ecommerce.amazon_sp import (
    SP_API_EU_ENDPOINT,
    SP_API_FE_ENDPOINT,
    SP_API_NA_ENDPOINT,
    AmazonListingReceipt,
    AmazonSpApiAdapter,
    amazon_sp_endpoint,
)
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

SELLER_ID = "A1SELLERTESTID"
CLIENT_ID = "amzn1.application-oa2-client.testid"
CLIENT_SECRET = "amzn1.oa2-cs.v1.super-secret-client-value"
REFRESH_TOKEN = "Atzr|super-secret-refresh-token-value"
ACCESS_TOKEN = "Atza|mock-access-token-value"
MARKETPLACE_US = "ATVPDKIKX0DER"

RequestHandler = Callable[[httpx.Request], httpx.Response]


def _adapter(
    handler: RequestHandler, *, marketplace_id: str = MARKETPLACE_US
) -> AmazonSpApiAdapter:
    return AmazonSpApiAdapter(
        seller_id=SELLER_ID,
        marketplace_id=marketplace_id,
        lwa_client_id=CLIENT_ID,
        lwa_client_secret=CLIENT_SECRET,
        refresh_token=REFRESH_TOKEN,
        transport=httpx.MockTransport(handler),
    )


def _lwa_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": ACCESS_TOKEN, "expires_in": 3600, "token_type": "bearer"},
    )


def _is_lwa(request: httpx.Request) -> bool:
    return request.url.host == "api.amazon.com"


# ---------------------------------------------------------------------------- endpoints


def test_amazon_sp_endpoint_region_mapping() -> None:
    for marketplace_id in (
        "ATVPDKIKX0DER",
        "A2EUQ1WTGCTBG2",
        "A1AM78C64UM0Y8",
        "A2Q3Y263D00KWC",
    ):
        assert amazon_sp_endpoint(marketplace_id) == SP_API_NA_ENDPOINT

    for marketplace_id in (
        "A1F83G8C2ARO7P",
        "A1PA6795UKMFR9",
        "A13V1IB3VIYZZH",
        "APJ6JRA9NG5V4",
        "A1RKKUPIHCS9HS",
        "A1805IZSGTT6HS",
        "A2NODRKZP88ZB9",
        "A1C3SOZRARQ6R3",
        "A33AVAJ2PDY3EV",
        "A2VIGQ35RCS4UG",
        "A17E79C6D8DWNP",
        "A21TJRUUN4KGV",
    ):
        assert amazon_sp_endpoint(marketplace_id) == SP_API_EU_ENDPOINT

    for marketplace_id in ("A1VC38T7YXB528", "A39IBJ37TRP1C6", "A19VAU5U5O7RUS"):
        assert amazon_sp_endpoint(marketplace_id) == SP_API_FE_ENDPOINT

    assert amazon_sp_endpoint("A1UNKNOWNMARKET") == SP_API_NA_ENDPOINT
    assert amazon_sp_endpoint(" a1f83g8c2aro7p ") == SP_API_EU_ENDPOINT


# ---------------------------------------------------------------------------- LWA auth


async def test_lwa_token_exchange_then_reuses_cached_token() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if _is_lwa(request):
            return _lwa_ok()
        return httpx.Response(200, json={"payload": {"Orders": []}})

    adapter = _adapter(handler)
    since = datetime(2026, 11, 20, tzinfo=UTC)
    await adapter.fetch_orders_updated_since(since)
    await adapter.fetch_orders_updated_since(since)

    lwa_calls = [request for request in calls if _is_lwa(request)]
    assert len(lwa_calls) == 1, "the second SP-API call must reuse the cached LWA token"

    form = parse_qs(lwa_calls[0].content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == [REFRESH_TOKEN]
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == [CLIENT_SECRET]
    assert lwa_calls[0].method == "POST"

    spapi_calls = [request for request in calls if not _is_lwa(request)]
    assert len(spapi_calls) == 2
    assert {request.headers["x-amz-access-token"] for request in spapi_calls} == {ACCESS_TOKEN}


async def test_expired_cached_token_is_refreshed() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if _is_lwa(request):
            return _lwa_ok()
        return httpx.Response(200, json={"payload": {"Orders": []}})

    adapter = _adapter(handler)
    since = datetime(2026, 11, 20, tzinfo=UTC)
    await adapter.fetch_orders_updated_since(since)
    # Simulate a token that is inside the 60s expiry buffer.
    adapter._access_token = "Atza|stale-token"  # noqa: SLF001 - explicit cache expiry probe
    adapter._token_expires_at = time.monotonic() - 1  # noqa: SLF001
    await adapter.fetch_orders_updated_since(since)

    assert len([request for request in calls if _is_lwa(request)]) == 2


async def test_lwa_401_raises_auth_failed_without_leaking_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _is_lwa(request)
        return httpx.Response(
            401,
            json={"error": "invalid_grant", "error_description": "The refresh token is invalid."},
        )

    adapter = _adapter(handler)
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 20, tzinfo=UTC))

    error = excinfo.value
    assert error.slug == "amazon-auth-failed"
    assert error.code == "ERR_AMAZON_AUTH_FAILED"
    assert error.status_code == 502
    blob = " ".join([error.detail, error.title, str(error), json.dumps({"d": error.detail})])
    assert CLIENT_SECRET not in blob
    assert REFRESH_TOKEN not in blob
    assert ACCESS_TOKEN not in blob


async def test_spapi_403_discards_token_and_raises_auth_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_lwa(request):
            return _lwa_ok()
        return httpx.Response(403, json={"errors": [{"code": "Unauthorized", "message": "nope"}]})

    adapter = _adapter(handler)
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 20, tzinfo=UTC))

    assert excinfo.value.code == "ERR_AMAZON_AUTH_FAILED"
    assert adapter._access_token is None  # noqa: SLF001 - the cached token must be dropped


async def test_spapi_429_maps_to_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_lwa(request):
            return _lwa_ok()
        return httpx.Response(429, headers={"Retry-After": "2"}, json={"errors": []})

    adapter = _adapter(handler)
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 20, tzinfo=UTC))

    error = excinfo.value
    assert error.slug == "amazon-rate-limited"
    assert error.code == "ERR_AMAZON_RATE_LIMITED"
    assert error.status_code == 429
    assert "2" in error.detail


# ---------------------------------------------------------------------------- orders


def _order_payload(
    *,
    order_id: str,
    seller_order_id: str,
    status: str,
    total: str,
    updated: str = "2026-11-20T09:15:00Z",
    payment_method: str = "Other",
) -> dict[str, Any]:
    return {
        "AmazonOrderId": order_id,
        "SellerOrderId": seller_order_id,
        "MarketplaceId": MARKETPLACE_US,
        "OrderStatus": status,
        "FulfillmentChannel": "MFN",
        "SalesChannel": "Amazon.com",
        "PaymentMethod": payment_method,
        "OrderTotal": {"Amount": total, "CurrencyCode": "USD"},
        "LastUpdateDate": updated,
        "BuyerInfo": {"BuyerEmail": "ops@euroled.de"},
        "ShippingAddress": {"CountryCode": "DE", "Phone": "+49301234567"},
        "IsBusinessOrder": False,
    }


ORDER_UNSHIPPED = _order_payload(
    order_id="111-2223334-5556667",
    seller_order_id="SO-9001",
    status="Unshipped",
    total="230.48",
)
ORDER_SHIPPED = _order_payload(
    order_id="111-2223334-5556668",
    seller_order_id="SO-9002",
    status="Shipped",
    total="89.99",
)
ORDER_CANCELED = _order_payload(
    order_id="111-2223334-5556669",
    seller_order_id="SO-9003",
    status="Canceled",
    total="15.00",
)

ORDER_ITEMS: dict[str, list[dict[str, Any]]] = {
    "111-2223334-5556667": [
        {
            "ASIN": "B0AAAA1111",
            "SellerSKU": "AMAZON-SRC-1A2B",
            "Title": "LED Panel 40W 4000K",
            "QuantityOrdered": 2,
            "ItemPrice": {"Amount": "200.00", "CurrencyCode": "USD"},
            "ItemTax": {"Amount": "11.48", "CurrencyCode": "USD"},
            "ShippingPrice": {"Amount": "24.00", "CurrencyCode": "USD"},
            "PromotionDiscount": {"Amount": "-5.00", "CurrencyCode": "USD"},
        }
    ],
    "111-2223334-5556668": [
        {
            "ASIN": "B0BBBB2222",
            "SellerSKU": "AMAZON-SRC-3C4D",
            "Title": "LED Strip 5m",
            "QuantityOrdered": 3,
            "ItemPrice": {"Amount": "89.99", "CurrencyCode": "USD"},
            "ItemTax": {"Amount": "0.00", "CurrencyCode": "USD"},
            "ShippingPrice": {"Amount": "0.00", "CurrencyCode": "USD"},
            "PromotionDiscount": {"Amount": "0.00", "CurrencyCode": "USD"},
        }
    ],
    "111-2223334-5556669": [
        {
            "ASIN": "B0CCCC3333",
            "SellerSKU": "AMAZON-SRC-5E6F",
            "Title": "LED Driver 24V",
            "QuantityOrdered": 1,
            "ItemPrice": {"Amount": "15.00", "CurrencyCode": "USD"},
            "ItemTax": {"Amount": "1.20", "CurrencyCode": "USD"},
            "ShippingPrice": {"Amount": "5.00", "CurrencyCode": "USD"},
            "PromotionDiscount": {"Amount": "0.00", "CurrencyCode": "USD"},
        }
    ],
}


async def test_fetch_orders_pagination_line_items_decimals_and_status_mapping() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if _is_lwa(request):
            return _lwa_ok()
        path = request.url.path
        if path == "/orders/v0/orders":
            if request.url.params.get("NextToken"):
                return httpx.Response(200, json={"payload": {"Orders": []}})
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "Orders": [ORDER_UNSHIPPED, ORDER_SHIPPED, ORDER_CANCELED],
                        "NextToken": "NEXT-PAGE-2",
                    }
                },
            )
        if path.endswith("/orderItems"):
            order_id = path.split("/")[-2]
            return httpx.Response(200, json={"payload": {"OrderItems": ORDER_ITEMS[order_id]}})
        raise AssertionError(f"unexpected path {path}")

    adapter = _adapter(handler)
    since = datetime(2026, 11, 20, tzinfo=UTC)
    orders, cursor = await adapter.fetch_orders_updated_since(since)

    assert cursor == "NEXT-PAGE-2"
    assert [order.external_order_id for order in orders] == [
        "111-2223334-5556667",
        "111-2223334-5556668",
        "111-2223334-5556669",
    ]

    first = orders[0]
    assert first.external_order_number == "SO-9001"
    assert first.currency == "USD"
    assert first.order_mode == OrderMode.B2C_RETAIL
    assert first.fx_rate_to_base is None, "the adapter must never invent an FX rate"
    assert isinstance(first.total_amount, Decimal)
    assert isinstance(first.subtotal_amount, Decimal)
    assert isinstance(first.shipping_charged, Decimal)
    assert isinstance(first.tax_collected, Decimal)
    assert isinstance(first.discount_amount, Decimal)
    assert isinstance(first.line_items[0].line_total, Decimal)
    assert not any(
        isinstance(value, float)
        for value in (
            first.total_amount,
            first.subtotal_amount,
            first.shipping_charged,
            first.tax_collected,
            first.discount_amount,
            first.line_items[0].line_total,
        )
    )
    assert str(first.total_amount) == "230.48"
    assert first.total_amount == Decimal("230.48")
    assert str(first.subtotal_amount) == "200.00"
    assert str(first.shipping_charged) == "24.00"
    assert str(first.tax_collected) == "11.48"
    assert str(first.discount_amount) == "5.00", "promotion discounts are stored as magnitude"
    assert first.fulfillment_status == FulfillmentStatus.CONFIRMED_RESERVED
    assert first.payment_status == PaymentStatus.PAID
    assert first.customer.email == "ops@euroled.de"
    assert first.customer.external_identity_type == IdentityType.AMAZON_BUYER_EMAIL
    assert first.customer.country_code == "DE"
    assert first.line_items[0].external_sku == "AMAZON-SRC-1A2B"
    assert first.line_items[0].quantity == 2
    assert str(first.line_items[0].line_total) == "200.00"
    assert first.channel_metadata["amazon_order_status"] == "Unshipped"
    assert first.updated_at == datetime(2026, 11, 20, 9, 15, tzinfo=UTC)

    assert orders[1].fulfillment_status == FulfillmentStatus.SHIPPED
    assert orders[1].payment_status == PaymentStatus.PAID
    assert orders[2].fulfillment_status == FulfillmentStatus.CANCELLED
    assert orders[2].payment_status == PaymentStatus.REFUNDED

    orders_call = next(
        request
        for request in calls
        if request.url.path == "/orders/v0/orders" and not request.url.params.get("NextToken")
    )
    assert orders_call.method == "GET"
    assert orders_call.url.params.get("MarketplaceIds") == MARKETPLACE_US
    assert orders_call.url.params.get("LastUpdatedAfter") == "2026-11-20T00:00:00.000Z"
    assert orders_call.url.params.get("MaxResultsPerPage") == "100"
    assert orders_call.headers["x-amz-access-token"] == ACCESS_TOKEN

    item_calls = [request for request in calls if request.url.path.endswith("/orderItems")]
    assert len(item_calls) == 3
    assert {request.url.params.get("MarketplaceIds") for request in item_calls} == {MARKETPLACE_US}

    # Second page: NextToken only, never LastUpdatedAfter (SP-API rejects the pair).
    page_two, page_two_cursor = await adapter.fetch_orders_updated_since(
        since, cursor="NEXT-PAGE-2"
    )
    assert page_two == []
    assert page_two_cursor is None
    cursored_call = calls[-1]
    assert cursored_call.url.params.get("NextToken") == "NEXT-PAGE-2"
    assert "LastUpdatedAfter" not in cursored_call.url.params


async def test_pending_order_is_unpaid_pending_payment() -> None:
    pending = _order_payload(
        order_id="111-2223334-5556670",
        seller_order_id="SO-9004",
        status="Pending",
        total="42.00",
        payment_method="",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_lwa(request):
            return _lwa_ok()
        if request.url.path == "/orders/v0/orders":
            return httpx.Response(200, json={"payload": {"Orders": [pending]}})
        return httpx.Response(
            200,
            json={
                "payload": {
                    "OrderItems": [
                        {
                            "SellerSKU": "AMAZON-PENDING",
                            "QuantityOrdered": 1,
                            "ItemPrice": {"Amount": "42.00", "CurrencyCode": "USD"},
                        }
                    ]
                }
            },
        )

    orders, cursor = await _adapter(handler).fetch_orders_updated_since(
        datetime(2026, 11, 20, tzinfo=UTC)
    )
    assert cursor is None
    assert orders[0].fulfillment_status == FulfillmentStatus.PENDING_PAYMENT
    assert orders[0].payment_status == PaymentStatus.UNPAID


# ---------------------------------------------------------------------------- listings read


async def test_fetch_products_pages_listings_items_and_filters_by_updated_since() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if _is_lwa(request):
            return _lwa_ok()
        assert request.url.path == f"/listings/2021-08-01/items/{SELLER_ID}"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "sku": "AMAZON-SRC-1A2B",
                        "summaries": [
                            {
                                "asin": "B0AAAA1111",
                                "itemName": "LED Panel 40W 4000K",
                                "status": ["BUYABLE"],
                                "lastUpdatedDate": "2026-11-20T05:00:00Z",
                            }
                        ],
                    },
                    {
                        "sku": "AMAZON-SRC-OLD1",
                        "summaries": [
                            {
                                "asin": "B0OLDLISTI",
                                "itemName": "Discontinued Panel",
                                "lastUpdatedDate": "2026-01-01T00:00:00Z",
                            }
                        ],
                    },
                ],
                "pagination": {"nextToken": "LIST-PAGE-2"},
            },
        )

    products, cursor = await _adapter(handler).fetch_products_updated_since(
        datetime(2026, 11, 1, tzinfo=UTC)
    )

    assert cursor == "LIST-PAGE-2"
    assert [product.external_product_id for product in products] == ["B0AAAA1111"]
    assert products[0].title == "LED Panel 40W 4000K"
    assert products[0].variants[0]["sku"] == "AMAZON-SRC-1A2B"
    assert products[0].variants[0]["status"] == "BUYABLE"
    assert calls[-1].url.params.get("marketplaceIds") == MARKETPLACE_US
    assert calls[-1].url.params.get("includedData") == "summaries"


# ---------------------------------------------------------------------------- listings write


async def test_publish_listing_put_payload_and_receipt() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if _is_lwa(request):
            return _lwa_ok()
        return httpx.Response(
            200,
            json={"status": "ACCEPTED", "submissionId": "sub-123", "asin": "B0TESTASIN"},
        )

    adapter = _adapter(handler)
    receipt = await adapter.publish_listing(
        sku="AMAZON-SRC-1A2B",
        title="LED Panel 40W 4000K",
        description="Factory-direct LED panel with CE/RoHS documentation.",
        bullet_points=["Factory-direct", "Export-ready CE/RoHS", "DDP air and sea"],
        price_usd=Decimal("19.99"),
        quantity=7,
        images=["https://cdn.example.com/main.jpg", "https://cdn.example.com/alt-1.jpg"],
        category="LED_PANEL_LIGHT",
        hs_code="9405.40",
        moq_tiers=[{"min_qty": 100, "unit_price_usd": Decimal("17.50")}],
    )

    assert isinstance(receipt, AmazonListingReceipt)
    assert receipt.external_product_id == "B0TESTASIN"
    assert receipt.external_sku == "AMAZON-SRC-1A2B"
    assert receipt.submission_id == "sub-123"
    assert receipt.status == "ACCEPTED"
    assert receipt.issues == []
    assert receipt.raw_response["asin"] == "B0TESTASIN"

    listing_call = calls[-1]
    assert listing_call.method == "PUT"
    assert listing_call.url.path == f"/listings/2021-08-01/items/{SELLER_ID}/AMAZON-SRC-1A2B"
    assert listing_call.url.params.get("marketplaceIds") == MARKETPLACE_US
    assert listing_call.headers["content-type"] == "application/json"
    assert listing_call.headers["x-amz-access-token"] == ACCESS_TOKEN

    body = json.loads(listing_call.content)
    assert body["productType"] == "PRODUCT"
    assert body["requirements"] == "LISTING"
    attributes = body["attributes"]
    assert attributes["item_name"][0]["value"] == "LED Panel 40W 4000K"
    assert attributes["item_name"][0]["marketplace_id"] == MARKETPLACE_US
    assert attributes["product_description"][0]["value"].startswith("Factory-direct")
    assert [entry["value"] for entry in attributes["bullet_point"]] == [
        "Factory-direct",
        "Export-ready CE/RoHS",
        "DDP air and sea",
    ]
    offer = attributes["purchasable_offer"][0]
    assert offer["currency"] == "USD"
    price_str = offer["our_price"][0]["schedule"][0]["value_with_tax"]
    assert price_str == "19.99"
    assert Decimal(price_str) == Decimal("19.99")
    assert attributes["fulfillment_availability"] == [
        {"fulfillment_channel_code": "DEFAULT", "quantity": 7}
    ]
    assert attributes["main_product_image_locator"] == [
        {"media_location": "https://cdn.example.com/main.jpg"}
    ]
    assert attributes["other_product_image_locator_1"] == [
        {"media_location": "https://cdn.example.com/alt-1.jpg"}
    ]
    assert attributes["item_type_keyword"] == [{"value": "LED_PANEL_LIGHT"}]
    assert attributes["hs_code"] == [{"value": "9405.40"}]
    assert attributes["quantity_discount_plan"][0]["levels"][0]["lower_bound"] == 100
    assert (
        attributes["quantity_discount_plan"][0]["levels"][0]["discount"]["value_with_tax"]
        == "17.50"
    )


async def test_publish_listing_invalid_status_raises_listing_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_lwa(request):
            return _lwa_ok()
        return httpx.Response(
            200,
            json={
                "status": "INVALID",
                "submissionId": "sub-bad",
                "issues": [
                    {
                        "code": "90220",
                        "severity": "ERROR",
                        "message": "The value provided for the attribute bullet_point is invalid.",
                    }
                ],
            },
        )

    with pytest.raises(AppError) as excinfo:
        await _adapter(handler).publish_listing(
            sku="AMAZON-SRC-1A2B",
            title="LED Panel 40W",
            description="Panel",
            bullet_points=["one"],
            price_usd=Decimal("9.99"),
            quantity=1,
            images=[],
        )

    error = excinfo.value
    assert error.slug == "amazon-listing-rejected"
    assert error.code == "ERR_AMAZON_LISTING_REJECTED"
    assert error.status_code == 502
    assert "90220" in error.detail
    assert "bullet_point" in error.detail
    assert CLIENT_SECRET not in error.detail
    assert REFRESH_TOKEN not in error.detail


async def test_publish_listing_http_error_raises_listing_rejected_without_secret_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_lwa(request):
            return _lwa_ok()
        return httpx.Response(
            400,
            json={
                "status": "INVALID",
                "issues": [
                    {"code": "4000001", "message": f"bad request for {CLIENT_SECRET}"},
                ],
            },
        )

    with pytest.raises(AppError) as excinfo:
        await _adapter(handler).publish_listing(
            sku="AMAZON-SRC-1A2B",
            title="LED Panel 40W",
            description="Panel",
            bullet_points=[],
            price_usd=Decimal("9.99"),
            quantity=1,
            images=[],
        )

    assert excinfo.value.code == "ERR_AMAZON_LISTING_REJECTED"
    assert "4000001" in excinfo.value.detail
    assert CLIENT_SECRET not in excinfo.value.detail
    assert "***" in excinfo.value.detail


async def test_push_inventory_level_sends_json_patch() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if _is_lwa(request):
            return _lwa_ok()
        return httpx.Response(200, json={"status": "ACCEPTED", "submissionId": "inv-sub-1"})

    adapter = _adapter(handler)
    result = await adapter.push_inventory_level(external_sku="AMAZON-SRC-1A2B", quantity=12)

    patch_call = calls[-1]
    assert patch_call.method == "PATCH"
    assert patch_call.url.path == f"/listings/2021-08-01/items/{SELLER_ID}/AMAZON-SRC-1A2B"
    assert patch_call.url.params.get("marketplaceIds") == MARKETPLACE_US
    assert json.loads(patch_call.content) == [
        {
            "op": "replace",
            "path": "/attributes/fulfillment_availability",
            "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": 12}],
        }
    ]
    assert result["external_sku"] == "AMAZON-SRC-1A2B"
    assert result["quantity"] == 12
    assert result["status"] == "ACCEPTED"
    assert result["submission_id"] == "inv-sub-1"

    negative = await adapter.push_inventory_level(external_sku="AMAZON-SRC-1A2B", quantity=-4)
    assert negative["quantity"] == 0
    assert json.loads(calls[-1].content)[0]["value"][0]["quantity"] == 0


# ---------------------------------------------------------------------------- fulfillment


async def test_create_fulfillment_posts_shipment_confirmation() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if _is_lwa(request):
            return _lwa_ok()
        return httpx.Response(200, content=b"")

    result = await _adapter(handler).create_fulfillment(
        external_order_id="111-2223334-5556667",
        tracking_number="1Z999AA10123456784",
        carrier_company="UPS",
        tracking_url="https://www.ups.com/track?num=1Z999AA10123456784",
    )

    call = calls[-1]
    assert call.method == "POST"
    assert call.url.path == "/orders/v0/orders/111-2223334-5556667/shipmentConfirmation"
    body = json.loads(call.content)
    assert body["marketplaceId"] == MARKETPLACE_US
    package = body["packageDetail"]
    assert package["packageReferenceId"] == "1"
    assert package["carrierCode"] == "UPS"
    assert package["trackingNumber"] == "1Z999AA10123456784"
    assert package["orderItems"] == []
    assert package["shipDate"].endswith("Z")
    datetime.fromisoformat(package["shipDate"].replace("Z", "+00:00"))

    assert result["status"] == "ACCEPTED"
    assert result["fulfillment_id"] == "111-2223334-5556667:1"
    assert result["tracking_number"] == "1Z999AA10123456784"
    assert result["tracking_url"] == "https://www.ups.com/track?num=1Z999AA10123456784"


async def test_amazon_cancelled_double_l_refunded_and_empty_sku_guard() -> None:
    cancelled_double_l = _order_payload(
        order_id="111-0000001-0000001",
        seller_order_id="SO-DBL",
        status="Cancelled",
        total="20.00",
    )
    refunded_order = _order_payload(
        order_id="111-0000001-0000002",
        seller_order_id="SO-REF",
        status="Refunded",
        total="30.00",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_lwa(request):
            return _lwa_ok()
        if request.url.path == "/orders/v0/orders":
            return httpx.Response(
                200, json={"payload": {"Orders": [cancelled_double_l, refunded_order]}}
            )
        return httpx.Response(
            200,
            json={
                "payload": {
                    "OrderItems": [
                        {
                            "OrderItemId": "OI-1",
                            "SellerSKU": "AMZ-SKU",
                            "QuantityOrdered": 1,
                            "ItemPrice": {"Amount": "20.00", "CurrencyCode": "USD"},
                        }
                    ]
                }
            },
        )

    adapter = _adapter(handler)
    orders, _ = await adapter.fetch_orders_updated_since(datetime(2026, 11, 20, tzinfo=UTC))
    by_id = {o.external_order_id: o for o in orders}
    assert by_id["111-0000001-0000001"].fulfillment_status == FulfillmentStatus.CANCELLED
    assert by_id["111-0000001-0000001"].payment_status == PaymentStatus.REFUNDED
    assert by_id["111-0000001-0000002"].fulfillment_status == FulfillmentStatus.REFUNDED
    assert by_id["111-0000001-0000002"].payment_status == PaymentStatus.REFUNDED

    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(external_sku="  ", quantity=5)
    assert excinfo.value.code == "ERR_AMAZON_INVENTORY_SYNC"
    assert excinfo.value.status_code == 422


# ---------------------------------------------------------------------------- webhooks


def test_verify_webhook_constant_time_hmac() -> None:
    adapter = _adapter(lambda request: httpx.Response(500))
    raw_body = b'{"Type":"Notification","MessageId":"m-1"}'
    secret = "amazon-sns-webhook-secret"
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()

    assert adapter.verify_webhook(raw_body, {"X-Amz-Sns-Signature": signature}, secret) is True
    assert adapter.verify_webhook(raw_body, {"x-amz-sns-signature": signature}, secret) is True
    assert adapter.verify_webhook(raw_body, {"X-Amazon-Signature": signature}, secret) is True
    assert (
        adapter.verify_webhook(raw_body, {"X-Amazon-Signature": signature}, "wrong-secret") is False
    )
    assert (
        adapter.verify_webhook(b'{"tampered":true}', {"X-Amazon-Signature": signature}, secret)
        is False
    )
    assert adapter.verify_webhook(raw_body, {"X-Amazon-Signature": ""}, secret) is False
    assert adapter.verify_webhook(raw_body, {"X-Amazon-Signature": signature}, "") is False
    assert adapter.verify_webhook(raw_body, {}, secret) is False
