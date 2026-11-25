"""Zero-network unit tests for the Lazada Open Platform (Lazada Seller Center) adapter."""

from __future__ import annotations

import base64
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
    MarketplaceListingDispatchReceipt,
    MarketplaceListingPayload,
    MarketplaceVariantPayload,
    SourcingOptionSpec,
)
from erp_core.channels.ecommerce.lazada import (
    DEFAULT_LAZADA_PRIMARY_CATEGORY,
    LAZADA_ORDER_ITEMS_PATH,
    LAZADA_ORDERS_FULFILL_PATH,
    LAZADA_ORDERS_PATH,
    LAZADA_PRICE_QUANTITY_PATH,
    LAZADA_PRODUCT_CREATE_PATH,
    LAZADA_PRODUCTS_PATH,
    LazadaAdapter,
    sign_lazada_request,
    verify_lazada_webhook,
)
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

SELLER_ID = "seller-lazada-7781"
APP_KEY = "test-lazada-app-key"
APP_SECRET = "test-lazada-app-secret-value"
ACCESS_TOKEN = "test-lazada-access-token"
BASE_URL = "https://api.lazada.test/rest"

Handler = Callable[[httpx.Request], httpx.Response]


def _adapter(handler: Handler, **overrides: Any) -> LazadaAdapter:
    kwargs: dict[str, Any] = {
        "seller_id": SELLER_ID,
        "app_key": APP_KEY,
        "app_secret": APP_SECRET,
        "access_token": ACCESS_TOKEN,
        "endpoint_url": BASE_URL,
        "transport": httpx.MockTransport(handler),
        "timeout_seconds": 5.0,
    }
    kwargs.update(overrides)
    return LazadaAdapter(**kwargs)


