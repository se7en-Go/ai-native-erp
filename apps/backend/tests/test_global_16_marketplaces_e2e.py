"""PG17 RLS end-to-end coverage for the eight new global marketplace channels.

Every test runs against the **real PostgreSQL 17** database as the non-superuser
``app_runtime`` role (tenant RLS forced) with real AES-256-GCM envelope-encrypted
credentials. All outbound marketplace traffic is served by ``httpx.MockTransport``, so no
test can reach a live marketplace.

Covered end to end for Shopee, Lazada, Temu, SHEIN, AliExpress, Ozon, Walmart and Mercado
Libre:

1. ``POST /api/v1/channels/<slug>/connect`` -> 201 for all eight slugs, zero plaintext
   secret in the response, sealed ciphertext in ``channel_credentials`` and a
   ``channel_routing_directory`` binding per account.
2. Multi-variant ``SourcingService.publish(..., push_to_marketplace=True)`` through
   ``MarketplaceListingDispatcher`` -> ``live_published`` receipts whose per-variant
   receipts land in ``platform_sku_mappings``.
3. ``InventoryLedgerService`` stock movement -> ``InventorySyncDispatcher`` outbox drain:
   eight pushes succeed and every ``synced_qty`` equals the clamped channel-sellable
   quantity (``safety_stock``/``bundle_multiplier`` clamps).
4. ``NormalizedOrder`` -> ``EcommerceOrderSyncService`` -> ``ChannelAnalyticsService``:
   refunds and 2dp ``Decimal`` ``platform_commission_base`` roll up across the new
   channels.
5. Signed webhook ingress on ``POST /api/v1/webhooks/<slug>``: a valid HMAC is persisted in
   ``raw_webhook_events`` (HTTP 200), a tampered/missing HMAC is rejected with 401
   ``ERR_INVALID_WEBHOOK_SIGNATURE``.
6. Cross-tenant PG17 RLS isolation: tenant B cannot read, sync, publish or reconcile
   tenant A's freshly connected accounts.

Teardown removes every row these tests create (via ``DATABASE_ADMIN_URL``) so ``erp_saas``
never keeps a stray tenant, credential or webhook receipt.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from erp_core.api_main import create_app
from erp_core.catalog.sourcing_service import (
    SourcingCollectRequest,
    SourcingOptionSpec,
    SourcingPublishRequest,
    SourcingService,
    SourcingVariantSpec,
    build_external_sku,
)
from erp_core.channels.ecommerce.base import (
    CredentialVaultHelper,
    EcommerceOrderSyncService,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
)
from erp_core.channels.ecommerce.inventory_sync_dispatcher import InventorySyncDispatcher
from erp_core.channels.ecommerce.listing_dispatcher import MarketplaceListingDispatcher
from erp_core.finance.channel_analytics_service import ChannelAnalyticsService
from erp_core.orders.inventory_service import (
    InventoryLedgerService,
    compute_sellable_channel_qty,
)
from erp_core.shared.config import get_settings
from erp_core.shared.db import create_runtime_engine, tenant_session
from erp_core.shared.enums import IdentityType, OrderMode, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]

# --------------------------------------------------------------------------------------
# Fleet definition
# --------------------------------------------------------------------------------------

PLATFORMS: tuple[str, ...] = (
    "shopee",
    "lazada",
    "temu",
    "shein",
    "aliexpress",
    "ozon",
    "walmart",
    "mercado_libre",
)

#: The connect route slug differs from the channel_type only for Mercado Libre.
CONNECT_SLUGS: dict[str, str] = {"mercado_libre": "mercado-libre"}

#: Same asymmetry on the signed-webhook ingress.
WEBHOOK_PATHS: dict[str, str] = {"mercado_libre": "/api/v1/webhooks/mercado-libre"}

CREDENTIAL_TYPES: dict[str, str] = {
    "shopee": "shopee_app",
    "lazada": "lazada_app",
    "temu": "temu_app",
    "shein": "shein_app",
    "aliexpress": "aliexpress_app",
    "ozon": "ozon_app",
    "walmart": "walmart_app",
    "mercado_libre": "mercado_libre_app",
}

CONNECT_PAYLOADS: dict[str, dict[str, Any]] = {
    "shopee": {
        "shop_id": "7788990001",
        "partner_id": "2010123",
        "partner_key": "shopee-partner-key-secret-value",
        "access_token": "shopee-access-token-secret-value",
        "region": "SG",
        "fulfillment_mode": "standard",
    },
    "lazada": {
        "seller_id": "laz-seller-1",
        "app_key": "lazada-app-key-public",
        "app_secret": "lazada-app-secret-value",
        "access_token": "lazada-access-token-value",
        "region": "SG",
        "fulfillment_mode": "standard",
    },
    "temu": {
        "mall_id": "6344182012345678",
        "app_key": "temu-app-key-public",
        "app_secret": "temu-app-secret-value",
        "access_token": "temu-access-token-value",
        "region": "US",
        "fulfillment_mode": "standard",
    },
    "shein": {
        "seller_id": "shein-seller-1",
        "open_key_id": "shein-open-key-id-public",
        "secret_key": "shein-secret-key-value",
        "region": "US",
        "fulfillment_mode": "standard",
    },
    "aliexpress": {
        "seller_id": "ae-seller-1",
        "app_key": "aliexpress-app-key-public",
        "app_secret": "aliexpress-app-secret-value",
        "access_token": "aliexpress-access-token-value",
        "fulfillment_mode": "standard",
    },
    "ozon": {
        "client_id": "ozon-client-1",
        "api_key": "ozon-api-key-secret-value",
        "warehouse_id": "9876543",
        "fulfillment_mode": "fbs",
    },
    "walmart": {
        "seller_id": "walmart-seller-1",
        "client_id": "walmart-client-id-public",
        "client_secret": "walmart-client-secret-value",
        "access_token": "walmart-access-token-value",
        "fulfillment_mode": "standard",
    },
    "mercado_libre": {
        "seller_id": "123456789",
        "access_token": "meli-access-token-secret-value",
        "site_id": "MLM",
        "refresh_token": "meli-refresh-token-secret-value",
        "fulfillment_mode": "standard",
    },
}

EXTERNAL_ACCOUNT_IDS: dict[str, str] = {
    "shopee": "7788990001",
    "lazada": "laz-seller-1",
    "temu": "6344182012345678",
    "shein": "shein-seller-1",
    "aliexpress": "ae-seller-1",
    "ozon": "ozon-client-1",
    "walmart": "walmart-seller-1",
    "mercado_libre": "123456789",
}

#: Which decrypted credential key the webhook ingress signs with (first hit wins in
#: ``connectors_router.WEBHOOK_SECRET_KEYS``).
WEBHOOK_SECRET_KEYS: dict[str, str] = {
    "shopee": "partner_key",
    "lazada": "app_secret",
    "temu": "app_secret",
    "shein": "secret_key",
    "aliexpress": "app_secret",
    "ozon": "api_key",
    "walmart": "client_secret",
    "mercado_libre": "access_token",
}

WEBHOOK_SECRETS: dict[str, str] = {
    platform: str(CONNECT_PAYLOADS[platform][key])
    for platform, key in WEBHOOK_SECRET_KEYS.items()
}

DOMAIN_HEADERS: dict[str, str] = {
    "shopee": "X-Shopee-Shop-Id",
    "lazada": "X-Lazada-Seller-Id",
    "temu": "X-Temu-Mall-Id",
    "shein": "X-Shein-Seller-Id",
    "aliexpress": "X-AliExpress-Seller-Id",
    "ozon": "X-Ozon-Client-Id",
    "walmart": "X-Walmart-Seller-Id",
    "mercado_libre": "X-Meli-Seller-Id",
}

DEDUP_HEADERS: dict[str, str] = {
    "shopee": "X-Shopee-Event-Id",
    "lazada": "X-Lazada-Event-Id",
    "temu": "X-Temu-Event-Id",
    "shein": "X-Shein-Event-Id",
    "aliexpress": "X-AliExpress-Event-Id",
    "ozon": "X-Ozon-Event-Id",
    "walmart": "X-Walmart-Event-Id",
    "mercado_libre": "X-Meli-Event-Id",
}

TOPIC_HEADERS: dict[str, str] = {
    "shopee": "X-Shopee-Topic",
    "lazada": "X-Lazada-Topic",
    "temu": "X-Temu-Topic",
    "shein": "X-Shein-Topic",
    "aliexpress": "X-AliExpress-Topic",
    "ozon": "X-Ozon-Topic",
    "walmart": "X-Walmart-Topic",
    "mercado_libre": "X-Meli-Topic",
}

SIGNATURE_HEADERS: dict[str, str] = {
    "shopee": "X-Shopee-Hmac-Sha256",
    "lazada": "X-Lazada-Signature",
    "temu": "X-Temu-Signature",
    "shein": "X-Shein-Signature",
    "aliexpress": "X-AliExpress-Hmac-Sha256",
    "ozon": "X-Ozon-Signature",
    "walmart": "wm-sec.signature",
    "mercado_libre": "X-Signature",
}

_SECRET_FIELDS = (
    "partner_key",
    "app_secret",
    "access_token",
    "secret_key",
    "api_key",
    "client_secret",
    "refresh_token",
)

#: Every plaintext secret handed to the connect routes; none may reach a response body,
#: a ciphertext column or a log line.
SECRET_VALUES: tuple[str, ...] = tuple(
    str(CONNECT_PAYLOADS[platform][field])
    for platform in PLATFORMS
    for field in _SECRET_FIELDS
    if field in CONNECT_PAYLOADS[platform]
)

#: Product id the mock marketplace assigned to the listing for each channel. Walmart
#: identifies an item by the seller SKU, so it has no entry here (asserted separately).
EXPECTED_PRODUCT_IDS: dict[str, str] = {
    "shopee": "987654321",
    "lazada": "LAZADA-ITEM-1",
    "temu": "TEMU-GOODS-1",
    "shein": "SHEIN-GOODS-1",
    "aliexpress": "AE-PROD-1",
    "ozon": "7001001",
    "mercado_libre": "MLM-ITEM-1",
}

#: ``(safety_stock, bundle_multiplier)`` written onto each mapping before the stock push.
STOCK_PROFILE: dict[str, tuple[int, int]] = {
    "shopee": (10, 1),
    "lazada": (0, 2),
    "temu": (500, 1),
    "shein": (20, 3),
    "aliexpress": (0, 1),
    "ozon": (5, 5),
    "walmart": (0, 1),
    "mercado_libre": (15, 2),
}

#: 120 units of on-hand stock minus the per-channel clamp rules above.
EXPECTED_SELLABLE_QTY: dict[str, int] = {
    "shopee": 110,
    "lazada": 60,
    "temu": 0,
    "shein": 33,
    "aliexpress": 120,
    "ozon": 23,
    "walmart": 120,
    "mercado_libre": 52,
}

INBOUND_QTY = 120

HOST_PLATFORMS: dict[str, str] = {
    "partner.shopeemobile.com": "shopee",
    "api.lazada.sg": "lazada",
    "openapi-b-us.temu.com": "temu",
    "openapi.sheincorp.com": "shein",
    "openapi.aliexpress.com": "aliexpress",
    "api-seller.ozon.ru": "ozon",
    "marketplace.walmartapis.com": "walmart",
    "api.mercadolibre.com": "mercado_libre",
}

ORDER_COMMISSION = Decimal("3.33")
ORDER_REFUND = Decimal("1.11")
ORDER_TOTAL = Decimal("100.00")


# --------------------------------------------------------------------------------------
# Network-free marketplace fleet
# --------------------------------------------------------------------------------------


def _shopee_envelope(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": "", "message": "", "request_id": "req-e2e", **payload},
    )


class MarketplaceFleet:
    """``httpx.MockTransport`` handler that speaks all eight marketplaces' wire formats."""

    def __init__(self) -> None:
        self.calls: dict[str, list[httpx.Request]] = {platform: [] for platform in PLATFORMS}
        self.ozon_offer_ids: list[str] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        platform = HOST_PLATFORMS.get(request.url.host)
        assert platform is not None, f"unexpected marketplace host {request.url.host}"
        self.calls[platform].append(request)
        handler: Callable[[httpx.Request], httpx.Response] = getattr(self, f"_{platform}")
        return handler(request)

    # ------------------------------------------------------------------------ Shopee

    def _shopee(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/logistics/get_channel_list":
            return _shopee_envelope(
                {
                    "response": {
                        "logistic_channel_list": [
                            {"logistics_channel_id": 80001, "enabled": True},
                            {"logistics_channel_id": 80002, "enabled": False},
                        ]
                    }
                }
            )
        if path == "/api/v2/product/add_item":
            body = json.loads(request.content)
            models = body.get("model") or []
            return _shopee_envelope(
                {
                    "response": {
                        "item_id": 987654321,
                        "model": [
                            {"model_id": 5001 + index, "model_sku": model.get("model_sku")}
                            for index, model in enumerate(models)
                        ],
                    }
                }
            )
        if path == "/api/v2/product/update_stock":
            return _shopee_envelope({"response": {}})
        return _shopee_envelope({"error": "error_path_not_found", "message": path}, 404)

    # ------------------------------------------------------------------------ Lazada

    def _lazada(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/rest/product/create":
            body = json.loads(request.content)
            skus = body["Request"]["Product"]["Skus"]
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": {
                        "item_id": "LAZADA-ITEM-1",
                        "skus": [
                            {"seller_sku": sku["SellerSku"], "sku_id": f"LAZADA-SKU-{index}"}
                            for index, sku in enumerate(skus, start=1)
                        ],
                    },
                },
            )
        if path == "/rest/product/price_quantity/update":
            return httpx.Response(200, json={"code": "0", "data": {}})
        return httpx.Response(404, json={"code": "404", "message": path})

    # -------------------------------------------------------------------------- Temu

    def _temu(self, request: httpx.Request) -> httpx.Response:
        envelope = json.loads(request.content)
        api_type = str(envelope.get("type") or "")
        data = envelope.get("data") or {}
        if api_type == "bg.goods.add":
            skus = data.get("skuList") or []
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "goodsId": "TEMU-GOODS-1",
                        "goodsStatus": "PUBLISHED",
                        "skuList": [
                            {"extCode": sku.get("extCode"), "skuId": f"TEMU-SKU-{index}"}
                            for index, sku in enumerate(skus, start=1)
                        ],
                    },
                },
            )
        if api_type == "bg.goods.stock.update":
            return httpx.Response(200, json={"success": True, "result": {}})
        return httpx.Response(
            404,
            json={"success": False, "errorCode": "9999999", "errorMsg": api_type},
        )

    # ------------------------------------------------------------------------- SHEIN

    def _shein(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/open-api/goods/product":
            skus = body.get("skuList") or []
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "info": {
                        "goodsId": "SHEIN-GOODS-1",
                        "skuList": [
                            {"skuCode": sku.get("skuCode"), "skuId": f"SHEIN-SKU-{index}"}
                            for index, sku in enumerate(skus, start=1)
                        ],
                    },
                },
            )
        if path == "/open-api/goods/inventory/update":
            return httpx.Response(200, json={"code": "0", "info": {}})
        return httpx.Response(404, json={"code": "404", "msg": path})

    # ------------------------------------------------------------------- AliExpress

    def _aliexpress(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode("utf-8")))
        method = form.get("method", "")
        if method == "aliexpress.solution.product.create":
            product = json.loads(form.get("product_body") or "{}")
            skus = product.get("skus") or []
            return httpx.Response(
                200,
                json={
                    "aliexpress_solution_product_create_response": {
                        "result": {
                            "product_id": "AE-PROD-1",
                            "skus": [
                                {"sku_code": sku.get("sku_code"), "sku_id": f"AE-SKU-{index}"}
                                for index, sku in enumerate(skus, start=1)
                            ],
                        }
                    }
                },
            )
        if method == "aliexpress.solution.sku.stock.update":
            return httpx.Response(
                200,
                json={"aliexpress_solution_sku_stock_update_response": {"result": {}}},
            )
        return httpx.Response(404, json={"error_response": {"code": 404, "msg": method}})

    # ------------------------------------------------------------------------- Ozon

    def _ozon(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/v3/product/import":
            self.ozon_offer_ids = [
                str(item.get("offer_id")) for item in body.get("items") or []
            ]
            return httpx.Response(200, json={"result": {"task_id": 98765}})
        if path == "/v1/product/import/info":
            items = [
                {
                    "offer_id": offer_id,
                    "product_id": f"70010{index:02d}",
                    "status": "imported",
                }
                for index, offer_id in enumerate(self.ozon_offer_ids, start=1)
            ]
            return httpx.Response(200, json={"result": {"items": items}})
        if path == "/v1/product/update":
            return httpx.Response(200, json={"result": {}})
        if path == "/v1/product/import/stocks":
            return httpx.Response(200, json={"result": {"updated": True}})
        return httpx.Response(404, json={"code": 404, "message": path})

    # ---------------------------------------------------------------------- Walmart

    def _walmart(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v3/token":
            return httpx.Response(
                200, json={"access_token": "walmart-refreshed-token", "expires_in": 900}
            )
        if path == "/v3/feeds":
            return httpx.Response(200, json={"feedId": "WALMART-FEED-1"})
        if path == "/v3/inventory":
            body = json.loads(request.content)
            return httpx.Response(
                200, json={"sku": body.get("sku"), "quantity": body.get("quantity")}
            )
        return httpx.Response(404, json={"errors": [{"code": "NOT_FOUND", "info": path}]})

    # ---------------------------------------------------------------- Mercado Libre

    def _mercado_libre(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/items":
            body = json.loads(request.content)
            variations = body.get("variations") or []
            return httpx.Response(
                200,
                json={
                    "id": "MLM-ITEM-1",
                    "status": "active",
                    "variations": [
                        {
                            "id": f"MLM-VAR-{index}",
                            "seller_custom_field": row.get("seller_custom_field"),
                        }
                        for index, row in enumerate(variations, start=1)
                    ],
                },
            )
        if path == "/items/MLM-ITEM-1/description":
            return httpx.Response(200, json={"plain_text": "merchandising copy"})
        if path.startswith("/items/MLM-ITEM-1"):
            body = json.loads(request.content) if request.content else {}
            return httpx.Response(200, json={"id": "MLM-ITEM-1", **body})
        return httpx.Response(404, json={"message": path})


# --------------------------------------------------------------------------------------
# Database fixtures & teardown
# --------------------------------------------------------------------------------------


def _admin_url() -> str:
    return os.environ.get(
        "DATABASE_ADMIN_URL",
        "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
    )


#: Tenant-owned rows, deleted children-first so no FK blocks the teardown.
_TENANT_DELETES: tuple[str, ...] = (
    "DELETE FROM inventory_sync_jobs WHERE tenant_id = :tid",
    "DELETE FROM channel_daily_metrics WHERE tenant_id = :tid",
    "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
    "DELETE FROM payment_records WHERE tenant_id = :tid",
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
    "DELETE FROM sourcing_drafts WHERE tenant_id = :tid",
    "DELETE FROM warehouses WHERE tenant_id = :tid",
    "DELETE FROM channel_credentials WHERE tenant_id = :tid",
    "DELETE FROM channel_routing_directory WHERE tenant_id = :tid",
    "DELETE FROM raw_webhook_events WHERE tenant_id = :tid",
    "DELETE FROM channel_accounts WHERE tenant_id = :tid",
    "DELETE FROM audit_events WHERE tenant_id = :tid",
    "DELETE FROM tenant_memberships WHERE tenant_id = :tid",
)

TEST_EMAIL_DOMAIN = "global-16-mkt.example.com"
WEBHOOK_EVENT_PREFIX = "global16mkt"


@contextlib.asynccontextmanager
async def _admin_connection() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(_admin_url())
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def _cleanup_tenants(tenant_ids: list[UUID]) -> None:
    engine = create_async_engine(_admin_url())
    try:
        async with engine.begin() as connection:
            for tenant_id in tenant_ids:
                for statement in _TENANT_DELETES:
                    await connection.execute(text(statement), {"tid": tenant_id})
                await connection.execute(
                    text("DELETE FROM users WHERE email LIKE :pattern"),
                    {"pattern": f"%{tenant_id.hex}@{TEST_EMAIL_DOMAIN}"},
                )
                await connection.execute(
                    text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id}
                )
            # Ingress receipts carry a NULL tenant (routing happens after signature check),
            # so they are swept by the per-run event-id prefix instead.
            await connection.execute(
                text("DELETE FROM raw_webhook_events WHERE external_event_id LIKE :pattern"),
                {"pattern": f"{WEBHOOK_EVENT_PREFIX}%"},
            )
    finally:
        await engine.dispose()


@pytest.fixture
async def rt_session_factory() -> AsyncIterator[tuple[Factory, list[UUID]]]:
    engine = create_runtime_engine(get_settings())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created: list[UUID] = []
    try:
        yield factory, created
    finally:
        await engine.dispose()
        if created:
            await _cleanup_tenants(created)


async def _seed_tenant(
    factory: Factory,
    created: list[UUID],
    *,
    label: str,
    with_warehouse: bool = True,
) -> dict[str, Any]:
    """Create one PG17 tenant with an owner membership (and warehouse when needed)."""
    tenant_id = uuid4()
    user_id = uuid4()
    warehouse_id = uuid4()
    created.append(tenant_id)
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, :name, 'USD')"
            ),
            {"id": tenant_id, "slug": f"{label}-{tenant_id.hex[:8]}", "name": f"{label} Co"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, :name, 'hash')"
            ),
            {
                "id": user_id,
                "email": f"{user_id.hex}-{tenant_id.hex}@{TEST_EMAIL_DOMAIN}",
                "name": f"{label} Owner",
            },
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, is_active) "
                "VALUES (:tid, :uid, 'owner', true)"
            ),
            {"tid": tenant_id, "uid": user_id},
        )
        if with_warehouse:
            await session.execute(
                text(
                    "INSERT INTO warehouses (id, tenant_id, code, name, country_code, city, "
                    "is_active) VALUES (:id, :tid, 'QZ-01', 'Quanzhou Main', 'CN', "
                    "'Quanzhou', true)"
                ),
                {"id": warehouse_id, "tid": tenant_id},
            )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "warehouse_id": warehouse_id,
        "principal": CurrentPrincipal(
            user_id=user_id,
            tenant_id=tenant_id,
            role=UserRole.OWNER,
            email=f"{label.lower()}-owner@{TEST_EMAIL_DOMAIN}",
        ),
    }


