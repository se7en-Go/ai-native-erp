"""Zero-network unit tests for the AliExpress Open Platform (TOP) adapter."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from erp_core.catalog.sourcing_service import (
    MarketplaceImageBlob,
    MarketplaceListingPayload,
    MarketplaceVariantPayload,
    SourcingMoqTier,
    SourcingOptionSpec,
)
from erp_core.channels.ecommerce.aliexpress import (
    ALIEXPRESS_IMAGE_UPLOAD_METHOD,
    ALIEXPRESS_ORDER_LIST_METHOD,
    ALIEXPRESS_PRODUCT_CREATE_METHOD,
    ALIEXPRESS_PRODUCT_LIST_METHOD,
    ALIEXPRESS_SHIPMENT_CREATE_METHOD,
    ALIEXPRESS_STOCK_UPDATE_METHOD,
    AliExpressAdapter,
    sign_aliexpress_request,
    verify_aliexpress_webhook,
)
from erp_core.shared.enums import FulfillmentStatus, IdentityType, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

APP_KEY = "test-app-key"
APP_SECRET = "test-app-secret-value"
ACCESS_TOKEN = "test-access-token-value"
SELLER_ID = "seller-aliexpress-1"
ENDPOINT_URL = "https://openapi.test/sync"
WEBHOOK_HEADER = "x-aliexpress-hmac-sha256"
SINCE = datetime(2024, 5, 1, tzinfo=UTC)

Route = Callable[[httpx.Request], httpx.Response]
Handler = Callable[[httpx.Request], httpx.Response]


# ------------------------------------------------------------------------------ helpers


def _wire_params(request: httpx.Request) -> dict[str, str]:
    """Decode the signed form map exactly as the AliExpress gateway would."""
    return dict(parse_qsl(request.content.decode("utf-8"), keep_blank_values=True))


def _assert_signed(request: httpx.Request, method: str) -> dict[str, str]:
    """Recompute the TOP signature from the wire parameters and pin transport hygiene."""
    params = _wire_params(request)
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT_URL
    assert params["method"] == method
    assert params["app_key"] == APP_KEY
    assert params["session"] == ACCESS_TOKEN
    assert params["format"] == "json"
    assert params["v"] == "2.0"
    assert params["sign_method"] == "hmac-sha256"
    unsigned = {key: value for key, value in params.items() if key != "sign"}
    assert params["sign"] == sign_aliexpress_request(method, unsigned, APP_SECRET)
    return params


class FakeAliExpress:
    """Deterministic route table standing in for the AliExpress gateway."""

    def __init__(self) -> None:
        self._routes: dict[str, Route] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, handler: Route) -> FakeAliExpress:
        self._routes[method] = handler
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = _wire_params(request)["method"]
        self.requests.append(request)
        handler = self._routes.get(method)
        if handler is None:
            raise AssertionError(f"unexpected AliExpress method on the wire: {method}")
        return handler(request)

    def params_of(self, method: str) -> dict[str, str]:
        for request in self.requests:
            params = _wire_params(request)
            if params.get("method") == method:
                return params
        raise AssertionError(f"no request was made for {method}")


def _adapter(handler: Handler, **overrides: Any) -> AliExpressAdapter:
    kwargs: dict[str, Any] = {
        "seller_id": SELLER_ID,
        "app_key": APP_KEY,
        "app_secret": APP_SECRET,
        "access_token": ACCESS_TOKEN,
        "endpoint_url": ENDPOINT_URL,
        "transport": httpx.MockTransport(handler),
        "timeout_seconds": 5.0,
    }
    kwargs.update(overrides)
    return AliExpressAdapter(**kwargs)


def _envelope(method: str, result: dict[str, Any]) -> dict[str, Any]:
    return {f"{method.replace('.', '_')}_response": {"result": result}}


def _order(**overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "order_id": "8192345678901234",
        "order_status": "WAIT_SELLER_SEND_GOODS",
        "payment_status": "PAID",
        "buyer_login_id": "buyer_ae_1",
        "buyer_email": "buyer@example.com",
        "buyer_country": "US",
        "order_amount": {"currency_code": "USD", "amount": "123.45"},
        "product_amount": {"currency_code": "USD", "amount": "100.00"},
        "shipping_fee": {"currency_code": "USD", "amount": "10.00"},
        "seller_discount": {"currency_code": "USD", "amount": "5.00"},
        "tax_amount": {"currency_code": "USD", "amount": "8.45"},
        "platform_commission": {"currency_code": "USD", "amount": "5.4321"},
        "refund_amount": {"currency_code": "USD", "amount": "0.00"},
        "gmt_modified": "2026-11-19 10:00:00",
        "order_line_list": {
            "order_line": [
                {
                    "product_id": "1005001",
                    "sku_code": "AE-RED-M",
                    "product_name": "LED Panel Light",
                    "product_count": 2,
                    "product_price": {"currency_code": "USD", "amount": "45.5"},
                },
                {
                    "product_id": "1005002",
                    "sku_code": "AE-BLU-L",
                    "product_name": "LED Strip",
                    "product_count": 1,
                    "order_line_total": {"currency_code": "USD", "amount": "9"},
                },
            ]
        },
    }
    order.update(overrides)
    return order


def _order_route(**overrides: Any) -> tuple[FakeAliExpress, AliExpressAdapter]:
    fake = FakeAliExpress().on(
        ALIEXPRESS_ORDER_LIST_METHOD,
        lambda request: httpx.Response(
            200, json=_envelope(ALIEXPRESS_ORDER_LIST_METHOD, {"orders": [_order(**overrides)]})
        ),
    )
    return fake, _adapter(fake)


def _listing_payload(**overrides: Any) -> MarketplaceListingPayload:
    payload: dict[str, Any] = {
        "channel_account_id": "2a8f78c4-9d5c-4b8c-9f0e-1c0b0e6c9d11",
        "channel_type": "aliexpress",
        "external_sku": "AE-LAMP-RED",
        "title": "LED Panel Light 24W",
        "description": "Dimmable LED panel with a 3-year warranty.",
        "bullet_points": ["24W dimmable", "IP44 rated"],
        "price_usd": Decimal("19.995"),
        "quantity": 25,
        "images": ["https://cdn.example.com/lamp.jpg", "http://insecure.example.com/x.jpg"],
        "category": "1503",
        "hs_code": "94054090",
    }
    payload.update(overrides)
    return MarketplaceListingPayload(**payload)


# ---------------------------------------------------------------------------- signing


def test_sign_aliexpress_request_matches_canonical_hmac_sha256() -> None:
    params = {
        "app_key": "k",
        "method": ALIEXPRESS_ORDER_LIST_METHOD,
        "page": "1",
        "sign": "deadbeef",
    }
    canonical = (
        ALIEXPRESS_ORDER_LIST_METHOD
        + "app_keyk"
        + "method"
        + ALIEXPRESS_ORDER_LIST_METHOD
        + "page1"
    )
    expected = hmac.new(
        APP_SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest().upper()

    assert sign_aliexpress_request(ALIEXPRESS_ORDER_LIST_METHOD, params, APP_SECRET) == expected
    assert expected.isupper()


def test_sign_aliexpress_request_is_order_independent_and_binds_the_method() -> None:
    params = {"app_key": APP_KEY, "page": "2", "page_size": "50"}
    reordered = {"page_size": "50", "page": "2", "app_key": APP_KEY}
    signature = sign_aliexpress_request(ALIEXPRESS_ORDER_LIST_METHOD, params, APP_SECRET)

    assert signature == sign_aliexpress_request(ALIEXPRESS_ORDER_LIST_METHOD, reordered, APP_SECRET)
    assert signature != sign_aliexpress_request(
        ALIEXPRESS_PRODUCT_LIST_METHOD, reordered, APP_SECRET
    )
    assert signature != sign_aliexpress_request(ALIEXPRESS_ORDER_LIST_METHOD, params, "other")


def test_sign_aliexpress_request_requires_a_secret() -> None:
    with pytest.raises(ValueError, match="app_secret"):
        sign_aliexpress_request(ALIEXPRESS_ORDER_LIST_METHOD, {"page": "1"}, "")


# ------------------------------------------------------------------------ construction


@pytest.mark.parametrize("missing", ["seller_id", "app_key", "app_secret", "access_token"])
def test_adapter_refuses_to_construct_without_required_credentials(missing: str) -> None:
    kwargs: dict[str, Any] = {
        "seller_id": SELLER_ID,
        "app_key": APP_KEY,
        "app_secret": APP_SECRET,
        "access_token": ACCESS_TOKEN,
    }
    kwargs[missing] = "   "
    with pytest.raises(ValueError, match=missing):
        AliExpressAdapter(**kwargs)


def test_adapter_rejects_an_unknown_fulfillment_mode() -> None:
    with pytest.raises(ValueError, match="fulfillment_mode"):
        AliExpressAdapter(
            seller_id=SELLER_ID,
            app_key=APP_KEY,
            app_secret=APP_SECRET,
            access_token=ACCESS_TOKEN,
            fulfillment_mode="consignment",
        )


def test_adapter_normalizes_mode_and_endpoint_and_redacts_repr() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(200, json={}), fulfillment_mode="MANAGED"
    )
    assert adapter.fulfillment_mode == "managed"
    assert adapter.endpoint_url == ENDPOINT_URL
    assert adapter.timeout_seconds == 5.0

    rendered = repr(adapter)
    assert APP_SECRET not in rendered
    assert ACCESS_TOKEN not in rendered
    assert rendered.count("[REDACTED]") == 2
    assert SELLER_ID in rendered


# ------------------------------------------------------------------------ order fetch


async def test_fetch_orders_normalizes_ds_order_with_exact_decimals() -> None:
    fake, adapter = _order_route()
    orders, cursor = await adapter.fetch_orders_updated_since(SINCE)

    _assert_signed(fake.requests[0], ALIEXPRESS_ORDER_LIST_METHOD)
    params = fake.params_of(ALIEXPRESS_ORDER_LIST_METHOD)
    assert params["modify_date_start"] == "2024-05-01 00:00:00"
    assert params["page"] == "1"
    assert params["page_size"] == "50"
    assert cursor is None
    assert len(orders) == 1

    order = orders[0]
    assert order.external_order_id == "8192345678901234"
    assert order.external_order_number is None
    assert order.order_mode == OrderMode.B2C_RETAIL
    assert order.currency == "USD"
    assert order.fx_rate_to_base is None
    assert order.subtotal_amount == Decimal("100.00")
    assert order.shipping_charged == Decimal("10.00")
    assert order.discount_amount == Decimal("5.00")
    assert order.tax_collected == Decimal("8.45")
    assert order.total_amount == Decimal("123.45")
    assert order.platform_commission_base == Decimal("5.43")
    assert str(order.platform_commission_base) == "5.43"
    assert order.fulfillment_status == FulfillmentStatus.CONFIRMED_RESERVED
    assert order.payment_status == PaymentStatus.PAID
    assert order.updated_at == datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    assert order.customer.email == "buyer@example.com"
    assert order.customer.external_customer_id == "buyer_ae_1"
    assert order.customer.external_identity_type == IdentityType.EMAIL
    assert order.customer.country_code == "US"
    assert [line.external_sku for line in order.line_items] == ["AE-RED-M", "AE-BLU-L"]
    assert [line.quantity for line in order.line_items] == [2, 1]
    assert [line.line_total for line in order.line_items] == [
        Decimal("91.00"),
        Decimal("9.00"),
    ]
    assert order.line_items[0].title == "LED Panel Light"
    assert order.channel_metadata["aliexpress_order_status"] == "wait_seller_send_goods"
    assert order.channel_metadata["aliexpress_seller_id"] == SELLER_ID
    assert order.channel_metadata["aliexpress_buyer_login_id"] == "buyer_ae_1"
    assert order.channel_metadata["platform_commission_base"] == "5.43"
    assert "refunded_amount" not in order.channel_metadata
    money = [
        order.subtotal_amount,
        order.shipping_charged,
        order.discount_amount,
        order.tax_collected,
        order.total_amount,
        order.platform_commission_base,
    ]
    assert all(isinstance(value, Decimal) for value in money)
    assert not any(isinstance(value, float) for value in money)


async def test_fetch_orders_quantizes_commission_and_refund_half_up() -> None:
    _fake, adapter = _order_route(
        platform_commission={"currency_code": "USD", "amount": "0.125"},
        refund_amount={"currency_code": "USD", "amount": "1.239"},
    )
    orders, _cursor = await adapter.fetch_orders_updated_since(SINCE)

    order = orders[0]
    assert order.platform_commission_base == Decimal("0.13")
    assert order.channel_metadata["platform_commission_base"] == "0.13"
    assert order.channel_metadata["refunded_amount"] == "1.24"


async def test_fetch_orders_derives_totals_from_line_items_without_floats() -> None:
    _fake, adapter = _order_route(
        product_amount=None,
        order_amount=None,
        currency="EUR",
        buyer_email=None,
    )
    orders, _cursor = await adapter.fetch_orders_updated_since(SINCE)

    order = orders[0]
    assert order.currency == "EUR"
    assert order.subtotal_amount == Decimal("100.00")
    assert order.total_amount == Decimal("113.45")
    assert isinstance(order.total_amount, Decimal)
    # Without an email the customer is anchored on the buyer login (fail closed).
    assert order.customer.email is None
    assert order.customer.external_identity_type == IdentityType.WEB_VISITOR_ID


async def test_fetch_orders_accepts_numeric_json_amounts_without_binary_floats() -> None:
    _fake, adapter = _order_route(
        order_amount={"currency_code": "USD", "amount": 113.45},
        platform_commission={"currency_code": "USD", "amount": 5},
    )
    orders, _cursor = await adapter.fetch_orders_updated_since(SINCE)

    order = orders[0]
    assert order.total_amount == Decimal("113.45")
    assert order.platform_commission_base == Decimal("5.00")
    assert isinstance(order.total_amount, Decimal)


async def test_fetch_orders_skips_orders_without_an_id_and_lines_without_a_sku() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_ORDER_LIST_METHOD,
        lambda request: httpx.Response(
            200,
            json=_envelope(
                ALIEXPRESS_ORDER_LIST_METHOD,
                {
                    "orders": [
                        {"order_status": "WAIT_SELLER_SEND_GOODS"},
                        _order(
                            product_amount=None,
                            order_line_list={
                                "order_line": [
                                    {"product_id": "1", "product_count": 1},
                                    {
                                        "sku_code": "AE-OK",
                                        "product_count": 3,
                                        "product_price": {"amount": "2.5", "currency_code": "USD"},
                                    },
                                ]
                            }
                        ),
                        _order(order_id="8192345678909999", order_line_list={"order_line": []}),
                    ]
                },
            ),
        ),
    )
    orders, _cursor = await _adapter(fake).fetch_orders_updated_since(SINCE)

    assert len(orders) == 1
    assert len(fake.requests) == 1
    assert [line.external_sku for line in orders[0].line_items] == ["AE-OK"]
    assert orders[0].line_items[0].line_total == Decimal("7.50")
    # The unpriced-missing line means the subtotal falls back to the surviving line.
    assert orders[0].subtotal_amount == Decimal("7.50")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("PLACE_ORDER_SUCCESS", FulfillmentStatus.PENDING_PAYMENT),
        ("WAIT_SELLER_SEND_GOODS", FulfillmentStatus.CONFIRMED_RESERVED),
        ("SELLER_PART_SEND_GOODS", FulfillmentStatus.PARTIALLY_SHIPPED),
        ("WAIT_BUYER_ACCEPT_GOODS", FulfillmentStatus.SHIPPED),
        ("FINISH", FulfillmentStatus.DELIVERED),
        ("IN_CANCEL", FulfillmentStatus.CANCELLED),
        ("BUYER_CANCEL", FulfillmentStatus.CANCELLED),
        ("SELLER_CANCEL_CLOSE_ORDER", FulfillmentStatus.CANCELLED),
        ("CANCEL", FulfillmentStatus.CANCELLED),
        ("CANCELLED", FulfillmentStatus.CANCELLED),
        ("CANCELED", FulfillmentStatus.CANCELLED),
        ("REFUNDED", FulfillmentStatus.REFUNDED),
        ("RETURNED", FulfillmentStatus.REFUNDED),
        ("SOME_FUTURE_STATUS", FulfillmentStatus.CONFIRMED_RESERVED),
    ],
)
async def test_fetch_orders_maps_statuses(
    status: str, expected: FulfillmentStatus
) -> None:
    _fake, adapter = _order_route(order_status=status, payment_status="PAID")
    orders, _cursor = await adapter.fetch_orders_updated_since(SINCE)
    assert orders[0].fulfillment_status == expected


@pytest.mark.parametrize(
    ("status", "payment_flag", "extra", "expected"),
    [
        ("PLACE_ORDER_SUCCESS", "NOT_PAID", {}, PaymentStatus.UNPAID),
        ("FINISH", "PAID", {}, PaymentStatus.PAID),
        ("BUYER_CANCEL", "PAID", {}, PaymentStatus.REFUNDED),
        ("BUYER_CANCEL", "NOT_PAID", {}, PaymentStatus.UNPAID),
        ("BUYER_CANCEL", "", {"pay_time": "2026-11-18 09:00:00"}, PaymentStatus.REFUNDED),
        ("BUYER_CANCEL", "", {}, PaymentStatus.UNPAID),
        ("REFUNDED", "", {}, PaymentStatus.REFUNDED),
        ("RETURNED", "", {}, PaymentStatus.REFUNDED),
        ("FINISH", "PAID", {"refund_amount": {"amount": "123.45"}}, PaymentStatus.REFUNDED),
        ("FINISH", "PAID", {"refund_amount": {"amount": "10.00"}}, PaymentStatus.PAID),
    ],
)
async def test_fetch_orders_maps_payment_states(
    status: str, payment_flag: str, extra: dict[str, Any], expected: PaymentStatus
) -> None:
    _fake, adapter = _order_route(order_status=status, payment_status=payment_flag, **extra)
    orders, _cursor = await adapter.fetch_orders_updated_since(SINCE)
    assert orders[0].payment_status == expected


async def test_fetch_orders_advances_the_cursor_on_has_next() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_ORDER_LIST_METHOD,
        lambda request: httpx.Response(
            200,
            json=_envelope(
                ALIEXPRESS_ORDER_LIST_METHOD,
                {"orders": [_order()], "has_next": True, "total_record": 240},
            ),
        ),
    )
    _orders, cursor = await _adapter(fake).fetch_orders_updated_since(SINCE, cursor="2")

    assert cursor == "3"
    assert fake.params_of(ALIEXPRESS_ORDER_LIST_METHOD)["page"] == "2"


async def test_fetch_orders_cursor_stops_at_the_last_total_page() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_ORDER_LIST_METHOD,
        lambda request: httpx.Response(
            200,
            json=_envelope(
                ALIEXPRESS_ORDER_LIST_METHOD, {"orders": [_order()], "total_record": 120}
            ),
        ),
    )
    _orders, cursor = await _adapter(fake).fetch_orders_updated_since(SINCE)
    assert cursor == "2"

    fake2 = FakeAliExpress().on(
        ALIEXPRESS_ORDER_LIST_METHOD,
        lambda request: httpx.Response(
            200,
            json=_envelope(
                ALIEXPRESS_ORDER_LIST_METHOD, {"orders": [_order()], "total_record": 50}
            ),
        ),
    )
    _orders2, cursor2 = await _adapter(fake2).fetch_orders_updated_since(SINCE, cursor="1")
    assert cursor2 is None


@pytest.mark.parametrize("cursor", ["abc", "0", "-1", "1.5"])
async def test_fetch_orders_rejects_a_non_numeric_cursor(cursor: str) -> None:
    fake = FakeAliExpress()
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).fetch_orders_updated_since(SINCE, cursor=cursor)

    assert excinfo.value.code == "ERR_ALIEXPRESS_INVALID_CURSOR"
    assert excinfo.value.status_code == 422
    assert fake.requests == []


# ---------------------------------------------------------------------- product fetch


async def test_fetch_products_normalizes_variants_and_next_cursor() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_PRODUCT_LIST_METHOD,
        lambda request: httpx.Response(
            200,
            json=_envelope(
                ALIEXPRESS_PRODUCT_LIST_METHOD,
                {
                    "products": [
                        {
                            "product_id": "1005001234",
                            "product_title": "USB-C Hub 7-in-1",
                            "sku_list": {
                                "sku": [
                                    {
                                        "sku_id": "sku-1",
                                        "sku_code": "AE-HUB-GRY",
                                        "sku_attr": "Color:Grey;Ports:7",
                                        "price": "12.345",
                                        "stock": -4,
                                    },
                                    {
                                        "sku_code": "AE-HUB-BLK",
                                        "option_values": {"Color": "Black"},
                                        "price": {"amount": "13", "currency_code": "USD"},
                                        "stock": 40,
                                    },
                                ]
                            },
                        },
                        {"product_title": "no id product"},
                    ],
                    "has_next": "true",
                },
            ),
        ),
    )
    products, cursor = await _adapter(fake).fetch_products_updated_since(SINCE)

    params = _assert_signed(fake.requests[0], ALIEXPRESS_PRODUCT_LIST_METHOD)
    assert params["modify_date_start"] == "2024-05-01 00:00:00"
    assert params["page"] == "1"
    assert cursor == "2"
    assert len(products) == 1
    product = products[0]
    assert product.external_product_id == "1005001234"
    assert product.title == "USB-C Hub 7-in-1"
    assert product.variants == [
        {
            "external_sku": "AE-HUB-GRY",
            "external_variant_id": "sku-1",
            "unit_price_usd": "12.35",
            "stock_quantity": 0,
            "option_values": {"Color": "Grey", "Ports": "7"},
        },
        {
            "external_sku": "AE-HUB-BLK",
            "external_variant_id": None,
            "unit_price_usd": "13.00",
            "stock_quantity": 40,
            "option_values": {"Color": "Black"},
        },
    ]


# ------------------------------------------------------------------------ fulfillment


async def test_create_fulfillment_standard_sends_tracking_and_carrier() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_SHIPMENT_CREATE_METHOD,
        lambda request: httpx.Response(
            200,
            json=_envelope(
                ALIEXPRESS_SHIPMENT_CREATE_METHOD, {"shipment_id": "SHP-1"}
            ),
        ),
    )
    result = await _adapter(fake).create_fulfillment(
        "8192345678901234",
        "LP00123456789CN",
        "China Post Registered Air Mail",
        tracking_url="https://track.example.com/LP00123456789CN",
        fulfillment_order_id="FO-9",
    )

    params = _assert_signed(fake.requests[0], ALIEXPRESS_SHIPMENT_CREATE_METHOD)
    assert params["order_id"] == "8192345678901234"
    assert params["tracking_number"] == "LP00123456789CN"
    assert params["logistics_service_name"] == "China Post Registered Air Mail"
    assert params["tracking_url"] == "https://track.example.com/LP00123456789CN"
    assert params["fulfillment_order_id"] == "FO-9"
    assert params["shipment_mode"] == "standard"
    assert params["notify_customer"] == "true"
    assert result == {
        "shipment_id": "SHP-1",
        "order_id": "8192345678901234",
        "shipment_mode": "standard",
        "fulfillment_id": "SHP-1",
    }


async def test_create_fulfillment_standard_requires_tracking_without_network() -> None:
    fake = FakeAliExpress()
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).create_fulfillment("8192345678901234", "  ", "")

    assert excinfo.value.code == "ERR_ALIEXPRESS_TRACKING_REQUIRED"
    assert excinfo.value.status_code == 422
    assert "managed" in excinfo.value.detail
    assert fake.requests == []


async def test_create_fulfillment_managed_mode_lets_aliexpress_book_the_label() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_SHIPMENT_CREATE_METHOD,
        lambda request: httpx.Response(
            200,
            json=_envelope(
                ALIEXPRESS_SHIPMENT_CREATE_METHOD,
                {"shipment_id": "SHP-2", "awb_number": "CN123456789"},
            ),
        ),
    )
    result = await _adapter(fake, fulfillment_mode="managed").create_fulfillment(
        "8192345678901234", "", "", notify_customer=False
    )

    params = _assert_signed(fake.requests[0], ALIEXPRESS_SHIPMENT_CREATE_METHOD)
    assert params["shipment_mode"] == "managed"
    assert "tracking_number" not in params
    assert "logistics_service_name" not in params
    assert params["notify_customer"] == "false"
    assert result["shipment_mode"] == "managed"
    assert result["awb_number"] == "CN123456789"


async def test_create_fulfillment_rejects_an_empty_order_id() -> None:
    fake = FakeAliExpress()
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).create_fulfillment("  ", "LP1", "China Post")

    assert excinfo.value.code == "ERR_ALIEXPRESS_FULFILLMENT_INVALID_ORDER"
    assert fake.requests == []


# --------------------------------------------------------------------- listing publish


async def test_publish_listing_sends_multi_variant_payload_and_returns_receipts() -> None:
    created: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        created.update(json.loads(_wire_params(request)["product_body"]))
        return httpx.Response(
            200,
            json=_envelope(
                ALIEXPRESS_PRODUCT_CREATE_METHOD,
                {
                    "product_id": "1005009999",
                    "sku_list": {
                        "sku": [
                            {"sku_id": "sku-red", "sku_code": "AE-LAMP-RED"},
                            {"sku_id": "sku-blue", "sku_code": "AE-LAMP-BLUE"},
                        ]
                    },
                },
            ),
        )

    fake = FakeAliExpress().on(ALIEXPRESS_PRODUCT_CREATE_METHOD, handler)
    payload = _listing_payload(
        quantity=77,
        options=[
            SourcingOptionSpec(name="Color", values=["Red", "Blue"]),
            SourcingOptionSpec(name="Plug", values=["US"]),
        ],
        variants=[
            MarketplaceVariantPayload(
                local_sku="LAMP-RED",
                external_sku="AE-LAMP-RED",
                option_values={"Color": "Red", "Plug": "US"},
                price_usd=Decimal("19.995"),
                quantity=12,
                image_url="https://cdn.example.com/red.jpg",
            ),
            MarketplaceVariantPayload(
                local_sku="LAMP-BLUE",
                external_sku="AE-LAMP-BLUE",
                option_values={"Color": "Blue", "Plug": "US"},
                price_usd=Decimal("21.5"),
                quantity=-3,
            ),
        ],
        moq_tiers=[
            SourcingMoqTier(min_qty=100, unit_price_usd=Decimal("15.5")),
            SourcingMoqTier(min_qty=10, unit_price_usd=Decimal("17.005")),
        ],
    )

    receipt = await _adapter(fake).publish_listing(payload)

    params = _assert_signed(fake.requests[0], ALIEXPRESS_PRODUCT_CREATE_METHOD)
    assert json.loads(params["product_body"]) == created
    assert created["product_title"] == "LED Panel Light 24W"
    assert created["category_id"] == "1503"
    assert created["hs_code"] == "94054090"
    assert created["image_urls"] == ["https://cdn.example.com/lamp.jpg"]
    assert created["options"] == [
        {"name": "Color", "values": ["Red", "Blue"]},
        {"name": "Plug", "values": ["US"]},
    ]
    assert created["skus"] == [
        {
            "sku_code": "AE-LAMP-RED",
            "price": "20.00",
            "currency_code": "USD",
            "stock": 12,
            "option_values": {"Color": "Red", "Plug": "US"},
            "image_url": "https://cdn.example.com/red.jpg",
        },
        {
            "sku_code": "AE-LAMP-BLUE",
            "price": "21.50",
            "currency_code": "USD",
            "stock": 0,
            "option_values": {"Color": "Blue", "Plug": "US"},
        },
    ]
    assert created["wholesale_ladder"] == [
        {"min_qty": 100, "unit_price_usd": "15.50"},
        {"min_qty": 10, "unit_price_usd": "17.01"},
    ]
    assert "24W dimmable" in created["description"]
    assert "IP44 rated" in created["description"]

    assert receipt.external_product_id == "1005009999"
    assert receipt.external_sku == "AE-LAMP-RED"
    assert receipt.status == "PUBLISHED"
    assert [row.external_sku for row in receipt.variant_receipts] == [
        "AE-LAMP-RED",
        "AE-LAMP-BLUE",
    ]
    assert [row.external_variant_id for row in receipt.variant_receipts] == [
        "sku-red",
        "sku-blue",
    ]


async def test_publish_listing_without_variants_publishes_the_base_sku() -> None:
    created: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        created.update(json.loads(_wire_params(request)["product_body"]))
        return httpx.Response(
            200, json=_envelope(ALIEXPRESS_PRODUCT_CREATE_METHOD, {"product_id": 777000111})
        )

    fake = FakeAliExpress().on(ALIEXPRESS_PRODUCT_CREATE_METHOD, handler)
    receipt = await _adapter(fake).publish_listing(
        _listing_payload(price_usd=Decimal("19.994"), quantity=25)
    )

    assert created["skus"] == [
        {
            "sku_code": "AE-LAMP-RED",
            "price": "19.99",
            "currency_code": "USD",
            "stock": 25,
        }
    ]
    assert created["options"] == []
    assert receipt.external_product_id == "777000111"
    # AliExpress returned no SKU rows, so the acknowledgement is bound to the base SKU.
    assert [row.external_sku for row in receipt.variant_receipts] == ["AE-LAMP-RED"]
    assert receipt.variant_receipts[0].external_variant_id is None


async def test_publish_listing_uploads_image_blobs_before_creating_the_product() -> None:
    blob_bytes = b"\x89PNG\r\n\x1a\n-fake-image"
    created: dict[str, Any] = {}

    def upload_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_envelope(
                ALIEXPRESS_IMAGE_UPLOAD_METHOD,
                {"image_url": "https://ae01.alicdn.com/kf/uploaded.png"},
            ),
        )

    def create_handler(request: httpx.Request) -> httpx.Response:
        created.update(json.loads(_wire_params(request)["product_body"]))
        return httpx.Response(
            200, json=_envelope(ALIEXPRESS_PRODUCT_CREATE_METHOD, {"product_id": "1"})
        )

    fake = (
        FakeAliExpress()
        .on(ALIEXPRESS_IMAGE_UPLOAD_METHOD, upload_handler)
        .on(ALIEXPRESS_PRODUCT_CREATE_METHOD, create_handler)
    )
    payload = _listing_payload(
        images=[],
        image_blobs=[
            MarketplaceImageBlob(filename="lamp.png", mime_type="image/png", data=blob_bytes)
        ],
    )

    receipt = await _adapter(fake).publish_listing(payload)

    upload_params = _assert_signed(fake.requests[0], ALIEXPRESS_IMAGE_UPLOAD_METHOD)
    assert upload_params["image_name"] == "lamp.png"
    assert upload_params["image_mime_type"] == "image/png"
    assert upload_params["image_base64"] == base64.b64encode(blob_bytes).decode("ascii")
    assert created["image_urls"] == ["https://ae01.alicdn.com/kf/uploaded.png"]
    assert receipt.external_product_id == "1"


async def test_publish_listing_fails_when_an_image_upload_returns_no_url() -> None:
    fake = (
        FakeAliExpress()
        .on(
            ALIEXPRESS_IMAGE_UPLOAD_METHOD,
            lambda request: httpx.Response(
                200, json=_envelope(ALIEXPRESS_IMAGE_UPLOAD_METHOD, {})
            ),
        )
        .on(
            ALIEXPRESS_PRODUCT_CREATE_METHOD,
            lambda request: httpx.Response(
                200, json=_envelope(ALIEXPRESS_PRODUCT_CREATE_METHOD, {"product_id": "1"})
            ),
        )
    )
    payload = _listing_payload(
        image_blobs=[
            MarketplaceImageBlob(filename="x.png", mime_type="image/png", data=b"\x00\x01")
        ]
    )

    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).publish_listing(payload)

    assert excinfo.value.code == "ERR_ALIEXPRESS_MEDIA_UPLOAD_FAILED"
    assert excinfo.value.status_code == 502
    assert len(fake.requests) == 1


async def test_publish_listing_without_a_product_id_is_rejected() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_PRODUCT_CREATE_METHOD,
        lambda request: httpx.Response(
            200, json=_envelope(ALIEXPRESS_PRODUCT_CREATE_METHOD, {"success": True})
        ),
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).publish_listing(_listing_payload())

    assert excinfo.value.code == "ERR_ALIEXPRESS_LISTING_REJECTED"
    assert excinfo.value.status_code == 502
    assert "AE-LAMP-RED" in excinfo.value.detail


@pytest.mark.parametrize(
    ("sku", "title"),
    [("", "LED Panel Light"), ("AE-LAMP-RED", "   ")],
)
async def test_publish_listing_rejects_blank_sku_or_title_without_network(
    sku: str, title: str
) -> None:
    fake = FakeAliExpress()
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).publish_listing(_listing_payload(external_sku=sku, title=title))

    assert excinfo.value.code == "ERR_ALIEXPRESS_LISTING_REJECTED"
    assert excinfo.value.status_code == 422
    assert fake.requests == []


async def test_publish_listing_rejects_an_undeclared_variant_option() -> None:
    fake = FakeAliExpress()
    payload = _listing_payload(
        options=[SourcingOptionSpec(name="Color", values=["Red"])],
        variants=[
            MarketplaceVariantPayload(
                local_sku="LAMP-RED",
                external_sku="AE-LAMP-RED",
                option_values={"Size": "XL"},
                price_usd=Decimal("19.99"),
                quantity=1,
            )
        ],
    )

    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).publish_listing(payload)

    assert excinfo.value.code == "ERR_ALIEXPRESS_LISTING_INVALID_OPTION"
    assert excinfo.value.status_code == 422
    assert "Size" in excinfo.value.detail
    assert fake.requests == []


async def test_publish_listing_maps_a_remote_error_envelope_to_a_typed_error() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_PRODUCT_CREATE_METHOD,
        lambda request: httpx.Response(
            200,
            json={
                "error_response": {
                    "code": "InvalidParameter",
                    "msg": "product_title must be unique",
                    "sub_code": "isv.invalid-title",
                }
            },
        ),
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).publish_listing(_listing_payload())

    assert excinfo.value.code == "ERR_ALIEXPRESS_API_ERROR"
    assert excinfo.value.status_code == 502
    assert "InvalidParameter" in excinfo.value.detail
    assert "product_title must be unique" in excinfo.value.detail


# ------------------------------------------------------------------ inventory push


async def test_push_inventory_level_sends_absolute_stock_and_clamps_negatives() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_STOCK_UPDATE_METHOD,
        lambda request: httpx.Response(
            200, json=_envelope(ALIEXPRESS_STOCK_UPDATE_METHOD, {"modified": "true"})
        ),
    )
    result = await _adapter(fake).push_inventory_level(
        external_sku=" AE-LAMP-RED ",
        quantity=-7,
        external_product_id="1005009999",
        external_variant_id="sku-red",
        external_inventory_item_id="INV-1",
        location_id="LOC-1",
        warehouse_id="WH-1",
        outbound_event_id="some-extra-ignored",
    )

    params = _assert_signed(fake.requests[0], ALIEXPRESS_STOCK_UPDATE_METHOD)
    assert params["sku_code"] == "AE-LAMP-RED"
    assert params["stock"] == "0"
    assert params["product_id"] == "1005009999"
    assert params["sku_id"] == "sku-red"
    assert params["inventory_item_id"] == "INV-1"
    assert params["location_id"] == "LOC-1"
    assert params["warehouse_id"] == "WH-1"
    assert "some-extra-ignored" not in params
    assert result == {
        "channel": "aliexpress",
        "external_sku": "AE-LAMP-RED",
        "quantity": 0,
        "external_product_id": "1005009999",
        "external_variant_id": "sku-red",
        "external_inventory_item_id": "INV-1",
        "location_id": "LOC-1",
        "warehouse_id": "WH-1",
        "raw": {"modified": "true"},
    }


async def test_push_inventory_level_sends_only_the_sku_when_no_ids_are_known() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_STOCK_UPDATE_METHOD,
        lambda request: httpx.Response(200, json=_envelope(ALIEXPRESS_STOCK_UPDATE_METHOD, {})),
    )
    result = await _adapter(fake).push_inventory_level(
        external_sku="AE-LAMP-RED", quantity=12
    )

    params = fake.params_of(ALIEXPRESS_STOCK_UPDATE_METHOD)
    assert params["sku_code"] == "AE-LAMP-RED"
    assert params["stock"] == "12"
    assert "sku_id" not in params
    assert result["quantity"] == 12
    assert result["warehouse_id"] is None


async def test_push_inventory_level_rejects_an_empty_sku() -> None:
    fake = FakeAliExpress()
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).push_inventory_level(external_sku="   ", quantity=3)

    assert excinfo.value.code == "ERR_ALIEXPRESS_INVENTORY_SYNC"
    assert excinfo.value.status_code == 422
    assert fake.requests == []


# ------------------------------------------------------------------------ failure map


async def test_authentication_failure_maps_to_a_typed_error_without_leaking_secrets() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_ORDER_LIST_METHOD,
        lambda request: httpx.Response(
            401,
            json={
                "error_response": {
                    "code": "InvalidSignature",
                    "msg": f"bad signature for token {ACCESS_TOKEN} using {APP_SECRET}",
                }
            },
        ),
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_ALIEXPRESS_AUTH_FAILED"
    assert excinfo.value.status_code == 502
    assert APP_SECRET not in excinfo.value.detail
    assert ACCESS_TOKEN not in excinfo.value.detail
    assert "[REDACTED]" in excinfo.value.detail


async def test_rate_limit_maps_to_a_429() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_ORDER_LIST_METHOD,
        lambda request: httpx.Response(
            429, json={"error_response": {"code": "429", "msg": "API flow control"}}
        ),
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_ALIEXPRESS_RATE_LIMITED"
    assert excinfo.value.status_code == 429


async def test_transport_failure_maps_to_a_retryable_error_without_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"dial failed while using {ACCESS_TOKEN} / {APP_SECRET}")

    with pytest.raises(AppError) as excinfo:
        await _adapter(handler).fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_ALIEXPRESS_API_ERROR"
    assert excinfo.value.status_code == 502
    assert "retried" in excinfo.value.detail
    assert APP_SECRET not in excinfo.value.detail
    assert ACCESS_TOKEN not in excinfo.value.detail


async def test_malformed_json_body_is_not_reported_as_success() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_ORDER_LIST_METHOD,
        lambda request: httpx.Response(200, text="<html>gateway error</html>"),
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_ALIEXPRESS_API_ERROR"
    assert "gateway error" in excinfo.value.detail


async def test_error_envelope_with_a_zero_code_is_fail_closed() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_PRODUCT_LIST_METHOD,
        lambda request: httpx.Response(
            200,
            json={
                "error_response": {"code": "0", "msg": "unexpected failure"},
            },
        ),
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).fetch_products_updated_since(SINCE)

    assert excinfo.value.code == "ERR_ALIEXPRESS_API_ERROR"
    assert "unexpected failure" in excinfo.value.detail


async def test_unparseable_money_is_reported_as_an_api_error() -> None:
    fake = FakeAliExpress().on(
        ALIEXPRESS_ORDER_LIST_METHOD,
        lambda request: httpx.Response(
            200,
            json=_envelope(
                ALIEXPRESS_ORDER_LIST_METHOD,
                {"orders": [_order(order_amount={"currency_code": "USD", "amount": "12,3x"})]},
            ),
        ),
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(fake).fetch_orders_updated_since(SINCE)

    assert excinfo.value.code == "ERR_ALIEXPRESS_API_ERROR"
    assert "order_amount" in excinfo.value.detail


# --------------------------------------------------------------------------- webhooks


def _signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_verify_webhook_uses_constant_time_hmac_over_raw_body() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    raw_body = b'{"order_id":"8192345678901234","order_status":"WAIT_SELLER_SEND_GOODS"}'
    secret = "aliexpress-webhook-secret"
    signature = _signature(raw_body, secret)

    assert adapter.verify_webhook(raw_body, {WEBHOOK_HEADER: signature}, secret)
    assert adapter.verify_webhook(raw_body, {WEBHOOK_HEADER.upper(): signature.upper()}, secret)
    assert adapter.verify_webhook(raw_body, {WEBHOOK_HEADER: f"  {signature}  "}, secret)
    assert verify_aliexpress_webhook(raw_body, {WEBHOOK_HEADER: signature}, secret)


def test_verify_webhook_fails_closed_on_missing_or_tampered_input() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    raw_body = b'{"order_id":"8192345678901234"}'
    secret = "aliexpress-webhook-secret"
    signature = _signature(raw_body, secret)

    assert not adapter.verify_webhook(raw_body, {}, secret)
    assert not adapter.verify_webhook(raw_body, {WEBHOOK_HEADER: ""}, secret)
    assert not adapter.verify_webhook(raw_body, {WEBHOOK_HEADER: signature}, "")
    assert not adapter.verify_webhook(raw_body + b" ", {WEBHOOK_HEADER: signature}, secret)
    assert not adapter.verify_webhook(
        raw_body, {WEBHOOK_HEADER: signature}, "a-different-secret"
    )
    assert not adapter.verify_webhook(raw_body, {WEBHOOK_HEADER: "0" * 64}, secret)
    assert not adapter.verify_webhook(raw_body, {WEBHOOK_HEADER: "签名"}, secret)


def test_verify_webhook_falls_back_to_the_configured_app_secret() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    raw_body = b'{"order_id":"8192345678901234"}'

    assert adapter.verify_webhook(
        raw_body, {WEBHOOK_HEADER: _signature(raw_body, APP_SECRET)}, ""
    )
    assert not adapter.verify_webhook(
        raw_body, {WEBHOOK_HEADER: _signature(raw_body, "not-the-secret")}, ""
    )
