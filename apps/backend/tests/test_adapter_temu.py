"""Zero-network unit tests for the Temu Open Platform connector adapter."""

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

import erp_core.channels.ecommerce.temu as temu_module
from erp_core.catalog.sourcing_service import (
    MarketplaceImageBlob,
    MarketplaceListingPayload,
    MarketplaceVariantPayload,
    SourcingOptionSpec,
)
from erp_core.channels.ecommerce.temu import (
    TEMU_ROUTER_ENDPOINTS,
    TemuAdapter,
    sign_temu_request,
    verify_temu_webhook,
)
from erp_core.shared.enums import FulfillmentStatus, IdentityType, PaymentStatus
from erp_core.shared.errors import AppError

MALL_ID = "6344182012345678"
APP_KEY = "temu-app-key-public"
APP_SECRET = "temu-app-secret-do-not-leak"
ACCESS_TOKEN = "temu-access-token-do-not-leak"
SINCE = datetime(2024, 5, 1, tzinfo=UTC)
SINCE_MS = 1_714_521_600_000

Route = Callable[[dict[str, Any]], httpx.Response]


class FakeTemu:
    """Deterministic router table standing in for every Temu RPC endpoint."""

    def __init__(self, routes: dict[str, Route] | None = None) -> None:
        self.routes = routes or {}
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, dict)
        self.bodies.append(body)
        self.headers.append(dict(request.headers))
        self.urls.append(str(request.url))
        route = self.routes.get(str(body.get("type")))
        if route is None:
            return httpx.Response(
                404,
                json={
                    "success": False,
                    "errorCode": "9999999",
                    "errorMsg": f"unrouted {body.get('type')}",
                },
            )
        return route(body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def sent(self, api_type: str) -> list[dict[str, Any]]:
        return [body for body in self.bodies if body.get("type") == api_type]

    def only(self, api_type: str) -> dict[str, Any]:
        matched = self.sent(api_type)
        assert len(matched) == 1, f"expected 1 {api_type} call, got {len(matched)}"
        return matched[0]


def build(adapter_transport: httpx.AsyncBaseTransport, **overrides: Any) -> TemuAdapter:
    kwargs: dict[str, Any] = {
        "mall_id": MALL_ID,
        "app_key": APP_KEY,
        "app_secret": APP_SECRET,
        "access_token": ACCESS_TOKEN,
        "transport": adapter_transport,
    }
    kwargs.update(overrides)
    return TemuAdapter(**kwargs)


def ok(result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "result": result})


def order_list_route(order_sns: list[str], total: int = 1) -> Route:
    return lambda _body: ok(
        {"orderList": [{"orderSn": sn} for sn in order_sns], "totalItemNum": total}
    )


def order_detail_row() -> dict[str, Any]:
    return {
        "orderSn": "TEMU-1001",
        "parentOrderSn": "TEMU-P-1001",
        "orderStatus": "SHIPPED",
        "currency": "USD",
        "updateTime": 1714521700,
        "buyerId": 8899,
        "orderAmount": {
            "currency": "USD",
            "goodsAmount": "10.005",
            "shippingAmount": "1.50",
            "discountAmount": "0.50",
            "taxAmount": "0.80",
            "orderTotalAmount": "11.805",
            "platformCommission": "2.345",
            "refundAmount": "1.005",
        },
        "goodsList": [
            {
                "extCode": "SKU-RED",
                "goodsName": "Solar Lamp Red",
                "quantity": 3,
                "goodsPrice": "2.345",
            },
            {
                "skuId": "sku-blue",
                "goodsName": "Solar Lamp Blue",
                "quantity": 1,
                "goodsPrice": "3.33",
                "goodsAmount": "3.33",
            },
        ],
    }


def order_detail_route(row: dict[str, Any] | None = None) -> Route:
    return lambda _body: ok({"orderList": [row if row is not None else order_detail_row()]})


# --------------------------------------------------------------------------------------
# constructor / credential hygiene
# --------------------------------------------------------------------------------------


def test_constructor_rejects_unknown_fulfillment_mode() -> None:
    with pytest.raises(AppError) as excinfo:
        build(httpx.MockTransport(lambda _r: httpx.Response(200)), fulfillment_mode="dropship")
    assert excinfo.value.code == "ERR_TEMU_FULFILLMENT_MODE_INVALID"
    assert excinfo.value.status_code == 422
    assert "semi_managed" in excinfo.value.detail