@contextlib.asynccontextmanager
async def _running_app(
    principal_holder: list[CurrentPrincipal],
    transport: httpx.MockTransport,
) -> AsyncIterator[AsyncClient]:
    """Real ``create_app()`` (real runtime DB + Redis) with outbound HTTP mocked."""
    app = create_app()
    app.state.marketplace_http_transport = transport
    app.dependency_overrides[get_current_principal] = lambda: principal_holder[0]
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


# --------------------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------------------


async def _connect_fleet(client: AsyncClient, *, suffix: str = "") -> dict[str, str]:
    """Connect all eight global marketplaces and return ``platform -> account id``."""
    accounts: dict[str, str] = {}
    for platform in PLATFORMS:
        slug = CONNECT_SLUGS.get(platform, platform)
        payload = dict(CONNECT_PAYLOADS[platform])
        payload["display_name"] = f"QZLED {platform}{suffix}"
        response = await client.post(f"/api/v1/channels/{slug}/connect", json=payload)
        assert response.status_code == 201, f"{platform}: {response.status_code} {response.text}"
        body = response.json()
        assert body["channel_type"] == platform
        accounts[platform] = body["channel_account_id"]
    return accounts


async def _collect_multi_variant_draft(
    session: AsyncSession, tenant_id: UUID, actor_id: str
) -> Any:
    """One 1688 sourcing draft with two priced variants exported as a multi-variant listing."""
    return await SourcingService().collect(
        session,
        tenant_id,
        actor_id,
        SourcingCollectRequest(
            source_platform="1688",
            source_url="https://detail.1688.com/offer/681234567890.html",
            raw_title="LED Solar Flood Light 100W Outdoor",
            raw_text="供应商报价 ¥45.00，≥100件 45.00元，工厂直发",
            raw_price_cny=Decimal("45.00"),
            target_margin_pct=Decimal("40.00"),
            category="100012",
            hs_code="9405429000",
            images=["https://cbu01.alicdn.com/img/ibank/flood-100w.jpg"],
            options_spec=[SourcingOptionSpec(name="Color Temp", values=["3000K", "6000K"])],
            variants_spec=[
                SourcingVariantSpec(
                    sku_suffix="3000K",
                    option_values={"Color Temp": "3000K"},
                    cost_price_cny=Decimal("36.00"),
                ),
                SourcingVariantSpec(
                    sku_suffix="6000K",
                    option_values={"Color Temp": "6000K"},
                    cost_price_cny=Decimal("43.20"),
                ),
            ],
        ),
    )


