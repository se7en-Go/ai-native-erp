"""Phase 3 PG17 RLS tests: 8-channel analytics, ROAS, velocity & 1688 replenishment."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.supply_chain.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.catalog.schemas import ProductCreate, SkuMappingCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.channels.ecommerce.alibaba_intl import AlibabaIntlAdapter
from erp_core.channels.ecommerce.amazon_sp import AmazonSpApiAdapter
from erp_core.channels.ecommerce.base import (
    EcommerceOrderSyncService,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
)
from erp_core.channels.ecommerce.ebay import EbayConnectorAdapter
from erp_core.channels.ecommerce.shopify import ShopifyGraphqlAdapter
from erp_core.channels.ecommerce.shoplazza import ShoplazzaAdapter
from erp_core.channels.ecommerce.shopline import ShoplineAdapter
from erp_core.channels.ecommerce.tiktok_shop import TikTokShopCoreAdapter
from erp_core.channels.ecommerce.woocommerce import WooCommerceAdapter
from erp_core.finance.router import router as finance_router
from erp_core.orders.inventory_service import InventoryLedgerService
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import IdentityType, UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.supply_chain.router import supply_router, wms_router
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]


async def _cleanup_tenants(tenant_ids: list[UUID]) -> None:
    admin = create_async_engine(
        os.environ.get(
            "DATABASE_ADMIN_URL",
            "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
        )
    )
    try:
        async with admin.begin() as conn:
            for tid in tenant_ids:
                for stmt in (
                    "DELETE FROM channel_daily_metrics WHERE tenant_id = :tid",
                    "DELETE FROM inventory_sync_jobs WHERE tenant_id = :tid",
                    "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                    "DELETE FROM payment_records WHERE tenant_id = :tid",
                    "DELETE FROM receivables_payables WHERE tenant_id = :tid",
                    "DELETE FROM purchase_order_items WHERE tenant_id = :tid",
                    "DELETE FROM purchase_orders WHERE tenant_id = :tid",
                    "DELETE FROM suppliers WHERE tenant_id = :tid",
                    "DELETE FROM inventory_ledger WHERE tenant_id = :tid",
                    "DELETE FROM inventory_balances WHERE tenant_id = :tid",
                    "DELETE FROM order_items WHERE tenant_id = :tid",
                    "DELETE FROM orders WHERE tenant_id = :tid",
                    "DELETE FROM platform_sku_mappings WHERE tenant_id = :tid",
                    "DELETE FROM product_variants WHERE tenant_id = :tid",
                    "DELETE FROM products WHERE tenant_id = :tid",
                    "DELETE FROM customer_identities WHERE tenant_id = :tid",
                    "DELETE FROM customers WHERE tenant_id = :tid",
                    "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                    "DELETE FROM sourcing_drafts WHERE tenant_id = :tid",
                    "DELETE FROM warehouses WHERE tenant_id = :tid",
                    "DELETE FROM audit_events WHERE tenant_id = :tid",
                    "DELETE FROM tenant_memberships WHERE tenant_id = :tid",
                ):
                    await conn.execute(text(stmt), {"tid": tid})
                await conn.execute(
                    text("DELETE FROM users WHERE email LIKE :pat"),
                    {"pat": f"%{tid.hex}@analytics-test.example.com"},
                )
                await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid})
    finally:
        await admin.dispose()


@pytest.fixture
async def rt_session_factory() -> AsyncIterator[tuple[Factory, list[UUID]]]:
    engine = create_async_engine(
        os.environ.get("DATABASE_RUNTIME_URL", get_settings().DATABASE_RUNTIME_URL)
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created: list[UUID] = []
    try:
        yield factory, created
    finally:
        await engine.dispose()
        if created:
            await _cleanup_tenants(created)


def test_all_8_adapters_normalize_refunds_and_commissions_decimal() -> None:
    now_iso = "2026-11-23T08:00:00Z"

    sh_dto = ShopifyGraphqlAdapter._normalize_order_node(
        {
            "id": "gid://shopify/Order/101",
            "name": "#101",
            "updatedAt": now_iso,
            "displayFinancialStatus": "PAID",
            "displayFulfillmentStatus": "FULFILLED",
            "currencyCode": "USD",
            "currentSubtotalPriceSet": {"shopMoney": {"amount": "100.00"}},
            "totalShippingPriceSet": {"shopMoney": {"amount": "0.00"}},
            "totalTaxSet": {"shopMoney": {"amount": "0.00"}},
            "currentTotalPriceSet": {"shopMoney": {"amount": "100.00"}},
            "totalRefundedSet": {"shopMoney": {"amount": "12.50"}},
            "platform_commission": "3.20",
            "customer": {"email": "a@example.com"},
            "lineItems": {
                "nodes": [
                    {
                        "sku": "SKU-1",
                        "quantity": 1,
                        "originalTotalSet": {"shopMoney": {"amount": "100.00"}},
                    }
                ]
            },
        }
    )
    assert sh_dto.channel_metadata["refunded_amount"] == "12.50"
    assert sh_dto.channel_metadata["platform_commission_base"] == "3.20"

    tt_adapter = TikTokShopCoreAdapter("k", "s", "t", "c")
    tt_dto = tt_adapter._normalize_order(
        {
            "id": "tt-1",
            "status": "AWAITING_SHIPMENT",
            "update_time": 1763884800,
            "payment": {
                "currency": "USD",
                "sub_total": "50.00",
                "total_amount": "50.00",
                "platform_commission": "4.50",
                "seller_refund_amount": "5.00",
            },
            "line_items": [{"seller_sku": "SKU-1", "quantity": 1, "sale_price": "50.00"}],
        }
    )
    assert tt_dto.channel_metadata["refunded_amount"] == "5.00"
    assert tt_dto.channel_metadata["platform_commission_base"] == "4.50"

    woo_adapter = WooCommerceAdapter("https://woo.example.com", "ck", "cs")
    woo_dto = woo_adapter._normalize_order(
        {
            "id": 201,
            "status": "completed",
            "total": "80.00",
            "shipping_total": "0.00",
            "discount_total": "0.00",
            "total_tax": "0.00",
            "refunds": [{"id": 1, "total": "-10.00"}],
            "platform_commission": "2.40",
            "date_modified_gmt": "2026-11-23T08:00:00",
            "line_items": [{"sku": "SKU-1", "quantity": 1, "total": "80.00"}],
        }
    )
    assert woo_dto.channel_metadata["refunded_amount"] == "10.00"
    assert woo_dto.channel_metadata["platform_commission_base"] == "2.40"

    sl_adapter = ShoplineAdapter("store.myshopline.com", "tok")
    sl_dto = sl_adapter._normalize_order(
        {
            "id": "sl-1",
            "financial_status": "paid",
            "subtotal_price": "60.00",
            "total_price": "60.00",
            "total_refunded": "6.00",
            "platform_fee": "1.80",
            "updated_at": now_iso,
            "line_items": [{"sku": "SKU-1", "quantity": 1, "price": "60.00"}],
        }
    )
    assert sl_dto.channel_metadata["refunded_amount"] == "6.00"
    assert sl_dto.channel_metadata["platform_commission_base"] == "1.80"

    sz_adapter = ShoplazzaAdapter("store.myshoplaza.com", "tok")
    sz_dto = sz_adapter._normalize_order(
        {
            "id": "sz-1",
            "financial_status": "paid",
            "subtotal_price": "70.00",
            "total_price": "70.00",
            "total_refunded": "7.00",
            "platform_commission": "2.10",
            "updated_at": now_iso,
            "line_items": [{"sku": "SKU-1", "quantity": 1, "price": "70.00"}],
        }
    )
    assert sz_dto.channel_metadata["refunded_amount"] == "7.00"
    assert sz_dto.channel_metadata["platform_commission_base"] == "2.10"

    ebay_adapter = EbayConnectorAdapter(
        seller_username="seller",
        site_id="EBAY_US",
        app_id="app",
        cert_id="cert",
        refresh_token="ref",
    )
    ebay_dto = ebay_adapter._normalize_order(
        {
            "orderId": "ebay-1",
            "pricingSummary": {"total": {"value": "90.00", "currency": "USD"}},
            "paymentSummary": {"refunds": [{"amount": {"value": "9.00", "currency": "USD"}}]},
            "totalMarketplaceFee": {"value": "11.70", "currency": "USD"},
            "lineItems": [
                {
                    "sku": "SKU-1",
                    "quantity": 1,
                    "lineItemCost": {"value": "90.00", "currency": "USD"},
                }
            ],
            "lastModifiedDate": now_iso,
        }
    )
    assert ebay_dto is not None
    assert ebay_dto.channel_metadata["refunded_amount"] == "9.00"
    assert ebay_dto.channel_metadata["platform_commission_base"] == "11.70"

    ali_adapter = AlibabaIntlAdapter(store_id="st-1", app_key="ak", app_secret="as")
    ali_dto = ali_adapter._normalize_order(
        {
            "trade_id": "ali-1",
            "status": "success",
            "total_amount": "500.00",
            "platform_commission": "15.00",
            "refund_amount": "25.00",
            "order_products": [{"sku_code": "SKU-1", "quantity": 10, "line_total": "500.00"}],
        },
        fallback_updated_at=datetime.now(UTC),
    )
    assert ali_dto is not None
    assert ali_dto.channel_metadata["refunded_amount"] == "25.00"
    assert ali_dto.channel_metadata["platform_commission_base"] == "15.00"


@pytest.mark.asyncio
async def test_amazon_adapter_normalizes_refund_and_commission_metadata() -> None:
    amz = AmazonSpApiAdapter(
        seller_id="S1",
        marketplace_id="ATVPDKIKX0DER",
        lwa_client_id="cid",
        lwa_client_secret="csec",
        refresh_token="ref",
    )
    amz._fetch_order_items = lambda _oid: _async_val(  # type: ignore[method-assign]
        [
            {
                "SellerSKU": "SKU-AMZ",
                "QuantityOrdered": 2,
                "ItemPrice": {"Amount": "120.00", "CurrencyCode": "USD"},
                "CommissionFee": {"Amount": "18.00", "CurrencyCode": "USD"},
            }
        ]
    )
    dto = await amz._normalize_order(
        {
            "AmazonOrderId": "AMZ-1",
            "OrderStatus": "Shipped",
            "OrderTotal": {"Amount": "120.00", "CurrencyCode": "USD"},
            "RefundedAmount": {"Amount": "12.00", "CurrencyCode": "USD"},
        }
    )
    assert dto is not None
    assert dto.channel_metadata["refunded_amount"] == "12.00"
    assert dto.channel_metadata["platform_commission_base"] == "18.00"


async def _async_val(val: Any) -> Any:
    return val


@pytest.mark.asyncio
async def test_multiplatform_channel_analytics_velocity_and_1688_replenishment(
    rt_session_factory: tuple[Factory, list[UUID]],
) -> None:
    factory, created_tenants = rt_session_factory
    tid_a = uuid4()
    tid_b = uuid4()
    created_tenants.extend([tid_a, tid_b])

    uid_a = uuid4()
    wid_a = uuid4()
    sup_a = uuid4()
    ca_shopify = uuid4()
    ca_tiktok = uuid4()
    sourcing_url = "https://detail.1688.com/offer/888999000111.html"

    async with tenant_session(factory, tid_a) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, 'Analytics Co', 'USD')"
            ),
            {"id": tid_a, "slug": f"ana-a-{tid_a.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, 'Owner A', 'hash')"
            ),
            {"id": uid_a, "email": f"{uid_a.hex}-{tid_a.hex}@analytics-test.example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, is_active) "
                "VALUES (:tid, :uid, 'owner', true)"
            ),
            {"tid": tid_a, "uid": uid_a},
        )
        await session.execute(
            text(
                "INSERT INTO warehouses (id, tenant_id, code, name, country_code, city, is_active) "
                "VALUES (:id, :tid, 'QZ-01', 'QZ Main', 'CN', 'Quanzhou', true)"
            ),
            {"id": wid_a, "tid": tid_a},
        )
        await session.execute(
            text(
                "INSERT INTO suppliers (id, tenant_id, code, name, lead_time_days, status) "
                "VALUES (:id, :tid, 'SUP-1688', '1688 LED Factory', 12, 'active')"
            ),
            {"id": sup_a, "tid": tid_a},
        )
        for ca_id, ctype, disp in (
            (ca_shopify, "shopify", "Shopify US"),
            (ca_tiktok, "tiktok_shop", "TikTok US"),
        ):
            await session.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status) "
                    "VALUES (:id, :tid, :ct, :ext, :disp, 'active')"
                ),
                {
                    "id": ca_id,
                    "tid": tid_a,
                    "ct": ctype,
                    "ext": f"ext-{ca_id.hex[:6]}",
                    "disp": disp,
                },
            )

        catalog = CatalogService()
        prod = await catalog.create_product(
            session, tid_a, str(uid_a), ProductCreate(spu_code="SPU-ANA-1", category="LED")
        )
        var = await catalog.create_variant(
            session,
            tid_a,
            str(uid_a),
            VariantCreate(
                product_id=prod.id,
                sku="LED-ANA-SKU1",
                attributes={"safety_stock_qty": 100},
                cost_price_base=Decimal("10.00"),
                base_currency="USD",
                retail_price_usd=Decimal("30.00"),
                weight_grams=200,
                lead_time_days=14,
            ),
            UserRole.OWNER,
        )
        await session.execute(
            text(
                "INSERT INTO sourcing_drafts "
                "(id, tenant_id, source_platform, source_url, raw_title, "
                "optimized_title_en, suggested_sku, imported_product_id, status) "
                "VALUES (:id, :tid, '1688', :url, '1688 LED Floodlight', "
                "'1688 LED Floodlight En', 'LED-ANA-SKU1', :pid, 'draft')"
            ),
            {"id": uuid4(), "tid": tid_a, "url": sourcing_url, "pid": prod.id},
        )
        for ca_id, ext_sku in ((ca_shopify, "SHOP-ANA-1"), (ca_tiktok, "TT-ANA-1")):
            await catalog.create_sku_mapping(
                session,
                tid_a,
                str(uid_a),
                SkuMappingCreate(
                    variant_id=var.id,
                    channel_account_id=ca_id,
                    external_product_id=f"EXT-{ext_sku}",
                    external_sku=ext_sku,
                ),
            )
        await InventoryLedgerService().inbound_stock(
            session,
            tid_a,
            wid_a,
            var.id,
            qty=80,
            reference_id=uuid4(),
            idempotency_key=f"inb-{uuid4()}",
        )

        # Ingest 1 Shopify order (20 units @ $30 = $600, commission $30, refund $50)
        # Remaining available = 80 - 20 = 60 (< safety_stock 100 -> low stock!)
        sync = EcommerceOrderSyncService()
        await sync.ingest_normalized_order(
            session,
            tenant_id=tid_a,
            channel_account_id=ca_shopify,
            default_warehouse_id=wid_a,
            order_dto=NormalizedOrder(
                external_order_id="SH-ANA-1",
                currency="USD",
                fx_rate_to_base=Decimal("1.000000"),
                subtotal_amount=Decimal("600.00"),
                total_amount=Decimal("600.00"),
                platform_commission_base=Decimal("30.00"),
                channel_metadata={"refunded_amount": "50.00"},
                customer=NormalizedCustomer(
                    email="shop@example.com",
                    external_identity_type=IdentityType.EMAIL,
                ),
                line_items=[
                    NormalizedLineItem(
                        external_sku="SHOP-ANA-1", quantity=20, line_total=Decimal("600.00")
                    )
                ],
                updated_at=datetime.now(UTC),
            ),
        )
        # Ingest 1 TikTok order (45 units @ $30 = $1350, commission $54)
        await sync.ingest_normalized_order(
            session,
            tenant_id=tid_a,
            channel_account_id=ca_tiktok,
            default_warehouse_id=wid_a,
            order_dto=NormalizedOrder(
                external_order_id="TT-ANA-1",
                currency="USD",
                fx_rate_to_base=Decimal("1.000000"),
                subtotal_amount=Decimal("1350.00"),
                total_amount=Decimal("1350.00"),
                platform_commission_base=Decimal("54.00"),
                customer=NormalizedCustomer(
                    email="tt@example.com",
                    external_identity_type=IdentityType.TIKTOK_BUYER_ID,
                    external_customer_id="tt-b-1",
                ),
                line_items=[
                    NormalizedLineItem(
                        external_sku="TT-ANA-1", quantity=45, line_total=Decimal("1350.00")
                    )
                ],
                updated_at=datetime.now(UTC),
            ),
        )
        # Remaining available = 15; sold = 65 in 30d -> 2.17/day -> cover = 6.9d < 14 -> critical

    # Seed Tenant B for RLS isolation test
    uid_b = uuid4()
    async with tenant_session(factory, tid_b) as session_b:
        await session_b.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, 'Tenant B', 'USD')"
            ),
            {"id": tid_b, "slug": f"ana-b-{tid_b.hex[:8]}"},
        )
        await session_b.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, 'Owner B', 'hash')"
            ),
            {"id": uid_b, "email": f"{uid_b.hex}-{tid_b.hex}@analytics-test.example.com"},
        )

    holder = [
        CurrentPrincipal(
            user_id=uid_a, tenant_id=tid_a, role=UserRole.OWNER, email="owner-a@example.com"
        )
    ]
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.include_router(finance_router, prefix="/api/v1")
    app.include_router(wms_router, prefix="/api/v1")
    app.include_router(supply_router, prefix="/api/v1")
    app.dependency_overrides[get_current_principal] = lambda: holder[0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Record ad spend for Shopify ($100.00) -> ROAS = 600 / 100 = 6.00
        up_res = await client.post(
            "/api/v1/finance/channel-analytics/daily-metrics",
            json={
                "channel_account_id": str(ca_shopify),
                "metric_date": date.today().isoformat(),
                "impressions": 5000,
                "clicks": 250,
                "ad_spend_base": "100.00",
                "external_sessions": 240,
            },
        )
        assert up_res.status_code == 201, up_res.text

        # 2. Check channel analytics overview
        ov_res = await client.get("/api/v1/finance/channel-analytics/overview?window=30d")
        assert ov_res.status_code == 200, ov_res.text
        ov = ov_res.json()
        assert ov["base_currency"] == "USD"
        assert ov["totals"]["order_count"] == 2
        assert ov["totals"]["units_sold"] == 65
        assert Decimal(ov["totals"]["gmv_base"]) == Decimal("1950.00")
        assert Decimal(ov["totals"]["refunded_amount_base"]) == Decimal("50.00")
        assert Decimal(ov["totals"]["ad_spend_base"]) == Decimal("100.00")
        assert Decimal(ov["totals"]["roas"]) == Decimal("19.50")

        by_type = {c["channel_type"]: c for c in ov["channels"]}
        assert Decimal(by_type["shopify"]["gmv_base"]) == Decimal("600.00")
        assert Decimal(by_type["shopify"]["roas"]) == Decimal("6.00")
        assert Decimal(by_type["tiktok_shop"]["gmv_base"]) == Decimal("1350.00")

        bad_win = await client.get("/api/v1/finance/channel-analytics/overview?window=15d")
        assert bad_win.status_code == 422

        # 3. Check SKU velocity + linked 1688 sourcing_url
        vel_res = await client.get("/api/v1/finance/channel-analytics/sku-velocity?window=30d")
        assert vel_res.status_code == 200, vel_res.text
        vel_item = vel_res.json()["items"][0]
        assert vel_item["sku"] == "LED-ANA-SKU1"
        assert vel_item["total_units_sold"] == 65
        assert vel_item["available_qty"] == 15
        assert Decimal(vel_item["avg_daily_units"]) == Decimal("2.17")
        assert Decimal(vel_item["days_of_cover"]) == Decimal("6.9")
        assert vel_item["stockout_risk"] == "critical"
        assert vel_item["recommended_reorder_qty"] > 0
        assert vel_item["sourcing_url"] == sourcing_url
        assert Decimal(vel_item["cost_price_base"]) == Decimal("10.00")

        # 4. Low stock & 1688-linked replenishment PO creation
        low_res = await client.get("/api/v1/wms/low-stock")
        assert low_res.status_code == 200
        assert low_res.json()["items"][0]["sourcing_url"] == sourcing_url

        po_res = await client.post(
            "/api/v1/wms/low-stock/create-po",
            json={
                "supplier_id": str(sup_a),
                "variant_id": vel_item["variant_id"],
                "quantity": vel_item["recommended_reorder_qty"],
                "unit_cost": "10.00",
                "notes": "Restock from velocity alert",
            },
        )
        assert po_res.status_code == 201, po_res.text
        assert sourcing_url in (po_res.json()["notes"] or "")

        # 5. Agent role cannot see cost_price_base in velocity
        holder[0] = CurrentPrincipal(
            user_id=uid_a, tenant_id=tid_a, role=UserRole.AGENT, email="agent-a@example.com"
        )
        agent_vel = await client.get("/api/v1/finance/channel-analytics/sku-velocity")
        assert agent_vel.status_code == 200
        assert agent_vel.json()["items"][0]["cost_price_base"] is None

        # 6. Tenant B RLS isolation
        holder[0] = CurrentPrincipal(
            user_id=uid_b, tenant_id=tid_b, role=UserRole.OWNER, email="owner-b@example.com"
        )
        ov_b = await client.get("/api/v1/finance/channel-analytics/overview?window=30d")
        assert ov_b.status_code == 200
        assert ov_b.json()["totals"]["order_count"] == 0
        up_b = await client.post(
            "/api/v1/finance/channel-analytics/daily-metrics",
            json={
                "channel_account_id": str(ca_shopify),
                "metric_date": date.today().isoformat(),
                "ad_spend_base": "50.00",
            },
        )
        assert up_b.status_code == 404