def test_constructor_rejects_missing_credentials() -> None:
    with pytest.raises(AppError) as excinfo:
        build(httpx.MockTransport(lambda _r: httpx.Response(200)), access_token="   ")
    assert excinfo.value.code == "ERR_TEMU_CREDENTIALS_INVALID"
    assert excinfo.value.status_code == 422


def test_constructor_rejects_unknown_region() -> None:
    with pytest.raises(AppError) as excinfo:
        build(httpx.MockTransport(lambda _r: httpx.Response(200)), region="MARS")
    assert excinfo.value.code == "ERR_TEMU_REGION_UNSUPPORTED"
    assert excinfo.value.status_code == 422


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("us", TEMU_ROUTER_ENDPOINTS["US"]),
        ("EU", TEMU_ROUTER_ENDPOINTS["EU"]),
        ("global", TEMU_ROUTER_ENDPOINTS["GLOBAL"]),
    ],
)
def test_constructor_maps_region_to_router(region: str, expected: str) -> None:
    adapter = build(httpx.MockTransport(lambda _r: httpx.Response(200)), region=region)
    assert adapter.endpoint_url == expected
    assert adapter.region == region.upper()


def test_constructor_honours_endpoint_override() -> None:
    adapter = build(
        httpx.MockTransport(lambda _r: httpx.Response(200)),
        endpoint_url="https://sandbox.temu.test/openapi/router/",
    )
    assert adapter.endpoint_url == "https://sandbox.temu.test/openapi/router"


@pytest.mark.parametrize("mode", ["standard", "semi_managed", "local"])
def test_constructor_accepts_every_fulfillment_mode(mode: str) -> None:
    adapter = build(httpx.MockTransport(lambda _r: httpx.Response(200)), fulfillment_mode=mode)
    assert adapter.fulfillment_mode == mode


def test_repr_redacts_secrets() -> None:
    adapter = build(httpx.MockTransport(lambda _r: httpx.Response(200)))
    rendered = repr(adapter)
    assert APP_SECRET not in rendered
    assert ACCESS_TOKEN not in rendered
    assert "[redacted]" in rendered
    assert MALL_ID in rendered
    assert "standard" in rendered


# --------------------------------------------------------------------------------------
# signing
# --------------------------------------------------------------------------------------


def test_signature_is_order_independent_and_excludes_sign() -> None:
    first = sign_temu_request(APP_SECRET, {"type": "a", "app_key": "b", "sign": "ignored"})
    second = sign_temu_request(APP_SECRET, {"app_key": "b", "type": "a"})
    assert first == second
    assert len(first) == 32
    assert first == first.upper()


def test_signature_binds_body_and_secret() -> None:
    params = {"type": "bg.order.list.get"}
    base = sign_temu_request(APP_SECRET, params)
    assert sign_temu_request(APP_SECRET, params, body="{}") != base
    assert sign_temu_request("another-secret", params) != base


def test_signature_requires_secret() -> None:
    with pytest.raises(AppError) as excinfo:
        sign_temu_request("", {"type": "bg.order.list.get"})
    assert excinfo.value.code == "ERR_TEMU_SIGNING_SECRET_MISSING"


async def test_request_envelope_is_signed_and_carries_no_plaintext_leak() -> None:
    fake = FakeTemu({"bg.order.list.get": order_list_route([])})
    adapter = build(fake.transport)
    await adapter.fetch_orders_updated_since(SINCE)

    envelope = fake.only("bg.order.list.get")
    assert envelope["mall_id"] == MALL_ID
    assert envelope["app_key"] == APP_KEY
    assert envelope["access_token"] == ACCESS_TOKEN
    assert len(str(envelope["timestamp"])) == 10
    unsigned = {key: value for key, value in envelope.items() if key != "sign"}
    expected = sign_temu_request(
        APP_SECRET,
        {key: str(value) for key, value in unsigned.items() if key != "data"},
        body=json.dumps(unsigned, separators=(",", ":")),
    )
    assert envelope["sign"] == expected
    assert fake.headers[0]["x-temu-access-token"] == ACCESS_TOKEN


# --------------------------------------------------------------------------------------
# orders
# --------------------------------------------------------------------------------------