async def _publish_to_fleet(
    session: AsyncSession,
    tenant_id: UUID,
    actor_id: str,
    accounts: dict[str, str],
    *,
    transport: httpx.MockTransport,
) -> tuple[Any, list[Any]]:
    draft = await _collect_multi_variant_draft(session, tenant_id, actor_id)
    results = await SourcingService().publish(
        session,
        tenant_id,
        actor_id,
        draft.id,
        SourcingPublishRequest(
            channel_account_ids=[UUID(account) for account in accounts.values()],
            push_to_marketplace=True,
            initial_stock_quantity=77,
        ),
        listing_publisher=MarketplaceListingDispatcher(transport=transport),
    )
    return draft, results


async def _variant_skus(session: AsyncSession, tenant_id: UUID) -> list[str]:
    rows = (
        await session.execute(
            text(
                "SELECT sku FROM product_variants WHERE tenant_id = :tid ORDER BY sku"
            ),
            {"tid": tenant_id},
        )
    ).scalars().all()
    return [str(sku) for sku in rows]


# --------------------------------------------------------------------------------------
# 1. Connect: 201, no plaintext secret, sealed credential + routing binding
# --------------------------------------------------------------------------------------


async def test_connect_all_eight_marketplaces_returns_201_without_plaintext_secrets(
    rt_session_factory: tuple[Factory, list[UUID]],
) -> None:
    factory, created = rt_session_factory
    workspace = await _seed_tenant(factory, created, label="Conn")
    holder = [workspace["principal"]]
    fleet = MarketplaceFleet()

    accounts: dict[str, str] = {}
    async with _running_app(holder, fleet.transport) as client:
        for platform in PLATFORMS:
            slug = CONNECT_SLUGS.get(platform, platform)
            payload = dict(CONNECT_PAYLOADS[platform])
            payload["display_name"] = f"QZLED {platform}"
            response = await client.post(f"/api/v1/channels/{slug}/connect", json=payload)
            assert response.status_code == 201, f"{platform}: {response.text}"
            body = response.json()
            assert body["channel_type"] == platform
            assert body["credential_type"] == CREDENTIAL_TYPES[platform]
            assert body["external_account_id"] == EXTERNAL_ACCOUNT_IDS[platform]
            assert body["display_name"] == f"QZLED {platform}"
            assert body["status"] == "active"
            serialized = json.dumps(body)
            for secret in SECRET_VALUES:
                assert secret not in serialized, f"{platform} leaked a plaintext secret"
            accounts[platform] = body["channel_account_id"]

    assert len(accounts) == len(PLATFORMS)
    assert len(set(accounts.values())) == len(PLATFORMS)

    async with tenant_session(factory, workspace["tenant_id"]) as session:
        account_rows = (
            await session.execute(
                text(
                    "SELECT id, channel_type, external_account_id, status, config "
                    "FROM channel_accounts WHERE tenant_id = :tid ORDER BY channel_type"
                ),
                {"tid": workspace["tenant_id"]},
            )
        ).all()
        assert len(account_rows) == len(PLATFORMS)
        by_type = {str(row.channel_type): row for row in account_rows}
        assert set(by_type) == set(PLATFORMS)
        for platform, row in by_type.items():
            assert str(row.id) == accounts[platform]
            assert str(row.external_account_id) == EXTERNAL_ACCOUNT_IDS[platform]
            assert str(row.status) == "active"
            # Marketplace-side config only; no secret is ever stored in ``config``.
            config_text = json.dumps(dict(row.config))
            for secret in SECRET_VALUES:
                assert secret not in config_text

        credential_rows = (
            await session.execute(
                text(
                    "SELECT channel_account_id, credential_type, ciphertext, nonce, "
                    "auth_tag, key_version FROM channel_credentials "
                    "WHERE tenant_id = :tid"
                ),
                {"tid": workspace["tenant_id"]},
            )
        ).all()
        assert len(credential_rows) == len(PLATFORMS)
        for credential in credential_rows:
            account_id = str(credential.channel_account_id)
            platform = next(p for p, a in accounts.items() if a == account_id)
            assert str(credential.credential_type) == CREDENTIAL_TYPES[platform]
            assert int(credential.key_version) >= 1
            assert len(bytes(credential.nonce)) == 12
            assert len(bytes(credential.auth_tag)) == 16
            ciphertext = bytes(credential.ciphertext)
            for secret in SECRET_VALUES:
                assert secret.encode() not in ciphertext, f"{platform} ciphertext leaked"
            # The vault round-trips the exact secret the connect route sealed.
            decrypted, _expires_at = await CredentialVaultHelper().load_secret_json(
                session,
                workspace["tenant_id"],
                UUID(account_id),
                CREDENTIAL_TYPES[platform],
            )
            assert decrypted[WEBHOOK_SECRET_KEYS[platform]] == WEBHOOK_SECRETS[platform]

        routing_rows = (
            await session.execute(
                text(
                    "SELECT channel_type, external_account_id, channel_account_id, is_active "
                    "FROM channel_routing_directory WHERE tenant_id = :tid"
                ),
                {"tid": workspace["tenant_id"]},
            )
        ).all()
        assert len(routing_rows) == len(PLATFORMS)
        routing = {str(row.channel_type): row for row in routing_rows}
        for platform, account_id in accounts.items():
            assert str(routing[platform].channel_account_id) == account_id
            assert str(routing[platform].external_account_id) == (
                EXTERNAL_ACCOUNT_IDS[platform]
            )
            assert routing[platform].is_active is True

        audit_count = await session.scalar(
            text(
                "SELECT count(*) FROM audit_events WHERE tenant_id = :tid "
                "AND action = 'channel.marketplace.connected'"
            ),
            {"tid": workspace["tenant_id"]},
        )
        assert int(audit_count or 0) == len(PLATFORMS)


