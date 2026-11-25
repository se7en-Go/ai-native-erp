"""Zero-network unit tests for the Shopee Open Platform v2 connector.

Every HTTP exchange runs through ``httpx.MockTransport``: no test touches the real Shopee
gateway, and every request is signature-checked by recomputing the partner HMAC from the
wire query parameters.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl
from uuid import uuid4

import httpx
import pytest

from erp_core.catalog.sourcing_service import (
    MarketplaceImageBlob,
    MarketplaceListingPayload,
    MarketplaceVariantPayload,
    SourcingOptionSpec,
)
from erp_core.channels.ecommerce.shopee import (
    ADD_ITEM_PATH,
    ITEM_BASE_INFO_PATH,
    ITEM_LIST_PATH,
    LOGISTICS_CHANNEL_LIST_PATH,
    MODEL_LIST_PATH,
    ORDER_DETAIL_PATH,
    ORDER_LIST_PATH,
    SHIP_ORDER_PATH,
    SHOPEE_API_BASE,
    SHOPEE_API_PREFIX,
    UPDATE_STOCK_PATH,
    UPLOAD_IMAGE_PATH,
    ShopeeAdapter,
    sign_shopee_request,
    verify_shopee_webhook,
)
from erp_core.shared.enums import FulfillmentStatus, PaymentStatus
from erp_core.shared.errors import AppError

SHOP_ID = "778899"
PARTNER_ID = "2010123"
PARTNER_KEY = "partner-key-super-secret-value"
ACCESS_TOKEN = "shop-access-token-super-secret"
CALLBACK_URL = "https://erp.example.com/webhooks/channels/shopee"

Handler = Callable[[httpx.Request], httpx.Response]


def _adapter(handler: Handler, **overrides: Any) -> ShopeeAdapter:
    kwargs: dict[str, Any] = {
        "shop_id": SHOP_ID,
        "partner_id": PARTNER_ID,
        "partner_key": PARTNER_KEY,
        "access_token": ACCESS_TOKEN,
        "endpoint_url": SHOPEE_API_BASE,
        "transport": httpx.MockTransport(handler),
        "timeout_seconds": 5.0,
    }
    kwargs.update(overrides)
    return ShopeeAdapter(**kwargs)


def _router(
    routes: Mapping[str, Handler],
    log: list[httpx.Request] | None = None,
) -> Handler:
    """Dispatch by path, recording every request for assertions."""

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(request)
        route = routes.get(request.url.path)
        if route is None:
            return _envelope({"error": "error_path_not_found", "message": request.url.path}, 404)
        return route(request)

    return handler


def _envelope(response: Mapping[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": "",
            "message": "",
            "warning": "",
            "request_id": "req-1",
            **response,
        },
    )


def _query(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.url.query.decode("utf-8"), keep_blank_values=True))


def _assert_signed(request: httpx.Request, api_path: str) -> dict[str, str]:
    """Recompute the Shopee signature from the wire and pin the transport hygiene."""
    params = _query(request)
    assert request.url.path == api_path
    assert params["partner_id"] == PARTNER_ID
    assert params["shop_id"] == SHOP_ID
    assert params["access_token"] == ACCESS_TOKEN
    timestamp = int(params["timestamp"])
    assert timestamp > 0
    assert params["sign"] == sign_shopee_request(
        partner_id=PARTNER_ID,
        partner_key=PARTNER_KEY,
        api_path=api_path,
        timestamp=timestamp,
        access_token=ACCESS_TOKEN,
        shop_id=SHOP_ID,
    )
    return params


def _push_signature(callback_url: str, body: bytes, key: str = PARTNER_KEY) -> str:
    """Independent implementation of Shopee's push signature: HMAC(key, url|body)."""
    signed = callback_url.encode("utf-8") + b"|" + body
    return hmac.new(key.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def _order_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_sn": "240101ABCDEFGH",
        "order_status": "READY_TO_SHIP",
        "currency": "SGD",
        "total_amount": 41.7,
        "estimated_shipping_fee": 3.2,
        "seller_discount": 1.05,
        "commission_fee": 2.345,
        "update_time": 1735689600,
        "buyer_username": "buyer01",
        "buyer_email": "buyer01@example.com",
        "recipient_address": {"name": "Ada Lovelace", "phone": "+6591234567", "region": "SG"},
        "item_list": [
            {
                "item_id": 1001,
                "item_name": "LED Slim Panel",
                "model_sku": "LED-PANEL-RED",
                "model_quantity_purchased": 3,
                "model_discounted_price": 12.345,
                "model_original_price": 15.0,
            }
        ],
    }
    row.update(overrides)
    return row