async def test_fetch_orders_normalizes_financials_exactly() -> None:
    fake = FakeTemu(
        {
            "bg.order.list.get": order_list_route(["TEMU-1001"]),
            "bg.order.detail.get": order_detail_route(),
        }
    )
    adapter = build(fake.transport)
    orders, cursor = await adapter.fetch_orders_updated_since(SINCE)

    assert cursor is None
    assert len(orders) == 1
    order = orders[0]
    assert order.external_order_id == "TEMU-1001"
    assert order.external_order_number == "TEMU-P-1001"
    assert order.currency == "USD"
    assert order.subtotal_amount == Decimal("10.01")
    assert order.total_amount == Decimal("11.81")
    assert order.shipping_charged == Decimal("1.50")
    assert order.discount_amount == Decimal("0.50")
    assert order.tax_collected == Decimal("0.80")
    # 2.345 -> exact 2dp ROUND_HALF_UP
    assert order.platform_commission_base == Decimal("2.35")
    assert order.platform_commission_base.as_tuple().exponent == -2
    assert order.fulfillment_status is FulfillmentStatus.SHIPPED
    assert order.payment_status is PaymentStatus.PAID
    assert order.customer.external_customer_id == "8899"
    assert order.customer.external_identity_type is IdentityType.WEB_VISITOR_ID
    assert order.updated_at == datetime(2024, 5, 1, 0, 1, 40, tzinfo=UTC)

    assert order.line_items[0].external_sku == "SKU-RED"
    assert order.line_items[0].quantity == 3
    # 2.345 * 3 == 7.035 -> ROUND_HALF_UP -> 7.04
    assert order.line_items[0].line_total == Decimal("7.04")
    assert order.line_items[1].external_sku == "sku-blue"
    assert order.line_items[1].line_total == Decimal("3.33")

    assert order.channel_metadata["platform_commission_base"] == "2.35"
    assert order.channel_metadata["refunded_amount"] == "1.01"
    assert order.channel_metadata["temu_order_status"] == "SHIPPED"
    assert order.channel_metadata["temu_parent_order_sn"] == "TEMU-P-1001"
    assert order.channel_metadata["temu_fulfillment_mode"] == "standard"


async def test_fetch_orders_sends_update_window_and_advances_cursor() -> None:
    fake = FakeTemu(
        {
            "bg.order.list.get": order_list_route(["TEMU-1001"], total=120),
            "bg.order.detail.get": order_detail_route(),
        }
    )
    adapter = build(fake.transport)
    _orders, cursor = await adapter.fetch_orders_updated_since(SINCE)
    assert cursor == "2"

    await adapter.fetch_orders_updated_since(SINCE, cursor=cursor)
    second = fake.sent("bg.order.list.get")[1]
    data = second["data"]
    assert data["updateTimeStart"] == SINCE_MS
    assert data["pageNumber"] == 2
    assert data["pageSize"] == 50
    assert data["mallId"] == MALL_ID
    assert fake.sent("bg.order.list.get")[0]["data"]["pageNumber"] == 1


async def test_fetch_orders_skips_orders_without_line_items() -> None:
    row = order_detail_row()
    row["goodsList"] = []
    fake = FakeTemu(
        {
            "bg.order.list.get": order_list_route(["TEMU-1001"]),
            "bg.order.detail.get": order_detail_route(row),
        }
    )
    orders, _cursor = await build(fake.transport).fetch_orders_updated_since(SINCE)
    assert orders == []