# --------------------------------------------------------------------------------------
# 2. Live multi-variant publish across all eight platforms
# --------------------------------------------------------------------------------------


async def test_multivariant_publish_goes_live_on_all_eight_marketplaces(
    rt_session_factory: tuple[Factory, list[UUID]],
) -> None:
    factory, created = rt_session_factory
    workspace = await _seed_tenant(factory, created, label="Pub")
    tenant_id = workspace["tenant_id"]
    actor_id = str(workspace["user_id"])
    holder = [workspace["principal"]]
    fleet = MarketplaceFleet()

    async with _running_app(holder, fleet.transport) as client:
        accounts = await _connect_fleet(client)

    async with tenant_session(factory, tenant_id) as session:
        draft, results = await _publish_to_fleet(
            session,
            tenant_id,
            actor_id,
            accounts,
            transport=fleet.transport,
        )
        suggested_sku = draft.suggested_sku
        expected_variant_skus = {
            platform: sorted(
                [
                    build_external_sku(platform, f"{suggested_sku}-3000K"),
                    build_external_sku(platform, f"{suggested_sku}-6000K"),
                ]
            )
            for platform in PLATFORMS
        }

        assert len(results) == len(PLATFORMS)
        by_platform = {result.channel_type: result for result in results}
        assert set(by_platform) == set(PLATFORMS)

        for platform, result in by_platform.items():
            assert result.listing_state == "live_published"
            assert result.external_listing_confirmed is True
            assert result.publish_mode == "live_marketplace_api"
            assert result.external_sku in expected_variant_skus[platform]
            expected_product_id = EXPECTED_PRODUCT_IDS.get(platform, result.external_sku)
            assert result.external_product_id == expected_product_id
            assert result.remote_status
            assert fleet.calls[platform], f"{platform} never received the listing"
            assert result.mapping_id is not None

        draft_row = (
            await session.execute(
                text(
                    "SELECT status, publish_history FROM sourcing_drafts "
                    "WHERE tenant_id = :tid AND id = :did"
                ),
                {"tid": tenant_id, "did": draft.id},
            )
        ).one()
        assert draft_row.status == "published"
        history = draft_row.publish_history
        assert len(history) == len(PLATFORMS)
        assert {entry["channel_type"] for entry in history} == set(PLATFORMS)
        assert all(entry["listing_state"] == "live_published" for entry in history)
        assert all(entry["external_listing_confirmed"] is True for entry in history)

        mapping_rows = (
            await session.execute(
                text(
                    "SELECT m.channel_account_id, m.external_sku, m.external_product_id, "
                    "m.external_variant_id, m.external_inventory_item_id, m.variant_id, "
                    "m.sync_inventory_enabled "
                    "FROM platform_sku_mappings m WHERE m.tenant_id = :tid "
                    "ORDER BY m.external_sku"
                ),
                {"tid": tenant_id},
            )
        ).all()
        assert len(mapping_rows) == len(PLATFORMS) * 2

        for platform, account_id in accounts.items():
            rows = [r for r in mapping_rows if str(r.channel_account_id) == account_id]
            assert sorted(str(row.external_sku) for row in rows) == (
                expected_variant_skus[platform]
            )
            expected_product_id = EXPECTED_PRODUCT_IDS.get(
                platform, by_platform[platform].external_sku
            )
            assert {str(row.external_product_id) for row in rows} == {expected_product_id}
            # Per-variant receipts landed next to the listing, not just a product id.
            if platform == "walmart":
                # Walmart identifies an item by seller SKU and returns no variant handles.
                assert all(row.external_variant_id is None for row in rows)
            else:
                assert all(row.external_variant_id for row in rows), platform
            # Lazada and Mercado Libre expose a dedicated per-SKU inventory handle.
            if platform in {"lazada", "mercado_libre"}:
                assert all(row.external_inventory_item_id for row in rows), platform
            assert all(row.sync_inventory_enabled is True for row in rows)

    # Remote payload sanity: each marketplace really saw the two priced variants.
    shopee_body = _last_call(fleet, "shopee", path="/api/v2/product/add_item")
    shopee_payload = json.loads(shopee_body.content)
    assert sorted(model["model_sku"] for model in shopee_payload["model"]) == (
        expected_variant_skus["shopee"]
    )
    assert shopee_payload["category_id"] == 100012
    assert shopee_payload["logistic_info"] == [{"logistic_id": 80001, "enabled": True}]

    lazada_body = json.loads(
        _last_call(fleet, "lazada", path="/rest/product/create").content
    )
    assert sorted(
        sku["SellerSku"] for sku in lazada_body["Request"]["Product"]["Skus"]
    ) == expected_variant_skus["lazada"]

    temu_envelope = json.loads(
        _last_call(fleet, "temu", path="/openapi/router").content
    )
    assert temu_envelope["type"] == "bg.goods.add"
    assert sorted(sku["extCode"] for sku in temu_envelope["data"]["skuList"]) == (
        expected_variant_skus["temu"]
    )

    shein_body = json.loads(
        _last_call(fleet, "shein", path="/open-api/goods/product").content
    )
    assert sorted(sku["skuCode"] for sku in shein_body["skuList"]) == (
        expected_variant_skus["shein"]
    )

    aliexpress_form = _last_aliexpress_form(fleet, "aliexpress.solution.product.create")
    aliexpress_product = json.loads(aliexpress_form["product_body"])
    assert sorted(sku["sku_code"] for sku in aliexpress_product["skus"]) == (
        expected_variant_skus["aliexpress"]
    )

    ozon_import = json.loads(_last_call(fleet, "ozon", path="/v3/product/import").content)
    assert sorted(item["offer_id"] for item in ozon_import["items"]) == (
        expected_variant_skus["ozon"]
    )

    walmart_feed = json.loads(_last_call(fleet, "walmart", path="/v3/feeds").content)
    assert sorted(item["Orderable"]["sku"] for item in walmart_feed["MPItem"]) == (
        expected_variant_skus["walmart"]
    )

    meli_body = json.loads(
        _last_call(fleet, "mercado_libre", path="/items", method="POST").content
    )
    assert sorted(row["seller_custom_field"] for row in meli_body["variations"]) == (
        expected_variant_skus["mercado_libre"]
    )


