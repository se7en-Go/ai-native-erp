"""Cross-channel Shopify-parity verification across all 16 ecommerce channels."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import erp_core.worker_main as worker_main
from erp_core.api_main import create_app
from erp_core.catalog.schemas import ProductCreate, SkuMappingCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.channels.ecommerce.alibaba_intl import AlibabaIntlAdapter
from erp_core.channels.ecommerce.aliexpress import AliExpressAdapter
from erp_core.channels.ecommerce.amazon_sp import AmazonSpApiAdapter
from erp_core.channels.ecommerce.base import (
    CredentialVaultHelper,
    EcommerceOrderSyncService,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
    register_channel_routing,
)
from erp_core.channels.ecommerce.connectors_router import (
    CREDENTIAL_TYPES as CONNECTOR_CREDENTIAL_TYPES,
)
from erp_core.channels.ecommerce.ebay import EbayConnectorAdapter
from erp_core.channels.ecommerce.fulfillment_dispatcher import (
    _extract_external_fulfillment_id,
    push_order_fulfillment,
)
from erp_core.channels.ecommerce.lazada import LazadaAdapter
from erp_core.channels.ecommerce.listing_dispatcher import SUPPORTED_CHANNEL_TYPES
from erp_core.channels.ecommerce.mercado_libre import MercadoLibreAdapter
from erp_core.channels.ecommerce.ozon import OzonAdapter
from erp_core.channels.ecommerce.shein import SheinAdapter
from erp_core.channels.ecommerce.shopee import ShopeeAdapter
from erp_core.channels.ecommerce.shoplazza import ShoplazzaAdapter
from erp_core.channels.ecommerce.shopline import ShoplineAdapter
from erp_core.channels.ecommerce.temu import TemuAdapter
from erp_core.channels.ecommerce.tiktok_shop import TikTokShopCoreAdapter
from erp_core.channels.ecommerce.walmart import WalmartAdapter
from erp_core.channels.ecommerce.woocommerce import WooCommerceAdapter
from erp_core.orders.order_service import OrderService
from erp_core.orders.reconciliation_worker import ECOMMERCE_CHANNELS
from erp_core.orders.schemas import ShipOrderRequest
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, PaymentStatus, TrackingProvider, UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal

ALL_16_ECOMMERCE_CHANNELS = frozenset(
    {
        "shopify",
        "woocommerce",
        "shopline",
        "shoplazza",
        "tiktok_shop",
        "amazon_sp",
        "ebay",
        "alibaba_email",
        "shopee",
        "lazada",
        "temu",
        "shein",
        "aliexpress",
        "ozon",
        "walmart",
        "mercado_libre",
    }
)


def _admin_url() -> str:
    return os.environ.get(
        "DATABASE_ADMIN_URL",
        "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
    )


async def _cleanup_tenant(tenant_id: UUID) -> None:
    admin_engine = create_async_engine(_admin_url())
    try:
        async with admin_engine.begin() as conn:
            for stmt in (
                "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                "DELETE FROM payment_records WHERE tenant_id = :tid",
                "DELETE FROM receivables_payables WHERE tenant_id = :tid",
                "DELETE FROM inventory_ledger WHERE tenant_id = :tid",
                "DELETE FROM inventory_balances WHERE tenant_id = :tid",
                "DELETE FROM shipments WHERE tenant_id = :tid",
                "DELETE FROM order_items WHERE tenant_id = :tid",
                "DELETE FROM orders WHERE tenant_id = :tid",
                "DELETE FROM platform_sku_mappings WHERE tenant_id = :tid",
                "DELETE FROM product_variants WHERE tenant_id = :tid",
                "DELETE FROM products WHERE tenant_id = :tid",
                "DELETE FROM customer_identities WHERE tenant_id = :tid",
                "DELETE FROM customers WHERE tenant_id = :tid",
                "DELETE FROM channel_credentials WHERE tenant_id = :tid",
                "DELETE FROM channel_routing_directory WHERE tenant_id = :tid",
                "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                "DELETE FROM warehouses WHERE tenant_id = :tid",
                "DELETE FROM tenant_keys WHERE tenant_id = :tid",
                "DELETE FROM audit_events WHERE tenant_id = :tid",
                "DELETE FROM raw_webhook_events WHERE tenant_id = :tid",
                "DELETE FROM tenants WHERE id = :tid",
            ):
                await conn.execute(text(stmt), {"tid": tenant_id})
    finally:
        await admin_engine.dispose()


def test_all_16_ecommerce_channels_registered_in_worker_webhooks_and_dispatchers() -> None:
    assert frozenset(c.value for c in ECOMMERCE_CHANNELS) == ALL_16_ECOMMERCE_CHANNELS
    assert SUPPORTED_CHANNEL_TYPES == ALL_16_ECOMMERCE_CHANNELS
    assert ALL_16_ECOMMERCE_CHANNELS.issubset(frozenset(worker_main.COMMERCE_TRACKING_CHANNELS))
    non_shopify = ALL_16_ECOMMERCE_CHANNELS - {"shopify"}
    assert set(CONNECTOR_CREDENTIAL_TYPES.keys()) == non_shopify

    app = create_app()
    paths = set(app.openapi()["paths"].keys())
    for webhook_path in (
        "/api/v1/webhooks/shopify",
        "/api/v1/webhooks/woocommerce",
        "/api/v1/webhooks/shopline",
        "/api/v1/webhooks/shoplazza",
        "/api/v1/webhooks/tiktok",
        "/api/v1/webhooks/amazon-sp",
        "/api/v1/webhooks/ebay",
        "/api/v1/webhooks/alibaba-intl",
        "/api/v1/webhooks/shopee",
        "/api/v1/webhooks/lazada",
        "/api/v1/webhooks/temu",
        "/api/v1/webhooks/shein",
        "/api/v1/webhooks/aliexpress",
        "/api/v1/webhooks/ozon",
        "/api/v1/webhooks/walmart",
        "/api/v1/webhooks/mercado-libre",
    ):
        assert webhook_path in paths, f"Missing webhook route {webhook_path}"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"fulfillment": {"id": "gid://shopify/Fulfillment/1"}}, "gid://shopify/Fulfillment/1"),
        ({"fulfillment_id": "ful-wc-1"}, "ful-wc-1"),
        ({"external_fulfillment_id": "amz-ship-1"}, "amz-ship-1"),
        ({"external_shipment_id": "meli-ship-1"}, "meli-ship-1"),
        ({"fulfillment_order_id": "ozon-pkg-1"}, "ozon-pkg-1"),
        ({"package_id": "tt-pkg-1"}, "tt-pkg-1"),
        ({"data": {"id": "shopline-9"}}, "shopline-9"),
        ({"raw": {"packageNo": "shein-pkg-7"}}, "shein-pkg-7"),
        ({"raw": {"packageSn": "temu-pkg-8"}}, "temu-pkg-8"),
        ({"result": {"fulfillmentId": "ebay-ful-4"}}, "ebay-ful-4"),
    ],
)
def test_extract_external_fulfillment_id_supports_all_16_channel_shapes(
    payload: dict[str, Any], expected: str
) -> None:
    assert _extract_external_fulfillment_id(payload) == expected


@pytest.mark.asyncio
async def test_all_non_shopify_adapters_normalize_refund_partial_refund_and_cancel() -> None:
    # WooCommerce
    wc = WooCommerceAdapter("https://wc.example.com", "ck", "cs")
    wc_partial = wc._normalize_order(
        {
            "id": 101,
            "number": "101",
            "status": "processing",
            "currency": "USD",
            "total": "40.00",
            "refunded_amount": "15.00",
            "refunds": [{"total": "-15.00"}],
            "billing": {"email": "a@example.com"},
            "line_items": [{"sku": "SKU-1", "name": "LED", "quantity": 2, "total": "40.00"}],
            "date_modified_gmt": "2026-11-25T00:00:00Z",
        }
    )
    assert wc_partial.payment_status == PaymentStatus.PAID
    assert wc_partial.channel_metadata["refunded_amount"] == "15.00"
    assert (
        wc._normalize_order(
            {
                "id": 102,
                "status": "cancelled",
                "currency": "USD",
                "total": "40.00",
                "billing": {"email": "a@example.com"},
                "line_items": [{"sku": "SKU-1", "name": "LED", "quantity": 1, "total": "40.00"}],
                "date_modified_gmt": "2026-11-25T00:00:00Z",
            }
        ).fulfillment_status
        == FulfillmentStatus.CANCELLED
    )
    assert (
        wc._normalize_order(
            {
                "id": 103,
                "status": "refunded",
                "currency": "USD",
                "total": "40.00",
                "billing": {"email": "a@example.com"},
                "line_items": [{"sku": "SKU-1", "name": "LED", "quantity": 1, "total": "40.00"}],
                "date_modified_gmt": "2026-11-25T00:00:00Z",
            }
        ).fulfillment_status
        == FulfillmentStatus.REFUNDED
    )

    # Shopline & Shoplazza
    sl = ShoplineAdapter("sl.myshopline.com", "tok")
    sl_partial = sl._normalize_order(
        {
            "id": "sl-1",
            "financial_status": "partially_refunded",
            "currency": "USD",
            "total_price": "50.00",
            "total_refunded": "10.00",
            "customer": {"email": "b@example.com"},
            "line_items": [{"sku": "SKU-1", "title": "LED", "quantity": 1, "price": "50.00"}],
            "updated_at": "2026-11-25T00:00:00Z",
        }
    )
    assert sl_partial.payment_status == PaymentStatus.PAID
    assert sl_partial.channel_metadata["refunded_amount"] == "10.00"
    assert (
        sl._normalize_order(
            {
                "id": "sl-2",
                "financial_status": "refunded",
                "currency": "USD",
                "total_price": "50.00",
                "total_refunded": "50.00",
                "customer": {"email": "b@example.com"},
                "line_items": [{"sku": "SKU-1", "title": "LED", "quantity": 1, "price": "50.00"}],
                "updated_at": "2026-11-25T00:00:00Z",
            }
        ).fulfillment_status
        == FulfillmentStatus.REFUNDED
    )

    sz = ShoplazzaAdapter("sz.myshoplazza.com", "tok")
    assert (
        sz._normalize_order(
            {
                "id": "sz-1",
                "cancelled_at": "2026-11-25T01:00:00Z",
                "financial_status": "refunded",
                "currency": "USD",
                "total_price": "30.00",
                "total_refunded": "30.00",
                "customer": {"email": "c@example.com"},
                "line_items": [{"sku": "SKU-1", "title": "LED", "quantity": 1, "price": "30.00"}],
                "updated_at": "2026-11-25T00:00:00Z",
            }
        ).fulfillment_status
        == FulfillmentStatus.CANCELLED
    )

    # TikTok Shop
    tts = TikTokShopCoreAdapter("ak", "as", "at", "cipher")
    tts_ref = tts._normalize_order(
        {
            "id": "tt-1",
            "status": "refunded",
            "payment": {"currency": "USD", "total_amount": "20.00"},
            "refunded_amount": "20.00",
            "line_items": [{"seller_sku": "SKU-1", "product_name": "LED", "sale_price": "20.00"}],
            "update_time": 1764028800,
        }
    )
    assert (tts_ref.payment_status, tts_ref.fulfillment_status) == (
        PaymentStatus.REFUNDED,
        FulfillmentStatus.REFUNDED,
    )

    # Amazon SP & eBay & Alibaba & Walmart
    def amz_handler(request: httpx.Request) -> httpx.Response:
        if "oauth" in str(request.url) or "token" in request.url.path:
            return httpx.Response(200, json={"access_token": "lwa-tok", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "payload": {
                    "OrderItems": [
                        {
                            "SellerSKU": "SKU-1",
                            "Title": "LED",
                            "QuantityOrdered": 1,
                            "ItemPrice": {"Amount": "60.00"},
                        }
                    ]
                }
            },
        )

    amz = AmazonSpApiAdapter(
        seller_id="S1",
        marketplace_id="ATVPDKIKX0DER",
        lwa_client_id="cid",
        lwa_client_secret="csec",
        refresh_token="rt",
        transport=httpx.MockTransport(amz_handler),
    )
    amz_cancel = await amz._normalize_order(
        {
            "AmazonOrderId": "111-222",
            "OrderStatus": "Cancelled",
            "OrderTotal": {"CurrencyCode": "USD", "Amount": "60.00"},
            "LastUpdateDate": "2026-11-25T00:00:00Z",
        }
    )
    assert amz_cancel is not None
    assert amz_cancel.fulfillment_status == FulfillmentStatus.CANCELLED

    ebay = EbayConnectorAdapter(
        seller_username="seller",
        site_id="EBAY_US",
        app_id="aid",
        cert_id="cid",
        refresh_token="rt",
    )
    ebay_ref = ebay._normalize_order(
        {
            "orderId": "EB-1",
            "orderPaymentStatus": "FULLY_REFUNDED",
            "orderFulfillmentStatus": "FULFILLED",
            "pricingSummary": {"total": {"value": "25.00", "currency": "USD"}},
            "paymentSummary": {"refunds": [{"amount": {"value": "25.00"}}]},
            "lineItems": [
                {
                    "sku": "SKU-1",
                    "title": "LED",
                    "quantity": 1,
                    "lineItemCost": {"value": "25.00"},
                }
            ],
            "lastModifiedDate": "2026-11-25T00:00:00Z",
        }
    )
    assert ebay_ref is not None
    assert (ebay_ref.payment_status, ebay_ref.fulfillment_status) == (
        PaymentStatus.REFUNDED,
        FulfillmentStatus.REFUNDED,
    )

    ali = AlibabaIntlAdapter(
        store_id="st1",
        app_key="ak",
        app_secret="as",
        access_token="at",
    )
    assert (
        ali._normalize_order(
            {
                "order_id": "AL-EMPTY",
                "order_status": "paid",
                "currency": "USD",
                "total_amount": "10.00",
                "product_items": [],
            },
            fallback_updated_at=datetime.now(UTC),
        )
        is None
    )

    wm = WalmartAdapter(seller_id="S1", client_id="cid", client_secret="csec")
    wm_partial = wm._normalize_order(
        {
            "purchaseOrderId": "WM-1",
            "paymentStatus": "PARTIALLY_REFUNDED",
            "orderDate": "2026-11-25T00:00:00Z",
            "orderLines": {
                "orderLine": [
                    {
                        "lineNumber": "1",
                        "item": {"sku": "SKU-1", "productName": "LED"},
                        "orderLineQuantity": {"amount": "2"},
                        "charges": {
                            "charge": [
                                {
                                    "chargeType": "PRODUCT",
                                    "chargeAmount": {"currency": "USD", "amount": "40.00"},
                                }
                            ]
                        },
                        "orderLineStatuses": {"orderLineStatus": [{"status": "Shipped"}]},
                        "refund": {
                            "refundCharges": {
                                "refundCharge": [
                                    {
                                        "refundReason": "Customer",
                                        "charge": {
                                            "chargeAmount": {
                                                "currency": "USD",
                                                "amount": "20.00",
                                            }
                                        },
                                    }
                                ]
                            }
                        },
                    }
                ]
            },
        }
    )
    assert wm_partial is not None
    assert wm_partial.payment_status == PaymentStatus.PAID
    assert wm_partial.channel_metadata["refunded_amount"] == "20.00"

    # Shopee, Lazada, Temu, SHEIN, AliExpress, Ozon, Mercado Libre
    shp = ShopeeAdapter(
        shop_id="123",
        partner_id="1",
        partner_key="pk",
        access_token="at",
        fulfillment_mode="full_managed",
    )
    assert shp.fulfillment_mode == "standard"
    assert (
        shp._normalize_order(
            {
                "order_sn": "SHP-1",
                "order_status": "REFUNDED",
                "currency": "USD",
                "total_amount": "15.00",
                "refund_amount": "15.00",
                "item_list": [
                    {
                        "item_sku": "SKU-1",
                        "item_name": "LED",
                        "model_quantity_purchased": 1,
                        "model_discounted_price": "15.00",
                    }
                ],
                "update_time": 1764028800,
            }
        ).fulfillment_status
        == FulfillmentStatus.REFUNDED
    )

    laz = LazadaAdapter(
        seller_id="S1",
        app_key="ak",
        app_secret="as",
        access_token="at",
        fulfillment_mode="full_managed",
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200,
                json={"data": [{"sku": "SKU-1", "name": "LED", "item_price": "20.00"}]},
            )
        ),
    )
    assert laz.fulfillment_mode == "full_managed"
    laz_ref = await laz._normalize_order(
        {
            "order_id": "LAZ-1",
            "status": "refunded",
            "currency": "USD",
            "price": "20.00",
            "refunded_amount": "20.00",
            "items": [{"sku": "SKU-1", "name": "LED", "item_price": "20.00"}],
        },
        fallback_updated_at=datetime.now(UTC),
    )
    assert laz_ref is not None
    assert (laz_ref.payment_status, laz_ref.fulfillment_status) == (
        PaymentStatus.REFUNDED,
        FulfillmentStatus.REFUNDED,
    )

    tmu = TemuAdapter(mall_id="M1", app_key="ak", app_secret="as", access_token="at")
    tmu_unpaid = tmu._normalize_order(
        {
            "orderSn": "TMU-1",
            "parentOrderStatus": "UNPAID",
            "orderAmount": {"currency": "USD", "totalAmount": "25.00"},
            "goodsList": [
                {"sellerSku": "SKU-1", "goodsName": "LED", "quantity": 1, "goodsPrice": "25.00"}
            ],
        }
    )
    assert tmu_unpaid is not None
    assert (tmu_unpaid.payment_status, tmu_unpaid.fulfillment_status) == (
        PaymentStatus.UNPAID,
        FulfillmentStatus.PENDING_PAYMENT,
    )

    shn = SheinAdapter(seller_id="S1", open_key_id="kid", secret_key="sec")
    shn_partial = shn._normalize_order(
        {
            "orderNo": "SHN-1",
            "orderStatus": "3",
            "currency": "USD",
            "orderAmount": "30.00",
            "refundAmount": "10.00",
            "goodsList": [
                {"sellerSku": "SKU-1", "goodsTitle": "LED", "quantity": 1, "unitPrice": "30.00"}
            ],
        },
        fallback_updated_at=datetime.now(UTC),
    )
    assert shn_partial is not None
    assert shn_partial.payment_status == PaymentStatus.PAID
    assert shn_partial.channel_metadata["refunded_amount"] == "10.00"

    ae = AliExpressAdapter(seller_id="S1", app_key="ak", app_secret="as", access_token="at")
    ae_cancel = ae._normalize_order(
        {
            "order_id": "AE-1",
            "order_status": "cancelled",
            "currency": "USD",
            "pay_amount": "18.00",
            "order_line_list": [
                {
                    "sku_code": "SKU-1",
                    "product_name": "LED",
                    "product_count": 1,
                    "product_price": "18.00",
                }
            ],
        },
        fallback_updated_at=datetime.now(UTC),
    )
    assert ae_cancel is not None
    assert ae_cancel.fulfillment_status == FulfillmentStatus.CANCELLED

    ozn = OzonAdapter(client_id="C1", api_key="ak")
    ozn_partial = ozn._normalize_order(
        {
            "posting_number": "OZN-1",
            "status": "delivering",
            "refunded_amount": "5.00",
            "products": [{"offer_id": "SKU-1", "name": "LED", "quantity": 1, "price": "20.00"}],
        },
        fallback_updated_at=datetime.now(UTC),
    )
    assert ozn_partial is not None
    assert ozn_partial.channel_metadata["refunded_amount"] == "5.00"

    ml = MercadoLibreAdapter(
        seller_id="S1",
        access_token="at",
        site_id="MLM",
        fulfillment_mode="full_managed",
    )
    assert ml.fulfillment_mode == "full"
    ml_partial = ml._normalize_order(
        {
            "id": "ML-1",
            "status": "partially_refunded",
            "currency_id": "USD",
            "total_amount": "40.00",
            "payments": [{"status": "partially_refunded", "transaction_amount_refunded": "12.00"}],
            "order_items": [
                {
                    "item": {"seller_sku": "SKU-1", "title": "LED"},
                    "quantity": 1,
                    "unit_price": "40.00",
                }
            ],
            "last_updated": "2026-11-25T00:00:00Z",
        }
    )
    assert ml_partial is not None
    assert ml_partial.payment_status == PaymentStatus.PAID
    assert ml_partial.channel_metadata["refunded_amount"] == "12.00"


@pytest.mark.asyncio
async def test_tiktok_create_fulfillment_auto_resolves_package_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/order/202309/orders":
            return httpx.Response(
                200,
                json={"data": {"orders": [{"id": "TT-ORD-1", "packages": [{"id": "TT-PKG-99"}]}]}},
            )
        if request.url.path == "/fulfillment/202309/packages/TT-PKG-99/ship":
            return httpx.Response(200, json={"data": {"package_id": "TT-PKG-99"}})
        raise AssertionError(f"Unexpected path {request.url.path}")

    tts = TikTokShopCoreAdapter("ak", "as", "at", "cipher", transport=httpx.MockTransport(handler))
    res = await tts.create_fulfillment(
        external_order_id="TT-ORD-1",
        tracking_number="DHL-TT-1",
        carrier_company="DHL",
        fulfillment_order_id=None,
    )
    assert res["fulfillment_id"] == "TT-PKG-99"
    assert _extract_external_fulfillment_id(res) == "TT-PKG-99"


@pytest.mark.asyncio
async def test_multi_warehouse_ship_auto_resolves_and_storefront_webhook_syncs_order(
    runtime_session_factory: async_sessionmaker[AsyncSession],
    migrator_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, account_id, wh_default, wh_channel, user_id = (uuid4() for _ in range(5))
    domain = f"wc-{tenant_id.hex[:6]}.example.com"
    order_state = {"status": "processing", "refunded": "0.00"}

    def wc_transport(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/orders" in request.url.path:
            return httpx.Response(
                200,
                headers={"X-WP-TotalPages": "1"},
                json=[
                    {
                        "id": 7788,
                        "number": "WC-7788",
                        "status": order_state["status"],
                        "currency": "USD",
                        "total": "30.00",
                        "refunded_amount": order_state["refunded"],
                        "billing": {"email": "wc-buyer@example.com"},
                        "line_items": [
                            {"sku": "WC-SKU-1", "name": "LED", "quantity": 2, "total": "30.00"}
                        ],
                        "date_modified_gmt": "2026-11-25T05:00:00Z",
                    }
                ],
            )
        if request.method == "POST" and "/fulfillments" in request.url.path:
            return httpx.Response(201, json={"id": "wc-ful-555"})
        if request.method == "POST" and "/notes" in request.url.path:
            return httpx.Response(201, json={"id": 1})
        raise AssertionError(f"Unexpected WC call {request.method} {request.url}")

    crypto = EnvelopeCryptoService(get_settings())
    try:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, base_currency) "
                    "VALUES (:tid, 'Parity Shop', :slug, 'USD')"
                ),
                {"tid": tenant_id, "slug": f"par-{tenant_id.hex}"},
            )
            await session.execute(
                text(
                    "INSERT INTO warehouses (id, tenant_id, code, name, is_active, created_at) "
                    "VALUES (:w1, :tid, 'WH-1', 'Default WH', true, now() - interval '2 days'), "
                    "       (:w2, :tid, 'WH-2', 'Channel WH', true, now() - interval '1 day')"
                ),
                {"tid": tenant_id, "w1": wh_default, "w2": wh_channel},
            )
            await session.execute(
                text(
                    "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                    "external_account_id, display_name, config) "
                    "VALUES (:id, :tid, 'woocommerce', :ext, 'EU Woo Store', CAST(:cfg AS jsonb))"
                ),
                {
                    "id": account_id,
                    "tid": tenant_id,
                    "ext": domain,
                    "cfg": json.dumps({"warehouse_id": str(wh_channel)}),
                },
            )
            await register_channel_routing(
                session,
                channel_type="woocommerce",
                external_account_id=domain,
                tenant_id=tenant_id,
                channel_account_id=account_id,
            )
            await crypto.provision_tenant_dek(session, tenant_id)
            await CredentialVaultHelper(crypto).save_secret_json(
                session,
                tenant_id=tenant_id,
                channel_account_id=account_id,
                credential_type="woocommerce_keys",
                payload={
                    "store_url": f"https://{domain}",
                    "consumer_key": "ck_test",
                    "consumer_secret": "cs_test",
                    "webhook_secret": "wc_sec",
                },
            )
            catalog = CatalogService()
            product = await catalog.create_product(
                session,
                tenant_id,
                actor_id="seed",
                payload=ProductCreate(spu_code="SPU-PAR-1", category="led"),
            )
            variant = await catalog.create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=product.id,
                    sku="LED-PAR-1",
                    cost_price_base=Decimal("5.0000"),
                    retail_price_usd=Decimal("15.00"),
                    weight_grams=200,
                ),
            )
            await catalog.create_sku_mapping(
                session,
                tenant_id,
                actor_id="seed",
                payload=SkuMappingCreate(
                    variant_id=variant.id,
                    channel_account_id=account_id,
                    external_product_id="wc-prod-1",
                    external_sku="WC-SKU-1",
                    bundle_multiplier=1,
                ),
            )
            await session.execute(
                text(
                    "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
                    "on_hand_qty, reserved_qty) VALUES (:tid, :wh, :vid, 20, 0)"
                ),
                {"tid": tenant_id, "wh": wh_channel, "vid": variant.id},
            )

            dto = NormalizedOrder(
                external_order_id="7788",
                external_order_number="WC-7788",
                currency="USD",
                subtotal_amount=Decimal("30.00"),
                total_amount=Decimal("30.00"),
                fulfillment_status=FulfillmentStatus.CONFIRMED_RESERVED,
                payment_status=PaymentStatus.PAID,
                customer=NormalizedCustomer(email="wc-buyer@example.com", display_name="WC Buyer"),
                line_items=[
                    NormalizedLineItem(
                        external_sku="WC-SKU-1",
                        title="LED",
                        quantity=2,
                        line_total=Decimal("30.00"),
                    )
                ],
                updated_at=datetime.now(UTC),
            )
            order_id, created = await EcommerceOrderSyncService().ingest_normalized_order(
                session,
                tenant_id=tenant_id,
                channel_account_id=account_id,
                default_warehouse_id=wh_channel,
                order_dto=dto,
            )
            assert created is True

        # Verify list_orders exposes channel_type & channel_label, and webhook syncs partial refund
        receipt_id = uuid4()
        async with migrator_session_factory() as broker_sess, broker_sess.begin():
            await broker_sess.execute(
                text(
                    "INSERT INTO raw_webhook_events "
                    "(id, tenant_id, channel_type, external_account_id, external_event_id, "
                    "signature_verified, processing_status, raw_payload, headers) "
                    "VALUES (:id, NULL, 'woocommerce', :ext, :evt, true, 'queued', "
                    "CAST(:payload AS jsonb), CAST(:hdrs AS jsonb))"
                ),
                {
                    "id": receipt_id,
                    "ext": domain,
                    "evt": f"evt-{receipt_id.hex}",
                    "payload": json.dumps({"id": 7788}),
                    "hdrs": json.dumps({"topic": "order.updated", "shop_domain": domain}),
                },
            )

        async def _allow_any_store(_url: str, *, allow_local: bool = False) -> None:
            del _url, allow_local

        async def _fake_instantiate(
            _ctype: Any, _creds: Any, _wh: Any, transport: Any = None
        ) -> WooCommerceAdapter:
            del _ctype, _creds, _wh, transport
            return WooCommerceAdapter(
                f"https://{domain}", "ck", "cs", transport=httpx.MockTransport(wc_transport)
            )

        order_state["refunded"] = "10.00"
        monkeypatch.setattr(
            "erp_core.channels.ecommerce.woocommerce.validate_store_url",
            _allow_any_store,
        )
        monkeypatch.setattr(worker_main, "_instantiate_adapter", _fake_instantiate)
        monkeypatch.setattr(
            "erp_core.orders.reconciliation_worker._instantiate_adapter",
            _fake_instantiate,
        )
        drained = await worker_main._handle_storefront_webhook_delivery(
            str(receipt_id),
            broker_session_factory=migrator_session_factory,
            runtime_session_factory=runtime_session_factory,
        )
        assert drained == 1

        # Verify webhook reconciled partial refund AND ship_order auto-resolves wh_channel
        principal = CurrentPrincipal(
            user_id=user_id,
            tenant_id=tenant_id,
            role=UserRole.OWNER,
            email="owner@example.com",
        )
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            listed = await OrderService().list_orders(session, tenant_id)
            assert listed.items[0].channel_type == "woocommerce"
            assert listed.items[0].channel_label == "EU Woo Store"
            assert listed.items[0].channel_metadata["refunded_amount"] == "10.00"

            shipment = await OrderService().ship_order(
                session,
                tenant_id,
                principal=principal,
                order_id=order_id,
                payload=ShipOrderRequest(
                    warehouse_id=None,
                    tracking_number="DHL-WC-7788",
                    carrier_code="DHL",
                    tracking_provider=TrackingProvider("manual"),
                ),
            )
            assert shipment.tracking_number == "DHL-WC-7788"

            wh_bal = (
                await session.execute(
                    text(
                        "SELECT on_hand_qty, reserved_qty FROM inventory_balances "
                        "WHERE tenant_id = :tid AND warehouse_id = :wh AND variant_id = :vid"
                    ),
                    {"tid": tenant_id, "wh": wh_channel, "vid": variant.id},
                )
            ).one()
            assert (wh_bal.on_hand_qty, wh_bal.reserved_qty) == (18, 0)

        async with tenant_session(runtime_session_factory, tenant_id) as session:
            push_res = await push_order_fulfillment(
                session,
                tenant_id,
                order_id,
                tracking_number="DHL-WC-7788",
                carrier_company="DHL",
                raise_on_error=True,
                transport=httpx.MockTransport(wc_transport),
            )
            assert push_res is not None
            assert push_res["status"] == "synced"
            assert push_res["external_fulfillment_id"] == "wc-ful-555"
    finally:
        await _cleanup_tenant(tenant_id)
