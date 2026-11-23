"""Zero-network unit tests for the SHEIN Open Platform adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
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
from erp_core.channels.ecommerce.base import CommercePlatformAdapter
from erp_core.channels.ecommerce.shein import (
    SHEIN_DEFAULT_ENDPOINT,
    SHEIN_INVENTORY_UPDATE_PATH,
    SHEIN_ORDER_DETAIL_PATH,
    SHEIN_ORDER_LIST_PATH,
    SHEIN_ORDER_SHIP_PATH,
    SHEIN_PRODUCT_CREATE_PATH,
    SHEIN_PRODUCT_IMAGE_PATH,
    SHEIN_PRODUCT_LIST_PATH,
    SheinAdapter,
    sign_shein_request,
    verify_shein_webhook,
)
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus
from erp_core.shared.errors import AppError

SELLER_ID = "shein-seller-778001"
APP_ID = "test-open-key-id"
SECRET = "test-secret-key-value"
ENDPOINT = "https://openapi.shein.test"

Handler = Callable[[httpx.Request], httpx.Response]


def _adapter(handler: Handler, **overrides: Any) -> SheinAdapter:
    options: dict[str, Any] = {
        "seller_id": SELLER_ID,
        "open_key_id": APP_ID,
        "secret_key": SECRET,
        "endpoint_url": ENDPOINT,
        "transport": httpx.MockTransport(handler),
        "timeout_seconds": 5.0,
    }
    options.update(overrides)
    return SheinAdapter(**options)


def _assert_signed(request: httpx.Request, api_path: str, *, json_body: bool = True) -> Any:
    """Recompute the SHEIN signature from the wire bytes and pin transport hygiene."""
    assert request.url.path == api_path
    raw_bytes = request.content or b""
    raw_body = raw_bytes.decode("utf-8") if raw_bytes and json_body else ""
    assert SECRET not in raw_body
    timestamp = request.headers["x-lt-timestamp"]
    assert len(timestamp) == 13 and timestamp.isdigit()
    assert request.headers["x-lt-appid"] == APP_ID
    assert request.headers["x-lt-signature"] == sign_shein_request(
        app_id=APP_ID,
        secret_key=SECRET,
        api_path=api_path,
        timestamp=timestamp,
        body_str=raw_body,
    )
    if not raw_body or not json_body:
        return None
    return json.loads(raw_body)


def _order(order_no: str = "GSO20261119001", status: str = "2", **overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "orderNo": order_no,
        "orderStatus": status,
        "orderCurrency": "USD",
        "goodsAmount": "1250.555",
        "shippingFee": "40.00",
        "discountAmount": "50.00",
        "taxAmount": "0.00",
        "totalAmount": "1240.555",
        "platformCommission": "62.525",
        "updateTime": "2026-11-19T02:30:00Z",
        "buyerEmail": "ops@nordiclights.se",
        "buyerPhone": "+46701234567",
        "buyerId": "buyer-778001",
        "buyerName": "Nordic Buyer",
        "countryCode": "SE",
        "goodsList": [
            {
                "skuCode": "NORA-220-SHEIN",
                "goodsTitle": "Nordic Pendant Light 220V",
                "quantity": "100",
                "unitPrice": "12.5055",
                "lineTotal": "1250.555",
            }
        ],
    }
    order.update(overrides)
    return order


def _envelope(**info: Any) -> dict[str, Any]:
    return {"code": "0", "msg": "OK", "info": info}


def _order_list_payload(orders: list[dict[str, Any]], **info_overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {"orderList": orders, "total": 60}
    info.update(info_overrides)
    return _envelope(**info)


def _listing_payload(**overrides: Any) -> MarketplaceListingPayload:
    data: dict[str, Any] = {
        "channel_account_id": uuid4(),
        "channel_type": "shein",
        "external_sku": "NORA-220-SHEIN",
        "title": "Nordic Pendant Light 220V",
        "description": "Hand-finished aluminium pendant light for contract projects.",
        "bullet_points": ["5-year warranty", "CE certified"],
        "price_usd": Decimal("12.345"),
        "quantity": 25,
        "images": ["https://cdn.test/nora-1.jpg"],
        "category": "Lighting > Pendant Lights",
    }
    data.update(overrides)
    return MarketplaceListingPayload(**data)


# --------------------------------------------------------------------- signing helper


def test_sign_shein_request_matches_canonical_hmac_sha256() -> None:
    signature = sign_shein_request(
        app_id=APP_ID,
        secret_key=SECRET,
        api_path=SHEIN_ORDER_LIST_PATH,
        timestamp="1700000000000",
        body_str='{"page":1}',
    )

    canonical = f'{APP_ID}1700000000000{SHEIN_ORDER_LIST_PATH}{{"page":1}}'
    expected = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert len(signature) == 64
    assert signature == signature.lower()


def test_sign_shein_request_binds_path_and_secret() -> None:
    baseline = sign_shein_request(
        app_id=APP_ID,
        secret_key=SECRET,
        api_path=SHEIN_ORDER_LIST_PATH,
        timestamp="1700000000000",
    )
    assert (
        sign_shein_request(
            app_id=APP_ID,
            secret_key=SECRET,
            api_path=SHEIN_ORDER_DETAIL_PATH,
            timestamp="1700000000000",
        )
        != baseline
    )
    assert (
        sign_shein_request(
            app_id="another-app",
            secret_key=SECRET,
            api_path=SHEIN_ORDER_LIST_PATH,
            timestamp="1700000000000",
        )
        != baseline
    )
    assert (
        sign_shein_request(
            app_id=APP_ID,
            secret_key="another-secret",
            api_path=SHEIN_ORDER_LIST_PATH,
            timestamp="1700000000000",
        )
        != baseline
    )


@pytest.mark.parametrize(("app_id", "secret"), [("", SECRET), (APP_ID, "")])
def test_sign_shein_request_requires_credentials(app_id: str, secret: str) -> None:
    with pytest.raises(ValueError, match="required"):
        sign_shein_request(
            app_id=app_id, secret_key=secret, api_path=SHEIN_ORDER_LIST_PATH, timestamp="1"
        )


# ------------------------------------------------------------------ construction


def test_adapter_is_a_commerce_platform_adapter() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_envelope()))
    assert isinstance(adapter, CommercePlatformAdapter)
    assert adapter.fulfillment_mode == "standard"
    assert adapter.region == "US"
    assert adapter.endpoint_url == ENDPOINT


@pytest.mark.parametrize(
    "overrides",
    [
        {"seller_id": ""},
        {"open_key_id": "  "},
        {"secret_key": ""},
    ],
)
def test_adapter_refuses_to_construct_without_credentials(overrides: Mapping[str, Any]) -> None:
    with pytest.raises(ValueError, match="seller_id"):
        _adapter(lambda request: httpx.Response(200, json={}), **dict(overrides))


def test_adapter_rejects_an_unknown_fulfillment_mode() -> None:
    with pytest.raises(ValueError, match="fulfillment_mode"):
        _adapter(lambda request: httpx.Response(200, json={}), fulfillment_mode="dropship")


def test_adapter_normalizes_mode_case_and_repr_redacts_the_secret() -> None:
    adapter = SheinAdapter(
        seller_id=SELLER_ID,
        open_key_id=APP_ID,
        secret_key=SECRET,
        fulfillment_mode="SEMI_MANAGED",
        region="eu",
    )
    assert adapter.fulfillment_mode == "semi_managed"
    assert adapter.region == "EU"
    assert adapter.endpoint_url == "https://openapi.sheincorp.eu"
    assert (
        SheinAdapter(
            seller_id=SELLER_ID,
            open_key_id=APP_ID,
            secret_key=SECRET,
            region="ZZ",
        ).endpoint_url
        == SHEIN_DEFAULT_ENDPOINT
    )

    representation = repr(adapter)
    assert SECRET not in representation
    assert "[REDACTED]" in representation
    assert SELLER_ID in representation


# ------------------------------------------------------------------ orders ingestion


async def test_fetch_orders_normalizes_decimals_and_financial_metadata() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _assert_signed(request, SHEIN_ORDER_LIST_PATH)
        return httpx.Response(200, json=_order_list_payload([_order()]))

    adapter = _adapter(handle)
    orders, cursor = await adapter.fetch_orders_updated_since(
        datetime(2026, 11, 19, 0, 0, tzinfo=UTC)
    )

    body = _assert_signed(seen[0], SHEIN_ORDER_LIST_PATH)
    assert body["queryType"] == 2
    assert body["startTime"] == int(datetime(2026, 11, 19, tzinfo=UTC).timestamp() * 1000)
    assert body["page"] == 1
    assert body["pageSize"] == 50
    assert cursor == "2"

    assert len(orders) == 1
    order = orders[0]
    assert order.external_order_id == "GSO20261119001"
    assert order.external_order_number == "GSO20261119001"
    assert order.order_mode is OrderMode.B2C_RETAIL
    assert order.currency == "USD"
    assert order.fx_rate_to_base is None
    assert order.fulfillment_status is FulfillmentStatus.CONFIRMED_RESERVED
    assert order.payment_status is PaymentStatus.PAID
    assert order.updated_at == datetime(2026, 11, 19, 2, 30, tzinfo=UTC)

    assert order.subtotal_amount == Decimal("1250.56")
    assert order.shipping_charged == Decimal("40.00")
    assert order.discount_amount == Decimal("50.00")
    assert order.tax_collected == Decimal("0.00")
    assert order.total_amount == Decimal("1240.56")
    assert order.platform_commission_base == Decimal("62.53")
    for amount in (
        order.subtotal_amount,
        order.shipping_charged,
        order.discount_amount,
        order.tax_collected,
        order.total_amount,
        order.platform_commission_base,
    ):
        assert isinstance(amount, Decimal)
        assert amount.as_tuple().exponent == -2

    assert order.channel_metadata["platform_commission_base"] == "62.53"
    assert "refunded_amount" not in order.channel_metadata
    assert order.channel_metadata["shein_order_status"] == "2"
    assert order.channel_metadata["shein_region"] == "US"
    assert order.channel_metadata["shein_fulfillment_mode"] == "standard"
    assert order.channel_metadata["shein_seller_id"] == SELLER_ID

    assert order.customer.email == "ops@nordiclights.se"
    assert order.customer.phone_e164 == "+46701234567"
    assert order.customer.external_customer_id == "buyer-778001"
    assert order.customer.display_name == "Nordic Buyer"
    assert order.customer.country_code == "SE"

    assert len(order.line_items) == 1
    line = order.line_items[0]
    assert line.external_sku == "NORA-220-SHEIN"
    assert line.title == "Nordic Pendant Light 220V"
    assert line.quantity == 100
    assert line.line_total == Decimal("1250.56")
    assert isinstance(line.line_total, Decimal)


async def test_fetch_orders_records_refunds_and_derives_totals_from_lines() -> None:
    order = _order(refundAmount="15.005")
    order.pop("goodsAmount")
    order.pop("totalAmount")
    order.pop("taxAmount")
    order.pop("discountAmount")
    order.pop("shippingFee")
    order["goodsList"] = [
        {
            "skuCode": "NORA-220-SHEIN",
            "quantity": "3",
            "unitPrice": "10.005",
            "goodsTitle": "Nordic Pendant Light 220V",
        },
        {
            "skuCode": "NORA-CABLE-2M",
            "quantity": "2",
            "unitPrice": "2.333",
            "lineTotal": "4.666",
            "goodsTitle": "Nordic Cable 2m",
        },
    ]

    adapter = _adapter(lambda request: httpx.Response(200, json=_order_list_payload([order])))
    orders, _ = await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))

    normalized = orders[0]
    assert normalized.line_items[0].line_total == Decimal("30.02")
    assert normalized.line_items[1].line_total == Decimal("4.67")
    assert normalized.subtotal_amount == Decimal("34.69")
    assert normalized.total_amount == Decimal("34.69")
    assert normalized.channel_metadata["refunded_amount"] == "15.01"
    assert isinstance(normalized.total_amount, Decimal)


@pytest.mark.parametrize(
    ("shein_status", "expected"),
    [
        ("1", FulfillmentStatus.PENDING_PAYMENT),
        ("2", FulfillmentStatus.CONFIRMED_RESERVED),
        ("3", FulfillmentStatus.SHIPPED),
        ("4", FulfillmentStatus.DELIVERED),
        ("5", FulfillmentStatus.CANCELLED),
        ("6", FulfillmentStatus.REFUNDED),
        ("7", FulfillmentStatus.CONFIRMED_RESERVED),
        ("shipped", FulfillmentStatus.SHIPPED),
        ("some_future_status", FulfillmentStatus.CONFIRMED_RESERVED),
    ],
)
async def test_fetch_orders_maps_shein_statuses(
    shein_status: str, expected: FulfillmentStatus
) -> None:
    adapter = _adapter(
        lambda request: httpx.Response(200, json=_order_list_payload([_order(status=shein_status)]))
    )
    orders, _ = await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))
    assert orders[0].fulfillment_status is expected


async def test_fetch_orders_maps_payment_states() -> None:
    payload = _order_list_payload(
        [
            _order(order_no="GSO-UNPAID", status="1"),
            _order(order_no="GSO-PAID", status="3"),
            _order(order_no="GSO-CANCELLED", status="5"),
            _order(order_no="GSO-REFUNDED", status="6"),
            _order(order_no="GSO-PARTIAL-REFUND", status="4", refundAmount="9.99"),
        ]
    )
    adapter = _adapter(lambda request: httpx.Response(200, json=payload))
    orders, _ = await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))
    by_id = {order.external_order_id: order for order in orders}
    assert by_id["GSO-UNPAID"].payment_status is PaymentStatus.UNPAID
    assert by_id["GSO-PAID"].payment_status is PaymentStatus.PAID
    assert by_id["GSO-CANCELLED"].payment_status is PaymentStatus.UNPAID
    assert by_id["GSO-REFUNDED"].payment_status is PaymentStatus.REFUNDED
    assert by_id["GSO-PARTIAL-REFUND"].payment_status is PaymentStatus.REFUNDED


async def test_fetch_orders_uses_order_detail_when_the_list_has_no_lines() -> None:
    seen: list[httpx.Request] = []
    header_only = {
        "orderNo": "GSO20261119009",
        "orderStatus": "2",
        "orderCurrency": "USD",
        "updateTime": "2026-11-19T04:00:00Z",
    }
    detail = _order(order_no="GSO20261119009", totalAmount="99.999", goodsAmount="99.995")

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == SHEIN_ORDER_DETAIL_PATH:
            _assert_signed(request, SHEIN_ORDER_DETAIL_PATH)
            assert request.url.params["orderNo"] == "GSO20261119009"
            return httpx.Response(200, json=_envelope(orderDetail=detail))
        _assert_signed(request, SHEIN_ORDER_LIST_PATH)
        return httpx.Response(200, json=_order_list_payload([header_only]))

    adapter = _adapter(handle)
    orders, _ = await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))

    assert [request.url.path for request in seen] == [
        SHEIN_ORDER_LIST_PATH,
        SHEIN_ORDER_DETAIL_PATH,
    ]
    assert len(orders) == 1
    assert orders[0].external_order_id == "GSO20261119009"
    assert orders[0].subtotal_amount == Decimal("100.00")
    assert orders[0].total_amount == Decimal("100.00")
    assert orders[0].line_items[0].external_sku == "NORA-220-SHEIN"


async def test_fetch_orders_skips_orders_without_a_resolvable_sku() -> None:
    payload = _order_list_payload(
        [
            {
                "orderNo": "GSO-EMPTY",
                "orderStatus": "2",
                "goodsList": [{"quantity": "1", "unitPrice": "5.00"}],
            },
            _order(order_no="GSO-KEPT"),
        ]
    )
    adapter = _adapter(lambda request: httpx.Response(200, json=payload))
    orders, _ = await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))
    assert [order.external_order_id for order in orders] == ["GSO-KEPT"]


async def test_fetch_orders_pagination_uses_has_next_and_total() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = _assert_signed(request, SHEIN_ORDER_LIST_PATH)["page"]
        if page == 1:
            return httpx.Response(200, json=_order_list_payload([_order()], hasNext=True, total=60))
        return httpx.Response(200, json=_order_list_payload([_order()], total=60))

    adapter = _adapter(handle)
    since = datetime(2026, 11, 19, tzinfo=UTC)
    _, first = await adapter.fetch_orders_updated_since(since)
    _, second = await adapter.fetch_orders_updated_since(since, first)
    _, third = await adapter.fetch_orders_updated_since(since, "2")

    assert first == "2"
    assert second is None
    assert third is None
    assert [request.url.params.get("page") for request in seen] == [None, None, None]

    # total=50 with no hasNext flag means page 1 is the whole window.
    adapter_single = _adapter(
        lambda request: httpx.Response(200, json=_order_list_payload([_order()], total=50))
    )
    _, cursor = await adapter_single.fetch_orders_updated_since(since)
    assert cursor is None


async def test_fetch_orders_advances_by_page_size_when_totals_are_absent() -> None:
    full_page = [_order(order_no=f"GSO-{index}") for index in range(50)]
    adapter = _adapter(lambda request: httpx.Response(200, json=_envelope(orderList=full_page)))
    _, cursor = await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))
    assert cursor == "2"

    short_page = [_order(order_no=f"GSO-{index}") for index in range(2)]
    adapter_short = _adapter(
        lambda request: httpx.Response(200, json=_envelope(orderList=short_page))
    )
    _, cursor_short = await adapter_short.fetch_orders_updated_since(
        datetime(2026, 11, 19, tzinfo=UTC)
    )
    assert cursor_short is None


@pytest.mark.parametrize("cursor", ["not-a-page", "0", "-2", "1.5"])
async def test_fetch_orders_rejects_an_invalid_cursor(cursor: str) -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_envelope(orderList=[])))
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC), cursor)
    assert excinfo.value.code == "ERR_SHEIN_INVALID_CURSOR"
    assert excinfo.value.status_code == 422


# ---------------------------------------------------------------- products ingestion


async def test_fetch_products_normalizes_variants_and_cursor() -> None:
    seen: list[httpx.Request] = []
    payload = _envelope(
        total=120,
        productList=[
            {
                "goodsId": "GS-9911",
                "goodsTitle": "Solar Garden Light 20W",
                "skuList": [
                    {
                        "skuCode": "SGL-100",
                        "skuId": "SKU-100",
                        "price": "8.555",
                        "quantity": "450",
                        "attributes": [{"name": "Color", "value": "Black"}],
                    },
                    {"skuCode": "SGL-200", "price": "11.20", "stock": 0},
                ],
            },
            {"goodsTitle": "Product without id"},
        ],
    )

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _assert_signed(request, SHEIN_PRODUCT_LIST_PATH)
        return httpx.Response(200, json=payload)

    adapter = _adapter(handle)
    products, cursor = await adapter.fetch_products_updated_since(
        datetime(2026, 11, 19, 3, 15, tzinfo=UTC)
    )

    body = _assert_signed(seen[0], SHEIN_PRODUCT_LIST_PATH)
    assert body["updateTimeStart"] == int(
        datetime(2026, 11, 19, 3, 15, tzinfo=UTC).timestamp() * 1000
    )
    assert body["pageSize"] == 50
    assert cursor == "2"

    assert len(products) == 1
    product = products[0]
    assert product.external_product_id == "GS-9911"
    assert product.title == "Solar Garden Light 20W"
    assert product.variants == [
        {
            "external_sku": "SGL-100",
            "external_variant_id": "SKU-100",
            "unit_price_usd": "8.56",
            "stock_quantity": 450,
            "option_values": {"Color": "Black"},
        },
        {
            "external_sku": "SGL-200",
            "external_variant_id": None,
            "unit_price_usd": "11.20",
            "stock_quantity": 0,
            "option_values": {},
        },
    ]


# ----------------------------------------------------------------------- fulfillment


async def test_create_fulfillment_sends_self_shipping_payload() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _assert_signed(request, SHEIN_ORDER_SHIP_PATH)
        return httpx.Response(
            200, json=_envelope(orderNo="GSO20261119001", packageNo="PKG-77", accepted=True)
        )

    adapter = _adapter(handle)
    result = await adapter.create_fulfillment(
        external_order_id=" GSO20261119001 ",
        tracking_number=" 1Z999AA10123456784 ",
        carrier_company=" UPS ",
        tracking_url="https://track.test/1Z999AA10123456784",
        fulfillment_order_id="PKG-77",
        notify_customer=True,
    )

    body = _assert_signed(seen[0], SHEIN_ORDER_SHIP_PATH)
    assert body == {
        "orderNo": "GSO20261119001",
        "deliveryType": "self_shipping",
        "expressCompany": "UPS",
        "trackingNumber": "1Z999AA10123456784",
        "notifyCustomer": True,
        "trackingUrl": "https://track.test/1Z999AA10123456784",
        "packageNo": "PKG-77",
    }
    assert result == {
        "orderNo": "GSO20261119001",
        "packageNo": "PKG-77",
        "accepted": True,
    }


async def test_create_fulfillment_uses_semi_managed_delivery_mode() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_envelope(orderNo="GSO-SEMI"))

    adapter = _adapter(handle, fulfillment_mode="semi_managed")
    await adapter.create_fulfillment(
        external_order_id="GSO-SEMI",
        tracking_number="TRACK-1",
        carrier_company="SHEIN Logistics",
        notify_customer=False,
    )

    body = _assert_signed(seen[0], SHEIN_ORDER_SHIP_PATH)
    assert body["deliveryType"] == "semi_managed"
    assert body["notifyCustomer"] is False
    assert "trackingUrl" not in body
    assert "packageNo" not in body


@pytest.mark.parametrize(
    ("order_id", "tracking", "carrier"),
    [
        ("", "TRACK-1", "UPS"),
        ("GSO-1", "  ", "UPS"),
        ("GSO-1", "TRACK-1", ""),
    ],
)
async def test_create_fulfillment_rejects_incomplete_input(
    order_id: str, tracking: str, carrier: str
) -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_envelope()))
    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment(
            external_order_id=order_id,
            tracking_number=tracking,
            carrier_company=carrier,
        )
    assert excinfo.value.code == "ERR_SHEIN_FULFILLMENT_REJECTED"
    assert excinfo.value.status_code == 422


# --------------------------------------------------------------- publish listings


async def test_publish_listing_sends_multi_variant_payload() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _assert_signed(request, SHEIN_PRODUCT_CREATE_PATH)
        return httpx.Response(
            200,
            json=_envelope(
                goodsId="GS-556677",
                skuList=[
                    {
                        "skuCode": "NORA-RED-S",
                        "skuId": "SKU-1",
                        "warehouseId": "WH-1",
                    }
                ],
            ),
        )

    adapter = _adapter(handle)
    payload = _listing_payload(
        title="Nordic Pendant Light 220V " + "x" * 300,
        images=["https://cdn.test/nora-1.jpg", "http://insecure.test/nora-2.jpg"],
        hs_code="9405.10",
        moq_tiers=[SourcingMoqTier(min_qty=100, unit_price_usd=Decimal("9.505"))],
        options=[SourcingOptionSpec(name="Color", values=["Red", "Black"])],
        variants=[
            MarketplaceVariantPayload(
                local_sku="LOCAL-RED-S",
                external_sku="NORA-RED-S",
                option_values={"Color": "Red", "Size": "S"},
                price_usd=Decimal("12.345"),
                quantity=12,
                image_url="https://cdn.test/red.jpg",
            ),
            MarketplaceVariantPayload(
                local_sku="LOCAL-BLK-M",
                external_sku="NORA-BLK-M",
                option_values={"Color": "Black"},
                price_usd=Decimal("13.005"),
                quantity=-4,
            ),
        ],
    )

    receipt = await adapter.publish_listing(payload)

    body = _assert_signed(seen[0], SHEIN_PRODUCT_CREATE_PATH)
    assert len(body["productName"]) == 255
    assert body["currency"] == "USD"
    assert body["region"] == "US"
    assert body["categoryName"] == "Lighting > Pendant Lights"
    assert body["hsCode"] == "9405.10"
    assert body["imageUrls"] == ["https://cdn.test/nora-1.jpg"]
    assert body["options"] == [{"name": "Color", "values": ["Red", "Black"]}]
    assert body["priceLadder"] == [{"minQty": 100, "unitPrice": "9.51"}]
    assert body["description"] == (
        "Hand-finished aluminium pendant light for contract projects.\n\n"
        "- 5-year warranty\n"
        "- CE certified"
    )
    assert body["skuList"] == [
        {
            "skuCode": "NORA-RED-S",
            "price": "12.35",
            "stock": 12,
            "attributes": [
                {"name": "Color", "value": "Red"},
                {"name": "Size", "value": "S"},
            ],
            "imageUrl": "https://cdn.test/red.jpg",
        },
        {
            "skuCode": "NORA-BLK-M",
            "price": "13.01",
            "stock": 0,
            "attributes": [{"name": "Color", "value": "Black"}],
        },
    ]

    assert receipt.external_product_id == "GS-556677"
    assert receipt.external_sku == "NORA-220-SHEIN"
    assert receipt.status == "PUBLISHED"
    assert receipt.raw_response["code"] == "0"
    assert len(receipt.variant_receipts) == 1
    assert receipt.variant_receipts[0].external_sku == "NORA-RED-S"
    assert receipt.variant_receipts[0].external_variant_id == "SKU-1"
    assert receipt.variant_receipts[0].external_inventory_item_id == "WH-1"


async def test_publish_listing_falls_back_to_a_single_default_variant() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_envelope(goodsId="GS-1"))

    adapter = _adapter(handle)
    receipt = await adapter.publish_listing(
        _listing_payload(images=[], bullet_points=[], quantity=0, price_usd=Decimal("0"))
    )

    body = _assert_signed(seen[0], SHEIN_PRODUCT_CREATE_PATH)
    assert body["skuList"] == [{"skuCode": "NORA-220-SHEIN", "price": "0.00", "stock": 0}]
    assert body["imageUrls"] == []
    assert body["options"] == []
    assert body["priceLadder"] == []
    assert "hsCode" not in body
    assert body["description"] == "Hand-finished aluminium pendant light for contract projects."
    assert receipt.variant_receipts[0].external_sku == "NORA-220-SHEIN"


async def test_publish_listing_uploads_image_blobs_before_creating_the_product() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == SHEIN_PRODUCT_IMAGE_PATH:
            assert request.headers["content-type"].startswith("multipart/form-data")
            _assert_signed(request, SHEIN_PRODUCT_IMAGE_PATH, json_body=False)
            return httpx.Response(200, json=_envelope(imageUrl="https://img.test/uploaded-1.jpg"))
        _assert_signed(request, SHEIN_PRODUCT_CREATE_PATH)
        return httpx.Response(200, json=_envelope(goodsId="GS-2"))

    adapter = _adapter(handle)
    await adapter.publish_listing(
        _listing_payload(
            images=["https://cdn.test/nora-1.jpg"],
            image_blobs=[
                MarketplaceImageBlob(
                    filename="nora.jpg",
                    mime_type="image/jpeg",
                    data=b"\x89PNG\r\n",
                    alt_text="Nordic pendant",
                )
            ],
        )
    )

    assert [request.url.path for request in seen] == [
        SHEIN_PRODUCT_IMAGE_PATH,
        SHEIN_PRODUCT_CREATE_PATH,
    ]
    body = json.loads(seen[1].content)
    assert body["imageUrls"] == [
        "https://img.test/uploaded-1.jpg",
        "https://cdn.test/nora-1.jpg",
    ]


async def test_publish_listing_rejects_an_empty_sku_or_title() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_envelope(goodsId="GS-1")))
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(_listing_payload(external_sku="   "))
    assert excinfo.value.code == "ERR_SHEIN_LISTING_REJECTED"
    assert excinfo.value.status_code == 422


async def test_publish_listing_without_product_id_is_rejected() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_envelope(accepted=True)))
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(_listing_payload())

    error = excinfo.value
    assert error.slug == "shein-listing-rejected"
    assert error.code == "ERR_SHEIN_LISTING_REJECTED"
    assert error.status_code == 502
    assert SECRET not in error.detail
    assert SECRET not in str(error)


async def test_publish_listing_remote_failure_is_typed_and_redacted() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": "50012", "msg": f"goods publish failed with secret {SECRET}"},
        )

    adapter = _adapter(handle)
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(_listing_payload())

    error = excinfo.value
    assert error.slug == "shein-api-error"
    assert error.code == "ERR_SHEIN_API_ERROR"
    assert error.status_code == 502
    assert SECRET not in error.detail
    assert "[REDACTED]" in error.detail


# ------------------------------------------------------------------ inventory push


async def test_push_inventory_level_pushes_absolute_stock_with_supplied_ids() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _assert_signed(request, SHEIN_INVENTORY_UPDATE_PATH)
        return httpx.Response(200, json=_envelope(goodsId="GS-9911", quantity=125))

    adapter = _adapter(handle)
    result = await adapter.push_inventory_level(
        external_sku=" SGL-100 ",
        quantity=125,
        external_product_id="GS-9911",
        external_variant_id="SKU-100",
        external_inventory_item_id="INV-9",
        location_id="LOC-3",
        warehouse_id="WH-1",
    )

    body = _assert_signed(seen[0], SHEIN_INVENTORY_UPDATE_PATH)
    assert body == {
        "goodsId": "GS-9911",
        "skuCode": "SGL-100",
        "quantity": 125,
        "skuId": "SKU-100",
        "inventoryId": "INV-9",
        "warehouseId": "WH-1",
        "locationId": "LOC-3",
    }
    assert result == {
        "channel": "shein",
        "external_sku": "SGL-100",
        "quantity": 125,
        "external_product_id": "GS-9911",
        "external_variant_id": "SKU-100",
        "external_inventory_item_id": "INV-9",
        "location_id": "LOC-3",
        "warehouse_id": "WH-1",
        "raw": {"goodsId": "GS-9911", "quantity": 125},
    }


async def test_push_inventory_level_resolves_the_product_and_clamps_negatives() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == SHEIN_PRODUCT_LIST_PATH:
            body = _assert_signed(request, SHEIN_PRODUCT_LIST_PATH)
            assert body == {"skuCodeList": ["SGL-100"], "page": 1, "pageSize": 20}
            return httpx.Response(
                200,
                json=_envelope(
                    productList=[
                        {
                            "goodsId": "GS-9911",
                            "skuList": [{"skuCode": "SGL-100", "skuId": "SKU-9"}],
                        }
                    ]
                ),
            )
        _assert_signed(request, SHEIN_INVENTORY_UPDATE_PATH)
        return httpx.Response(200, json=_envelope())

    adapter = _adapter(handle)
    result = await adapter.push_inventory_level(external_sku="SGL-100 ", quantity=-5)

    assert [request.url.path for request in seen] == [
        SHEIN_PRODUCT_LIST_PATH,
        SHEIN_INVENTORY_UPDATE_PATH,
    ]
    body = json.loads(seen[1].content)
    assert body == {"goodsId": "GS-9911", "skuCode": "SGL-100", "quantity": 0, "skuId": "SKU-9"}
    assert result["quantity"] == 0
    assert result["external_product_id"] == "GS-9911"
    assert result["external_variant_id"] == "SKU-9"
    assert result["raw"] == {}
    assert isinstance(result["quantity"], int)


async def test_push_inventory_level_fails_when_the_sku_cannot_be_resolved() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_envelope(productList=[])))
    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(external_sku="SGL-404", quantity=3)
    assert excinfo.value.slug == "shein-inventory-sync-failed"
    assert excinfo.value.code == "ERR_SHEIN_INVENTORY_SYNC"
    assert excinfo.value.status_code == 502
    assert SECRET not in excinfo.value.detail


async def test_push_inventory_level_rejects_an_empty_sku() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json=_envelope()))
    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(external_sku="  ", quantity=3)
    assert excinfo.value.code == "ERR_SHEIN_INVENTORY_SYNC"
    assert excinfo.value.status_code == 422


# ------------------------------------------------------------------------ webhooks


def test_verify_webhook_uses_constant_time_hmac_over_raw_body() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    raw_body = b'{"orderNo":"GSO20261119001","event":"order_status_changed"}'
    secret = "shein-webhook-secret"
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()

    assert adapter.verify_webhook(raw_body, {"x-shein-signature": signature}, secret)
    assert adapter.verify_webhook(raw_body, {"X-SHEIN-Signature": signature.upper()}, secret)
    assert adapter.verify_webhook(
        raw_body, {"x-shein-signature": f"  sha256={signature}  "}, secret
    )

    # Fail closed: no header, blank secret, tampered body, wrong digest, non-ASCII digest.
    assert not adapter.verify_webhook(raw_body, {}, secret)
    assert not adapter.verify_webhook(raw_body, {"x-shein-signature": ""}, secret)
    assert not adapter.verify_webhook(raw_body + b" ", {"x-shein-signature": signature}, secret)
    assert not adapter.verify_webhook(raw_body, {"x-shein-signature": "0" * 64}, secret)
    assert not adapter.verify_webhook(raw_body, {"x-shein-signature": "签名"}, secret)
    assert not adapter.verify_webhook(raw_body, {"x-shein-signature": signature}, "")


def test_verify_webhook_falls_back_to_the_adapter_secret() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    raw_body = b'{"orderNo":"GSO-1"}'
    signature = hmac.new(SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    assert adapter.verify_webhook(raw_body, {"x-shein-signature": signature}, "")


def test_module_level_verify_shein_webhook_is_fail_closed() -> None:
    raw_body = b'{"orderNo":"GSO-1"}'
    signature = hmac.new(b"hook-secret", raw_body, hashlib.sha256).hexdigest()
    assert verify_shein_webhook(raw_body, {"x-shein-signature": signature}, "hook-secret")
    assert not verify_shein_webhook(raw_body, {"x-shein-signature": signature}, "")
    assert not verify_shein_webhook(raw_body, {}, "hook-secret")
    assert not verify_shein_webhook(b"", {"x-shein-signature": signature}, "hook-secret")


# --------------------------------------------------------------------- error mapping


async def test_api_error_is_redacted_even_when_upstream_echoes_the_secret() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "50000",
                "msg": f"seller backend exploded with {SECRET} while signing",
            },
        )

    adapter = _adapter(handle)
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))

    error = excinfo.value
    assert error.code == "ERR_SHEIN_API_ERROR"
    assert error.status_code == 502
    assert SECRET not in error.detail
    assert SECRET not in str(error)
    assert "[REDACTED]" in error.detail


@pytest.mark.parametrize(
    ("status_code", "payload", "slug", "code", "expected_status"),
    [
        (
            401,
            {},
            "shein-auth-failed",
            "ERR_SHEIN_AUTH_FAILED",
            502,
        ),
        (
            200,
            {"code": "1001", "msg": "signature verify failed"},
            "shein-auth-failed",
            "ERR_SHEIN_AUTH_FAILED",
            502,
        ),
        (
            403,
            {"code": "2003", "msg": "appid has no permission"},
            "shein-auth-failed",
            "ERR_SHEIN_AUTH_FAILED",
            502,
        ),
        (
            429,
            {},
            "shein-rate-limited",
            "ERR_SHEIN_RATE_LIMITED",
            429,
        ),
        (
            200,
            {"code": "3001", "msg": "flow limit exceeded, please retry"},
            "shein-rate-limited",
            "ERR_SHEIN_RATE_LIMITED",
            429,
        ),
        (
            200,
            {"code": "50000", "msg": "unexpected upstream failure"},
            "shein-api-error",
            "ERR_SHEIN_API_ERROR",
            502,
        ),
        (500, {}, "shein-api-error", "ERR_SHEIN_API_ERROR", 502),
    ],
)
async def test_shein_failures_map_to_typed_app_errors(
    status_code: int,
    payload: dict[str, Any],
    slug: str,
    code: str,
    expected_status: int,
) -> None:
    adapter = _adapter(lambda request: httpx.Response(status_code, json=payload))
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_products_updated_since(datetime(2026, 11, 19, tzinfo=UTC))

    error = excinfo.value
    assert error.slug == slug
    assert error.code == code
    assert error.status_code == expected_status
    assert SECRET not in error.detail


async def test_transport_failure_maps_to_a_retryable_api_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed for {APP_ID}", request=request)

    adapter = _adapter(handle)
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))

    error = excinfo.value
    assert error.code == "ERR_SHEIN_API_ERROR"
    assert error.status_code == 502
    assert "retried" in error.detail
    assert SECRET not in error.detail


async def test_malformed_json_body_is_not_reported_as_success() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(502, text="<html>upstream proxy error</html>")
    )
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))
    assert excinfo.value.code == "ERR_SHEIN_API_ERROR"


async def test_invalid_amounts_are_rejected_without_floats() -> None:
    order = _order(totalAmount="not-money")
    adapter = _adapter(lambda request: httpx.Response(200, json=_order_list_payload([order])))
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(datetime(2026, 11, 19, tzinfo=UTC))
    assert excinfo.value.code == "ERR_SHEIN_INVALID_AMOUNT"
    assert excinfo.value.status_code == 422