# --------------------------------------------------------------------------------------
# 3. Stock mutation -> outbox drain across all eight platforms
# --------------------------------------------------------------------------------------


async def test_inventory_outbox_pushes_clamped_sellable_qty_to_all_eight(
    rt_session_factory: tuple[Factory, list[UUID]],
) -> None:
    factory, created = rt_session_factory
    workspace = await _seed_tenant(factory, created, label="Stock")
    tenant_id = workspace["tenant_id"]
    actor_id = str(workspace["user_id"])
    holder = [workspace["principal"]]
    fleet = MarketplaceFleet()

    async with _running_app(holder, fleet.transport) as client:
        accounts = await _connect_fleet(client)

    async with tenant_session(factory, tenant_id) as session:
        draft, results = await _publish_to_fleet(
            session, tenant_id, actor_id, accounts, transport=fleet.transport
        )
        assert len(results) == len(PLATFORMS)
        assert draft.suggested_sku
        published_skus = await _variant_skus(session, tenant_id)
        assert len(published_skus) == 2

        primary_variant = (
            await session.execute(
                text(
                    "SELECT id, sku FROM product_variants WHERE tenant_id = :tid "
                    "AND sku LIKE :pattern ORDER BY sku LIMIT 1"
                ),
                {"tid": tenant_id, "pattern": "%-3000K"},
            )
        ).one()
        primary_variant_id = UUID(str(primary_variant.id))
        primary_external_skus = {
            platform: build_external_sku(platform, str(primary_variant.sku))
            for platform in PLATFORMS
        }

        # Pin the per-channel safety stock / bundle multiplier the push must honour.
        tuned = 0
        for platform in PLATFORMS:
            safety, bundle = STOCK_PROFILE[platform]
            tuned += int(
                await session.scalar(
                    text(
                        "UPDATE platform_sku_mappings SET safety_stock = :safety, "
                        "bundle_multiplier = :bundle WHERE tenant_id = :tid "
                        "AND variant_id = :vid AND channel_account_id = :caid "
                        "RETURNING 1"
                    ),
                    {
                        "tid": tenant_id,
                        "vid": primary_variant_id,
                        "caid": UUID(accounts[platform]),
                        "safety": safety,
                        "bundle": bundle,
                    },
                )
                or 0
            )
        assert tuned == len(PLATFORMS)

        await InventoryLedgerService().inbound_stock(
            session,
            tenant_id,
            workspace["warehouse_id"],
            primary_variant_id,
            qty=INBOUND_QTY,
            reference_id=uuid4(),
            idempotency_key=f"inbound-{uuid4()}",
        )

        summary = await InventorySyncDispatcher(transport=fleet.transport).dispatch_pending_jobs(
            session, tenant_id, limit=50
        )

        # "pushed_count == 8": one successful live push per newly connected marketplace.
        assert summary.dispatched == len(PLATFORMS)
        assert summary.succeeded == len(PLATFORMS)
        assert summary.failed == 0
        assert summary.dead_letter == 0

        jobs_by_channel = {
            (job.channel_type, job.external_sku): job for job in summary.jobs
        }
        assert len(jobs_by_channel) == len(PLATFORMS)
        for platform in PLATFORMS:
            job = jobs_by_channel[(platform, primary_external_skus[platform])]
            safety, bundle = STOCK_PROFILE[platform]
            expected_qty = compute_sellable_channel_qty(
                INBOUND_QTY, safety_stock=safety, bundle_multiplier=bundle
            )
            assert expected_qty == EXPECTED_SELLABLE_QTY[platform]
            assert job.status == "succeeded"
            assert job.requested_qty == expected_qty
            assert job.synced_qty == expected_qty
            assert job.error_code is None

        mapping_rows = (
            await session.execute(
                text(
                    "SELECT channel_account_id, last_inventory_synced_qty, "
                    "last_sync_status, last_sync_error FROM platform_sku_mappings "
                    "WHERE tenant_id = :tid AND variant_id = :vid"
                ),
                {"tid": tenant_id, "vid": primary_variant_id},
            )
        ).all()
        synced_by_account = {str(row.channel_account_id): row for row in mapping_rows}
        assert len(synced_by_account) == len(PLATFORMS)
        for platform, account_id in accounts.items():
            row = synced_by_account[account_id]
            expected_qty = EXPECTED_SELLABLE_QTY[platform]
            assert int(row.last_inventory_synced_qty) == expected_qty
            assert str(row.last_sync_status) == "succeeded"
            assert row.last_sync_error is None

    _assert_stock_payloads(fleet, primary_external_skus)