async def test_fetch_orders_maps_unpaid_pending_and_full_refund_statuses() -> None:
    rows = [
        {**order_detail_row(), "orderSn": "TEMU-UNPAID", "orderStatus": "UNPAID"},
        {**order_detail_row(), "orderSn": "TEMU-PENDING", "orderStatus": "PENDING"},
        {
            **order_detail_row(),
            "orderSn": "TEMU-FULL-REFUND",
            "orderStatus": "DELIVERED",
            "orderAmount": {
                "currency": "USD",
                "orderTotalAmount": "11.81",
                "refundAmount": "11.81",
            },
        },
        {
            **order_detail_row(),
            "orderSn": "TEMU-PARTIAL-REFUND",
            "orderStatus": "DELIVERED",
            "orderAmount": {"currency": "USD", "orderTotalAmount": "11.81", "refundAmount": "2.00"},
        },
    ]
    fake = FakeTemu(
        {
            "bg.order.list.get": order_list_route([r["orderSn"] for r in rows], total=4),
            "bg.order.detail.get": lambda _body: ok({"orderList": rows}),
        }
    )
    orders, _ = await build(fake.transport).fetch_orders_updated_since(SINCE)
    by_id = {order.external_order_id: order for order in orders}
    assert by_id["TEMU-UNPAID"].fulfillment_status is FulfillmentStatus.PENDING_PAYMENT
    assert by_id["TEMU-UNPAID"].payment_status is PaymentStatus.UNPAID
    assert by_id["TEMU-PENDING"].payment_status is PaymentStatus.UNPAID
    assert by_id["TEMU-FULL-REFUND"].fulfillment_status is FulfillmentStatus.REFUNDED
    assert by_id["TEMU-FULL-REFUND"].payment_status is PaymentStatus.REFUNDED
    assert by_id["TEMU-PARTIAL-REFUND"].fulfillment_status is FulfillmentStatus.DELIVERED
    assert by_id["TEMU-PARTIAL-REFUND"].payment_status is PaymentStatus.PAID
    assert by_id["TEMU-PARTIAL-REFUND"].channel_metadata["refunded_amount"] == "2.00"


async def test_remote_error_detail_redacts_credentials() -> None:
    def rejected(_body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": False,
                "errorCode": "1200001",
                "errorMsg": f"bad sign for {APP_SECRET} with {ACCESS_TOKEN}",
            },
        )

    fake = FakeTemu({"bg.order.list.get": rejected})
    with pytest.raises(AppError) as excinfo:
        await build(fake.transport).fetch_orders_updated_since(SINCE)
    assert excinfo.value.code == "ERR_TEMU_API_ERROR"
    assert excinfo.value.status_code == 502
    assert APP_SECRET not in excinfo.value.detail
    assert ACCESS_TOKEN not in excinfo.value.detail
    assert "[redacted]" in excinfo.value.detail
    assert "1200001" in excinfo.value.detail


