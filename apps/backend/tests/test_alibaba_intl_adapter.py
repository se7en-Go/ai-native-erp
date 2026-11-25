"""Zero-network unit tests for the Alibaba.com International Open Platform (IOP) adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from erp_core.channels.ecommerce.alibaba_intl import (
    ALIBABA_IOP_INVENTORY_UPDATE_PATH,
    ALIBABA_IOP_ORDER_LIST_PATH,
    ALIBABA_IOP_PRODUCT_ADD_PATH,
    ALIBABA_IOP_PRODUCT_LIST_PATH,
    ALIBABA_IOP_SHIPPING_SEND_PATH,
    AlibabaIntlAdapter,
    sign_alibaba_iop_request,
)
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

APP_KEY = "test-app-key"
APP_SECRET = "test-app-secret-value"
ACCESS_TOKEN = "test-session-token"
BASE_URL = "https://iop.test/rest"

Handler = Callable[[httpx.Request], httpx.Response]


def _adapter(handler: Handler) -> AlibabaIntlAdapter:
    return AlibabaIntlAdapter(
        store_id="store-alibaba-1",
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        access_token=ACCESS_TOKEN,
        api_base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )


def _params(request: httpx.Request) -> dict[str, str]:
    """Decode the signed parameter map exactly as the IOP gateway would (blanks kept)."""
    if request.method in {"POST", "PUT", "PATCH"}:
        return dict(parse_qsl(request.content.decode("utf-8"), keep_blank_values=True))
    return dict(parse_qsl(request.url.query.decode("utf-8"), keep_blank_values=True))


def _assert_signed(request: httpx.Request, api_path: str) -> dict[str, str]:
    """Recompute the IOP signature from the wire parameters and pin the transport hygiene."""
    params = _params(request)
    assert request.url.path == f"/rest{api_path}"
    assert params["app_key"] == APP_KEY
    assert params["sign_method"] == "sha256"
    assert params["session"] == ACCESS_TOKEN
    assert len(params["timestamp"]) == 13 and params["timestamp"].isdigit()
    raw_body = request.content.decode("utf-8") if request.content else ""
    assert APP_SECRET not in raw_body
    assert params["sign"] == sign_alibaba_iop_request(api_path, params, APP_SECRET)
    assert params["sign"] == params["sign"].upper()
    return params


def _order(
    trade_id: str = "AT202611190001",
    status: str = "waitbuyerpay",
    **overrides: Any,
) -> dict[str, Any]:
    order: dict[str, Any] = {
        "trade_id": trade_id,
        "order_no": f"NO-{trade_id}",
        "status": status,
        "currency": "USD",
        "subtotal": "1250.555",
        "shipping_fee": "40.00",
        "discount_amount": "50.00",
        "tax_amount": "0.00",
        "total_amount": "1240.555",
        "gmt_modified": "2026-11-19 02:30:00",
        "buyer_company_name": "Nordic Lights AB",
        "buyer_company_id": "ALB-778001",
        "buyer_country": "SE",
        "buyer_email": "ops@nordiclights.se",
        "buyer_phone": "+46701234567",
        "buyer_login_id": "nordiclights",
        "products": [
            {
                "product_id": "6601999001",
                "sku_code": "NORA-220-AMZ",
                "product_name": "Nordic Pendant Light 220V",
                "quantity": "100",
                "unit_price": "12.5055",
                "amount": "1250.555",
            }
        ],
    }
    order.update(overrides)
    return order


def _order_payload(orders: list[dict[str, Any]], **data_overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"total_count": "60", "orders": orders}
    data.update(data_overrides)
    return {"result": {"success": True, "code": "0", "data": data}}


# --------------------------------------------------------------------- signing helper


def test_sign_alibaba_iop_request_matches_canonical_hmac_sha256() -> None:
    params = {
        "timestamp": "1700000000000",
        "sign_method": "sha256",
        "app_key": "ak-1",
        "page": "2",
        "sign": "SHOULD_BE_IGNORED",
    }
    signature = sign_alibaba_iop_request(ALIBABA_IOP_ORDER_LIST_PATH, params, APP_SECRET)

    canonical = (
        f"{ALIBABA_IOP_ORDER_LIST_PATH}"
        "app_keyak-1"
        "page2"
        "sign_methodsha256"
        "timestamp1700000000000"
    )
    expected = hmac.new(
        APP_SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest().upper()
    assert signature == expected
    assert len(signature) == 64
    assert signature == signature.upper()


def test_sign_alibaba_iop_request_is_order_independent_and_binds_the_path() -> None:
    params = {"app_key": "ak-1", "page": "3", "timestamp": "1700000000123"}
    baseline = sign_alibaba_iop_request(ALIBABA_IOP_ORDER_LIST_PATH, params, APP_SECRET)
    reordered = dict(reversed(list(params.items())))
    assert sign_alibaba_iop_request(ALIBABA_IOP_ORDER_LIST_PATH, reordered, APP_SECRET) == baseline
    assert (
        sign_alibaba_iop_request(ALIBABA_IOP_PRODUCT_LIST_PATH, params, APP_SECRET) != baseline
    )
    assert sign_alibaba_iop_request(ALIBABA_IOP_ORDER_LIST_PATH, params, "other") != baseline


def test_sign_alibaba_iop_request_requires_a_secret() -> None:
    with pytest.raises(ValueError, match="app_secret"):
        sign_alibaba_iop_request(ALIBABA_IOP_ORDER_LIST_PATH, {"app_key": "ak"}, "")


# ------------------------------------------------------------------ orders ingestion


async def test_fetch_orders_normalizes_b2b_wholesale_order_with_decimals() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _assert_signed(request, ALIBABA_IOP_ORDER_LIST_PATH)
        return httpx.Response(200, json=_order_payload([_order()]))

    adapter = _adapter(handle)
    orders, cursor = await adapter.fetch_orders_updated_since(
        datetime(2026, 11, 19, 0, 0, tzinfo=UTC)
    )

    params = _params(seen[0])
    assert params["gmt_modified_start"] == "2026-11-19 00:00:00"
    assert params["page"] == "1"
    assert params["page_size"] == "50"
    assert cursor == "2"

    assert len(orders) == 1
    order = orders[0]
    assert order.external_order_id == "AT202611190001"
    assert order.external_order_number == "NO-AT202611190001"
    assert order.order_mode is OrderMode.B2B_WHOLESALE
    assert order.currency == "USD"
    assert order.fx_rate_to_base is None
    assert order.fulfillment_status is FulfillmentStatus.PENDING_PAYMENT
    assert order.payment_status is PaymentStatus.UNPAID
    assert order.updated_at == datetime(2026, 11, 19, 2, 30, tzinfo=UTC)

    assert order.subtotal_amount == Decimal("1250.56")
    assert order.shipping_charged == Decimal("40.00")
    assert order.discount_amount == Decimal("50.00")
    assert order.tax_collected == Decimal("0.00")
    assert order.total_amount == Decimal("1240.56")
    assert order.platform_commission_base == Decimal("0.00")
    for amount in (
        order.subtotal_amount,
        order.shipping_charged,
        order.discount_amount,
        order.tax_collected,
        order.total_amount,
        order.platform_commission_base,
    ):
        assert isinstance(amount, Decimal)

    assert order.b2b_company is not None
    assert order.b2b_company.legal_name == "Nordic Lights AB"
    assert order.b2b_company.country_code == "SE"
    assert order.b2b_company.external_company_id == "ALB-778001"
    assert order.customer.email == "ops@nordiclights.se"
    assert order.customer.phone_e164 == "+46701234567"
    assert order.customer.external_customer_id == "nordiclights"
    assert order.channel_metadata["alibaba_trade_status"] == "waitbuyerpay"

    assert len(order.line_items) == 1
    line = order.line_items[0]
    assert line.external_sku == "NORA-220-AMZ"
    assert line.quantity == 100
    assert line.line_total == Decimal("1250.56")
    assert isinstance(line.line_total, Decimal)


async def test_fetch_orders_derives_totals_from_line_items_without_floats() -> None:
    order = _order()
    order.pop("subtotal")
    order.pop("total_amount")
    order.pop("tax_amount")
    order.pop("discount_amount")
    order.pop("shipping_fee")
    order["products"] = [
        {
            "sku_code": "NORA-220-AMZ",
            "quantity": "3",
            "unit_price": "10.005",
            "product_name": "Nordic Pendant Light 220V",
        },
        {
            "sku_code": "NORA-CABLE-2M",
            "quantity": "2",
            "unit_price": "2.333",
            "total_price": "4.666",
        },
    ]

    adapter = _adapter(lambda request: httpx.Response(200, json=_order_payload([order])))
    orders, _ = await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))

    normalized = orders[0]
    assert normalized.line_items[0].line_total == Decimal("30.02")
    assert normalized.line_items[1].line_total == Decimal("4.67")
    assert normalized.subtotal_amount == Decimal("34.69")
    assert normalized.total_amount == Decimal("34.69")
    assert isinstance(normalized.total_amount, Decimal)


@pytest.mark.parametrize(
    ("alibaba_status", "expected"),
    [
        ("waitbuyerpay", FulfillmentStatus.PENDING_PAYMENT),
        ("waitsellersend", FulfillmentStatus.CONFIRMED_RESERVED),
        ("waitbuyerreceive", FulfillmentStatus.SHIPPED),
        ("success", FulfillmentStatus.DELIVERED),
        ("cancel", FulfillmentStatus.CANCELLED),
        ("terminated", FulfillmentStatus.CANCELLED),
        ("some_future_status", FulfillmentStatus.CONFIRMED_RESERVED),
    ],
)
async def test_fetch_orders_maps_trade_statuses(
    alibaba_status: str, expected: FulfillmentStatus
) -> None:
    payload = _order_payload([_order(status=alibaba_status)])
    adapter = _adapter(lambda request: httpx.Response(200, json=payload))
    orders, _ = await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))
    assert orders[0].fulfillment_status is expected


async def test_fetch_orders_paid_and_cancelled_payment_states() -> None:
    orders = [
        _order(trade_id="AT-PAID", status="waitbuyerreceive", gmt_pay="2026-11-19 01:00:00"),
        _order(trade_id="AT-CANCELLED", status="cancel"),
        _order(trade_id="AT-REFUNDED", status="cancel", gmt_pay="2026-11-19 01:00:00"),
    ]
    payload = _order_payload(orders)
    adapter = _adapter(lambda request: httpx.Response(200, json=payload))
    normalized, _ = await adapter.fetch_orders_updated_since(
        datetime(2026, 11, 19, tzinfo=UTC)
    )
    by_id = {order.external_order_id: order for order in normalized}
    assert by_id["AT-PAID"].payment_status is PaymentStatus.PAID
    assert by_id["AT-CANCELLED"].payment_status is PaymentStatus.UNPAID
    assert by_id["AT-REFUNDED"].payment_status is PaymentStatus.REFUNDED


async def test_fetch_orders_cursor_advances_by_has_next_and_stops_at_last_page() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = _params(request)["page"]
        if page == "1":
            return httpx.Response(
                200,
                json=_order_payload([_order()], total_count="60", has_next=True),
            )
        return httpx.Response(200, json=_order_payload([_order()], total_count="60"))

    adapter = _adapter(handle)
    _, first_cursor = await adapter.fetch_orders_updated_since(
        datetime(2026, 11, 19, tzinfo=UTC)
    )
    _, second_cursor = await adapter.fetch_orders_updated_since(
        datetime(2026, 11, 19, tzinfo=UTC), first_cursor
    )
    _, third_cursor = await adapter.fetch_orders_updated_since(
        datetime(2026, 11, 19, tzinfo=UTC), "2"
    )

    assert first_cursor == "2"
    assert second_cursor is None
    assert third_cursor is None
    assert [ _params(request)["page"] for request in seen] == ["1", "2", "2"]


async def test_fetch_orders_stops_when_total_covers_no_further_page() -> None:
    payload = _order_payload([_order()], total_count="50")
    adapter = _adapter(lambda request: httpx.Response(200, json=payload))
    _, cursor = await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))
    assert cursor is None


async def test_fetch_orders_rejects_a_non_numeric_cursor() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_order_payload([])))
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(
            datetime(2026, 11, 19, tzinfo=UTC), "not-a-page"
        )
    assert excinfo.value.code == "ERR_ALIBABA_INVALID_CURSOR"


# ------------------------------------------------------------------ products ingestion


async def test_fetch_products_normalizes_products_and_next_cursor() -> None:
    payload = {
        "result": {
            "success": True,
            "code": "0",
            "data": {
                "total_count": 120,
                "products": [
                    {
                        "product_id": 6601999001,
                        "subject": "Solar Garden Light 20W",
                        "skus": [
                            {"sku_code": "SGL-100", "price": "8.555", "quantity": "450"},
                            {"sku_code": "SGL-200", "price": "11.20", "quantity": 0},
                        ],
                    },
                    {"subject": "Product without id"},
                ],
            },
        }
    }
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _assert_signed(request, ALIBABA_IOP_PRODUCT_LIST_PATH)
        return httpx.Response(200, json=payload)

    adapter = _adapter(handle)
    products, cursor = await adapter.fetch_products_updated_since(
        datetime(2026, 11, 19, 3, 15, tzinfo=UTC)
    )

    params = _params(seen[0])
    assert params["gmt_modified_start"] == "2026-11-19 03:15:00"
    assert cursor == "2"
    assert len(products) == 1
    product = products[0]
    assert product.external_product_id == "6601999001"
    assert product.title == "Solar Garden Light 20W"
    assert product.variants == [
        {"external_sku": "SGL-100", "unit_price_usd": "8.56", "stock_quantity": 450},
        {"external_sku": "SGL-200", "unit_price_usd": "11.20", "stock_quantity": 0},
    ]


# ------------------------------------------------------------- publish & inventory APIs


def _listing_payload(product_id: object | None = 6601234567) -> dict[str, Any]:
    data: dict[str, Any] = {"success": True, "code": "0"}
    if product_id is not None:
        data["product_id"] = product_id
    return {"result": data}


async def test_publish_listing_sends_signed_payload_with_moq_ladder() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _assert_signed(request, ALIBABA_IOP_PRODUCT_ADD_PATH)
        return httpx.Response(200, json=_listing_payload())

    adapter = _adapter(handle)
    receipt = await adapter.publish_listing(
        sku="NORA-220-AMZ",
        title="Nordic Pendant Light 220V " + "x" * 200,
        description="Hand-finished aluminium pendant light for contract projects.",
        bullet_points=["5-year warranty", "CE + RoHS certified", ""],
        price_usd=Decimal("12.345"),
        quantity=250,
        images=["https://cdn.test/nora-1.jpg", "https://cdn.test/nora-2.jpg"],
        currency="usd",
        category="Lighting > Pendant Lights",
        hs_code="9405.10",
        moq_tiers=[
            {"min_qty": 500, "unit_price_usd": Decimal("8.255")},
            {"min_qty": 100, "unit_price_usd": 9.5},
        ],
    )

    params = _params(seen[0])
    assert len(params["subject"]) == 128
    assert params["sku_code"] == "NORA-220-AMZ"
    assert params["category_id"] == "390501"
    assert params["category_name"] == "Lighting > Pendant Lights"
    assert params["description"] == (
        "Hand-finished aluminium pendant light for contract projects.\n\n"
        "- 5-year warranty\n"
        "- CE + RoHS certified"
    )
    assert json.loads(params["image_urls"]) == [
        "https://cdn.test/nora-1.jpg",
        "https://cdn.test/nora-2.jpg",
    ]
    assert params["fob_price"] == "12.35"
    assert params["currency"] == "USD"
    assert params["stock_quantity"] == "250"
    assert params["hs_code"] == "9405.10"
    assert json.loads(params["wholesale_ladder"]) == [
        {"min_qty": 100, "unit_price_usd": "9.50"},
        {"min_qty": 500, "unit_price_usd": "8.26"},
    ]

    assert receipt.external_product_id == "6601234567"
    assert receipt.external_sku == "NORA-220-AMZ"
    assert receipt.status == "PUBLISHED"
    assert receipt.raw_response["result"]["product_id"] == 6601234567


async def test_publish_listing_defaults_minimum_stock_and_empty_hs_code() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"product_id": 660100000001, "success": True, "code": "0"}
        )

    adapter = _adapter(handle)
    receipt = await adapter.publish_listing(
        sku="SGL-100",
        title="Solar Garden Light",
        description="IP65 solar path light.",
        bullet_points=[],
        price_usd=Decimal("0"),
        quantity=0,
        images=[],
    )

    params = _params(seen[0])
    assert params["stock_quantity"] == "1"
    assert params["hs_code"] == ""
    assert params["category_id"] == "390501"
    assert json.loads(params["wholesale_ladder"]) == []
    assert params["description"] == "IP65 solar path light."
    assert receipt.external_product_id == "660100000001"


async def test_publish_listing_without_product_id_is_rejected() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_listing_payload(None)))
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(
            sku="NORA-220-AMZ",
            title="Nordic Pendant Light",
            description="Pendant light.",
            bullet_points=[],
            price_usd=Decimal("12.35"),
            quantity=10,
            images=[],
        )
    assert excinfo.value.slug == "alibaba-listing-rejected"
    assert excinfo.value.code == "ERR_ALIBABA_LISTING_REJECTED"
    assert excinfo.value.status_code == 502
    assert APP_SECRET not in excinfo.value.detail


@pytest.mark.parametrize(
    "tier",
    [
        {"unit_price_usd": "9.50"},
        {"min_qty": 100},
        {"min_qty": 0, "unit_price_usd": "9.50"},
    ],
)
async def test_publish_listing_rejects_incomplete_moq_tiers(tier: Mapping[str, Any]) -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_listing_payload()))
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(
            sku="NORA-220-AMZ",
            title="Nordic Pendant Light",
            description="Pendant light.",
            bullet_points=[],
            price_usd=Decimal("12.35"),
            quantity=10,
            images=[],
            moq_tiers=[tier],
        )
    assert excinfo.value.code == "ERR_ALIBABA_INVALID_MOQ_TIER"


async def test_push_inventory_level_sends_signed_absolute_stock() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _assert_signed(request, ALIBABA_IOP_INVENTORY_UPDATE_PATH)
        return httpx.Response(
            200, json={"result": {"success": True, "code": "0", "data": {"sku_code": "SGL-100"}}}
        )

    adapter = _adapter(handle)
    result = await adapter.push_inventory_level(external_sku="SGL-100 ", quantity=125)
    assert result == {"sku_code": "SGL-100"}
    assert _params(seen[0])["sku_code"] == "SGL-100"
    assert _params(seen[0])["stock_quantity"] == "125"

    await adapter.push_inventory_level(external_sku="SGL-100", quantity=-5)
    assert _params(seen[1])["stock_quantity"] == "0"


# ----------------------------------------------------------------------- fulfillment


async def test_create_fulfillment_sends_signed_shipping_notice() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _assert_signed(request, ALIBABA_IOP_SHIPPING_SEND_PATH)
        return httpx.Response(
            200,
            json={
                "result": {
                    "success": True,
                    "code": "0",
                    "data": {"trade_id": "AT202611190001", "accepted": True},
                }
            },
        )

    adapter = _adapter(handle)
    result = await adapter.create_fulfillment(
        external_order_id="AT202611190001",
        tracking_number="1Z999AA10123456784",
        carrier_company="UPS",
        tracking_url="https://track.test/1Z999AA10123456784",
        fulfillment_order_id="DO-77",
        notify_customer=True,
    )

    params = _params(seen[0])
    assert params["trade_id"] == "AT202611190001"
    assert params["logistics_company"] == "UPS"
    assert params["tracking_no"] == "1Z999AA10123456784"
    assert params["tracking_url"] == "https://track.test/1Z999AA10123456784"
    assert result == {
        "trade_id": "AT202611190001",
        "accepted": True,
        "fulfillment_id": "AT202611190001",
    }


async def test_alibaba_refund_empty_lines_and_empty_sku_validation() -> None:
    orders = [
        _order(trade_id="AT-NO-LINES", products=[]),
        _order(trade_id="AT-FULL-REFUND", status="refunded", total_amount="100.00"),
        _order(
            trade_id="AT-PARTIAL-REFUND",
            status="waitbuyerreceive",
            total_amount="100.00",
            refund_amount="15.00",
        ),
    ]
    adapter = _adapter(lambda _req: httpx.Response(200, json=_order_payload(orders)))
    normalized, _ = await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))
    by_id = {o.external_order_id: o for o in normalized}
    assert "AT-NO-LINES" not in by_id
    assert by_id["AT-FULL-REFUND"].fulfillment_status is FulfillmentStatus.REFUNDED
    assert by_id["AT-FULL-REFUND"].payment_status is PaymentStatus.REFUNDED
    assert by_id["AT-PARTIAL-REFUND"].fulfillment_status is FulfillmentStatus.SHIPPED
    assert by_id["AT-PARTIAL-REFUND"].payment_status is PaymentStatus.PAID
    assert by_id["AT-PARTIAL-REFUND"].channel_metadata["refunded_amount"] == "15.00"

    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(external_sku="   ", quantity=5)
    assert excinfo.value.code == "ERR_ALIBABA_INVENTORY_SYNC"
    assert excinfo.value.status_code == 422


# ------------------------------------------------------------------- error mapping


async def test_ioP_api_error_code_maps_to_api_error_without_leaking_credentials() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error_response": {
                    "code": "50000",
                    "msg": f"seller backend exploded with {APP_SECRET} and {ACCESS_TOKEN}",
                }
            },
        )

    adapter = _adapter(handle)
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))

    error = excinfo.value
    assert error.slug == "alibaba-api-error"
    assert error.code == "ERR_ALIBABA_API_ERROR"
    assert error.status_code == 502
    assert APP_SECRET not in error.detail
    assert ACCESS_TOKEN not in error.detail
    assert APP_SECRET not in str(error)
    assert "[REDACTED]" in error.detail


@pytest.mark.parametrize(
    ("status_code", "payload", "slug", "code", "expected_status"),
    [
        (
            200,
            {"code": "InvalidSignature", "msg": f"bad signature for {APP_SECRET}"},
            "alibaba-auth-failed",
            "ERR_ALIBABA_AUTH_FAILED",
            502,
        ),
        (
            200,
            {"error_response": {"code": "IllegalAccessToken", "msg": "session expired"}},
            "alibaba-auth-failed",
            "ERR_ALIBABA_AUTH_FAILED",
            502,
        ),
        (401, {}, "alibaba-auth-failed", "ERR_ALIBABA_AUTH_FAILED", 502),
        (
            403,
            {"code": "InsufficientPermission"},
            "alibaba-auth-failed",
            "ERR_ALIBABA_AUTH_FAILED",
            502,
        ),
        (
            200,
            {"code": "Throttled", "message": "flow limit exceeded"},
            "alibaba-rate-limited",
            "ERR_ALIBABA_RATE_LIMITED",
            429,
        ),
        (429, {}, "alibaba-rate-limited", "ERR_ALIBABA_RATE_LIMITED", 429),
        (
            200,
            {"code": "50000", "message": "unexpected upstream failure"},
            "alibaba-api-error",
            "ERR_ALIBABA_API_ERROR",
            502,
        ),
        (500, {}, "alibaba-api-error", "ERR_ALIBABA_API_ERROR", 502),
    ],
)
async def test_ioP_failures_map_to_typed_app_errors(
    status_code: int,
    payload: dict[str, Any],
    slug: str,
    code: str,
    expected_status: int,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    adapter = _adapter(handle)
    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(external_sku="SGL-100", quantity=5)

    error = excinfo.value
    assert error.slug == slug
    assert error.code == code
    assert error.status_code == expected_status
    assert APP_SECRET not in error.detail
    assert ACCESS_TOKEN not in error.detail


async def test_transport_failure_maps_to_retryable_api_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed for {APP_KEY}", request=request)

    adapter = _adapter(handle)
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))

    error = excinfo.value
    assert error.code == "ERR_ALIBABA_API_ERROR"
    assert APP_SECRET not in error.detail
    assert ACCESS_TOKEN not in error.detail


async def test_malformed_json_body_is_not_reported_as_success() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>upstream proxy error</html>")

    adapter = _adapter(handle)
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))
    assert excinfo.value.code == "ERR_ALIBABA_API_ERROR"


def test_adapter_refuses_to_construct_without_credentials() -> None:
    with pytest.raises(ValueError, match="app_key"):
        AlibabaIntlAdapter(store_id="store-1", app_key="", app_secret=APP_SECRET)
    with pytest.raises(ValueError, match="app_key"):
        AlibabaIntlAdapter(store_id="store-1", app_key=APP_KEY, app_secret="")


# ------------------------------------------------------------------------- webhooks


def test_verify_webhook_uses_constant_time_hmac_over_raw_body() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    raw_body = b'{"trade_id":"AT202611190001","status":"waitbuyerreceive"}'
    secret = "alibaba-webhook-secret"
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    assert adapter.verify_webhook(raw_body, {"X-Alibaba-Signature": signature}, secret)
    assert adapter.verify_webhook(raw_body, {"x-alibaba-signature": signature.upper()}, secret)
    assert adapter.verify_webhook(raw_body, {"X-Alibaba-Signature": f"  {signature}  "}, secret)

    # Fail closed: no signature, no secret, tampered body, non-hex signature.
    assert not adapter.verify_webhook(raw_body, {}, secret)
    assert not adapter.verify_webhook(raw_body, {"X-Alibaba-Signature": ""}, secret)
    assert not adapter.verify_webhook(raw_body, {"X-Alibaba-Signature": signature}, "")
    assert not adapter.verify_webhook(raw_body + b" ", {"X-Alibaba-Signature": signature}, secret)
    assert not adapter.verify_webhook(
        raw_body, {"X-Alibaba-Signature": "0" * 64}, secret
    )
    assert not adapter.verify_webhook(
        raw_body, {"X-Alibaba-Signature": "签名"}, secret
    )
