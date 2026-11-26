"""Phase 1 PG17 RLS + MinIO + MockTransport integration tests:
- Unified CatalogMediaService (PNG upload magic-byte check, SSRF guard, URL mirror)
- Multi-variant 1688 sourcing draft -> import N variants -> Shopify stagedUploads + productSet
- Newly wired platforms: TikTok Shop, WooCommerce (variable + variations), SHOPLINE, Shoplazza
- Master catalog direct multi-variant publish (/api/v1/catalog/products/{id}/publish)
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
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
import erp_core.tenancy.models  # noqa: F401
from erp_core.catalog.media_service import CatalogMediaService
from erp_core.catalog.router import router as catalog_router
from erp_core.catalog.schemas import ProductCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.catalog.sourcing_service import (
    SourcingCollectRequest,
    SourcingOptionSpec,
    SourcingPublishRequest,
    SourcingService,
    SourcingVariantSpec,
    build_external_sku,
)
from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.channels.ecommerce.listing_dispatcher import MarketplaceListingDispatcher
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError, register_exception_handlers
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]

VALID_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def _purge_tenant(tenant_id: UUID) -> None:
    admin = create_async_engine(
        os.environ.get(
            "DATABASE_ADMIN_URL",
            "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
        )
    )
    try:
        async with admin.begin() as conn:
            for stmt in (
                "DELETE FROM product_media_assets WHERE tenant_id = :tid",
                "DELETE FROM platform_sku_mappings WHERE tenant_id = :tid",
                "DELETE FROM product_variants WHERE tenant_id = :tid",
                "DELETE FROM products WHERE tenant_id = :tid",
                "DELETE FROM sourcing_drafts WHERE tenant_id = :tid",
                "DELETE FROM channel_credentials WHERE tenant_id = :tid",
                "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                "DELETE FROM audit_events WHERE tenant_id = :tid",
                "DELETE FROM tenant_memberships WHERE tenant_id = :tid",
            ):
                await conn.execute(text(stmt), {"tid": tenant_id})
            user_ids = [
                row.id
                for row in (
                    await conn.execute(
                        text("SELECT id FROM users WHERE email LIKE :pat"),
                        {"pat": f"%{tenant_id.hex}@phase1.example.com"},
                    )
                ).all()
            ]
            for uid in user_ids:
                await conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
    finally:
        await admin.dispose()


@pytest.fixture
async def workspace(runtime_session_factory: Factory) -> AsyncIterator[dict[str, Any]]:
    tenant = uuid4()
    owner = uuid4()
    async with tenant_session(runtime_session_factory, tenant) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, 'Phase1 Test Factory', 'CNY')"
            ),
            {"id": tenant, "slug": f"p1-{tenant.hex[:12]}"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, 'Owner', 'hash')"
            ),
            {"id": owner, "email": f"{owner.hex}@phase1.example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                "VALUES (:tid, :uid, 'owner')"
            ),
            {"tid": tenant, "uid": owner},
        )
    try:
        yield {
            "tenant": tenant,
            "owner_id": owner,
            "principal": CurrentPrincipal(
                user_id=owner,
                tenant_id=tenant,
                role=UserRole.OWNER,
                email=f"{owner.hex}@phase1.example.com",
            ),
        }
    finally:
        await _purge_tenant(tenant)


async def _seed_channel(
    session: AsyncSession,
    tenant: UUID,
    channel_type: str,
    cred_type: str,
    credentials: dict[str, Any],
    ext_id: str,
) -> UUID:
    account_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO channel_accounts (id, tenant_id, channel_type, external_account_id, "
            "display_name, status, config) "
            "VALUES (:id, :tid, :ctype, :ext, :name, 'active', '{}'::jsonb)"
        ),
        {
            "id": account_id,
            "tid": tenant,
            "ctype": channel_type,
            "ext": ext_id,
            "name": f"Test {channel_type}",
        },
    )
    await CredentialVaultHelper().save_secret_json(
        session, tenant, account_id, cred_type, credentials
    )
    return account_id


async def test_media_upload_magic_bytes_ssrf_and_url_mirror(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    media = CatalogMediaService(ObjectStorageService(get_settings()))

    async with tenant_session(runtime_session_factory, tenant) as session:
        with pytest.raises(AppError) as bad_magic:
            await media.upload_image_bytes(
                session, tenant, filename="fake.png", data=b"MZ\x90\x00NOT_A_PNG_EXECUTABLE_PAYLOAD"
            )
        assert bad_magic.value.status_code == 422
        assert bad_magic.value.code == "ERR_MEDIA_TYPE_UNSUPPORTED"

        for blocked_url in (
            "https://127.0.0.1/internal.png",
            "https://169.254.169.254/latest/meta-data.png",
            "http://metadata.google.internal/secret.png",
        ):
            with pytest.raises(AppError) as ssrf_err:
                await media.ingest_external_urls(
                    session,
                    tenant,
                    urls=[blocked_url],
                    dns_resolver=lambda _host: _return_async(["127.0.0.1"]),
                )
            assert ssrf_err.value.code == "ERR_MEDIA_URL_UNSAFE"

        uploaded = await media.upload_image_bytes(
            session,
            tenant,
            filename="cob-strip.png",
            data=VALID_PNG,
            alt_text="24V COB Strip",
        )
        assert uploaded.mime_type == "image/png"
        assert uploaded.byte_size == len(VALID_PNG)
        raw, mime, _fname = await media.read_asset_content(session, tenant, uploaded.id)
        assert mime == "image/png"
        assert raw == VALID_PNG

        def img_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=VALID_PNG)

        mirrored = await media.ingest_external_urls(
            session,
            tenant,
            urls=["https://cdn.example.com/led/strip.png"],
            transport=httpx.MockTransport(img_handler),
            dns_resolver=lambda _host: _return_async(["93.184.216.34"]),
        )
        assert len(mirrored) == 1
        assert mirrored[0].source_url == "https://cdn.example.com/led/strip.png"


async def _return_async(val: list[str]) -> tuple[str, ...]:
    return tuple(val)


async def test_multivariant_sourcing_to_shopify_staged_upload_and_variant_mappings(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])
    shop_domain = "qzled-multi.myshopify.com"
    staged_hits: list[httpx.Request] = []
    product_set_vars: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "uploads.shopify.test":
            staged_hits.append(request)
            return httpx.Response(204)
        body = json.loads(request.content)
        query = body["query"]
        if "stagedUploadsCreate" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "stagedUploadsCreate": {
                            "stagedTargets": [
                                {
                                    "url": "https://uploads.shopify.test/upload",
                                    "resourceUrl": "https://cdn.shopify.com/staged/cob-strip.png",
                                    "parameters": [{"name": "key", "value": "tmp/cob.png"}],
                                }
                            ],
                            "userErrors": [],
                        }
                    }
                },
            )
        if "publications(" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "publications": {
                            "nodes": [
                                {"id": "gid://shopify/Publication/1", "name": "Online Store"}
                            ]
                        }
                    }
                },
            )
        if "publishablePublish(" in query:
            return httpx.Response(
                200, json={"data": {"publishablePublish": {"userErrors": []}}}
            )
        ps_input = body["variables"]["productSet"]
        product_set_vars.append(ps_input)
        nodes = [
            {
                "id": f"gid://shopify/ProductVariant/{100 + idx}",
                "sku": var["sku"],
                "price": var["price"],
                "inventoryItem": {"id": f"gid://shopify/InventoryItem/{200 + idx}"},
            }
            for idx, var in enumerate(ps_input["variants"], start=1)
        ]
        return httpx.Response(
            200,
            json={
                "data": {
                    "productSet": {
                        "product": {
                            "id": "gid://shopify/Product/88001",
                            "title": ps_input["title"],
                            "handle": "cob-24v-multi",
                            "status": "ACTIVE",
                            "variants": {"nodes": nodes},
                        },
                        "userErrors": [],
                    }
                }
            },
        )

    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_channel(
            session,
            tenant,
            "shopify",
            "shopify_custom_app",
            {
                "shop_domain": shop_domain,
                "auth_mode": "oauth",
                "access_token": "shpat_multi_token",
            },
            shop_domain,
        )
        draft = await SourcingService().collect(
            session,
            tenant,
            actor,
            SourcingCollectRequest(
                source_platform="1688",
                source_url="https://detail.1688.com/offer/777000111222.html",
                raw_title="24V COB 双色温灯带",
                target_margin_pct=Decimal("40.00"),
                fx_rate_usd_cny=Decimal("7.200000"),
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
        assert len(draft.variants_spec) == 2
        assert draft.variants_spec[0].retail_price_usd == Decimal("8.33")
        assert draft.variants_spec[1].retail_price_usd == Decimal("10.00")

        media = CatalogMediaService(ObjectStorageService(get_settings()))
        await media.upload_image_bytes(
            session,
            tenant,
            filename="main-cob.png",
            data=VALID_PNG,
            sourcing_draft_id=draft.id,
        )

        results = await SourcingService().publish(
            session,
            tenant,
            actor,
            draft.id,
            SourcingPublishRequest(channel_account_ids=[account_id], push_to_marketplace=True),
            listing_publisher=MarketplaceListingDispatcher(
                transport=httpx.MockTransport(handler)
            ),
        )
        assert results[0].listing_state == "live_published"
        assert results[0].external_product_id == "gid://shopify/Product/88001"

        rows = (
            await session.execute(
                text(
                    "SELECT external_sku, external_variant_id, external_inventory_item_id "
                    "FROM platform_sku_mappings WHERE tenant_id = :tid "
                    "AND channel_account_id = :caid ORDER BY external_sku"
                ),
                {"tid": tenant, "caid": account_id},
            )
        ).all()
        assert len(rows) == 2
        expected_skus = sorted(
            [
                build_external_sku("shopify", f"{draft.suggested_sku}-3000K"),
                build_external_sku("shopify", f"{draft.suggested_sku}-6000K"),
            ]
        )
        assert [r.external_sku for r in rows] == expected_skus
        assert all(
            r.external_variant_id
            and r.external_variant_id.startswith("gid://shopify/ProductVariant/")
            for r in rows
        )
        assert all(
            r.external_inventory_item_id
            and r.external_inventory_item_id.startswith("gid://shopify/InventoryItem/")
            for r in rows
        )

    assert len(staged_hits) == 1
    assert len(product_set_vars) == 1
    assert product_set_vars[0]["files"][0]["originalSource"] == "https://cdn.shopify.com/staged/cob-strip.png"
    assert len(product_set_vars[0]["variants"]) == 2
    assert all(
        variant.get("inventoryItem") == {"tracked": True}
        for variant in product_set_vars[0]["variants"]
    )


async def test_tiktok_woocommerce_shopline_shoplazza_live_multivariant_publish(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "open-api.tiktokglobalshop.com":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "product_id": "tt-prod-9001",
                        "skus": [
                            {"id": f"tt-sku-{i}", "seller_sku": s["seller_sku"]}
                            for i, s in enumerate(body["skus"], start=1)
                        ],
                    },
                },
            )
        if host == "woo.qzled.example":
            body = json.loads(request.content)
            if path == "/wp-json/wc/v3/products":
                return httpx.Response(201, json={"id": 5001, "status": "publish"})
            if path == "/wp-json/wc/v3/products/5001/variations/batch":
                return httpx.Response(
                    200,
                    json={
                        "create": [
                            {"id": 6000 + i, "sku": v["sku"]}
                            for i, v in enumerate(body["create"], start=1)
                        ]
                    },
                )
        if host == "qz.myshopline.com":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "product": {
                        "id": "sl-prod-7001",
                        "variants": [
                            {
                                "id": f"sl-var-{i}",
                                "sku": v["sku"],
                                "inventory_item_id": f"sl-inv-{i}",
                            }
                            for i, v in enumerate(body["product"]["variants"], start=1)
                        ],
                    }
                },
            )
        if host == "qz.myshoplazza.com":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "product": {
                        "id": "sz-prod-8001",
                        "variants": [
                            {"id": f"sz-var-{i}", "sku": v["sku"]}
                            for i, v in enumerate(body["product"]["variants"], start=1)
                        ],
                    }
                },
            )
        return httpx.Response(404, json={"unexpected": str(request.url)})

    async with tenant_session(runtime_session_factory, tenant) as session:
        accounts = [
            await _seed_channel(
                session,
                tenant,
                "tiktok_shop",
                "tiktok_shop_app",
                {
                    "app_key": "tt-app",
                    "app_secret": "tt-secret-key",
                    "access_token": "tt-access",
                    "shop_cipher": "CIPHER1",
                    "warehouse_id": "wh-tt-1",
                },
                "tt-shop-1",
            ),
            await _seed_channel(
                session,
                tenant,
                "woocommerce",
                "woocommerce_keys",
                {
                    "store_url": "https://woo.qzled.example",
                    "consumer_key": "ck_1",
                    "consumer_secret": "cs_1",
                },
                "woo.qzled.example",
            ),
            await _seed_channel(
                session,
                tenant,
                "shopline",
                "shopline_app",
                {"store_domain": "qz.myshopline.com", "access_token": "sl-tok"},
                "qz.myshopline.com",
            ),
            await _seed_channel(
                session,
                tenant,
                "shoplazza",
                "shoplazza_app",
                {"store_domain": "qz.myshoplazza.com", "access_token": "sz-tok"},
                "qz.myshoplazza.com",
            ),
        ]
        draft = await SourcingService().collect(
            session,
            tenant,
            actor,
            SourcingCollectRequest(
                source_platform="1688",
                source_url="https://detail.1688.com/offer/777000999888.html",
                raw_title="RGBIC 幻彩灯带",
                images=["https://cdn.example.com/rgb1.jpg"],
                variants_spec=[
                    SourcingVariantSpec(
                        sku_suffix="5M",
                        option_values={"Length": "5m"},
                        cost_price_cny=Decimal("28.80"),
                    ),
                    SourcingVariantSpec(
                        sku_suffix="10M",
                        option_values={"Length": "10m"},
                        cost_price_cny=Decimal("50.40"),
                    ),
                ],
            ),
        )
        results = await SourcingService().publish(
            session,
            tenant,
            actor,
            draft.id,
            SourcingPublishRequest(channel_account_ids=accounts, push_to_marketplace=True),
            listing_publisher=MarketplaceListingDispatcher(
                transport=httpx.MockTransport(handler)
            ),
        )
        by_type = {res.channel_type: res.external_product_id for res in results}
        assert by_type == {
            "tiktok_shop": "tt-prod-9001",
            "woocommerce": "5001",
            "shopline": "sl-prod-7001",
            "shoplazza": "sz-prod-8001",
        }

        mapped_cnt = await session.scalar(
            text("SELECT count(*) FROM platform_sku_mappings WHERE tenant_id = :tid"),
            {"tid": tenant},
        )
        # 4 platforms * 2 variants = 8 variant-level SKU mappings persisted
        assert int(mapped_cnt or 0) == 8


async def test_master_catalog_product_publish_endpoint(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query = str(body.get("query") or "")
        if "publications(" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "publications": {
                            "nodes": [
                                {"id": "gid://shopify/Publication/1", "name": "Online Store"}
                            ]
                        }
                    }
                },
            )
        if "publishablePublish(" in query:
            return httpx.Response(
                200, json={"data": {"publishablePublish": {"userErrors": []}}}
            )
        ps_input = body["variables"]["productSet"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "productSet": {
                        "product": {
                            "id": "gid://shopify/Product/99002",
                            "title": ps_input["title"],
                            "handle": "factory-master-cob",
                            "status": "ACTIVE",
                            "variants": {
                                "nodes": [
                                    {
                                        "id": "gid://shopify/ProductVariant/99102",
                                        "sku": ps_input["variants"][0]["sku"],
                                        "price": ps_input["variants"][0]["price"],
                                        "inventoryItem": {"id": "gid://shopify/InventoryItem/99202"},
                                    }
                                ]
                            },
                        },
                        "userErrors": [],
                    }
                }
            },
        )

    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_channel(
            session,
            tenant,
            "shopify",
            "shopify_custom_app",
            {
                "shop_domain": "qz-master.myshopify.com",
                "auth_mode": "oauth",
                "access_token": "shpat_master",
            },
            "qz-master.myshopify.com",
        )
        cat = CatalogService()
        product = await cat.create_product(
            session,
            tenant,
            actor,
            ProductCreate(
                spu_code="MASTER-COB-SPU",
                category="LED Strip",
                i18n_specs={"en": {"title": "Master Factory COB Strip 24V"}},
            ),
        )
        await cat.create_variant(
            session,
            tenant,
            actor,
            VariantCreate(
                product_id=product.id,
                sku="MASTER-COB-SPU-24V",
                cost_price_base=Decimal("20.00"),
                retail_price_usd=Decimal("12.50"),
                weight_grams=300,
            ),
            role=UserRole.OWNER,
        )

    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = runtime_session_factory
    app.state.marketplace_listing_publisher = MarketplaceListingDispatcher(
        transport=httpx.MockTransport(handler)
    )
    app.dependency_overrides[get_current_principal] = lambda: workspace["principal"]
    app.include_router(catalog_router, prefix="/api/v1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/catalog/products/{product.id}/publish",
            json={
                "channel_account_ids": [str(account_id)],
                "push_to_marketplace": True,
                "initial_stock_quantity": 50,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["listing_state"] == "live_published"
    assert body[0]["external_product_id"] == "gid://shopify/Product/99002"


@pytest.mark.asyncio
async def test_sourcing_import_converts_cny_cost_and_shopify_tracks_single_variant(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    """CNY sourcing costs convert to USD base currency on import, and Shopify defaults tracked."""
    from erp_core.catalog.sourcing_service import _resolve_variant_base_cost
    from erp_core.channels.ecommerce.shopify import ShopifyGraphqlAdapter

    # 1. Pure currency resolution check (USD vs CNY tenant)
    assert _resolve_variant_base_cost(Decimal("68.00"), Decimal("7.200000"), "USD") == (
        Decimal("9.4444"),
        "USD",
    )
    assert _resolve_variant_base_cost(Decimal("68.00"), Decimal("7.200000"), "CNY") == (
        Decimal("68.0000"),
        "CNY",
    )

    # 2. Single-variant Shopify productSet sets inventoryItem.tracked = True
    options, single_vars = ShopifyGraphqlAdapter._build_product_options_and_variants(
        clean_sku="SKU-SINGLE-TRACKED",
        normalized_price=Decimal("16.90"),
        options=None,
        variants=None,
    )
    assert options == [{"name": "Title", "values": [{"name": "Default Title"}]}]
    assert single_vars[0]["inventoryItem"] == {"tracked": True}

    # 3. PG17 import_to_catalog keeps CNY for CNY tenants and converts CNY->USD for USD tenants
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])
    async with tenant_session(runtime_session_factory, tenant) as session:
        service = SourcingService()
        cny_single_draft = await service.collect(
            session,
            tenant,
            actor,
            SourcingCollectRequest(
                source_platform="1688",
                source_url="https://detail.1688.com/offer/777889001.html",
                raw_title="CNY Tenant Passthrough Test",
                raw_price_cny=Decimal("36.00"),
                fx_rate_usd_cny=Decimal("7.200000"),
                target_margin_pct=Decimal("40.00"),
            ),
        )
        await service.import_to_catalog(session, tenant, actor, cny_single_draft.id)
        cny_row = (
            await session.execute(
                text(
                    "SELECT cost_price_base, base_currency, attributes "
                    "FROM product_variants WHERE tenant_id = :tid AND sku = :sku"
                ),
                {"tid": tenant, "sku": cny_single_draft.suggested_sku},
            )
        ).mappings().one()
        assert cny_row["cost_price_base"] == Decimal("36.0000")
        assert cny_row["base_currency"] == "CNY"
        assert cny_row["attributes"]["raw_cost_cny"] == "36.00"

        await session.execute(
            text("UPDATE tenants SET base_currency = 'USD' WHERE id = :tid"),
            {"tid": tenant},
        )
        multi_draft = await service.collect(
            session,
            tenant,
            actor,
            SourcingCollectRequest(
                source_platform="1688",
                source_url="https://detail.1688.com/offer/777888999.html",
                raw_title="CNY Cost Convert Test LED Lamp",
                raw_price_cny=Decimal("72.00"),
                fx_rate_usd_cny=Decimal("7.200000"),
                target_margin_pct=Decimal("40.00"),
                variants_spec=[
                    SourcingVariantSpec(
                        sku_suffix="C1",
                        option_values={"Power": "50W"},
                        cost_price_cny=Decimal("72.00"),
                        retail_price_usd=Decimal("18.00"),
                    )
                ],
            ),
        )
        await service.import_to_catalog(session, tenant, actor, multi_draft.id)
        multi_row = (
            await session.execute(
                text(
                    "SELECT cost_price_base, base_currency, attributes "
                    "FROM product_variants WHERE tenant_id = :tid AND sku = :sku"
                ),
                {"tid": tenant, "sku": f"{multi_draft.suggested_sku}-C1"},
            )
        ).mappings().one()
        assert multi_row["cost_price_base"] == Decimal("10.0000")
        assert multi_row["base_currency"] == "USD"
        assert multi_row["attributes"]["raw_cost_cny"] == "72.00"

        single_draft = await service.collect(
            session,
            tenant,
            actor,
            SourcingCollectRequest(
                source_platform="1688",
                source_url="https://detail.1688.com/offer/777889000.html",
                raw_title="Single Variant CNY Convert Test",
                raw_price_cny=Decimal("36.00"),
                fx_rate_usd_cny=Decimal("7.200000"),
                target_margin_pct=Decimal("40.00"),
            ),
        )
        await service.import_to_catalog(session, tenant, actor, single_draft.id)
        single_row = (
            await session.execute(
                text(
                    "SELECT cost_price_base, base_currency, attributes "
                    "FROM product_variants WHERE tenant_id = :tid AND sku = :sku"
                ),
                {"tid": tenant, "sku": single_draft.suggested_sku},
            )
        ).mappings().one()
        assert single_row["cost_price_base"] == Decimal("5.0000")
        assert single_row["base_currency"] == "USD"
        assert single_row["attributes"]["raw_cost_cny"] == "36.00"