def _listing_payload(**overrides: Any) -> MarketplaceListingPayload:
    values: dict[str, Any] = {
        "channel_account_id": uuid4(),
        "channel_type": "shopee",
        "external_sku": "LED-PANEL-SET",
        "title": "  LED Slim Panel Set  ",
        "description": "Ultra slim LED panel with dimmable driver.",
        "bullet_points": [" 5-year lifespan ", ""],
        "price_usd": Decimal("19.999"),
        "quantity": 12,
        "images": ["https://cdn.example.com/panel.jpg"],
        "category": "100012",
        "hs_code": "9405.40",
    }
    values.update(overrides)
    return MarketplaceListingPayload(**values)


# ------------------------------------------------------------------------------ security


def test_repr_and_errors_never_expose_credentials() -> None:
    adapter = _adapter(_router({}))
    text = repr(adapter)
    assert PARTNER_KEY not in text
    assert ACCESS_TOKEN not in text
    assert SHOP_ID in text and PARTNER_ID in text
    assert adapter.region == "GLOBAL"
    assert adapter.fulfillment_mode == "standard"


def test_missing_credentials_fail_closed() -> None:
    with pytest.raises(AppError) as excinfo:
        _adapter(_router({}), access_token="   ")
    assert excinfo.value.code == "ERR_SHOPEE_CREDENTIALS_MISSING"
    assert excinfo.value.status_code == 422
    assert ACCESS_TOKEN not in excinfo.value.detail


def test_unknown_fulfillment_mode_fails_closed() -> None:
    with pytest.raises(AppError) as excinfo:
        _adapter(_router({}), fulfillment_mode="teleport")
    assert excinfo.value.code == "ERR_SHOPEE_FULFILLMENT_MODE_INVALID"


@pytest.mark.parametrize("mode", ["full_managed", "light_export"])
def test_connect_fulfillment_modes_normalize_safely(mode: str) -> None:
    adapter = _adapter(_router({}), fulfillment_mode=mode)
    assert adapter.fulfillment_mode == "standard"


async def test_429_maps_to_rate_limited_error() -> None:
    handler = _router({ORDER_LIST_PATH: lambda _r: httpx.Response(429, json={})})
    with pytest.raises(AppError) as excinfo:
        await _adapter(handler).fetch_orders_updated_since(datetime(2025, 1, 1, tzinfo=UTC))
    assert excinfo.value.code == "ERR_SHOPEE_RATE_LIMITED"
    assert excinfo.value.status_code == 429


