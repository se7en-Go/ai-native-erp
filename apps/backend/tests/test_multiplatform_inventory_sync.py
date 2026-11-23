"""Phase 2 PG17 RLS integration tests: all 8 ecommerce channels real-time inventory sync."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import httpx
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
from erp_core.channels.ecommerce.base import (
    CredentialVaultHelper,
    EcommerceOrderSyncService,
    NormalizedCustomer,
    NormalizedLineItem,
    NormalizedOrder,
)
from erp_core.channels.ecommerce.inventory_sync_dispatcher import (
    InventorySyncDispatcher,
    drain_inventory_sync_all_tenants,
    reconcile_inventory_sync_cron,
)
from erp_core.channels.ecommerce.inventory_sync_router import router as inventory_sync_router
from erp_core.channels.ecommerce.listing_dispatcher import CREDENTIAL_TYPES
from erp_core.orders.inventory_service import InventoryLedgerService, compute_sellable_channel_qty
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import (
    FulfillmentStatus,
    IdentityType,
    OrderMode,
    PaymentStatus,
    UserRole,
)
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]

EIGHT_CHANNEL_CREDENTIALS: dict[str, dict[str, Any]] = {
    "shopify": {
        "shop_domain": "qzled-inv-sync.myshopify.com",
        "access_token": "shpat_inv_sync_test_001",
    },
    "tiktok_shop": {
        "app_key": "tt_key_inv",
        "app_secret": "tt_secret_inv",
        "access_token": "tt_tok_inv",
        "shop_cipher": "CIPHER_INV_01",
        "warehouse_id": "WH_TT_01",
    },
    "amazon_sp": {
        "seller_id": "A2INVSYNC99",
        "marketplace_id": "ATVPDKIKX0DER",
        "lwa_client_id": "amzn1.client.inv",
        "lwa_client_secret": "amzn1.secret.inv",
        "refresh_token": "Atzr|inv_refresh",
    },
    "ebay": {
        "seller_username": "qz_inv_ebay",
        "site_id": "EBAY_US",
        "app_id": "ebay_app_inv",
        "cert_id": "ebay_cert_inv",
        "refresh_token": "v^1.1#inv_ebay",
    },
    "alibaba_email": {
        "store_id": "ali_inv_store",
        "app_key": "ali_app_inv",
        "app_secret": "ali_secret_inv",
        "access_token": "ali_tok_inv",
    },
    "woocommerce": {
        "store_url": "woo-inv.example.com",
        "consumer_key": "ck_inv_001",
        "consumer_secret": "cs_inv_001",
    },
    "shopline": {
        "store_domain": "shopline-inv.myshopline.com",
        "access_token": "sl_tok_inv_001",
    },
    "shoplazza": {
        "store_domain": "shoplazza-inv.myshoplaza.com",
        "access_token": "sz_tok_inv_001",
    },
}


def _runtime_url() -> str:
    return os.environ.get(
        "DATABASE_RUNTIME_URL",
        get_settings().DATABASE_RUNTIME_URL,
    )


def _migrator_url() -> str:
    return os.environ.get(
        "DATABASE_MIGRATOR_URL",
        get_settings().DATABASE_MIGRATOR_URL,
    )


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
                    "DELETE FROM inventory_sync_jobs WHERE tenant_id = :tid",
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
                    "DELETE FROM channel_routing_directory WHERE tenant_id = :tid",
                    "DELETE FROM channel_credentials WHERE tenant_id = :tid",
                    "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                    "DELETE FROM warehouses WHERE tenant_id = :tid",
                    "DELETE FROM audit_events WHERE tenant_id = :tid",
                    "DELETE FROM tenant_memberships WHERE tenant_id = :tid",
                ):
                    await conn.execute(text(stmt), {"tid": tid})
                await conn.execute(
                    text("DELETE FROM users WHERE email LIKE :pat"),
                    {"pat": f"%{tid.hex}@inv-sync-test.example.com"},
                )
                await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid})
    finally:
        await admin.dispose()


@pytest.fixture
async def session_factories() -> AsyncIterator[tuple[Factory, Factory, list[UUID]]]:
    rt_engine = create_async_engine(_runtime_url())
    mig_engine = create_async_engine(_migrator_url())
    rt_factory = async_sessionmaker(rt_engine, expire_on_commit=False)
    mig_factory = async_sessionmaker(mig_engine, expire_on_commit=False)
    created_tenants: list[UUID] = []
    try:
        yield rt_factory, mig_factory, created_tenants
    finally:
        await rt_engine.dispose()
        await mig_engine.dispose()
        if created_tenants:
            await _cleanup_tenants(created_tenants)


async def _seed_tenant_with_variant(
    session: AsyncSession,
    *,
    tid: UUID,
    slug_prefix: str,
    sku: str,
) -> dict[str, Any]:
    uid = uuid4()
    wid = uuid4()
    await session.execute(
        text(
            "INSERT INTO tenants (id, slug, name, base_currency) "
            "VALUES (:id, :slug, :n, 'CNY')"
        ),
        {"id": tid, "slug": f"{slug_prefix}-{tid.hex[:8]}", "n": "Inv Sync Test"},
    )
    await session.execute(
        text(
            "INSERT INTO users (id, email, full_name, password_hash) "
            "VALUES (:id, :email, 'Owner', 'hash')"
        ),
        {"id": uid, "email": f"{uid.hex}-{tid.hex}@inv-sync-test.example.com"},
    )
    await session.execute(
        text(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, is_active) "
            "VALUES (:tid, :uid, 'owner', true)"
        ),
        {"tid": tid, "uid": uid},
    )
    await session.execute(
        text(
            "INSERT INTO warehouses (id, tenant_id, code, name, country_code, city, is_active) "
            "VALUES (:id, :tid, :code, 'Main WH', 'CN', 'Quanzhou', true)"
        ),
        {"id": wid, "tid": tid, "code": f"WH-{tid.hex[:6]}"},
    )
    catalog = CatalogService()
    prod = await catalog.create_product(
        session,
        tid,
        str(uid),
        ProductCreate(spu_code=f"SPU-{tid.hex[:6]}", category="LED"),
    )
    variant = await catalog.create_variant(
        session,
        tid,
        str(uid),
        VariantCreate(
            product_id=prod.id,
            sku=sku,
            cost_price_base=Decimal("10.00"),
            retail_price_usd=Decimal("19.90"),
            weight_grams=250,
        ),
        UserRole.OWNER,
    )
    return {
        "tenant_id": tid,
        "user_id": uid,
        "warehouse_id": wid,
        "product": prod,
        "variant": variant,
    }


def _make_multiplatform_mock_transport(
    recorded: dict[str, list[dict[str, Any]]],
    *,
    fail_woo_status: int | None = None,
) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.url.host or ""
        path = request.url.path
        body_text = request.content.decode("utf-8", errors="replace") if request.content else ""
        body_json: Any = None
        if body_text:
            try:
                body_json = json.loads(body_text)
            except ValueError:
                body_json = body_text

        if host == "qzled-inv-sync.myshopify.com" and path.endswith("/graphql.json"):
            q = str((body_json or {}).get("query") or "")
            if "PrimaryLocations" in q:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "locations": {
                                "nodes": [{"id": "gid://shopify/Location/9001", "name": "QZWH"}]
                            }
                        }
                    },
                )
            if "VariantBySku" in q:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "productVariants": {
                                "nodes": [
                                    {
                                        "id": "gid://shopify/ProductVariant/8001",
                                        "sku": "SHOP-SYNC-SKU",
                                        "inventoryItem": {
                                            "id": "gid://shopify/InventoryItem/7001"
                                        },
                                    }
                                ]
                            }
                        }
                    },
                )
            if "InventorySetQuantities" in q:
                vars_input = ((body_json or {}).get("variables") or {}).get("input") or {}
                qty_item = (vars_input.get("quantities") or [{}])[0]
                recorded.setdefault("shopify", []).append(
                    {"qty": qty_item.get("quantity"), "item_id": qty_item.get("inventoryItemId")}
                )
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "inventorySetQuantities": {
                                "inventoryAdjustmentGroup": {
                                    "reason": "correction",
                                    "changes": [{"quantityAfterChange": qty_item.get("quantity")}],
                                },
                                "userErrors": [],
                            }
                        }
                    },
                )

        if "tiktokglobalshop.com" in host and path.endswith("/inventory/update"):
            skus = (body_json or {}).get("skus") or []
            inv = (skus[0].get("inventory") or [{}])[0] if skus else {}
            recorded.setdefault("tiktok_shop", []).append({"qty": inv.get("quantity"), "url": url})
            return httpx.Response(200, json={"code": 0, "data": {"failed_skus": []}})

        if "api.amazon.com" in host:
            parsed = parse_qs(body_text)
            if "grant_type" in parsed or path == "/auth/o2/token":
                return httpx.Response(
                    200, json={"access_token": "Atza|inv_access", "expires_in": 3600}
                )
        if "sellingpartnerapi-na.amazon.com" in host and "/listings/2021-08-01/items/" in path:
            qty = (
                (((body_json or [{}])[0].get("value") or [{}])[0].get("quantity"))
                if isinstance(body_json, list)
                else None
            )
            recorded.setdefault("amazon_sp", []).append({"qty": qty, "path": path})
            return httpx.Response(
                200, json={"sku": "AMZ-SYNC-SKU", "status": "ACCEPTED", "submissionId": "SUB-1"}
            )

        if "api.ebay.com" in host and path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "ebay_tok_inv", "expires_in": 7200})
        if "api.ebay.com" in host and path == "/sell/inventory/v1/bulk_update_price_quantity":
            reqs = (body_json or {}).get("requests") or []
            avail = reqs[0].get("shipToLocationAvailability") or {} if reqs else {}
            qty = avail.get("quantity")
            recorded.setdefault("ebay", []).append({"qty": qty})
            return httpx.Response(
                200, json={"responses": [{"sku": "EBAY-SYNC-SKU", "statusCode": 200}]}
            )

        if "openapi-api.alibaba.com" in host or "open-api.alibaba.com" in host:
            parsed_qs = parse_qs(body_text) if body_text else parse_qs(urlparse(url).query)
            qty = int((parsed_qs.get("stock_quantity") or ["0"])[0])
            recorded.setdefault("alibaba_email", []).append({"qty": qty})
            return httpx.Response(200, json={"code": "0", "data": {"success": True}})

        if host == "woo-inv.example.com" and "/wp-json/wc/v3/products/" in path:
            if fail_woo_status is not None:
                return httpx.Response(fail_woo_status, json={"message": "upstream overloaded"})
            qty_val = (body_json or {}).get("stock_quantity")
            recorded.setdefault("woocommerce", []).append({"qty": qty_val, "path": path})
            return httpx.Response(200, json={"id": 501, "stock_quantity": qty_val})

        if host == "shopline-inv.myshopline.com":
            if path.endswith("/locations.json"):
                return httpx.Response(200, json={"locations": [{"id": "SL-LOC-1"}]})
            if path.endswith("/inventory_levels/set.json"):
                avail_val = (body_json or {}).get("available")
                recorded.setdefault("shopline", []).append({"qty": avail_val})
                return httpx.Response(
                    200, json={"inventory_level": {"available": avail_val}}
                )

        if host == "shoplazza-inv.myshoplaza.com" and "/variants/" in path:
            qty_val = (body_json or {}).get("inventory_quantity")
            recorded.setdefault("shoplazza", []).append({"qty": qty_val, "path": path})
            return httpx.Response(
                200,
                json={"variant": {"id": "SZ-VAR-1", "inventory_quantity": qty_val}},
            )

        return httpx.Response(404, json={"error": f"unmocked {request.method} {url}"})

    return httpx.MockTransport(handler)


def test_compute_sellable_channel_qty_unit() -> None:
    assert compute_sellable_channel_qty(100, safety_stock=10, bundle_multiplier=1) == 90
    assert compute_sellable_channel_qty(90, safety_stock=10, bundle_multiplier=2) == 40
    assert compute_sellable_channel_qty(5, safety_stock=10, bundle_multiplier=1) == 0
    assert compute_sellable_channel_qty(0, safety_stock=0, bundle_multiplier=3) == 0


@pytest.mark.asyncio
async def test_tiktok_order_triggers_8_platform_inventory_sync_and_prevents_oversell(
    session_factories: tuple[Factory, Factory, list[UUID]],
) -> None:
    rt_factory, _, created_tenants = session_factories
    vault = CredentialVaultHelper()
    catalog = CatalogService()
    ledger = InventoryLedgerService()

    real_tid = uuid4()
    created_tenants.append(real_tid)
    async with tenant_session(rt_factory, real_tid) as session:
        uid = uuid4()
        wid = uuid4()
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, '8Chan Tenant', 'CNY')"
            ),
            {"id": real_tid, "slug": f"sync8-{real_tid.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, 'Owner', 'hash')"
            ),
            {"id": uid, "email": f"{uid.hex}-{real_tid.hex}@inv-sync-test.example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, is_active) "
                "VALUES (:tid, :uid, 'owner', true)"
            ),
            {"tid": real_tid, "uid": uid},
        )
        await session.execute(
            text(
                "INSERT INTO warehouses (id, tenant_id, code, name, country_code, city, is_active) "
                "VALUES (:id, :tid, :code, 'Main', 'CN', 'Quanzhou', true)"
            ),
            {"id": wid, "tid": real_tid, "code": f"W8-{real_tid.hex[:6]}"},
        )
        prod = await catalog.create_product(
            session,
            real_tid,
            str(uid),
            ProductCreate(spu_code="SPU-LED-8CH", category="LED"),
        )
        variant = await catalog.create_variant(
            session,
            real_tid,
            str(uid),
            VariantCreate(
                product_id=prod.id,
                sku="LED-MASTER-8CH",
                cost_price_base=Decimal("12.00"),
                retail_price_usd=Decimal("24.90"),
                weight_grams=200,
            ),
            UserRole.OWNER,
        )

        account_ids: dict[str, UUID] = {}
        mapping_ids: dict[str, UUID] = {}
        for channel_type, creds in EIGHT_CHANNEL_CREDENTIALS.items():
            ca_id = uuid4()
            account_ids[channel_type] = ca_id
            await session.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status) "
                    "VALUES (:id, :tid, :ctype, :ext_acc, :disp, 'active')"
                ),
                {
                    "id": ca_id,
                    "tid": real_tid,
                    "ctype": channel_type,
                    "ext_acc": f"acc-{channel_type}-{ca_id.hex[:6]}",
                    "disp": f"Store {channel_type}",
                },
            )
            await vault.save_secret_json(
                session,
                real_tid,
                ca_id,
                CREDENTIAL_TYPES[channel_type],
                creds,
            )
            is_bundle = channel_type == "amazon_sp"
            m = await catalog.create_sku_mapping(
                session,
                real_tid,
                str(uid),
                SkuMappingCreate(
                    variant_id=variant.id,
                    channel_account_id=ca_id,
                    external_product_id=f"EXT-PROD-{channel_type}",
                    external_sku=(
                        "SHOP-SYNC-SKU"
                        if channel_type == "shopify"
                        else f"{channel_type.upper()}-SYNC-SKU"
                    ),
                    external_variant_id=(
                        f"EXT-VAR-{channel_type}" if channel_type != "shopify" else None
                    ),
                    external_inventory_item_id=(
                        "SL-INV-ITEM-1" if channel_type == "shopline" else None
                    ),
                    bundle_multiplier=2 if is_bundle else 1,
                    safety_stock=10 if channel_type == "shopify" else 0,
                    sync_inventory_enabled=True,
                ),
            )
            mapping_ids[channel_type] = m.id

        # 1. Inbound 100 units into WMS -> queues 8 pending jobs
        await ledger.inbound_stock(
            session,
            real_tid,
            wid,
            variant.id,
            qty=100,
            reference_id=uuid4(),
            idempotency_key=f"inb-{uuid4().hex}",
        )

        # 2. Simulate TikTok Shop order for 10 units -> reserves 10 -> remaining available = 90
        await EcommerceOrderSyncService().ingest_normalized_order(
            session,
            tenant_id=real_tid,
            channel_account_id=account_ids["tiktok_shop"],
            default_warehouse_id=wid,
            order_dto=NormalizedOrder(
                external_order_id="TT-ORD-9901",
                external_order_number="#TT-9901",
                order_mode=OrderMode.B2C_RETAIL,
                currency="USD",
                subtotal_amount=Decimal("249.00"),
                total_amount=Decimal("249.00"),
                fulfillment_status=FulfillmentStatus.CONFIRMED_RESERVED,
                payment_status=PaymentStatus.PAID,
                customer=NormalizedCustomer(
                    email="ttbuyer@example.com",
                    external_customer_id="tt-cust-1",
                    external_identity_type=IdentityType.TIKTOK_BUYER_ID,
                    display_name="TT Buyer",
                    country_code="US",
                ),
                line_items=[
                    NormalizedLineItem(
                        external_sku="TIKTOK_SHOP-SYNC-SKU",
                        title="LED Sync",
                        quantity=10,
                        line_total=Decimal("249.00"),
                    )
                ],
                updated_at=datetime.now(UTC),
            ),
        )

        # Confirm burst coalescing kept exactly 8 pending jobs (not 16)
        pending_count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM inventory_sync_jobs "
                    "WHERE tenant_id = :tid AND status = 'pending'"
                ),
                {"tid": real_tid},
            )
        ).scalar_one()
        assert int(pending_count) == 8

        # 3. Dispatch all 8 channels over MockTransport
        recorded: dict[str, list[dict[str, Any]]] = {}
        dispatcher = InventorySyncDispatcher(
            transport=_make_multiplatform_mock_transport(recorded)
        )
        summary = await dispatcher.dispatch_pending_jobs(session, real_tid, limit=50)
        assert summary.dispatched == 8
        assert summary.succeeded == 8
        assert summary.failed == 0
        assert summary.dead_letter == 0

        # Shopify had safety_stock=10 -> pushed 90 - 10 = 80
        assert recorded["shopify"] == [
            {"qty": 80, "item_id": "gid://shopify/InventoryItem/7001"}
        ]
        # Amazon had bundle_multiplier=2 -> pushed 90 // 2 = 45
        assert recorded["amazon_sp"][0]["qty"] == 45
        # All other 6 channels received 90
        for ch in ("tiktok_shop", "ebay", "alibaba_email", "woocommerce", "shopline", "shoplazza"):
            assert recorded[ch][0]["qty"] == 90, f"Channel {ch} qty mismatch: {recorded[ch]}"

        # Verify Shopify external_inventory_item_id was backfilled on platform_sku_mappings
        shop_map_row = (
            await session.execute(
                text(
                    "SELECT external_inventory_item_id, last_inventory_synced_qty, "
                    "last_sync_status "
                    "FROM platform_sku_mappings WHERE tenant_id = :tid AND id = :mid"
                ),
                {"tid": real_tid, "mid": mapping_ids["shopify"]},
            )
        ).one()
        assert shop_map_row.external_inventory_item_id == "gid://shopify/InventoryItem/7001"
        assert int(shop_map_row.last_inventory_synced_qty) == 80
        assert shop_map_row.last_sync_status == "succeeded"


@pytest.mark.asyncio
async def test_inventory_sync_backoff_dead_letter_retry_and_tenant_rls(
    session_factories: tuple[Factory, Factory, list[UUID]],
) -> None:
    rt_factory, mig_factory, created_tenants = session_factories
    vault = CredentialVaultHelper()
    catalog = CatalogService()
    ledger = InventoryLedgerService()

    tid_a = uuid4()
    tid_b = uuid4()
    created_tenants.extend([tid_a, tid_b])

    async with tenant_session(rt_factory, tid_a) as session_a:
        data_a = await _seed_tenant_with_variant(
            session_a, tid=tid_a, slug_prefix="rls-a", sku="SKU-WOO-RETRY"
        )
        ca_id = uuid4()
        await session_a.execute(
            text(
                "INSERT INTO channel_accounts "
                "(id, tenant_id, channel_type, external_account_id, display_name, status) "
                "VALUES (:id, :tid, 'woocommerce', :ext, 'Woo Store', 'active')"
            ),
            {"id": ca_id, "tid": tid_a, "ext": f"woo-{ca_id.hex[:6]}"},
        )
        await vault.save_secret_json(
            session_a,
            tid_a,
            ca_id,
            CREDENTIAL_TYPES["woocommerce"],
            EIGHT_CHANNEL_CREDENTIALS["woocommerce"],
        )
        await catalog.create_sku_mapping(
            session_a,
            tid_a,
            str(data_a["user_id"]),
            SkuMappingCreate(
                variant_id=data_a["variant"].id,
                channel_account_id=ca_id,
                external_product_id="501",
                external_sku="WOO-RETRY-1",
                external_variant_id="501",
                sync_inventory_enabled=True,
            ),
        )
        await ledger.inbound_stock(
            session_a,
            tid_a,
            data_a["warehouse_id"],
            data_a["variant"].id,
            qty=40,
            reference_id=uuid4(),
            idempotency_key=f"inb-{uuid4().hex}",
        )

        recorded_fail: dict[str, list[dict[str, Any]]] = {}
        failing_dispatcher = InventorySyncDispatcher(
            transport=_make_multiplatform_mock_transport(recorded_fail, fail_woo_status=502)
        )
        # Attempt 1 -> failed with 30s backoff
        s1 = await failing_dispatcher.dispatch_pending_jobs(session_a, tid_a)
        assert s1.failed == 1
        job_id = s1.jobs[0].id
        assert s1.jobs[0].attempts == 1
        assert s1.jobs[0].error_code == "HTTP_502"

        # Fast-forward next_retry_at twice to hit attempt 2 (failed) and attempt 3 (dead_letter)
        await session_a.execute(
            text(
                "UPDATE inventory_sync_jobs SET next_retry_at = now() - interval '1 second' "
                "WHERE tenant_id = :tid AND id = :jid"
            ),
            {"tid": tid_a, "jid": job_id},
        )
        s2 = await failing_dispatcher.dispatch_pending_jobs(session_a, tid_a)
        assert s2.failed == 1
        assert s2.jobs[0].attempts == 2

        await session_a.execute(
            text(
                "UPDATE inventory_sync_jobs SET next_retry_at = now() - interval '1 second' "
                "WHERE tenant_id = :tid AND id = :jid"
            ),
            {"tid": tid_a, "jid": job_id},
        )
        s3 = await failing_dispatcher.dispatch_pending_jobs(session_a, tid_a)
        assert s3.dead_letter == 1
        assert s3.jobs[0].status == "dead_letter"
        assert s3.jobs[0].attempts == 3

    async with tenant_session(rt_factory, tid_b) as session_b:
        data_b = await _seed_tenant_with_variant(
            session_b, tid=tid_b, slug_prefix="rls-b", sku="SKU-B-ONLY"
        )
    uid_b = data_b["user_id"]

    # Mount HTTP API for Tenant A and Tenant B
    recorded_ok: dict[str, list[dict[str, Any]]] = {}
    ok_dispatcher = InventorySyncDispatcher(
        transport=_make_multiplatform_mock_transport(recorded_ok)
    )
    principal_holder = [
        CurrentPrincipal(
            user_id=data_a["user_id"],
            tenant_id=tid_a,
            role=UserRole.OWNER,
            email="owner-a@example.com",
        )
    ]

    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = rt_factory
    app.state.inventory_sync_dispatcher = ok_dispatcher
    app.include_router(inventory_sync_router)
    app.dependency_overrides[get_current_principal] = lambda: principal_holder[0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Tenant A sees the dead-letter job and retries it -> succeeds!
        logs_res = await client.get("/api/v1/orders/inventory/sync-logs")
        assert logs_res.status_code == 200
        assert logs_res.json()["total"] >= 1

        retry_res = await client.post(f"/api/v1/orders/inventory/sync-jobs/{job_id}/retry")
        assert retry_res.status_code == 200, retry_res.text
        assert retry_res.json()["status"] == "succeeded"
        assert retry_res.json()["synced_qty"] == 40
        assert recorded_ok["woocommerce"][-1]["qty"] == 40

        # Trigger manual sync-now across Tenant A mappings
        sync_now_res = await client.post("/api/v1/orders/inventory/sync-now", json={})
        assert sync_now_res.status_code == 200
        assert sync_now_res.json()["succeeded"] == 1

        # Cross-tenant RLS isolation: Tenant B cannot see or retry Tenant A's sync jobs
        principal_holder[0] = CurrentPrincipal(
            user_id=uid_b,
            tenant_id=tid_b,
            role=UserRole.OWNER,
            email="owner-b@example.com",
        )
        rls_logs = await client.get("/api/v1/orders/inventory/sync-logs")
        assert rls_logs.status_code == 200
        assert rls_logs.json()["total"] == 0

        rls_retry = await client.post(f"/api/v1/orders/inventory/sync-jobs/{job_id}/retry")
        assert rls_retry.status_code == 404

    # Verify drain_inventory_sync_all_tenants + reconcile_inventory_sync_cron work against PG17
    async with tenant_session(rt_factory, tid_a) as session_a:
        await session_a.execute(
            text(
                "UPDATE platform_sku_mappings SET last_inventory_synced_qty = 10 "
                "WHERE tenant_id = :tid"
            ),
            {"tid": tid_a},
        )
    cron_res = await reconcile_inventory_sync_cron(
        broker_session_factory=mig_factory,
        runtime_session_factory=rt_factory,
        dispatcher=ok_dispatcher,
    )
    assert cron_res["enqueued"] >= 1
    assert cron_res["succeeded"] >= 1

    drain_res = await drain_inventory_sync_all_tenants(
        mig_factory, rt_factory, dispatcher=ok_dispatcher
    )
    assert drain_res["dispatched"] == 0