# --------------------------------------------------------------------------------------
# 4. Order reconciliation: refund + 2dp commission roll-up across the new channels
# --------------------------------------------------------------------------------------


async def test_order_reconciliation_rolls_up_refunds_and_commissions_across_eight_channels(
    rt_session_factory: tuple[Factory, list[UUID]],
) -> None:
    factory, created = rt_session_factory
    workspace = await _seed_tenant(factory, created, label="Analytics")
    tenant_id = workspace["tenant_id"]
    actor_id = str(workspace["user_id"])
    holder = [workspace["principal"]]
    fleet = MarketplaceFleet()

    async with _running_app(holder, fleet.transport) as client:
        accounts = await _connect_fleet(client)

    async with tenant_session(factory, tenant_id) as session:
        draft, results = await _publish_to_fleet(
            session, tenant_id, actor_id, accounts, transport=fleet.transport
        )
        assert len(results) == len(PLATFORMS)

        primary_variant = (
            await session.execute(
                text(
                    "SELECT id, sku FROM product_variants WHERE tenant_id = :tid "
                    "AND sku LIKE :pattern ORDER BY sku LIMIT 1"
                ),
                {"tid": tenant_id, "pattern": "%-3000K"},
            )
        ).one()
        primary_variant_id = UUID(str(primary_variant.id))
        await InventoryLedgerService().inbound_stock(
            session,
            tenant_id,
            workspace["warehouse_id"],
            primary_variant_id,
            qty=200,
            reference_id=uuid4(),
            idempotency_key=f"inbound-{uuid4()}",
        )

        sync = EcommerceOrderSyncService()
        for platform in PLATFORMS:
            external_sku = build_external_sku(platform, str(primary_variant.sku))
            await sync.ingest_normalized_order(
                session,
                tenant_id=tenant_id,
                channel_account_id=UUID(accounts[platform]),
                default_warehouse_id=workspace["warehouse_id"],
                order_dto=NormalizedOrder(
                    external_order_id=f"{platform.upper()}-ORDER-1",
                    order_mode=OrderMode.B2C_RETAIL,
                    currency="USD",
                    fx_rate_to_base=Decimal("1.000000"),
                    subtotal_amount=ORDER_TOTAL,
                    total_amount=ORDER_TOTAL,
                    platform_commission_base=ORDER_COMMISSION,
                    channel_metadata={"refunded_amount": str(ORDER_REFUND)},
                    customer=NormalizedCustomer(
                        email=f"{platform}-buyer@{TEST_EMAIL_DOMAIN}",
                        external_identity_type=IdentityType.EMAIL,
                        display_name=f"{platform} buyer",
                    ),
                    line_items=[
                        NormalizedLineItem(
                            external_sku=external_sku,
                            quantity=1,
                            line_total=ORDER_TOTAL,
                        )
                    ],
                    updated_at=datetime.now(UTC),
                ),
            )

        order_rows = (
            await session.execute(
                text(
                    "SELECT channel_account_id, external_order_id, currency, total_amount, "
                    "channel_metadata FROM orders WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            )
        ).all()
        assert len(order_rows) == len(PLATFORMS)
        assert {str(row.external_order_id) for row in order_rows} == {
            f"{platform.upper()}-ORDER-1" for platform in PLATFORMS
        }
        for row in order_rows:
            metadata = dict(row.channel_metadata)
            assert metadata["refunded_amount"] == "1.11"
            assert metadata["platform_commission_base"] == "3.33"
            assert Decimal(str(row.total_amount)) == ORDER_TOTAL

        overview = await ChannelAnalyticsService().get_overview(
            session, tenant_id, window="30d"
        )

    expected_commission = ORDER_COMMISSION * len(PLATFORMS)
    expected_refund = ORDER_REFUND * len(PLATFORMS)
    expected_gmv = ORDER_TOTAL * len(PLATFORMS)
    assert overview.base_currency == "USD"
    assert overview.totals.order_count == len(PLATFORMS)
    assert overview.totals.units_sold == len(PLATFORMS)
    assert overview.totals.gmv_base == expected_gmv
    assert overview.totals.refunded_amount_base == expected_refund
    assert overview.totals.platform_commission_base == expected_commission
    assert expected_commission == Decimal("26.64")
    assert expected_refund == Decimal("8.88")

    breakdown = {item.channel_type: item for item in overview.channels}
    assert set(breakdown) == set(PLATFORMS)
    for platform, item in breakdown.items():
        assert item.order_count == 1, platform
        assert item.units_sold == 1, platform
        assert item.gmv_base == ORDER_TOTAL
        assert item.refunded_amount_base == ORDER_REFUND
        assert item.platform_commission_base == ORDER_COMMISSION
        # Money never drifts below the 2dp NUMERIC scale.
        assert item.platform_commission_base.as_tuple().exponent == -2

    # The same roll-up is visible on the service's HTTP surface.
    holder[0] = workspace["principal"]
    async with _running_app(holder, fleet.transport) as client:
        response = await client.get(
            "/api/v1/finance/channel-analytics/overview", params={"window": "30d"}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["order_count"] == len(PLATFORMS)
    assert Decimal(body["totals"]["platform_commission_base"]) == expected_commission
    assert Decimal(body["totals"]["refunded_amount_base"]) == expected_refund


# --------------------------------------------------------------------------------------
# 5. Signed webhook ingress for all eight platforms
# --------------------------------------------------------------------------------------


def _webhook_body(platform: str, sequence: int) -> bytes:
    return json.dumps(
        {"event": "order.updated", "platform": platform, "sequence": sequence},
        separators=(",", ":"),
    ).encode()


def _signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _webhook_headers(platform: str, *, event_id: str, signature: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        DOMAIN_HEADERS[platform]: EXTERNAL_ACCOUNT_IDS[platform],
        DEDUP_HEADERS[platform]: event_id,
        TOPIC_HEADERS[platform]: "order.updated",
    }
    if signature is not None:
        headers[SIGNATURE_HEADERS[platform]] = signature
    return headers


async def test_signed_webhooks_are_persisted_and_tampered_ones_are_401(
    rt_session_factory: tuple[Factory, list[UUID]],
) -> None:
    factory, created = rt_session_factory
    workspace = await _seed_tenant(factory, created, label="Hook", with_warehouse=False)
    holder = [workspace["principal"]]
    fleet = MarketplaceFleet()
    run_id = uuid4().hex[:12]

    async with _running_app(holder, fleet.transport) as client:
        await _connect_fleet(client)

        for platform in PLATFORMS:
            route = WEBHOOK_PATHS.get(platform, f"/api/v1/webhooks/{platform}")
            body = _webhook_body(platform, 1)
            event_id = f"{WEBHOOK_EVENT_PREFIX}-{run_id}-{platform}"
            good = _webhook_headers(
                platform,
                event_id=event_id,
                signature=_signature(WEBHOOK_SECRETS[platform], body),
            )

            accepted = await client.post(route, content=body, headers=good)
            assert accepted.status_code == 200, f"{platform}: {accepted.text}"
            assert accepted.json() == {
                "code": 200,
                "status": "accepted",
                "deduplicated": False,
            }

            duplicate = await client.post(route, content=body, headers=good)
            assert duplicate.status_code == 200, duplicate.text
            assert duplicate.json()["deduplicated"] is True

            tampered = await client.post(
                route,
                content=body + b" ",
                headers=good,
            )
            assert tampered.status_code == 401, f"{platform}: {tampered.text}"
            assert tampered.json()["code"] == "ERR_INVALID_WEBHOOK_SIGNATURE"

            unsigned = await client.post(
                route,
                content=body,
                headers=_webhook_headers(platform, event_id=f"{event_id}-unsigned", signature=None),
            )
            assert unsigned.status_code == 401
            assert unsigned.json()["code"] == "ERR_INVALID_WEBHOOK_SIGNATURE"

    async with _admin_connection() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT channel_type, external_account_id, raw_payload, "
                    "signature_verified, processing_status FROM raw_webhook_events "
                    "WHERE external_event_id LIKE :pattern ORDER BY channel_type"
                ),
                {"pattern": f"{WEBHOOK_EVENT_PREFIX}-{run_id}-%"},
            )
        ).all()
    # One receipt per platform: the duplicate delivery deduplicated, the tampered pair
    # never reached the store.
    assert len(rows) == len(PLATFORMS)
    stored = {str(row.channel_type): row for row in rows}
    assert set(stored) == set(PLATFORMS)
    for platform, row in stored.items():
        assert str(row.external_account_id) == EXTERNAL_ACCOUNT_IDS[platform]
        assert dict(row.raw_payload) == json.loads(_webhook_body(platform, 1))
        assert row.signature_verified is True
        assert str(row.processing_status) in {"received", "queued"}