async def test_auth_failure_maps_to_typed_error_without_leaking_token() -> None:
    handler = _router(
        {
            ORDER_LIST_PATH: lambda _r: httpx.Response(
                401, json={"error": "error_auth", "message": f"bad token {ACCESS_TOKEN}"}
            )
        }
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(handler).fetch_orders_updated_since(datetime(2025, 1, 1, tzinfo=UTC))
    assert excinfo.value.code == "ERR_SHOPEE_AUTH_FAILED"
    assert ACCESS_TOKEN not in excinfo.value.detail


# ---------------------------------------------------------------------------- order sync


async def test_fetch_orders_signs_the_call_and_lists_updated_orders() -> None:
    log: list[httpx.Request] = []
    seen: dict[str, dict[str, str]] = {}

    def order_list(request: httpx.Request) -> httpx.Response:
        seen["list"] = _assert_signed(request, ORDER_LIST_PATH)
        return _envelope(
            {"response": {"order_list": [{"order_sn": "240101ABCDEFGH"}], "more": False}}
        )

    def order_detail(request: httpx.Request) -> httpx.Response:
        seen["detail"] = _assert_signed(request, ORDER_DETAIL_PATH)
        return _envelope({"response": {"order_list": [_order_row()]}})

    handler = _router({ORDER_LIST_PATH: order_list, ORDER_DETAIL_PATH: order_detail}, log)
    since = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    orders, cursor = await _adapter(handler).fetch_orders_updated_since(since)

    assert [request.url.path for request in log] == [ORDER_LIST_PATH, ORDER_DETAIL_PATH]
    assert seen["list"]["time_range_field"] == "update_time"
    assert seen["list"]["time_from"] == str(int(since.timestamp()))
    assert seen["detail"]["order_sn_list"] == "240101ABCDEFGH"
    assert cursor is None
    assert len(orders) == 1


async def test_fetch_orders_normalizes_decimal_money_and_financial_metadata() -> None:
    row = _order_row(refunded_amount=5.5, commission_fee=2.345)
    handler = _router(
        {
            ORDER_LIST_PATH: lambda _r: _envelope(
                {"response": {"order_list": [{"order_sn": "240101ABCDEFGH"}]}}
            ),
            ORDER_DETAIL_PATH: lambda _r: _envelope({"response": {"order_list": [row]}}),
        }
    )
    orders, _ = await _adapter(handler).fetch_orders_updated_since(
        datetime(2025, 1, 1, tzinfo=UTC)
    )

    order = orders[0]
    assert order.external_order_id == "240101ABCDEFGH"
    assert order.currency == "SGD"
    # 12.345 unit price * 3 units = 37.035 -> half-up 37.04 (never a float round trip).
    assert order.line_items[0].line_total == Decimal("37.04")
    assert order.subtotal_amount == Decimal("37.04")
    assert order.total_amount == Decimal("41.70")
    assert order.shipping_charged == Decimal("3.20")
    assert order.discount_amount == Decimal("1.05")
    assert order.platform_commission_base == Decimal("2.35")
    assert order.fulfillment_status is FulfillmentStatus.CONFIRMED_RESERVED
    assert order.payment_status is PaymentStatus.PAID
    assert order.channel_metadata["refunded_amount"] == "5.50"
    assert order.channel_metadata["platform_commission_base"] == "2.35"
    assert order.channel_metadata["shopee_order_status"] == "READY_TO_SHIP"
    assert order.channel_metadata["shopee_region"] == "GLOBAL"
    assert order.customer.email == "buyer01@example.com"
    assert order.customer.phone_e164 == "+6591234567"
    assert order.customer.country_code == "SG"
    assert order.updated_at == datetime(2025, 1, 1, 0, 0, tzinfo=UTC)


async def test_fetch_orders_keeps_zero_commission_out_of_metadata() -> None:
    row = _order_row(commission_fee=0, refunded_amount=0)
    handler = _router(
        {
            ORDER_LIST_PATH: lambda _r: _envelope(
                {"response": {"order_list": [{"order_sn": "240101ABCDEFGH"}]}}
            ),
            ORDER_DETAIL_PATH: lambda _r: _envelope({"response": {"order_list": [row]}}),
        }
    )
    orders, _ = await _adapter(handler).fetch_orders_updated_since(
        datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert orders[0].platform_commission_base == Decimal("0.00")
    assert "refunded_amount" not in orders[0].channel_metadata
    assert "platform_commission_base" not in orders[0].channel_metadata


async def test_fetch_orders_maps_shopee_statuses() -> None:
    cases = {
        "UNPAID": (FulfillmentStatus.PENDING_PAYMENT, PaymentStatus.UNPAID),
        "SHIPPED": (FulfillmentStatus.SHIPPED, PaymentStatus.PAID),
        "TO_CONFIRM_RECEIVE": (FulfillmentStatus.SHIPPED, PaymentStatus.PAID),
        "COMPLETED": (FulfillmentStatus.DELIVERED, PaymentStatus.PAID),
        "CANCELLED": (FulfillmentStatus.CANCELLED, PaymentStatus.REFUNDED),
        "CANCELED": (FulfillmentStatus.CANCELLED, PaymentStatus.REFUNDED),
        "REFUNDED": (FulfillmentStatus.REFUNDED, PaymentStatus.REFUNDED),
        "RETURNED": (FulfillmentStatus.REFUNDED, PaymentStatus.REFUNDED),
        "TO_RETURN": (FulfillmentStatus.EXCEPTION, PaymentStatus.PAID),
    }
    for status, (fulfillment, payment) in cases.items():
        row = _order_row(order_status=status)
        handler = _router(
            {
                ORDER_LIST_PATH: lambda _r: _envelope(
                    {"response": {"order_list": [{"order_sn": "240101ABCDEFGH"}]}}
                ),
                ORDER_DETAIL_PATH: lambda _r, row=row: _envelope(
                    {"response": {"order_list": [row]}}
                ),
            }
        )
        orders, _ = await _adapter(handler).fetch_orders_updated_since(
            datetime(2025, 1, 1, tzinfo=UTC)
        )
        assert orders[0].fulfillment_status is fulfillment
        assert orders[0].payment_status is payment


async def test_fetch_orders_marks_a_full_refund_as_refunded() -> None:
    row = _order_row(refunded_amount=41.7, total_amount=41.7)
    handler = _router(
        {
            ORDER_LIST_PATH: lambda _r: _envelope(
                {"response": {"order_list": [{"order_sn": "240101ABCDEFGH"}]}}
            ),
            ORDER_DETAIL_PATH: lambda _r: _envelope({"response": {"order_list": [row]}}),
        }
    )
    orders, _ = await _adapter(handler).fetch_orders_updated_since(
        datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert orders[0].fulfillment_status is FulfillmentStatus.REFUNDED
    assert orders[0].payment_status is PaymentStatus.REFUNDED
    assert orders[0].channel_metadata["refunded_amount"] == "41.70"


async def test_fetch_orders_threads_cursor_and_returns_the_next_page() -> None:
    seen: dict[str, str] = {}

    def order_list(request: httpx.Request) -> httpx.Response:
        seen.update(_query(request))
        return _envelope(
            {
                "response": {
                    "order_list": [{"order_sn": "A1"}],
                    "more": True,
                    "next_cursor": "cursor-page-2",
                }
            }
        )

    handler = _router(
        {
            ORDER_LIST_PATH: order_list,
            ORDER_DETAIL_PATH: lambda _r: _envelope({"response": {"order_list": [_order_row()]}}),
        }
    )
    orders, cursor = await _adapter(handler).fetch_orders_updated_since(
        datetime(2025, 1, 1, tzinfo=UTC), cursor="cursor-page-1"
    )
    assert seen["cursor"] == "cursor-page-1"
    assert cursor == "cursor-page-2"
    assert len(orders) == 1


async def test_fetch_orders_skips_detail_call_for_an_empty_page() -> None:
    log: list[httpx.Request] = []
    handler = _router(
        {ORDER_LIST_PATH: lambda _r: _envelope({"response": {"order_list": []}})}, log
    )
    orders, cursor = await _adapter(handler).fetch_orders_updated_since(
        datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert orders == []
    assert cursor is None
    assert [request.url.path for request in log] == [ORDER_LIST_PATH]


async def test_fetch_orders_drops_orders_without_line_items() -> None:
    handler = _router(
        {
            ORDER_LIST_PATH: lambda _r: _envelope(
                {"response": {"order_list": [{"order_sn": "240101ABCDEFGH"}]}}
            ),
            ORDER_DETAIL_PATH: lambda _r: _envelope(
                {"response": {"order_list": [_order_row(item_list=[])]}}
            ),
        }
    )
    orders, _ = await _adapter(handler).fetch_orders_updated_since(
        datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert orders == []


# -------------------------------------------------------------------------- product sync


async def test_fetch_products_hydrates_variants_and_option_values() -> None:
    seen: dict[str, str] = {}

    def base_info(request: httpx.Request) -> httpx.Response:
        seen.update(_assert_signed(request, ITEM_BASE_INFO_PATH))
        return _envelope(
            {
                "response": {
                    "item_list": [{"item_id": 1001, "item_name": "LED Slim Panel"}]
                }
            }
        )

    def model_list(request: httpx.Request) -> httpx.Response:
        seen.update(_assert_signed(request, MODEL_LIST_PATH))
        return _envelope(
            {
                "response": {
                    "tier_variation": [
                        {
                            "name": "Color",
                            "option_list": [{"option": "Red"}, {"option": "Blue"}],
                        }
                    ],
                    "model": [
                        {
                            "model_id": 501,
                            "model_sku": "LED-PANEL-RED",
                            "tier_index": [0],
                            "price_info": [
                                {"currency": "SGD", "original_price": 12.345, "current_price": 11.1}
                            ],
                            "stock_info_v2": {"seller_stock": [{"stock": 7}]},
                        },
                        {
                            "model_id": 502,
                            "model_sku": "LED-PANEL-BLUE",
                            "tier_index": [1],
                            "price_info": [{"currency": "SGD", "original_price": 9.0}],
                            "stock_info_v2": {"seller_stock": [{"stock": -4}]},
                        },
                    ],
                }
            }
        )

    handler = _router(
        {
            ITEM_LIST_PATH: lambda r: (
                _assert_signed(r, ITEM_LIST_PATH)
                and _envelope(
                    {
                        "response": {
                            "item": [{"item_id": 1001}],
                            "has_next_page": True,
                            "next_offset": 50,
                        }
                    }
                )
            ),
            ITEM_BASE_INFO_PATH: base_info,
            MODEL_LIST_PATH: model_list,
        }
    )
    products, cursor = await _adapter(handler).fetch_products_updated_since(
        datetime(2025, 1, 1, tzinfo=UTC)
    )

    assert cursor == "50"
    assert seen["item_id_list"] == "1001"
    assert seen["item_id"] == "1001"
    assert len(products) == 1
    product = products[0]
    assert product.external_product_id == "1001"
    assert product.title == "LED Slim Panel"
    assert [variant["external_variant_id"] for variant in product.variants] == ["501", "502"]
    assert product.variants[0]["external_sku"] == "LED-PANEL-RED"
    assert product.variants[0]["price"] == "11.10"
    assert product.variants[0]["quantity"] == 7
    assert product.variants[0]["option_values"] == {"Color": "Red"}
    assert product.variants[1]["option_values"] == {"Color": "Blue"}
    assert product.variants[1]["quantity"] == 0


async def test_fetch_products_returns_empty_page_without_extra_calls() -> None:
    log: list[httpx.Request] = []
    handler = _router(
        {
            ITEM_LIST_PATH: lambda _r: _envelope(
                {"response": {"item": [], "has_next_page": False}}
            )
        },
        log,
    )
    products, cursor = await _adapter(handler).fetch_products_updated_since(
        datetime(2025, 1, 1, tzinfo=UTC), cursor="50"
    )
    assert products == []
    assert cursor is None
    assert [request.url.path for request in log] == [ITEM_LIST_PATH]
    assert _query(log[0])["offset"] == "50"


# --------------------------------------------------------------------------- fulfillment


async def test_create_fulfillment_ships_non_integrated_tracking() -> None:
    captured: dict[str, Any] = {}

    def ship_order(request: httpx.Request) -> httpx.Response:
        _assert_signed(request, SHIP_ORDER_PATH)
        captured["body"] = json.loads(request.content)
        return _envelope({"response": {}})

    result = await _adapter(_router({SHIP_ORDER_PATH: ship_order})).create_fulfillment(
        "240101ABCDEFGH",
        "TRK-889900",
        "Ninja Van",
        tracking_url="https://track.example.com/TRK-889900",
        fulfillment_order_id="PKG-1",
    )
    assert captured["body"] == {
        "order_sn": "240101ABCDEFGH",
        "tracking_number": "TRK-889900",
        "shipping_carrier": "Ninja Van",
        "package_number": "PKG-1",
    }
    assert result["channel"] == "shopee"
    assert result["external_order_id"] == "240101ABCDEFGH"
    assert result["fulfillment_id"] == "PKG-1"
    assert result["tracking_number"] == "TRK-889900"
    assert result["package_number"] == "PKG-1"
    assert result["fulfillment_mode"] == "standard"


async def test_create_fulfillment_requires_tracking_and_carrier() -> None:
    adapter = _adapter(_router({}))
    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment("240101ABCDEFGH", "  ", "Ninja Van")
    assert excinfo.value.code == "ERR_SHOPEE_FULFILLMENT_REJECTED"
    assert excinfo.value.status_code == 422


async def test_create_fulfillment_rejects_integrated_modes_it_cannot_book() -> None:
    adapter = _adapter(_router({}), fulfillment_mode="pickup")
    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment("240101ABCDEFGH", "TRK-1", "Ninja Van")
    assert excinfo.value.code == "ERR_SHOPEE_FULFILLMENT_MODE_UNSUPPORTED"


async def test_create_fulfillment_remote_rejection_is_typed() -> None:
    handler = _router(
        {
            SHIP_ORDER_PATH: lambda _r: _envelope(
                {"error": "error_order_status", "message": "order is not shippable"}, 400
            )
        }
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(handler).create_fulfillment("240101ABCDEFGH", "TRK-1", "Ninja Van")
    assert excinfo.value.code == "ERR_SHOPEE_FULFILLMENT_REJECTED"
    assert "order is not shippable" in excinfo.value.detail


# ------------------------------------------------------------------------------ listings


def _publish_routes(
    add_item: Handler,
    *,
    log: list[httpx.Request] | None = None,
) -> Handler:
    return _router(
        {
            LOGISTICS_CHANNEL_LIST_PATH: lambda _r: _envelope(
                {
                    "response": {
                        "logistic_channel_list": [
                            {"logistics_channel_id": 80001, "enabled": True},
                            {"logistics_channel_id": 80002, "enabled": False},
                        ]
                    }
                }
            ),
            ADD_ITEM_PATH: add_item,
        },
        log,
    )


async def test_publish_listing_pushes_multi_variant_payload() -> None:
    captured: dict[str, Any] = {}

    def add_item(request: httpx.Request) -> httpx.Response:
        _assert_signed(request, ADD_ITEM_PATH)
        captured["body"] = json.loads(request.content)
        return _envelope(
            {
                "response": {
                    "item_id": 987654321,
                    "model": [
                        {"model_id": 111, "model_sku": "LED-RED"},
                        {"model_id": 222, "model_sku": "LED-BLUE"},
                    ],
                }
            }
        )

    payload = _listing_payload(
        options=[SourcingOptionSpec(name="Color", values=["Red", "Blue"])],
        variants=[
            MarketplaceVariantPayload(
                local_sku="L-RED",
                external_sku="LED-RED",
                option_values={"Color": "Red"},
                price_usd=Decimal("19.999"),
                quantity=10,
            ),
            MarketplaceVariantPayload(
                local_sku="L-BLUE",
                external_sku="LED-BLUE",
                option_values={"Color": "Blue"},
                price_usd=Decimal("12.5"),
                quantity=-3,
            ),
        ],
    )
    receipt = await _adapter(_publish_routes(add_item)).publish_listing(payload)

    body = captured["body"]
    assert body["item_name"] == "LED Slim Panel Set"
    assert body["category_id"] == 100012
    assert body["logistic_info"] == [{"logistic_id": 80001, "enabled": True}]
    assert body["image"] == {"image_url_list": ["https://cdn.example.com/panel.jpg"]}
    assert body["tier_variation"] == [
        {"name": "Color", "option_list": [{"option": "Red"}, {"option": "Blue"}]}
    ]
    assert body["model"][0] == {
        "tier_index": [0],
        "model_sku": "LED-RED",
        "price_info": [{"currency": "USD", "original_price": "20.00"}],
        "stock_info_v2": {"seller_stock": [{"stock": 10}]},
    }
    assert body["model"][1]["tier_index"] == [1]
    assert body["model"][1]["price_info"][0]["original_price"] == "12.50"
    assert body["model"][1]["stock_info_v2"]["seller_stock"] == [{"stock": 0}]
    # Bulk description carries the bullets so Shopee shows a full PDP.
    assert "5-year lifespan" in body["description"]

    assert receipt.external_product_id == "987654321"
    assert receipt.external_sku == "LED-PANEL-SET"
    assert receipt.status == "NORMAL"
    assert [variant.external_variant_id for variant in receipt.variant_receipts] == ["111", "222"]


async def test_publish_listing_single_sku_sends_item_level_price_and_stock() -> None:
    captured: dict[str, Any] = {}

    def add_item(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _envelope({"response": {"item_id": 42}})

    receipt = await _adapter(_publish_routes(add_item)).publish_listing(_listing_payload())
    body = captured["body"]
    assert body["price_info"] == [{"currency": "USD", "original_price": "20.00"}]
    assert body["stock_info_v2"] == {"seller_stock": [{"stock": 12}]}
    assert body["model"] == [
        {
            "tier_index": [],
            "model_sku": "LED-PANEL-SET",
            "price_info": [{"currency": "USD", "original_price": "20.00"}],
            "stock_info_v2": {"seller_stock": [{"stock": 12}]},
        }
    ]
    assert "tier_variation" not in body
    assert receipt.external_product_id == "42"
    assert receipt.variant_receipts == []


async def test_publish_listing_uploads_image_blobs_before_add_item() -> None:
    log: list[httpx.Request] = []

    def upload(request: httpx.Request) -> httpx.Response:
        _assert_signed(request, UPLOAD_IMAGE_PATH)
        assert request.headers["content-type"].startswith("multipart/form-data")
        return _envelope({"response": {"image_info": {"image_id": "sg-image-777"}}})

    def add_item(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        log.append(request)
        assert body["image"] == {
            "image_id_list": ["sg-image-777"],
            "image_url_list": ["https://cdn.example.com/panel.jpg"],
        }
        return _envelope({"response": {"item_id": 555}})

    payload = _listing_payload(
        image_blobs=[
            MarketplaceImageBlob(
                filename="panel.png",
                mime_type="image/png",
                data=b"\x89PNG fake bytes",
            )
        ]
    )
    handler = _router(
        {
            LOGISTICS_CHANNEL_LIST_PATH: lambda _r: _envelope(
                {"response": {"logistic_channel_list": [{"logistics_channel_id": 1}]}}
            ),
            UPLOAD_IMAGE_PATH: upload,
            ADD_ITEM_PATH: add_item,
        }
    )
    receipt = await _adapter(handler).publish_listing(payload)
    assert receipt.external_product_id == "555"
    assert len(log) == 1


async def test_publish_listing_remote_error_is_typed_and_redacted() -> None:
    def add_item(_request: httpx.Request) -> httpx.Response:
        return _envelope(
            {
                "error": "error_param",
                "message": f"invalid logistic for partner {PARTNER_KEY} token {ACCESS_TOKEN}",
            },
            400,
        )

    with pytest.raises(AppError) as excinfo:
        await _adapter(_publish_routes(add_item)).publish_listing(_listing_payload())
    assert excinfo.value.code == "ERR_SHOPEE_LISTING_REJECTED"
    assert excinfo.value.status_code == 502
    assert PARTNER_KEY not in excinfo.value.detail
    assert ACCESS_TOKEN not in excinfo.value.detail
    assert "[redacted]" in excinfo.value.detail


async def test_publish_listing_rejects_non_numeric_category() -> None:
    adapter = _adapter(_publish_routes(lambda _r: _envelope({"response": {"item_id": 1}})))
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(_listing_payload(category="LED Lighting"))
    assert excinfo.value.code == "ERR_SHOPEE_CATEGORY_REQUIRED"


async def test_publish_listing_rejects_empty_sku_and_title() -> None:
    adapter = _adapter(_publish_routes(lambda _r: _envelope({"response": {"item_id": 1}})))
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(_listing_payload(external_sku="  "))
    assert excinfo.value.code == "ERR_SHOPEE_LISTING_REJECTED"
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(_listing_payload(title="   "))
    assert excinfo.value.code == "ERR_SHOPEE_LISTING_REJECTED"


async def test_publish_listing_rejects_variant_missing_a_tier_value() -> None:
    payload = _listing_payload(
        options=[SourcingOptionSpec(name="Color", values=["Red", "Blue"])],
        variants=[
            MarketplaceVariantPayload(
                local_sku="L-ONLY",
                external_sku="LED-ONLY",
                option_values={},
                price_usd=Decimal("9.99"),
                quantity=1,
            )
        ],
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(
            _publish_routes(lambda _r: _envelope({"response": {"item_id": 1}}))
        ).publish_listing(payload)
    assert excinfo.value.code == "ERR_SHOPEE_LISTING_REJECTED"
    assert "tier 'Color'" in excinfo.value.detail


async def test_publish_listing_requires_item_id_in_the_response() -> None:
    handler = _publish_routes(lambda _r: _envelope({"response": {}}))
    with pytest.raises(AppError) as excinfo:
        await _adapter(handler).publish_listing(_listing_payload())
    assert excinfo.value.code == "ERR_SHOPEE_LISTING_REJECTED"
    assert "no item_id" in excinfo.value.detail


async def test_publish_listing_fails_closed_without_logistics_channels() -> None:
    handler = _router(
        {
            LOGISTICS_CHANNEL_LIST_PATH: lambda _r: _envelope(
                {
                    "response": {
                        "logistic_channel_list": [
                            {"logistics_channel_id": 9, "enabled": False}
                        ]
                    }
                }
            ),
            ADD_ITEM_PATH: lambda _r: _envelope({"response": {"item_id": 1}}),
        }
    )
    with pytest.raises(AppError) as excinfo:
        await _adapter(handler).publish_listing(_listing_payload())
    assert excinfo.value.code == "ERR_SHOPEE_LOGISTICS_UNAVAILABLE"


async def test_logistics_channel_list_is_fetched_once_per_adapter() -> None:
    log: list[httpx.Request] = []
    adapter = _adapter(_publish_routes(lambda _r: _envelope({"response": {"item_id": 7}}), log=log))
    await adapter.publish_listing(_listing_payload())
    await adapter.publish_listing(_listing_payload())
    paths = [request.url.path for request in log]
    assert paths.count(LOGISTICS_CHANNEL_LIST_PATH) == 1
    assert paths.count(ADD_ITEM_PATH) == 2


# -------------------------------------------------------------------------------- stock


async def test_push_inventory_level_clamps_and_returns_the_contract() -> None:
    captured: dict[str, Any] = {}

    def update_stock(request: httpx.Request) -> httpx.Response:
        _assert_signed(request, UPDATE_STOCK_PATH)
        captured["body"] = json.loads(request.content)
        return _envelope({"response": {}})

    result = await _adapter(_router({UPDATE_STOCK_PATH: update_stock})).push_inventory_level(
        external_sku="LED-PANEL-RED",
        quantity=-7,
        external_product_id="1001",
        location_id="SG-WH",
        warehouse_id="SG-WH-1",
        ignored_extra="ignored",
    )
    assert captured["body"] == {
        "item_id": 1001,
        "stock_list": [{"model_id": 0, "seller_stock": [{"stock": 0}]}],
    }
    assert result["channel"] == "shopee"
    assert result["external_sku"] == "LED-PANEL-RED"
    assert result["quantity"] == 0
    assert result["product_id"] == 1001
    assert result["variant_id"] == 0
    assert result["location_id"] == "SG-WH"
    assert result["warehouse_id"] == "SG-WH-1"


async def test_push_inventory_level_targets_an_explicit_model() -> None:
    captured: dict[str, Any] = {}

    def update_stock(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _envelope({"response": {}})

    result = await _adapter(_router({UPDATE_STOCK_PATH: update_stock})).push_inventory_level(
        external_sku="LED-PANEL-BLUE",
        quantity=31,
        external_product_id="1001",
        external_variant_id="502",
    )
    assert captured["body"]["stock_list"] == [
        {"model_id": 502, "seller_stock": [{"stock": 31}]}
    ]
    assert result["quantity"] == 31
    assert result["variant_id"] == 502


async def test_push_inventory_level_requires_a_product_id() -> None:
    with pytest.raises(AppError) as excinfo:
        await _adapter(_router({})).push_inventory_level(external_sku="LED-PANEL", quantity=3)
    assert excinfo.value.code == "ERR_SHOPEE_INVENTORY_SYNC"
    assert excinfo.value.status_code == 422


async def test_push_inventory_level_rejects_non_numeric_ids() -> None:
    adapter = _adapter(_router({}))
    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(
            external_sku="LED-PANEL", quantity=3, external_product_id="sku-1"
        )
    assert excinfo.value.code == "ERR_SHOPEE_INVENTORY_SYNC"
    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(
            external_sku="LED-PANEL",
            quantity=3,
            external_product_id="1001",
            external_variant_id="model-x",
        )
    assert excinfo.value.code == "ERR_SHOPEE_INVENTORY_SYNC"


# ----------------------------------------------------------------------------- webhooks


def test_verify_shopee_webhook_accepts_url_bound_signature() -> None:
    body = b'{"code":3,"shop_id":778899,"timestamp":1735689600}'
    headers = {"x-shopee-hmac-sha256": _push_signature(CALLBACK_URL, body)}
    assert verify_shopee_webhook(body, headers, PARTNER_KEY, callback_url=CALLBACK_URL) is True


def test_verify_shopee_webhook_accepts_body_only_and_legacy_authorization() -> None:
    body = b'{"code":3}'
    body_only = hmac.new(PARTNER_KEY.encode(), body, hashlib.sha256).hexdigest()
    assert verify_shopee_webhook(body, {"X-Shopee-Hmac-Sha256": body_only}, PARTNER_KEY) is True
    assert verify_shopee_webhook(body, {"Authorization": body_only}, PARTNER_KEY) is True


def test_verify_shopee_webhook_rejects_tampering_and_weak_input() -> None:
    body = b'{"code":3}'
    signature = _push_signature(CALLBACK_URL, body)
    assert (
        verify_shopee_webhook(
            b'{"code":3,"tampered":true}',
            {"x-shopee-hmac-sha256": signature},
            PARTNER_KEY,
            callback_url=CALLBACK_URL,
        )
        is False
    )
    assert verify_shopee_webhook(body, {}, PARTNER_KEY) is False
    assert verify_shopee_webhook(body, {"x-shopee-hmac-sha256": signature}, "") is False
    assert (
        verify_shopee_webhook(
            body,
            {"x-shopee-hmac-sha256": "deadbeef"},
            PARTNER_KEY,
            callback_url=CALLBACK_URL,
        )
        is False
    )


def test_adapter_verify_webhook_uses_its_partner_key() -> None:
    body = b'{"code":2,"order_sn":"240101ABCDEFGH"}'
    signature = hmac.new(PARTNER_KEY.encode(), body, hashlib.sha256).hexdigest()
    adapter = _adapter(_router({}))
    assert adapter.verify_webhook(body, {"x-shopee-hmac-sha256": signature}, "") is True
    assert adapter.verify_webhook(b"other", {"x-shopee-hmac-sha256": signature}, "") is False
    assert (
        adapter.verify_webhook(body, {"x-shopee-hmac-sha256": signature}, "override-key") is False
    )


def test_module_constants_pin_the_shopee_v2_prefix() -> None:
    assert SHOPEE_API_PREFIX == "/api/v2"
    assert SHOPEE_API_BASE == "https://partner.shopeemobile.com"