def _route(routes: Mapping[str, Handler]) -> Handler:
    """Dispatch a MockTransport request by REST path (``/rest`` prefix stripped)."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/rest")
        handler = routes.get(path)
        assert handler is not None, f"unexpected Lazada path {path!r}"
        return handler(request)

    return handle


def _query(request: httpx.Request) -> dict[str, str]:
    """Decode the signed parameter map exactly as the Lazada gateway would."""
    return dict(parse_qsl(request.url.query.decode("utf-8"), keep_blank_values=True))


def _assert_signed(request: httpx.Request, path: str) -> dict[str, str]:
    """Recompute the Lazada signature from the wire params and pin transport hygiene."""
    params = _query(request)
    assert request.url.path == f"/rest{path}"
    assert params["app_key"] == APP_KEY
    assert params["sign_method"] == "sha256"
    assert params["access_token"] == ACCESS_TOKEN
    assert len(params["timestamp"]) == 13 and params["timestamp"].isdigit()
    assert params["sign"] == sign_lazada_request(params, APP_SECRET)
    assert params["sign"] == params["sign"].upper()
    body = request.content.decode("utf-8") if request.content else ""
    assert APP_SECRET not in str(request.url)
    assert APP_SECRET not in body
    assert ACCESS_TOKEN not in body
    return params


def _body(request: httpx.Request) -> dict[str, Any]:
    raw = request.content.decode("utf-8") if request.content else ""
    parsed = json.loads(raw or "{}")
    assert isinstance(parsed, dict)
    return parsed


def _order(order_id: str = "LAZ-10001", **overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "order_id": order_id,
        "order_number": f"NUM-{order_id}",
        "status": "ready_to_ship",
        "currency": "SGD",
        "price": "120.555",
        "shipping_fee": "6.00",
        "voucher": "5.50",
        "tax_amount": "1.20",
        "platform_commission": "7.345",
        "refund_amount": "3.005",
        "payment_method": "Credit Card",
        "customer_id": "buyer-7781",
        "customer_first_name": "Mei",
        "customer_last_name": "Tan",
        "customer_email": "mei.tan@example.sg",
        "updated_at": "2026-11-19T02:30:00+08:00",
        "address_shipping": {"phone": "+6591234567", "country": "SG", "city": "Singapore"},
    }
    order.update(overrides)
    return order


def _items_response(order_id: str = "LAZ-10001") -> dict[str, Any]:
    return {
        "code": "0",
        "request_id": "req-items",
        "data": [
            {
                "order_item_id": "OI-1",
                "order_id": order_id,
                "shop_sku": "NORA-220-SG",
                "name": "Nordic Pendant Light 220V",
                "item_price": "40.185",
                "quantity": 3,
                "status": "ready_to_ship",
            },
            {
                "order_item_id": "OI-2",
                "order_id": order_id,
                "shop_sku": "NORA-CABLE-2M",
                "name": "Pendant Cable 2m",
                "paid_price": "10.00",
                "quantity": 2,
                "line_total": "20.005",
            },
            {"order_item_id": "OI-3", "order_id": order_id, "name": "Row without a SKU"},
        ],
    }


def _orders_response(orders: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "code": "0",
        "request_id": "req-orders",
        "data": {"count": len(orders), "orders": orders},
    }


def _listing_payload(**overrides: Any) -> MarketplaceListingPayload:
    values: dict[str, Any] = {
        "channel_account_id": uuid4(),
        "channel_type": "lazada",
        "external_sku": "NORA-220-SG",
        "title": "Nordic Pendant Light 220V",
        "description": "Hand-finished aluminium pendant light.",
        "bullet_points": ["5-year warranty", "CE certified"],
        "price_usd": Decimal("12.345"),
        "quantity": 250,
        "images": ["https://cdn.test/nora-1.jpg", "https://cdn.test/nora-2.jpg"],
        "category": "Lighting > Pendant Lights",
    }
    values.update(overrides)
    return MarketplaceListingPayload(**values)


# ------------------------------------------------------------------- construction


def test_adapter_refuses_to_construct_without_credentials() -> None:
    for missing in ("seller_id", "app_key", "app_secret", "access_token"):
        kwargs: dict[str, Any] = {
            "seller_id": SELLER_ID,
            "app_key": APP_KEY,
            "app_secret": APP_SECRET,
            "access_token": ACCESS_TOKEN,
        }
        kwargs[missing] = ""
        with pytest.raises(ValueError, match=missing):
            LazadaAdapter(**kwargs)


def test_unknown_region_is_rejected_without_an_explicit_endpoint() -> None:
    with pytest.raises(ValueError, match="Unsupported Lazada region"):
        LazadaAdapter(
            seller_id=SELLER_ID,
            app_key=APP_KEY,
            app_secret=APP_SECRET,
            access_token=ACCESS_TOKEN,
            region="XX",
        )


def test_non_positive_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        LazadaAdapter(
            seller_id=SELLER_ID,
            app_key=APP_KEY,
            app_secret=APP_SECRET,
            access_token=ACCESS_TOKEN,
            timeout_seconds=0,
        )


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("SG", "https://api.lazada.sg/rest"),
        ("my", "https://api.lazada.my/rest"),
        ("TH", "https://api.lazada.th/rest"),
        ("ID", "https://api.lazada.id/rest"),
    ],
)
def test_default_endpoint_follows_the_region(region: str, expected: str) -> None:
    adapter = LazadaAdapter(
        seller_id=SELLER_ID,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        access_token=ACCESS_TOKEN,
        region=region,
    )
    assert adapter.endpoint_url == expected
    assert adapter.region == region.upper()


def test_explicit_endpoint_wins_and_unknown_region_is_kept_verbatim() -> None:
    adapter = LazadaAdapter(
        seller_id=SELLER_ID,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        access_token=ACCESS_TOKEN,
        region="xx",
        endpoint_url=f"{BASE_URL}/",
        fulfillment_mode="Dropship",
    )
    assert adapter.endpoint_url == BASE_URL
    assert adapter.region == "XX"
    assert adapter.fulfillment_mode == "dropship"


def test_repr_redacts_every_credential() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"code": "0"}))
    rendered = repr(adapter)
    assert APP_SECRET not in rendered
    assert ACCESS_TOKEN not in rendered
    assert APP_KEY not in rendered
    assert rendered.count("[REDACTED]") == 3
    assert SELLER_ID in rendered


# ------------------------------------------------------------------------ signing


def test_sign_lazada_request_matches_canonical_hmac_sha256() -> None:
    params = {
        "access_token": "token-1",
        "app_key": "ak-1",
        "timestamp": "1700000000000",
        "sign_method": "sha256",
        "offset": "50",
        "sign": "SHOULD_BE_IGNORED",
    }
    signature = sign_lazada_request(params, APP_SECRET)

    canonical = "access_tokentoken-1app_keyak-1offset50sign_methodsha256timestamp1700000000000"
    expected = (
        hmac.new(APP_SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256)
        .hexdigest()
        .upper()
    )
    assert signature == expected
    assert len(signature) == 64
    assert signature == signature.upper()


def test_sign_lazada_request_is_order_independent_and_secret_bound() -> None:
    params = {"app_key": "ak-1", "offset": "100", "timestamp": "1700000000123"}
    baseline = sign_lazada_request(params, APP_SECRET)
    reordered = dict(reversed(list(params.items())))
    assert sign_lazada_request(reordered, APP_SECRET) == baseline
    assert sign_lazada_request(params, "other-secret") != baseline
    # ``sign`` never signs itself, so re-signing a signed map is stable.
    assert sign_lazada_request({**params, "sign": baseline}, APP_SECRET) == baseline


def test_sign_lazada_request_flattens_decimals_and_booleans() -> None:
    params = {"price": Decimal("12.30"), "notify": True, "note": None}
    assert sign_lazada_request(params, APP_SECRET) == sign_lazada_request(
        {"price": "12.30", "notify": "true", "note": ""}, APP_SECRET
    )


def test_sign_lazada_request_requires_a_secret() -> None:
    with pytest.raises(ValueError, match="app_secret"):
        sign_lazada_request({"app_key": "ak-1"}, "")


# ----------------------------------------------------------------------- webhooks


def test_verify_lazada_webhook_is_constant_time_hmac_over_the_raw_body() -> None:
    raw_body = b'{"order_id":"LAZ-10001","status":"ready_to_ship"}'
    secret = "lazada-push-secret"
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256)
    hex_signature = digest.hexdigest()
    base64_signature = base64.b64encode(digest.digest()).decode("ascii")

    assert verify_lazada_webhook(raw_body, {"x-lazada-signature": hex_signature}, secret)
    assert verify_lazada_webhook(raw_body, {"X-Lazada-Signature": hex_signature.upper()}, secret)
    assert verify_lazada_webhook(raw_body, {"x-lazada-signature": f"  {hex_signature}  "}, secret)
    assert verify_lazada_webhook(raw_body, {"x-lazada-signature": base64_signature}, secret)
    assert verify_lazada_webhook(raw_body, {"Authorization": hex_signature}, secret)

    # Fail closed: no secret, no signature, tampered body, wrong signature, non-ASCII.
    assert not verify_lazada_webhook(raw_body, {"x-lazada-signature": hex_signature}, "")
    assert not verify_lazada_webhook(raw_body, {}, secret)
    assert not verify_lazada_webhook(raw_body, {"x-lazada-signature": ""}, secret)
    assert not verify_lazada_webhook(raw_body + b" ", {"x-lazada-signature": hex_signature}, secret)
    assert not verify_lazada_webhook(raw_body, {"x-lazada-signature": "0" * 64}, secret)
    # The legacy plaintext-secret push style is rejected on purpose.
    assert not verify_lazada_webhook(raw_body, {"Authorization": secret}, secret)
    assert not verify_lazada_webhook(raw_body, {"x-lazada-signature": "签名"}, secret)


def test_adapter_verify_webhook_falls_back_to_the_configured_app_secret() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"code": "0"}))
    raw_body = b'{"order_id":"LAZ-10001"}'
    signature = hmac.new(APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    assert adapter.verify_webhook(raw_body, {"x-lazada-signature": signature}, "")
    assert (
        adapter.verify_webhook(raw_body, {"x-lazada-signature": signature}, "explicit-push-secret")
        is False
    )


# --------------------------------------------------------------- orders ingestion


async def _fetch_orders(adapter: LazadaAdapter) -> tuple[list[Any], str | None]:
    return await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, 0, 0, tzinfo=UTC))


async def test_fetch_orders_normalizes_an_order_with_exact_decimals() -> None:
    seen: list[httpx.Request] = []

    def orders_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_orders_response([_order()]))

    def items_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert _query(request)["order_id"] == "LAZ-10001"
        return httpx.Response(200, json=_items_response())

    adapter = _adapter(
        _route({LAZADA_ORDERS_PATH: orders_handler, LAZADA_ORDER_ITEMS_PATH: items_handler})
    )
    orders, cursor = await _fetch_orders(adapter)

    params = _assert_signed(seen[0], LAZADA_ORDERS_PATH)
    assert params["update_after"] == "2026-11-19T00:00:00+00:00"
    assert params["limit"] == "50"
    assert params["offset"] == "0"
    assert params["sort_by"] == "updated_at"
    assert cursor is None

    assert len(orders) == 1
    order = orders[0]
    assert order.external_order_id == "LAZ-10001"
    assert order.external_order_number == "NUM-LAZ-10001"
    assert order.order_mode is OrderMode.B2C_RETAIL
    assert order.currency == "SGD"
    assert order.fx_rate_to_base is None
    assert order.fulfillment_status is FulfillmentStatus.CONFIRMED_RESERVED
    assert order.payment_status is PaymentStatus.PAID
    assert order.updated_at == datetime(2026, 11, 18, 18, 30, tzinfo=UTC)

    assert order.subtotal_amount == Decimal("120.56")
    assert order.shipping_charged == Decimal("6.00")
    assert order.discount_amount == Decimal("5.50")
    assert order.tax_collected == Decimal("1.20")
    assert order.total_amount == Decimal("122.26")
    assert order.platform_commission_base == Decimal("7.35")
    for amount in (
        order.subtotal_amount,
        order.shipping_charged,
        order.discount_amount,
        order.tax_collected,
        order.total_amount,
        order.platform_commission_base,
    ):
        assert isinstance(amount, Decimal)

    assert order.channel_metadata["lazada_seller_id"] == SELLER_ID
    assert order.channel_metadata["lazada_region"] == "SG"
    assert order.channel_metadata["lazada_order_status"] == "ready_to_ship"
    assert order.channel_metadata["lazada_order_number"] == "NUM-LAZ-10001"
    assert order.channel_metadata["lazada_payment_method"] == "Credit Card"
    assert order.channel_metadata["refunded_amount"] == "3.01"
    assert order.channel_metadata["platform_commission_base"] == "7.35"

    assert order.customer.email == "mei.tan@example.sg"
    assert order.customer.phone_e164 == "+6591234567"
    assert order.customer.external_customer_id == "buyer-7781"
    assert order.customer.display_name == "Mei Tan"
    assert order.customer.country_code == "SG"

    assert [line.external_sku for line in order.line_items] == ["NORA-220-SG", "NORA-CABLE-2M"]
    assert order.line_items[0].quantity == 3
    assert order.line_items[0].line_total == Decimal("120.56")
    assert order.line_items[0].title == "Nordic Pendant Light 220V"
    assert order.line_items[1].line_total == Decimal("20.01")
    assert all(isinstance(line.line_total, Decimal) for line in order.line_items)


async def test_fetch_orders_uses_inline_items_without_a_second_call() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=_orders_response(
                [
                    _order(
                        order_items=[
                            {
                                "order_item_id": "OI-9",
                                "seller_sku": "NORA-INLINE",
                                "name": "Inline item",
                                "item_price": "5.00",
                                "quantity": 2,
                            }
                        ]
                    )
                ]
            ),
        )

    adapter = _adapter(handler)
    orders, _ = await _fetch_orders(adapter)
    assert len(seen) == 1
    assert len(orders[0].line_items) == 1
    assert orders[0].line_items[0].line_total == Decimal("10.00")


@pytest.mark.parametrize(
    ("lazada_status", "expected"),
    [
        ("pending", FulfillmentStatus.PENDING_PAYMENT),
        ("processing", FulfillmentStatus.CONFIRMED_RESERVED),
        ("ready_to_ship", FulfillmentStatus.CONFIRMED_RESERVED),
        ("ready_to_pick", FulfillmentStatus.CONFIRMED_RESERVED),
        ("shipped", FulfillmentStatus.SHIPPED),
        ("delivered", FulfillmentStatus.DELIVERED),
        ("returned", FulfillmentStatus.REFUNDED),
        ("refunded", FulfillmentStatus.REFUNDED),
        ("canceled", FulfillmentStatus.CANCELLED),
        ("failed", FulfillmentStatus.EXCEPTION),
        ("some_future_status", FulfillmentStatus.CONFIRMED_RESERVED),
    ],
)
async def test_fetch_orders_maps_lazada_statuses(
    lazada_status: str, expected: FulfillmentStatus
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(LAZADA_ORDER_ITEMS_PATH):
            return httpx.Response(200, json=_items_response())
        return httpx.Response(200, json=_orders_response([_order(status=lazada_status)]))

    adapter = _adapter(handler)
    orders, _ = await _fetch_orders(adapter)
    assert orders[0].fulfillment_status is expected


async def test_fetch_orders_reads_the_statuses_list_when_status_is_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(LAZADA_ORDER_ITEMS_PATH):
            return httpx.Response(200, json=_items_response())
        order = _order()
        order.pop("status")
        order["statuses"] = ["shipped"]
        return httpx.Response(200, json=_orders_response([order]))

    adapter = _adapter(handler)
    orders, _ = await _fetch_orders(adapter)
    assert orders[0].fulfillment_status is FulfillmentStatus.SHIPPED
    assert orders[0].channel_metadata["lazada_order_status"] == "shipped"


@pytest.mark.parametrize(
    ("lazada_status", "extra", "expected"),
    [
        ("pending", {"payment_method": ""}, PaymentStatus.UNPAID),
        ("canceled", {"payment_method": ""}, PaymentStatus.UNPAID),
        ("canceled", {"payment_method": "Credit Card"}, PaymentStatus.REFUNDED),
        ("cancelled", {"paid_at": "2026-11-18T10:00:00+08:00"}, PaymentStatus.REFUNDED),
        ("returned", {"payment_method": "Credit Card"}, PaymentStatus.REFUNDED),
        ("refunded", {}, PaymentStatus.REFUNDED),
        ("delivered", {"refund_amount": "122.26"}, PaymentStatus.REFUNDED),
        ("delivered", {}, PaymentStatus.PAID),
    ],
)
async def test_fetch_orders_payment_states(
    lazada_status: str, extra: Mapping[str, Any], expected: PaymentStatus
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(LAZADA_ORDER_ITEMS_PATH):
            return httpx.Response(200, json=_items_response())
        return httpx.Response(
            200, json=_orders_response([_order(status=lazada_status, **dict(extra))])
        )

    adapter = _adapter(handler)
    orders, _ = await _fetch_orders(adapter)
    assert orders[0].payment_status is expected


async def test_fetch_orders_cursor_advances_on_a_full_page_and_stops_when_short() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith(LAZADA_ORDER_ITEMS_PATH):
            return httpx.Response(200, json=_items_response())
        offset = _query(request)["offset"]
        rows = [_order(order_id=f"LAZ-{offset}-{index}") for index in range(50)]
        return httpx.Response(200, json=_orders_response(rows))

    adapter = _adapter(handler)
    _, first_cursor = await _fetch_orders(adapter)
    assert first_cursor == "50"

    _, second_cursor = await adapter.fetch_orders_updated_since(
        datetime(2026, 11, 19, tzinfo=UTC), first_cursor
    )
    order_pages = [request for request in seen if request.url.path.endswith(LAZADA_ORDERS_PATH)]
    assert [_query(request)["offset"] for request in order_pages] == ["0", "50"]

    # A short page ends the window.
    def short_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(LAZADA_ORDER_ITEMS_PATH):
            return httpx.Response(200, json=_items_response())
        return httpx.Response(200, json=_orders_response([_order()]))

    short_adapter = _adapter(short_handler)
    _, cursor = await short_adapter.fetch_orders_updated_since(
        datetime(2026, 11, 19, tzinfo=UTC), second_cursor
    )
    assert cursor is None


async def test_fetch_orders_rejects_a_non_numeric_cursor() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_orders_response([])))
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC), "next")
    assert excinfo.value.slug == "lazada-invalid-cursor"
    assert excinfo.value.code == "ERR_LAZADA_INVALID_CURSOR"
    assert excinfo.value.status_code == 422


async def test_fetch_orders_skips_an_order_without_a_mappable_line_item() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(LAZADA_ORDER_ITEMS_PATH):
            return httpx.Response(200, json={"code": "0", "data": []})
        return httpx.Response(200, json=_orders_response([_order()]))

    adapter = _adapter(handler)
    orders, cursor = await _fetch_orders(adapter)
    assert orders == []
    assert cursor is None


async def test_fetch_orders_parses_epoch_and_blank_timestamps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(LAZADA_ORDER_ITEMS_PATH):
            return httpx.Response(200, json=_items_response())
        return httpx.Response(
            200,
            json=_orders_response(
                [
                    _order(order_id="LAZ-EPOCH", updated_at=1763538600000),
                    _order(order_id="LAZ-BLANK", updated_at=""),
                ]
            ),
        )

    adapter = _adapter(handler)
    since = datetime(2026, 11, 19, 0, 0, tzinfo=UTC)
    orders, _ = await adapter.fetch_orders_updated_since(since)
    by_id = {order.external_order_id: order for order in orders}
    assert by_id["LAZ-EPOCH"].updated_at == datetime(2025, 11, 19, 7, 50, tzinfo=UTC)
    assert by_id["LAZ-BLANK"].updated_at == since


# ------------------------------------------------------------- products ingestion


async def test_fetch_products_normalizes_variants_and_cursor() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": {
                    "count": 2,
                    "products": [
                        {
                            "item_id": "123456789",
                            "attributes": {"name": "Nordic Pendant Light 220V"},
                            "skus": [
                                {
                                    "SkuId": "sku-1",
                                    "SellerSku": "NORA-220-SG",
                                    "price": "12.345",
                                    "quantity": 42,
                                },
                                {"SellerSku": "NORA-220-MY", "price": "11.20", "quantity": -3},
                            ],
                        },
                        {"attributes": {"name": "Product without an item_id"}},
                    ],
                },
            },
        )

    adapter = _adapter(handler)
    products, cursor = await adapter.fetch_products_updated_since(
        datetime(2026, 11, 19, 3, 15, tzinfo=UTC)
    )

    params = _assert_signed(seen[0], LAZADA_PRODUCTS_PATH)
    assert params["update_after"] == "2026-11-19T03:15:00+00:00"
    assert cursor is None
    assert len(products) == 1
    product = products[0]
    assert product.external_product_id == "123456789"
    assert product.title == "Nordic Pendant Light 220V"
    assert product.variants == [
        {
            "external_sku": "NORA-220-SG",
            "external_variant_id": "sku-1",
            "unit_price_usd": "12.35",
            "stock_quantity": 42,
        },
        {
            "external_sku": "NORA-220-MY",
            "external_variant_id": None,
            "unit_price_usd": "11.20",
            "stock_quantity": 0,
        },
    ]


# ----------------------------------------------------------------------- shipping


async def test_create_fulfillment_sends_carrier_and_tracking() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": {"order_id": "LAZ-10001", "package_id": "PKG-77", "accepted": True},
            },
        )

    adapter = _adapter(handler)
    result = await adapter.create_fulfillment(
        external_order_id="LAZ-10001",
        tracking_number="TRK-999-SG",
        carrier_company="Ninja Van",
        tracking_url="https://track.test/TRK-999-SG",
        fulfillment_order_id="PKG-77",
        notify_customer=True,
    )

    params = _assert_signed(seen[0], LAZADA_ORDERS_FULFILL_PATH)
    assert seen[0].method == "POST"
    assert params["order_id"] == "LAZ-10001"
    assert params["delivery_type"] == "dropship"
    assert params["shipping_provider"] == "Ninja Van"
    assert params["tracking_number"] == "TRK-999-SG"
    assert params["tracking_url"] == "https://track.test/TRK-999-SG"
    assert params["package_id"] == "PKG-77"
    assert result == {
        "order_id": "LAZ-10001",
        "package_id": "PKG-77",
        "accepted": True,
        "fulfillment_id": "PKG-77",
    }


async def test_create_fulfillment_maps_the_configured_fulfillment_mode() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": "0", "data": {}})

    adapter = _adapter(handler, fulfillment_mode="full_managed")
    result = await adapter.create_fulfillment(
        external_order_id="LAZ-10001",
        tracking_number="TRK-1",
        carrier_company="Own Fleet",
    )
    params = _query(seen[0])
    assert params["delivery_type"] == "dropship"
    assert result["fulfillment_id"] == "LAZ-10001"

    seen.clear()
    adapter = _adapter(handler, fulfillment_mode="self_delivery")
    await adapter.create_fulfillment(
        external_order_id="LAZ-10001",
        tracking_number="TRK-1",
        carrier_company="Own Fleet",
    )
    params = _query(seen[0])
    assert params["delivery_type"] == "send_delivery"
    assert "package_id" not in params
    assert "tracking_url" not in params


async def test_create_fulfillment_rejects_an_empty_order_id() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"code": "0"}))
    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment(
            external_order_id="  ",
            tracking_number="TRK-1",
            carrier_company="Ninja Van",
        )
    assert excinfo.value.code == "ERR_LAZADA_FULFILLMENT_REJECTED"
    assert excinfo.value.status_code == 422


# ---------------------------------------------------------------- publish listing


async def test_publish_listing_multi_variant_body_and_receipt() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": {
                    "item_id": "6601999001",
                    "skus": [
                        {"seller_sku": "NORA-220-BLK", "sku_id": "sku-blk"},
                        {"seller_sku": "NORA-220-WHT", "sku_id": "sku-wht"},
                    ],
                },
            },
        )

    adapter = _adapter(handler)
    payload = _listing_payload(
        external_sku="NORA-220",
        hs_code="9405.10",
        images=[
            "https://cdn.test/nora-1.jpg",
            "http://cdn.test/insecure.jpg",
            "https://cdn.test/nora-1.jpg",
        ],
        options=[SourcingOptionSpec(name="Color", values=["Black", "White"])],
        variants=[
            MarketplaceVariantPayload(
                local_sku="NORA-220-BLK",
                external_sku="NORA-220-BLK",
                option_values={"Color": "Black"},
                price_usd=Decimal("12.345"),
                quantity=40,
                image_url="https://cdn.test/nora-blk.jpg",
            ),
            MarketplaceVariantPayload(
                local_sku="NORA-220-WHT",
                external_sku="NORA-220-WHT",
                option_values={"Color": "White"},
                price_usd=Decimal("13.5"),
                quantity=-2,
            ),
        ],
    )

    receipt = await adapter.publish_listing(payload)

    request = seen[0]
    _assert_signed(request, LAZADA_PRODUCT_CREATE_PATH)
    assert request.method == "POST"
    product = _body(request)["Request"]["Product"]
    assert product["PrimaryCategory"] == DEFAULT_LAZADA_PRIMARY_CATEGORY
    assert product["Images"] == ["https://cdn.test/nora-1.jpg"]
    assert product["Attributes"]["name"] == "Nordic Pendant Light 220V"
    assert product["Attributes"]["HSCode"] == "9405.10"
    assert product["Attributes"]["description"] == (
        "Hand-finished aluminium pendant light.\n\n- 5-year warranty\n- CE certified"
    )
    assert product["Skus"] == [
        {
            "SellerSku": "NORA-220-BLK",
            "Price": "12.35",
            "Quantity": "40",
            "Images": ["https://cdn.test/nora-blk.jpg"],
            "VariantAttributes": [{"name": "Color", "value": "Black"}],
        },
        {
            "SellerSku": "NORA-220-WHT",
            "Price": "13.50",
            "Quantity": "0",
            "Images": ["https://cdn.test/nora-1.jpg"],
            "VariantAttributes": [{"name": "Color", "value": "White"}],
        },
    ]
    assert isinstance(receipt, MarketplaceListingDispatchReceipt)
    assert receipt.external_product_id == "6601999001"
    assert receipt.external_sku == "NORA-220"
    assert receipt.status == "PUBLISHED"
    assert [(r.external_sku, r.external_variant_id) for r in receipt.variant_receipts] == [
        ("NORA-220-BLK", "sku-blk"),
        ("NORA-220-WHT", "sku-wht"),
    ]
    assert [r.external_inventory_item_id for r in receipt.variant_receipts] == [
        "sku-blk",
        "sku-wht",
    ]
    assert receipt.raw_response["data"]["item_id"] == "6601999001"


async def test_publish_listing_single_sku_uses_a_numeric_category_verbatim() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": "0", "data": {"item_id": "660100000001"}})

    adapter = _adapter(handler)
    receipt = await adapter.publish_listing(
        _listing_payload(
            category="10000348",
            images=[],
            variants=[],
            price_usd=Decimal("0"),
            quantity=0,
            hs_code=None,
        )
    )

    product = _body(seen[0])["Request"]["Product"]
    assert product["PrimaryCategory"] == "10000348"
    assert "Images" not in product
    assert product["Skus"] == [{"SellerSku": "NORA-220-SG", "Price": "0.00", "Quantity": "0"}]
    assert "HSCode" not in product["Attributes"]
    assert receipt.external_product_id == "660100000001"
    assert receipt.variant_receipts == []


async def test_publish_listing_requires_a_sku_and_title() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"code": "0"}))
    for overrides in ({"external_sku": "  "}, {"title": ""}):
        with pytest.raises(AppError) as excinfo:
            await adapter.publish_listing(_listing_payload(**overrides))
        assert excinfo.value.slug == "lazada-listing-rejected"
        assert excinfo.value.code == "ERR_LAZADA_LISTING_REJECTED"
        assert excinfo.value.status_code == 422


async def test_publish_listing_rejects_options_without_variants() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"code": "0"}))
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(
            _listing_payload(
                options=[SourcingOptionSpec(name="Color", values=["Black"])],
                variants=[],
            )
        )
    assert excinfo.value.code == "ERR_LAZADA_LISTING_REJECTED"
    assert excinfo.value.status_code == 422


async def test_publish_listing_rejects_raw_image_blobs_without_https_urls() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"code": "0"}))
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(
            _listing_payload(
                images=[],
                image_blobs=[
                    MarketplaceImageBlob(
                        filename="main.jpg", mime_type="image/jpeg", data=b"\xff\xd8"
                    )
                ],
            )
        )
    assert excinfo.value.code == "ERR_LAZADA_LISTING_REJECTED"
    assert excinfo.value.status_code == 422


async def test_publish_listing_falls_back_to_https_images_when_blobs_are_present() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": "0", "data": {"item_id": "6601999002"}})

    adapter = _adapter(handler)
    receipt = await adapter.publish_listing(
        _listing_payload(
            image_blobs=[
                MarketplaceImageBlob(
                    filename="main.jpg", mime_type="image/jpeg", data=b"\xff\xd8"
                )
            ]
        )
    )
    assert receipt.external_product_id == "6601999002"
    product = _body(seen[0])["Request"]["Product"]
    assert product["Images"] == ["https://cdn.test/nora-1.jpg", "https://cdn.test/nora-2.jpg"]


async def test_publish_listing_rejects_a_variant_missing_a_declared_option() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"code": "0"}))
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(
            _listing_payload(
                options=[SourcingOptionSpec(name="Color", values=["Black", "White"])],
                variants=[
                    MarketplaceVariantPayload(
                        local_sku="NORA-220-BLK",
                        external_sku="NORA-220-BLK",
                        option_values={"Color": "Black"},
                        price_usd=Decimal("12.35"),
                        quantity=5,
                    ),
                    MarketplaceVariantPayload(
                        local_sku="NORA-220-WHT",
                        external_sku="NORA-220-WHT",
                        option_values={},
                        price_usd=Decimal("12.35"),
                        quantity=5,
                    ),
                ],
            )
        )
    assert excinfo.value.code == "ERR_LAZADA_LISTING_REJECTED"
    assert "NORA-220-WHT" in excinfo.value.detail
    assert "Color" in excinfo.value.detail


async def test_publish_listing_without_item_id_is_rejected_without_leaking_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "0", "data": {"skus": []}})

    adapter = _adapter(handler)
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(_listing_payload())
    error = excinfo.value
    assert error.slug == "lazada-listing-rejected"
    assert error.code == "ERR_LAZADA_LISTING_REJECTED"
    assert error.status_code == 502
    assert APP_SECRET not in error.detail
    assert ACCESS_TOKEN not in error.detail


async def test_publish_listing_remote_error_is_a_typed_app_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "901",
                "message": f"category invalid for {APP_SECRET} with {ACCESS_TOKEN}",
            },
        )

    adapter = _adapter(handler)
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(_listing_payload())
    error = excinfo.value
    assert error.code == "ERR_LAZADA_API_ERROR"
    assert error.status_code == 502
    assert APP_SECRET not in error.detail
    assert ACCESS_TOKEN not in error.detail
    assert "[REDACTED]" in error.detail


# --------------------------------------------------------------- inventory pushes


async def test_push_inventory_level_sends_an_absolute_quantity() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"code": "0", "data": {"skus": [{"SellerSku": "NORA-220-SG"}]}},
        )

    adapter = _adapter(handler)
    result = await adapter.push_inventory_level(
        external_sku=" NORA-220-SG ",
        quantity=125,
        external_product_id="6601999001",
        external_variant_id="sku-1",
        external_inventory_item_id="sku-1",
        location_id="loc-sg",
        warehouse_id=None,
        ignored_extra="dropped",
    )

    request = seen[0]
    _assert_signed(request, LAZADA_PRICE_QUANTITY_PATH)
    assert request.method == "POST"
    assert _body(request) == {
        "skus": [{"SellerSku": "NORA-220-SG", "Quantity": 125, "SkuId": "sku-1"}]
    }
    assert result == {
        "channel": "lazada",
        "external_sku": "NORA-220-SG",
        "quantity": 125,
        "product_id": "6601999001",
        "variant_id": "sku-1",
        "external_inventory_item_id": "sku-1",
        "location_id": "loc-sg",
        "warehouse_id": None,
        "raw": {"skus": [{"SellerSku": "NORA-220-SG"}]},
    }


async def test_push_inventory_level_clamps_negative_quantity_to_zero() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": "0", "data": {}})

    adapter = _adapter(handler)
    result = await adapter.push_inventory_level(external_sku="NORA-220-SG", quantity=-5)
    assert _body(seen[0]) == {"skus": [{"SellerSku": "NORA-220-SG", "Quantity": 0}]}
    assert result["quantity"] == 0
    assert result["variant_id"] is None
    assert result["product_id"] is None


async def test_push_inventory_level_rejects_an_empty_sku() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"code": "0"}))
    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(external_sku="   ", quantity=1)
    assert excinfo.value.slug == "lazada-inventory-sync-failed"
    assert excinfo.value.code == "ERR_LAZADA_INVENTORY_SYNC"
    assert excinfo.value.status_code == 422


# ------------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    ("status_code", "payload", "slug", "code", "expected_status"),
    [
        (
            200,
            {"code": "InvalidSignature", "message": f"bad signature for {APP_SECRET}"},
            "lazada-auth-failed",
            "ERR_LAZADA_AUTH_FAILED",
            502,
        ),
        (401, {}, "lazada-auth-failed", "ERR_LAZADA_AUTH_FAILED", 502),
        (
            403,
            {"code": "InsufficientPermission", "message": "no access"},
            "lazada-auth-failed",
            "ERR_LAZADA_AUTH_FAILED",
            502,
        ),
        (
            200,
            {"code": "Throttling", "message": "flow control triggered"},
            "lazada-rate-limited",
            "ERR_LAZADA_RATE_LIMITED",
            429,
        ),
        (429, {}, "lazada-rate-limited", "ERR_LAZADA_RATE_LIMITED", 429),
        (
            200,
            {"code": "901", "message": "unexpected upstream failure"},
            "lazada-api-error",
            "ERR_LAZADA_API_ERROR",
            502,
        ),
        (500, {}, "lazada-api-error", "ERR_LAZADA_API_ERROR", 502),
    ],
)
async def test_gateway_failures_map_to_typed_app_errors(
    status_code: int,
    payload: dict[str, Any],
    slug: str,
    code: str,
    expected_status: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    adapter = _adapter(handler)
    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(external_sku="NORA-220-SG", quantity=5)

    error = excinfo.value
    assert error.slug == slug
    assert error.code == code
    assert error.status_code == expected_status
    assert APP_SECRET not in error.detail
    assert ACCESS_TOKEN not in error.detail
    assert APP_SECRET not in str(error)


async def test_transport_failure_maps_to_a_retryable_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed for {APP_KEY}", request=request)

    adapter = _adapter(handler)
    with pytest.raises(AppError) as excinfo:
        await _fetch_orders(adapter)

    error = excinfo.value
    assert error.code == "ERR_LAZADA_API_ERROR"
    assert error.status_code == 502
    assert APP_SECRET not in error.detail
    assert ACCESS_TOKEN not in error.detail
    assert APP_SECRET not in str(error)


async def test_malformed_body_is_not_reported_as_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>upstream proxy error</html>")

    adapter = _adapter(handler)
    with pytest.raises(AppError) as excinfo:
        await _fetch_orders(adapter)
    assert excinfo.value.code == "ERR_LAZADA_API_ERROR"
