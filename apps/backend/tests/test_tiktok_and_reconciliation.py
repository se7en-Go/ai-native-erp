from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from taskiq.schedule_sources import LabelScheduleSource

import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.catalog.schemas import ProductCreate, SkuMappingCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.channels.ecommerce.tiktok_shop import (
    TikTokShopCoreAdapter,
    sign_tiktok_request,
    verify_tiktok_webhook_signature,
)
from erp_core.orders.reconciliation_worker import (
    reconcile_channel_account_orders,
    reconcile_ecommerce_orders_cron,
)
from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_runtime_engine,
    create_session_factory,
    tenant_session,
)
from erp_core.shared.enums import ChannelType, OrderMode, UserRole
from erp_core.worker_main import scheduler


def test_tiktok_request_and_webhook_signatures() -> None:
    app_key, app_secret, shop_cipher = "tt_app_key_001", "tt_app_secret_xyz", "GCP_CIPHER_01"
    computed = sign_tiktok_request(
        app_secret=app_secret,
        path="/order/202309/orders/search",
        query_params={
            "timestamp": "1763510000",
            "app_key": app_key,
            "shop_cipher": shop_cipher,
            "access_token": "ignored",
        },
        body_str='{"page_size":50}',
    )
    expected_message = (
        f"{app_secret}/order/202309/orders/search"
        f"app_key{app_key}shop_cipher{shop_cipher}timestamp1763510000"
        '{"page_size":50}'
        f"{app_secret}"
    )
    assert (
        computed
        == hmac.new(app_secret.encode(), expected_message.encode(), hashlib.sha256).hexdigest()
    )

    raw = (
        b'{"type":1,"tts_notification_id":"tts_evt_771","shop_id":"7499911",'
        b'"data":{"order_id":"577001"}}'
    )
    signature = hmac.new(app_secret.encode(), app_key.encode() + raw, hashlib.sha256).hexdigest()
    assert (
        verify_tiktok_webhook_signature(
            raw, {"Authorization": signature}, app_key=app_key, app_secret=app_secret
        )
        is True
    )
    assert (
        verify_tiktok_webhook_signature(
            raw, {"Authorization": "bad_sig"}, app_key=app_key, app_secret=app_secret
        )
        is False
    )
    assert (
        verify_tiktok_webhook_signature(
            raw + b" ", {"Authorization": signature}, app_key=app_key, app_secret=app_secret
        )
        is False
    )