# --------------------------------------------------------------------------------------
# 6. Cross-tenant PG17 RLS isolation
# --------------------------------------------------------------------------------------


async def test_cross_tenant_rls_blocks_read_sync_publish_and_reconcile(
    rt_session_factory: tuple[Factory, list[UUID]],
) -> None:
    factory, created = rt_session_factory
    workspace_a = await _seed_tenant(factory, created, label="RlsA")
    workspace_b = await _seed_tenant(factory, created, label="RlsB", with_warehouse=False)
    tenant_a = workspace_a["tenant_id"]
    tenant_b = workspace_b["tenant_id"]
    actor_a = str(workspace_a["user_id"])
    holder = [workspace_a["principal"]]
    fleet = MarketplaceFleet()

    async with _running_app(holder, fleet.transport) as client:
        accounts = await _connect_fleet(client, suffix=" A")

    async with tenant_session(factory, tenant_a) as session:
        _draft, results = await _publish_to_fleet(
            session, tenant_a, actor_a, accounts, transport=fleet.transport
        )
        assert len(results) == len(PLATFORMS)
        primary_variant = (
            await session.execute(
                text(
                    "SELECT id FROM product_variants WHERE tenant_id = :tid "
                    "AND sku LIKE :pattern ORDER BY sku LIMIT 1"
                ),
                {"tid": tenant_a, "pattern": "%-3000K"},
            )
        ).scalar_one()
        primary_variant_id = UUID(str(primary_variant))
        await InventoryLedgerService().inbound_stock(
            session,
            tenant_a,
            workspace_a["warehouse_id"],
            primary_variant_id,
            qty=INBOUND_QTY,
            reference_id=uuid4(),
            idempotency_key=f"inbound-{uuid4()}",
        )

    # --- Tenant B sees nothing of tenant A through a runtime (RLS) session ------------
    async with tenant_session(factory, tenant_b) as session_b:
        for table in (
            "channel_accounts",
            "channel_credentials",
            "platform_sku_mappings",
            "inventory_sync_jobs",
            "audit_events",
        ):
            visible = await session_b.scalar(text(f"SELECT count(*) FROM {table}"))
            assert int(visible or 0) == 0, f"RLS leaked {table} across tenants"

        blocked_sync = await InventorySyncDispatcher(
            transport=fleet.transport
        ).dispatch_pending_jobs(session_b, tenant_b)
        assert blocked_sync.dispatched == 0
        assert blocked_sync.succeeded == 0

        blocked_orders = await session_b.scalar(
            text("SELECT count(*) FROM orders WHERE tenant_id = :tid"),
            {"tid": tenant_a},
        )
        assert int(blocked_orders or 0) == 0

    # --- Tenant B cannot reconcile, publish to, or read tenant A's accounts ----------
    holder[0] = workspace_b["principal"]
    async with _running_app(holder, fleet.transport) as client:
        for platform, account_id in accounts.items():
            sync_attempt = await client.post(
                f"/api/v1/channels/marketplace/{account_id}/sync-orders"
            )
            assert sync_attempt.status_code == 404, f"{platform}: {sync_attempt.text}"
            assert sync_attempt.json()["code"] == "ERR_CHANNEL_ACCOUNT_NOT_FOUND"

        overview_b = await client.get(
            "/api/v1/finance/channel-analytics/overview", params={"window": "30d"}
        )
        assert overview_b.status_code == 200
        assert overview_b.json()["totals"]["order_count"] == 0
        assert overview_b.json()["channels"] == []

    async with tenant_session(factory, tenant_b) as session_b:
        draft_b = await _collect_multi_variant_draft(
            session_b, tenant_b, str(workspace_b["user_id"])
        )
        with pytest.raises(AppError) as excinfo:
            await SourcingService().publish(
                session_b,
                tenant_b,
                str(workspace_b["user_id"]),
                draft_b.id,
                SourcingPublishRequest(
                    channel_account_ids=[UUID(accounts["shopee"])],
                    push_to_marketplace=True,
                ),
                listing_publisher=MarketplaceListingDispatcher(transport=fleet.transport),
            )
        assert excinfo.value.status_code == 404
        assert excinfo.value.code == "ERR_CHANNEL_ACCOUNT_NOT_FOUND"

    # --- Control: tenant A still drains its own (and only its own) outbox ------------
    async with tenant_session(factory, tenant_a) as session_a:
        own_summary = await InventorySyncDispatcher(
            transport=fleet.transport
        ).dispatch_pending_jobs(session_a, tenant_a, limit=50)
    assert own_summary.dispatched == len(PLATFORMS)
    assert own_summary.succeeded == len(PLATFORMS)