async def test_http_500_body_is_redacted() -> None:
    def boom(_body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(500, text=f"upstream broke for token {ACCESS_TOKEN}")

    fake = FakeTemu({"bg.order.list.get": boom})
    with pytest.raises(AppError) as excinfo:
        await build(fake.transport).fetch_orders_updated_since(SINCE)
    assert excinfo.value.code == "ERR_TEMU_API_ERROR"
    assert ACCESS_TOKEN not in excinfo.value.detail


@pytest.mark.parametrize(
    ("status", "code", "expected_status"),
    [(429, "ERR_TEMU_RATE_LIMITED", 429), (401, "ERR_TEMU_AUTH_FAILED", 502)],
)
async def test_auth_and_rate_limit_errors_are_typed(
    status: int, code: str, expected_status: int
) -> None:
    fake = FakeTemu({"bg.order.list.get": lambda _body: httpx.Response(status, text="nope")})
    with pytest.raises(AppError) as excinfo:
        await build(fake.transport).fetch_orders_updated_since(SINCE)
    assert excinfo.value.code == code
    assert excinfo.value.status_code == expected_status


async def test_transport_error_is_typed() -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = build(httpx.MockTransport(explode))
    with pytest.raises(AppError) as excinfo:
        await adapter.fetch_orders_updated_since(SINCE)
    assert excinfo.value.code == "ERR_TEMU_TRANSPORT"
    assert "ConnectError" in excinfo.value.detail


async def test_malformed_body_is_typed() -> None:
    fake = FakeTemu({"bg.order.list.get": lambda _body: httpx.Response(200, text="<html>nope")})
    with pytest.raises(AppError) as excinfo:
        await build(fake.transport).fetch_orders_updated_since(SINCE)
    assert excinfo.value.code == "ERR_TEMU_RESPONSE_MALFORMED"


async def test_json_float_amounts_never_become_binary_floats() -> None:
    row = order_detail_row()
    row["orderAmount"] = {"currency": "USD", "goodsAmount": 11.8, "orderTotalAmount": 11.8}
    row["goodsList"] = [{"extCode": "SKU-RED", "quantity": 1, "goodsPrice": 11.8}]
    fake = FakeTemu(
        {
            "bg.order.list.get": order_list_route(["TEMU-1001"]),
            "bg.order.detail.get": order_detail_route(row),
        }
    )
    orders, _cursor = await build(fake.transport).fetch_orders_updated_since(SINCE)
    assert orders[0].total_amount == Decimal("11.80")
    assert orders[0].subtotal_amount == Decimal("11.80")
    assert orders[0].line_items[0].line_total == Decimal("11.80")


def test_float_amount_is_refused_outright() -> None:
    with pytest.raises(AppError) as excinfo:
        temu_module._decimal(1.5, field="total")
    assert excinfo.value.code == "ERR_TEMU_FLOAT_AMOUNT"


# --------------------------------------------------------------------------------------
# products
# --------------------------------------------------------------------------------------


async def test_fetch_products_normalizes_variants() -> None:
    fake = FakeTemu(
        {
            "bg.local.goods.list.get": lambda _body: ok(
                {
                    "goodsList": [
                        {
                            "goodsId": 501,
                            "goodsName": "Solar Lamp",
                            "skuList": [
                                {
                                    "extCode": "SKU-RED",
                                    "skuId": "sku-1",
                                    "inventoryItemId": "inv-1",
                                    "price": "12.5",
                                    "quantity": 7,
                                },
                                {"goodsName": "sku-row-without-any-id"},
                            ],
                        }
                    ],
                    "totalItemNum": 1,
                }
            )
        }
    )
    adapter = build(fake.transport, fulfillment_mode="local")
    products, cursor = await adapter.fetch_products_updated_since(SINCE)

    assert cursor is None
    assert len(products) == 1
    product = products[0]
    assert product.external_product_id == "501"
    assert product.title == "Solar Lamp"
    assert product.variants == [
        {
            "external_sku": "SKU-RED",
            "external_variant_id": "sku-1",
            "external_inventory_item_id": "inv-1",
            "price": "12.50",
            "quantity": 7,
        }
    ]
    assert fake.only("bg.local.goods.list.get")["data"]["updateTimeStart"] == SINCE_MS


async def test_fetch_products_uses_managed_namespace_for_standard_mode() -> None:
    fake = FakeTemu({"bg.goods.list.get": lambda _body: ok({"goodsList": [], "totalItemNum": 0})})
    products, cursor = await build(fake.transport).fetch_products_updated_since(SINCE)
    assert products == []
    assert cursor is None
    assert fake.only("bg.goods.list.get")["data"]["regionId"] == "US"


# --------------------------------------------------------------------------------------
# fulfillment
# --------------------------------------------------------------------------------------


async def test_create_fulfillment_in_seller_managed_mode() -> None:
    fake = FakeTemu(
        {
            "bg.logistics.shipment.create": lambda _body: ok(
                {"packageSn": "PKG-77", "shipmentId": "SHIP-77"}
            )
        }
    )
    adapter = build(fake.transport, fulfillment_mode="semi_managed")
    result = await adapter.create_fulfillment(
        "TEMU-1001",
        "1Z999AA10123456784",
        "UPS",
        tracking_url="https://track.example.com/1Z999",
        fulfillment_order_id="PKG-77",
        notify_customer=True,
    )

    data = fake.only("bg.logistics.shipment.create")["data"]
    assert data["orderSn"] == "TEMU-1001"
    assert data["trackingNumber"] == "1Z999AA10123456784"
    assert data["shippingCompany"] == "UPS"
    assert data["trackingUrl"] == "https://track.example.com/1Z999"
    assert data["packageSn"] == "PKG-77"
    assert data["notifyCustomer"] is True
    assert result["channel"] == "temu"
    assert result["external_order_id"] == "TEMU-1001"
    assert result["fulfillment_id"] == "SHIP-77"
    assert result["fulfillment_order_id"] == "PKG-77"
    assert result["tracking_number"] == "1Z999AA10123456784"
    assert result["fulfillment_mode"] == "semi_managed"
    assert result["raw"] == {"packageSn": "PKG-77", "shipmentId": "SHIP-77"}


async def test_create_fulfillment_in_standard_mode_confirms_tracking() -> None:
    fake = FakeTemu({"bg.logistics.shipment.confirm": lambda _body: ok({"confirmed": True})})
    adapter = build(fake.transport)
    result = await adapter.create_fulfillment("TEMU-1002", "TRACK-1", "FedEx")
    data = fake.only("bg.logistics.shipment.confirm")["data"]
    assert data["orderSn"] == "TEMU-1002"
    assert data["trackingNumber"] == "TRACK-1"
    assert "packageSn" not in data
    assert result["fulfillment_id"] == "TEMU-1002"


async def test_create_fulfillment_requires_tracking_and_carrier() -> None:
    adapter = build(httpx.MockTransport(lambda _r: httpx.Response(200)))
    with pytest.raises(AppError) as excinfo:
        await adapter.create_fulfillment("TEMU-1001", "   ", "UPS")
    assert excinfo.value.code == "ERR_TEMU_FULFILLMENT_INCOMPLETE"
    assert excinfo.value.status_code == 422


# --------------------------------------------------------------------------------------
# webhooks
# --------------------------------------------------------------------------------------


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_webhook_accepts_only_a_matching_hmac() -> None:
    body = b'{"eventCode":"bg_order_status_change_event","orderSn":"TEMU-1001"}'
    adapter = build(httpx.MockTransport(lambda _r: httpx.Response(200)))

    assert adapter.verify_webhook(body, {"x-temu-signature": _sign(body)}, "")
    assert adapter.verify_webhook(body, {"X-Temu-Signature": _sign(body).upper()}, APP_SECRET)
    assert adapter.verify_webhook(body, {"x-tm-signature": _sign(body)}, APP_SECRET)
    assert adapter.verify_webhook(body, {"x-temu-signature": f"sha256={_sign(body)}"}, "")
    assert not adapter.verify_webhook(body + b" ", {"x-temu-signature": _sign(body)}, "")
    assert not adapter.verify_webhook(body, {}, "")
    assert not adapter.verify_webhook(body, {"x-temu-signature": _sign(body, "other")}, "")
    assert not adapter.verify_webhook(body, {"x-temu-signature": _sign(body)}, "other-secret")


def test_module_level_verify_temu_webhook() -> None:
    body = b"{}"
    assert verify_temu_webhook(body, {"x-temu-signature": _sign(body)}, APP_SECRET) is True
    assert verify_temu_webhook(body, {"x-temu-signature": _sign(body)}, "") is False
    assert verify_temu_webhook(body, {"x-temu-signature": "deadbeef"}, APP_SECRET) is False


# --------------------------------------------------------------------------------------
# listing publish
# --------------------------------------------------------------------------------------


def listing_payload(**overrides: Any) -> MarketplaceListingPayload:
    base: dict[str, Any] = {
        "channel_account_id": uuid4(),
        "channel_type": "temu",
        "external_sku": "SKU-BASE",
        "title": "  Solar Garden Lamp  ",
        "description": "Weatherproof solar lamp",
        "bullet_points": ["IP65 rated", ""],
        "price_usd": Decimal("12.345"),
        "quantity": 5,
        "images": ["https://cdn.example.com/main.jpg", "http://insecure.example.com/b.jpg"],
        "category": "LED Lighting",
        "options": [SourcingOptionSpec(name="Color", values=["Red", "Blue"])],
        "variants": [
            MarketplaceVariantPayload(
                local_sku="LOCAL-RED",
                external_sku="SKU-RED",
                option_values={"Color": "Red"},
                price_usd=Decimal("13.335"),
                quantity=4,
                image_url="https://cdn.example.com/red.jpg",
            ),
            MarketplaceVariantPayload(
                local_sku="LOCAL-BLUE",
                external_sku="SKU-BLUE",
                option_values={"Color": "Blue"},
                price_usd=Decimal("15.00"),
                quantity=0,
            ),
        ],
    }
    base.update(overrides)
    return MarketplaceListingPayload(**base)


async def test_publish_listing_pushes_multi_variant_payload() -> None:
    fake = FakeTemu(
        {
            "bg.local.goods.add": lambda _body: ok(
                {
                    "goodsId": 9911,
                    "goodsStatus": "PUBLISHING",
                    "skuList": [
                        {
                            "extCode": "SKU-RED",
                            "skuId": "sku-9911-1",
                            "inventoryItemId": "inv-9911",
                        },
                        {"extCode": "SKU-BLUE", "skuId": "sku-9911-2"},
                    ],
                }
            )
        }
    )
    adapter = build(fake.transport, fulfillment_mode="semi_managed")
    receipt = await adapter.publish_listing(listing_payload())

    data = fake.only("bg.local.goods.add")["data"]
    assert data["goodsName"] == "Solar Garden Lamp"
    assert data["goodsDesc"].startswith("Weatherproof solar lamp")
    assert "- IP65 rated" in data["goodsDesc"]
    assert data["goodsImage"] == ["https://cdn.example.com/main.jpg"]
    assert data["categoryName"] == "LED Lighting"
    assert data["goodsSpecList"] == [{"specName": "Color", "specValues": ["Red", "Blue"]}]
    assert "hsCode" not in data
    skus = data["skuList"]
    assert [sku["extCode"] for sku in skus] == ["SKU-RED", "SKU-BLUE"]
    assert skus[0]["price"] == "13.34"
    assert skus[0]["currency"] == "USD"
    assert skus[0]["quantity"] == 4
    assert skus[0]["specList"] == [{"specKey": "Color", "specValue": "Red"}]
    assert skus[0]["images"] == ["https://cdn.example.com/red.jpg"]
    assert skus[1]["quantity"] == 0
    assert "specList" in skus[1]
    assert "images" not in skus[1]

    assert receipt.external_product_id == "9911"
    assert receipt.external_sku == "SKU-BASE"
    assert receipt.status == "PUBLISHING"
    assert [item.external_variant_id for item in receipt.variant_receipts] == [
        "sku-9911-1",
        "sku-9911-2",
    ]
    assert receipt.variant_receipts[0].external_inventory_item_id == "inv-9911"


async def test_publish_listing_single_variant_and_numeric_category() -> None:
    fake = FakeTemu(
        {"bg.goods.add": lambda _body: ok({"goodsId": "8801", "goodsStatus": "PUBLISHED"})}
    )
    adapter = build(fake.transport)
    payload = listing_payload(quantity=9, category="601452", variants=[], options=[])
    receipt = await adapter.publish_listing(payload)

    data = fake.only("bg.goods.add")["data"]
    assert data["catId"] == "601452"
    assert "categoryName" not in data
    assert data["skuList"] == [
        {
            "extCode": "SKU-BASE",
            "price": "12.35",
            "currency": "USD",
            "quantity": 9,
        }
    ]
    assert receipt.external_product_id == "8801"
    assert receipt.variant_receipts == []


async def test_publish_listing_accepts_legacy_connector_keywords() -> None:
    fake = FakeTemu(
        {"bg.local.goods.add": lambda _body: ok({"goodsId": 7700, "goodsStatus": "PUBLISHED"})}
    )
    adapter = build(fake.transport, fulfillment_mode="local")
    receipt = await adapter.publish_listing(
        sku="SKU-LEGACY",
        title="Legacy Lamp",
        description="desc",
        bullet_points=["bullet"],
        price_usd=Decimal("9.99"),
        quantity=2,
        images=["https://cdn.example.com/legacy.jpg"],
        category="",
        hs_code="9405.40",
        moq_tiers=[{"min_qty": 10, "unit_price_usd": Decimal("8.50")}],
        options=[{"name": "Size", "values": ["S", "M"]}],
        variants=[],
        image_blobs=[],
    )
    data = fake.only("bg.local.goods.add")["data"]
    assert data["skuList"][0]["extCode"] == "SKU-LEGACY"
    assert data["hsCode"] == "9405.40"
    assert data["priceLadder"] == [{"minQuantity": 10, "unitPrice": "8.50"}]
    assert data["goodsSpecList"] == [{"specName": "Size", "specValues": ["S", "M"]}]
    assert "catId" not in data
    assert "categoryName" not in data
    assert receipt.external_product_id == "7700"


async def test_publish_listing_uploads_image_blobs() -> None:
    fake = FakeTemu(
        {
            "bg.local.goods.image.upload": lambda _body: ok(
                {"imageUrl": "https://cdn.temu.com/uploaded.jpg"}
            ),
            "bg.local.goods.add": lambda _body: ok({"goodsId": 6600}),
        }
    )
    adapter = build(fake.transport, fulfillment_mode="semi_managed")
    payload = listing_payload(
        images=[],
        variants=[],
        image_blobs=[
            MarketplaceImageBlob(
                filename="lamp.jpg", mime_type="image/jpeg", data=b"\xff\xd8\xffmain"
            )
        ],
    )
    await adapter.publish_listing(payload)

    upload = fake.only("bg.local.goods.image.upload")["data"]
    assert upload["fileName"] == "lamp.jpg"
    assert upload["mimeType"] == "image/jpeg"
    assert base64.b64decode(upload["imageBase64"]) == b"\xff\xd8\xffmain"
    goods = fake.only("bg.local.goods.add")["data"]
    assert goods["goodsImage"] == ["https://cdn.temu.com/uploaded.jpg"]


async def test_publish_listing_surfaces_remote_rejection() -> None:
    fake = FakeTemu(
        {
            "bg.goods.add": lambda _body: httpx.Response(
                200,
                json={
                    "success": False,
                    "errorCode": "1300101",
                    "errorMsg": f"category rejected for {APP_SECRET}",
                },
            )
        }
    )
    adapter = build(fake.transport)
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(listing_payload())
    assert excinfo.value.code == "ERR_TEMU_API_ERROR"
    assert APP_SECRET not in excinfo.value.detail
    assert "1300101" in excinfo.value.detail


async def test_publish_listing_rejects_missing_goods_id() -> None:
    fake = FakeTemu({"bg.goods.add": lambda _body: ok({"goodsStatus": "PUBLISHED"})})
    adapter = build(fake.transport)
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(listing_payload())
    assert excinfo.value.code == "ERR_TEMU_LISTING_REJECTED"
    assert "SKU-BASE" in excinfo.value.detail


async def test_publish_listing_requires_sku_and_unambiguous_arguments() -> None:
    adapter = build(httpx.MockTransport(lambda _r: httpx.Response(200)))
    with pytest.raises(AppError) as excinfo:
        await adapter.publish_listing(listing_payload(external_sku="   "))
    assert excinfo.value.code == "ERR_TEMU_LISTING_REJECTED"
    assert excinfo.value.status_code == 422

    with pytest.raises(AppError) as both:
        await adapter.publish_listing(listing_payload(), sku="SKU-OTHER")
    assert both.value.code == "ERR_TEMU_LISTING_ARGUMENTS"
    assert both.value.status_code == 422

    with pytest.raises(AppError) as neither:
        await adapter.publish_listing()
    assert neither.value.code == "ERR_TEMU_LISTING_ARGUMENTS"


# --------------------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------------------


async def test_push_inventory_level_clamps_and_reports() -> None:
    fake = FakeTemu({"bg.goods.stock.update": lambda _body: ok({"updated": True})})
    adapter = build(fake.transport)
    result = await adapter.push_inventory_level(external_sku=" SKU-RED ", quantity=-7)

    data = fake.only("bg.goods.stock.update")["data"]
    assert data["extCode"] == "SKU-RED"
    assert data["quantity"] == 0
    assert result["channel"] == "temu"
    assert result["external_sku"] == "SKU-RED"
    assert result["quantity"] == 0
    assert result["raw"] == {"updated": True}


async def test_push_inventory_level_forwards_ids_and_mode_endpoint() -> None:
    fake = FakeTemu({"bg.local.goods.stock.update": lambda _body: ok({})})
    adapter = build(fake.transport, fulfillment_mode="local")
    result = await adapter.push_inventory_level(
        external_sku="SKU-RED",
        quantity=12,
        external_product_id="501",
        external_variant_id="sku-1",
        external_inventory_item_id="inv-1",
        location_id="loc-9",
        warehouse_id="wh-3",
        ignored_extra="ignored",
    )
    data = fake.only("bg.local.goods.stock.update")["data"]
    assert data == {
        "mallId": MALL_ID,
        "regionId": "US",
        "extCode": "SKU-RED",
        "quantity": 12,
        "goodsId": "501",
        "skuId": "sku-1",
        "inventoryItemId": "inv-1",
        "warehouseId": "wh-3",
        "locationId": "loc-9",
    }
    assert result["external_product_id"] == "501"
    assert result["external_variant_id"] == "sku-1"
    assert result["external_inventory_item_id"] == "inv-1"
    assert result["warehouse_id"] == "wh-3"
    assert result["location_id"] == "loc-9"
    assert result["quantity"] == 12


async def test_push_inventory_level_requires_sku() -> None:
    adapter = build(httpx.MockTransport(lambda _r: httpx.Response(200)))
    with pytest.raises(AppError) as excinfo:
        await adapter.push_inventory_level(external_sku="  ", quantity=1)
    assert excinfo.value.code == "ERR_TEMU_INVENTORY_SYNC"
    assert excinfo.value.status_code == 422