async def test_tiktok_signing_webhook_order_import_and_15min_reconciliation_cron() -> None:
    app_key, app_secret, shop_cipher = "tt_app_key_001", "tt_app_secret_xyz", "GCP_CIPHER_01"
    tenant_id, account_id, warehouse_id = uuid4(), uuid4(), uuid4()
    shipped: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-tts-access-token") == "tts_live_tok"
        assert "sign" in request.url.params
        if request.url.path == "/order/202309/orders/search":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"orders": [{"id": "TT-ORD-88001"}], "next_page_token": ""},
                },
            )
        if request.url.path == "/order/202309/orders":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "orders": [
                            {
                                "id": "TT-ORD-88001",
                                "status": "AWAITING_SHIPMENT",
                                "update_time": 1763510500,
                                "buyer_email": "tt.shopper@example.com",
                                "user_id": "tt_buyer_991",
                                "recipient_address": {
                                    "name": "Mia Torres",
                                    "phone_number": "+14155550199",
                                    "region_code": "US",
                                },
                                "payment": {
                                    "currency": "USD",
                                    "sub_total": "45.00",
                                    "shipping_fee": "5.00",
                                    "tax": "0.00",
                                    "total_amount": "50.00",
                                    "platform_discount": "0.00",
                                },
                                "line_items": [
                                    {
                                        "id": "li_1",
                                        "seller_sku": "TT-LED-5M-1PK",
                                        "product_name": "LED Strip",
                                        "sale_price": "45.00",
                                    }
                                ],
                                "packages": [{"id": "pkg_551"}],
                            }
                        ]
                    },
                },
            )
        if request.url.path == "/fulfillment/202309/packages/pkg_551/ship":
            shipped.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    runtime_engine = create_runtime_engine(get_settings())
    factory = create_session_factory(runtime_engine)
    try:
        async with tenant_session(factory, tenant_id) as session:
            await session.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'TT', :slug)"),
                {"tid": tenant_id, "slug": f"tt-{tenant_id.hex}"},
            )
            await session.execute(
                text(
                    "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                    "external_account_id, display_name) VALUES "
                    "(:id, :tid, 'tiktok_shop', '7499911', 'QZ US TikTok Shop')"
                ),
                {"id": account_id, "tid": tenant_id},
            )
            catalog = CatalogService()
            product = await catalog.create_product(
                session,
                tenant_id,
                actor_id="seed",
                payload=ProductCreate(spu_code="SPU-TT-5M", category="led"),
            )
            variant = await catalog.create_variant(
                session,
                tenant_id,
                actor_id="seed",
                role=UserRole.ADMIN,
                payload=VariantCreate(
                    product_id=product.id,
                    sku="LED-STRIP-5M-IP65",
                    cost_price_base=Decimal("32.0000"),
                    retail_price_usd=Decimal("45.00"),
                    weight_grams=300,
                ),
            )
            await catalog.create_sku_mapping(
                session,
                tenant_id,
                actor_id="seed",
                payload=SkuMappingCreate(
                    variant_id=variant.id,
                    channel_account_id=account_id,
                    external_product_id="tt_prod_1",
                    external_sku="TT-LED-5M-1PK",
                ),
            )
            await session.execute(
                text(
                    "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
                    "on_hand_qty, reserved_qty) VALUES (:tid, :wid, :vid, 15, 0)"
                ),
                {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = TikTokShopCoreAdapter(
                app_key=app_key,
                app_secret=app_secret,
                access_token="tts_live_tok",
                shop_cipher=shop_cipher,
                app_mode="seller_byok",
                http_client=client,
            )
            async with tenant_session(factory, tenant_id) as session:
                since = datetime.now(UTC) - timedelta(minutes=30)
                first = await reconcile_channel_account_orders(
                    session,
                    tenant_id=tenant_id,
                    channel_account_id=account_id,
                    channel_type=ChannelType.TIKTOK_SHOP,
                    adapter=adapter,
                    default_warehouse_id=warehouse_id,
                    since=since,
                )
                assert (first["recovered"], first["updated"]) == (1, 0)
                second = await reconcile_channel_account_orders(
                    session,
                    tenant_id=tenant_id,
                    channel_account_id=account_id,
                    channel_type=ChannelType.TIKTOK_SHOP,
                    adapter=adapter,
                    default_warehouse_id=warehouse_id,
                    since=since,
                )
                assert (second["recovered"], second["updated"]) == (0, 1)

                reserved = await session.scalar(
                    text(
                        "SELECT reserved_qty FROM inventory_balances WHERE tenant_id = :tid "
                        "AND warehouse_id = :wid AND variant_id = :vid"
                    ),
                    {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id},
                )
                assert reserved == 1
                row = (
                    await session.execute(
                        text(
                            "SELECT order_mode, total_amount FROM orders WHERE tenant_id = :tid "
                            "AND external_order_id = 'TT-ORD-88001'"
                        ),
                        {"tid": tenant_id},
                    )
                ).one()
                assert row.order_mode == OrderMode.B2C_RETAIL.value
                assert Decimal(str(row.total_amount)) == Decimal("50.00")

            await adapter.create_fulfillment(
                external_order_id="TT-ORD-88001",
                fulfillment_order_id="pkg_551",
                tracking_number="USPS-TT-9900",
                carrier_company="7117021",
            )
        assert len(shipped) == 1
        assert shipped[0]["tracking_number"] == "USPS-TT-9900"
        assert callable(reconcile_ecommerce_orders_cron)

        schedules = []
        for source in scheduler.sources:
            if isinstance(source, LabelScheduleSource):
                await source.startup()
                try:
                    schedules.extend(await source.get_schedules())
                finally:
                    await source.shutdown()
        reconcile_crons = [
            task.cron
            for task in schedules
            if task.task_name.endswith(":reconcile_ecommerce_orders_cron")
        ]
        assert reconcile_crons == ["*/15 * * * *"]
    finally:
        admin = create_async_engine(
            os.environ.get(
                "DATABASE_ADMIN_URL",
                "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
            )
        )
        async with admin.begin() as conn:
            for statement in (
                "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                "DELETE FROM receivables_payables WHERE tenant_id = :tid",
                "DELETE FROM inventory_ledger WHERE tenant_id = :tid",
                "DELETE FROM inventory_balances WHERE tenant_id = :tid",
                "DELETE FROM order_items WHERE tenant_id = :tid",
                "DELETE FROM orders WHERE tenant_id = :tid",
                "DELETE FROM platform_sku_mappings WHERE tenant_id = :tid",
                "DELETE FROM product_variants WHERE tenant_id = :tid",
                "DELETE FROM products WHERE tenant_id = :tid",
                "DELETE FROM customer_identities WHERE tenant_id = :tid",
                "DELETE FROM customers WHERE tenant_id = :tid",
                "DELETE FROM channel_routing_directory WHERE tenant_id = :tid",
                "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                "DELETE FROM audit_events WHERE tenant_id = :tid",
            ):
                await conn.execute(text(statement), {"tid": tenant_id})
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await admin.dispose()
        await runtime_engine.dispose()