# --------------------------------------------------------------------------------------
# Mock-payload assertions for the outbound stock pushes
# --------------------------------------------------------------------------------------


def _last_call(
    fleet: MarketplaceFleet,
    platform: str,
    *,
    path: str | None = None,
    method: str | None = None,
) -> httpx.Request:
    matches = [
        call
        for call in fleet.calls[platform]
        if (path is None or call.url.path == path)
        and (method is None or call.method == method)
    ]
    assert matches, f"{platform}: no call matched path={path} method={method}"
    return matches[-1]


def _last_aliexpress_form(fleet: MarketplaceFleet, api_method: str) -> dict[str, str]:
    matches = [
        dict(parse_qsl(call.content.decode("utf-8")))
        for call in fleet.calls["aliexpress"]
        if "method=" + api_method in call.content.decode("utf-8")
    ]
    assert matches, f"aliexpress: no {api_method} call"
    return matches[-1]


def _assert_stock_payloads(
    fleet: MarketplaceFleet, primary_external_skus: dict[str, str]
) -> None:
    """Pin the exact quantity each marketplace received for the same stock movement."""
    shopee = json.loads(
        _last_call(fleet, "shopee", path="/api/v2/product/update_stock").content
    )
    assert shopee["item_id"] == 987654321
    assert shopee["stock_list"][0]["model_id"] in {5001, 5002}
    assert shopee["stock_list"][0]["seller_stock"][0]["stock"] == EXPECTED_SELLABLE_QTY["shopee"]

    lazada = json.loads(
        _last_call(fleet, "lazada", path="/rest/product/price_quantity/update").content
    )
    assert lazada["skus"][0]["SellerSku"] == primary_external_skus["lazada"]
    assert lazada["skus"][0]["Quantity"] == EXPECTED_SELLABLE_QTY["lazada"]

    temu = json.loads(_last_call(fleet, "temu", path="/openapi/router").content)
    assert temu["type"] == "bg.goods.stock.update"
    assert temu["data"]["extCode"] == primary_external_skus["temu"]
    assert temu["data"]["quantity"] == EXPECTED_SELLABLE_QTY["temu"]

    shein = json.loads(
        _last_call(fleet, "shein", path="/open-api/goods/inventory/update").content
    )
    assert shein["goodsId"] == "SHEIN-GOODS-1"
    assert shein["skuCode"] == primary_external_skus["shein"]
    assert shein["quantity"] == EXPECTED_SELLABLE_QTY["shein"]

    aliexpress_form = _last_aliexpress_form(fleet, "aliexpress.solution.sku.stock.update")
    assert aliexpress_form["sku_code"] == primary_external_skus["aliexpress"]
    assert int(aliexpress_form["stock"]) == EXPECTED_SELLABLE_QTY["aliexpress"]

    ozon = json.loads(
        _last_call(fleet, "ozon", path="/v1/product/import/stocks").content
    )
    assert ozon["stocks"][0]["offer_id"] == primary_external_skus["ozon"]
    assert ozon["stocks"][0]["stock"] == EXPECTED_SELLABLE_QTY["ozon"]
    assert ozon["stocks"][0]["warehouse_id"] == 9876543

    walmart = json.loads(
        _last_call(fleet, "walmart", path="/v3/inventory", method="PUT").content
    )
    assert walmart["sku"] == primary_external_skus["walmart"]
    assert walmart["quantity"] == {
        "unit": "EACH",
        "amount": EXPECTED_SELLABLE_QTY["walmart"],
    }

    meli_calls = [
        call
        for call in fleet.calls["mercado_libre"]
        if call.method == "PUT"
        and call.url.path.startswith("/items/MLM-ITEM-1")
        and not call.url.path.endswith("/description")
    ]
    assert meli_calls, "mercado_libre: no stock push"
    meli = json.loads(meli_calls[-1].content)
    assert meli["available_quantity"] == EXPECTED_SELLABLE_QTY["mercado_libre"]
