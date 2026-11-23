"""Live marketplace integration coverage: SP-API / eBay Sell / Alibaba.com ICBU.

Everything here runs against the **real PostgreSQL 17** database with tenant RLS in force
and real AES-256-GCM envelope encryption for the channel credentials. Outbound HTTP is
always served by ``httpx.MockTransport``, so no test can reach a marketplace.

Covered invariants:

* ``SourcingService.publish(..., push_to_marketplace=True)`` pushes the listing through the
  injected ``MarketplaceListingPublisher`` and persists the *real* remote product id in
  ``platform_sku_mappings``, the publish history and the audit trail; the draft then reads
  ``listing_state == "live_published"``.
* A rejected/unsupported/missing-credential push fails closed: nothing is ever recorded as
  a live listing (no fabricated ``live_published``).
* ``POST /api/v1/channels/marketplace/{id}/sync-orders`` ingests normalized orders into
  ``orders`` + ``order_items`` for all three channels.
* ``ECOMMERCE_CHANNELS`` / ``_instantiate_adapter`` recognize and build the marketplaces.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Configure every ORM module so the string FKs used below resolve in this isolated
# test app, exactly like the api_main composition root.
import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.catalog.schemas import ProductCreate, SkuMappingCreate, VariantCreate
from erp_core.catalog.service import CatalogService
from erp_core.catalog.sourcing_service import (
    PENDING_LISTING_PREFIX,
    SourcingCollectRequest,
    SourcingPublishRequest,
    SourcingService,
    build_external_sku,
)
from erp_core.channels.ecommerce import marketplace_connect_router as marketplace
from erp_core.channels.ecommerce.alibaba_intl import AlibabaIntlAdapter
from erp_core.channels.ecommerce.amazon_sp import AmazonSpApiAdapter
from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.channels.ecommerce.ebay import EbayConnectorAdapter
from erp_core.channels.ecommerce.listing_dispatcher import (
    CREDENTIAL_TYPES as DISPATCHER_CREDENTIAL_TYPES,
)
from erp_core.channels.ecommerce.listing_dispatcher import (
    MarketplaceListingDispatcher,
)
from erp_core.orders.reconciliation_worker import (
    CREDENTIAL_TYPES as RECONCILIATION_CREDENTIAL_TYPES,
)
from erp_core.orders.reconciliation_worker import (
    ECOMMERCE_CHANNELS,
    _instantiate_adapter,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus, UserRole
from erp_core.shared.errors import AppError, register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]

AMAZON_CHANNEL = "amazon_sp"
EBAY_CHANNEL = "ebay"
ALIBABA_CHANNEL = "alibaba_email"
MARKETPLACE_CHANNELS = (AMAZON_CHANNEL, EBAY_CHANNEL, ALIBABA_CHANNEL)

AMAZON_SELLER_ID = "A2EUQ1WTGCTBG2"
AMAZON_CLIENT_ID = "amzn1.application-oa2-client.live"
AMAZON_CLIENT_SECRET = "amzn1.oa2-cs.v1.live-super-secret"
AMAZON_REFRESH_TOKEN = "Atzr|live-amazon-refresh-token"
AMAZON_ACCESS_TOKEN = "Atza|live-amazon-access-token"
AMAZON_ASIN = "B0LIVEASIN1"

EBAY_SELLER = "qz_led_official"
EBAY_APP_ID = "QZLed-EBAY-App"
EBAY_CERT_ID = "ebay-live-cert-secret"
EBAY_REFRESH_TOKEN = "v^1.1#live-ebay-refresh-token"
EBAY_ACCESS_TOKEN = "v^1.1#live-ebay-access-token"
EBAY_OFFER_ID = "OFFER-LIVE-778001"
EBAY_LISTING_ID = "110556789012"

ALIBABA_STORE_ID = "ali-store-7788"
ALIBABA_APP_KEY = "ali-live-app-key"
ALIBABA_APP_SECRET = "ali-live-app-secret-001"
ALIBABA_ACCESS_TOKEN = "ali-live-session-token"
ALIBABA_PRODUCT_ID = "660100000001"

SECRETS = (
    AMAZON_CLIENT_SECRET,
    AMAZON_REFRESH_TOKEN,
    EBAY_CERT_ID,
    EBAY_REFRESH_TOKEN,
    ALIBABA_APP_SECRET,
    ALIBABA_ACCESS_TOKEN,
)

MARKETPLACE_CREDENTIALS: Mapping[str, dict[str, Any]] = {
    AMAZON_CHANNEL: {
        "seller_id": AMAZON_SELLER_ID,
        "marketplace_id": "ATVPDKIKX0DER",
        "lwa_client_id": AMAZON_CLIENT_ID,
        "lwa_client_secret": AMAZON_CLIENT_SECRET,
        "refresh_token": AMAZON_REFRESH_TOKEN,
    },
    EBAY_CHANNEL: {
        "seller_username": EBAY_SELLER,
        "site_id": "EBAY_US",
        "app_id": EBAY_APP_ID,
        "cert_id": EBAY_CERT_ID,
        "refresh_token": EBAY_REFRESH_TOKEN,
    },
    ALIBABA_CHANNEL: {
        "store_id": ALIBABA_STORE_ID,
        "app_key": ALIBABA_APP_KEY,
        "app_secret": ALIBABA_APP_SECRET,
        "access_token": ALIBABA_ACCESS_TOKEN,
    },
}

EXTERNAL_ACCOUNT_IDS: Mapping[str, str] = {
    AMAZON_CHANNEL: AMAZON_SELLER_ID,
    EBAY_CHANNEL: EBAY_SELLER,
    ALIBABA_CHANNEL: ALIBABA_STORE_ID,
}


# --------------------------------------------------------------------------------------
# Unencrypted, network-free transport fixtures
# --------------------------------------------------------------------------------------


def amazon_publish_handler(calls: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "api.amazon.com":
            return httpx.Response(
                200,
                json={
                    "access_token": AMAZON_ACCESS_TOKEN,
                    "expires_in": 3600,
                    "token_type": "bearer",
                },
            )
        assert request.headers["x-amz-access-token"] == AMAZON_ACCESS_TOKEN
        return httpx.Response(
            200,
            json={"status": "ACCEPTED", "submissionId": "sub-live-1", "asin": AMAZON_ASIN},
        )

    return handler


def ebay_publish_handler(calls: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/identity/v1/oauth2/token":
            return httpx.Response(
                200, json={"access_token": EBAY_ACCESS_TOKEN, "expires_in": 7200}
            )
        assert request.headers["authorization"] == f"Bearer {EBAY_ACCESS_TOKEN}"
        if path.startswith("/sell/inventory/v1/inventory_item/"):
            return httpx.Response(204)
        if path == "/sell/inventory/v1/offer":
            return httpx.Response(201, json={"offerId": EBAY_OFFER_ID})
        if path == f"/sell/inventory/v1/offer/{EBAY_OFFER_ID}/publish":
            return httpx.Response(200, json={"listingId": EBAY_LISTING_ID})
        return httpx.Response(404, json={"errors": [{"message": f"unexpected {path}"}]})

    return handler


def alibaba_publish_handler(
    calls: list[httpx.Request],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "result": {
                    "success": True,
                    "code": "0",
                    "data": {"product_id": ALIBABA_PRODUCT_ID},
                }
            },
        )

    return handler


PUBLISH_HANDLERS: Mapping[
    str, Callable[[list[httpx.Request]], Callable[[httpx.Request], httpx.Response]]
] = {
    AMAZON_CHANNEL: amazon_publish_handler,
    EBAY_CHANNEL: ebay_publish_handler,
    ALIBABA_CHANNEL: alibaba_publish_handler,
}

EXPECTED_EXTERNAL_PRODUCT_ID: Mapping[str, str] = {
    AMAZON_CHANNEL: AMAZON_ASIN,
    EBAY_CHANNEL: EBAY_LISTING_ID,
    ALIBABA_CHANNEL: ALIBABA_PRODUCT_ID,
}

EXPECTED_REMOTE_STATUS: Mapping[str, str] = {
    AMAZON_CHANNEL: "ACCEPTED",
    EBAY_CHANNEL: "PUBLISHED",
    ALIBABA_CHANNEL: "PUBLISHED",
}


# --------------------------------------------------------------------------------------
# Workspace fixture (real PG17 tenant, owner/manager, default warehouse)
# --------------------------------------------------------------------------------------


async def _purge_tenant(tenant_id: UUID) -> None:
    admin = create_async_engine(
        os.environ.get(
            "DATABASE_ADMIN_URL",
            "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
        )
    )
    try:
        async with admin.begin() as conn:
            for statement in (
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
                "DELETE FROM b2b_companies WHERE tenant_id = :tid",
                "DELETE FROM channel_routing_directory WHERE tenant_id = :tid",
                "DELETE FROM channel_credentials WHERE tenant_id = :tid",
                "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                "DELETE FROM sourcing_drafts WHERE tenant_id = :tid",
                "DELETE FROM warehouses WHERE tenant_id = :tid",
                "DELETE FROM audit_events WHERE tenant_id = :tid",
                "DELETE FROM tenant_memberships WHERE tenant_id = :tid",
            ):
                await conn.execute(text(statement), {"tid": tenant_id})
            user_ids = [
                row.id
                for row in (
                    await conn.execute(
                        text(
                            "SELECT id FROM users WHERE email LIKE :pattern"
                        ),
                        {"pattern": f"%{tenant_id.hex}@marketplace-live.example.com"},
                    )
                ).all()
            ]
            for user_id in user_ids:
                await conn.execute(
                    text("DELETE FROM users WHERE id = :uid"), {"uid": user_id}
                )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
    finally:
        await admin.dispose()


@pytest.fixture
async def workspace(runtime_session_factory: Factory) -> AsyncIterator[dict[str, Any]]:
    tenant = uuid4()
    owner, manager = (uuid4() for _ in range(2))
    warehouse_id = uuid4()
    async with tenant_session(runtime_session_factory, tenant) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, :n, 'CNY')"
            ),
            {"id": tenant, "slug": f"mkt-live-{tenant.hex}", "n": "Marketplace Live"},
        )
        for user_id, role in ((owner, UserRole.OWNER), (manager, UserRole.MANAGER)):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, :name, 'test')"
                ),
                {
                    "id": user_id,
                    "email": f"{user_id.hex}@marketplace-live.example.com",
                    "name": role.value,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (:tid, :uid, :role)"
                ),
                {"tid": tenant, "uid": user_id, "role": role.value},
            )
        await session.execute(
            text(
                "INSERT INTO warehouses (id, tenant_id, code, name) "
                "VALUES (:id, :tid, 'MAIN', 'Quanzhou Main')"
            ),
            {"id": warehouse_id, "tid": tenant},
        )
    try:
        yield {
            "tenant": tenant,
            "owner_id": owner,
            "manager_id": manager,
            "warehouse_id": warehouse_id,
            "owner": CurrentPrincipal(
                user_id=owner,
                tenant_id=tenant,
                role=UserRole.OWNER,
                email="owner@marketplace-live.example.com",
            ),
            "manager": CurrentPrincipal(
                user_id=manager,
                tenant_id=tenant,
                role=UserRole.MANAGER,
                email="manager@marketplace-live.example.com",
            ),
        }
    finally:
        await _purge_tenant(tenant)


# --------------------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------------------


async def _seed_marketplace_account(
    session: AsyncSession,
    tenant_id: UUID,
    channel_type: str,
    *,
    store_credentials: bool = True,
) -> UUID:
    """Create one active marketplace channel account + its encrypted credential row."""
    account_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO channel_accounts (id, tenant_id, channel_type, external_account_id, "
            "display_name, status, config) VALUES (:id, :tid, :ctype, :ext, :name, 'active', "
            "'{}'::jsonb)"
        ),
        {
            "id": account_id,
            "tid": tenant_id,
            "ctype": channel_type,
            "ext": EXTERNAL_ACCOUNT_IDS[channel_type],
            "name": f"Live {channel_type}",
        },
    )
    if store_credentials:
        await CredentialVaultHelper().save_secret_json(
            session,
            tenant_id,
            account_id,
            DISPATCHER_CREDENTIAL_TYPES[channel_type],
            dict(MARKETPLACE_CREDENTIALS[channel_type]),
        )
    return account_id


async def _seed_mapped_variant(
    session: AsyncSession,
    tenant_id: UUID,
    account_id: UUID,
    *,
    external_sku: str,
    warehouse_id: UUID,
    on_hand_qty: int = 25,
) -> UUID:
    """Real product/variant + platform SKU mapping + stock, so an order can be ingested."""
    suffix = external_sku.replace("-", "")
    catalog = CatalogService()
    product = await catalog.create_product(
        session,
        tenant_id,
        actor_id="seed",
        payload=ProductCreate(spu_code=f"SPU-{suffix}", category="led"),
    )
    variant = await catalog.create_variant(
        session,
        tenant_id,
        actor_id="seed",
        role=UserRole.ADMIN,
        payload=VariantCreate(
            product_id=product.id,
            sku=f"INT-{suffix}",
            cost_price_base=Decimal("10.0000"),
            retail_price_usd=Decimal("19.99"),
            weight_grams=250,
        ),
    )
    await catalog.create_sku_mapping(
        session,
        tenant_id,
        actor_id="seed",
        payload=SkuMappingCreate(
            variant_id=variant.id,
            channel_account_id=account_id,
            external_product_id=f"remote-{suffix}",
            external_sku=external_sku,
        ),
    )
    await session.execute(
        text(
            "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
            "on_hand_qty, reserved_qty) VALUES (:tid, :wid, :vid, :qty, 0)"
        ),
        {"tid": tenant_id, "wid": warehouse_id, "vid": variant.id, "qty": on_hand_qty},
    )
    return variant.id


async def _collect_draft(
    session: AsyncSession, tenant_id: UUID, actor_id: str
) -> Any:
    """One 1688 sourcing draft priced in Decimal USD, with 5 export bullets."""
    return await SourcingService().collect(
        session,
        tenant_id,
        actor_id,
        SourcingCollectRequest(
            source_platform="1688",
            source_url="https://detail.1688.com/offer/681234567890.html",
            raw_title="LED太阳能投光灯100W户外防水",
            raw_text="供应商报价 ¥45.00，≥100件 45.00元，工厂直发",
            raw_price_cny=Decimal("45.00"),
            target_margin_pct=Decimal("40.00"),
            category="LED Lighting",
            hs_code="9405429000",
            images=[
                "https://cbu01.alicdn.com/img/ibank/flood-100w.jpg",
                "https://cbu01.alicdn.com/img/ibank/flood-100w-2.jpg",
            ],
        ),
    )


async def _mapping_row(
    session: AsyncSession, tenant_id: UUID, account_id: UUID, external_sku: str
) -> Any:
    return (
        await session.execute(
            text(
                "SELECT external_product_id, external_sku, sync_inventory_enabled "
                "FROM platform_sku_mappings WHERE tenant_id = :tid "
                "AND channel_account_id = :caid AND external_sku = :esku"
            ),
            {"tid": tenant_id, "caid": account_id, "esku": external_sku},
        )
    ).one_or_none()


# --------------------------------------------------------------------------------------
# 1. Live push: real remote id persisted for all three marketplaces
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("channel_type", MARKETPLACE_CHANNELS)
async def test_live_push_persists_the_real_remote_product_id(
    channel_type: str,
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(PUBLISH_HANDLERS[channel_type](calls))
    expected_product_id = EXPECTED_EXTERNAL_PRODUCT_ID[channel_type]

    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_marketplace_account(session, tenant, channel_type)
        draft = await _collect_draft(session, tenant, actor)
        external_sku = build_external_sku(channel_type, draft.suggested_sku)

        results = await SourcingService().publish(
            session,
            tenant,
            actor,
            draft.id,
            SourcingPublishRequest(
                channel_account_ids=[account_id],
                push_to_marketplace=True,
                initial_stock_quantity=77,
            ),
            listing_publisher=MarketplaceListingDispatcher(transport=transport),
        )

        assert len(results) == 1
        result = results[0]
        assert result.channel_type == channel_type
        assert result.external_sku == external_sku
        assert result.external_product_id == expected_product_id
        assert not result.external_product_id.startswith(PENDING_LISTING_PREFIX)
        assert result.listing_state == "live_published"
        assert result.external_listing_confirmed is True
        assert result.publish_mode == "live_marketplace_api"

        mapping = await _mapping_row(session, tenant, account_id, external_sku)
        assert mapping is not None
        assert mapping.external_product_id == expected_product_id
        assert mapping.external_product_id != external_sku

        draft_row = (
            await session.execute(
                text(
                    "SELECT status, publish_history FROM sourcing_drafts "
                    "WHERE tenant_id = :tid AND id = :did"
                ),
                {"tid": tenant, "did": draft.id},
            )
        ).one()
        assert draft_row.status == "published"
        entry = draft_row.publish_history[-1]
        assert entry["channel_type"] == channel_type
        assert entry["external_product_id"] == expected_product_id
        assert entry["listing_state"] == "live_published"
        assert entry["external_listing_confirmed"] is True
        assert entry["publish_mode"] == "live_marketplace_api"
        assert entry["remote_status"] == EXPECTED_REMOTE_STATUS[channel_type]

        read = await SourcingService().get_draft(session, tenant, draft.id)
        assert read.listing_state == "live_published"
        assert read.status == "published"

        audit = (
            await session.execute(
                text(
                    "SELECT after_state FROM audit_events "
                    "WHERE action = 'catalog.sourcing_draft.published' "
                    "AND resource_id = :did"
                ),
                {"did": draft.id},
            )
        ).one()
        assert audit.after_state["listing_state"] == "live_published"
        assert audit.after_state["external_listing_confirmed"] is True
        assert audit.after_state["publish_mode"] == "live_marketplace_api"

        # The credential really is sealed in the vault: no plaintext secret hits the table.
        credential = (
            await session.execute(
                text(
                    "SELECT credential_type, ciphertext, nonce, auth_tag, key_version "
                    "FROM channel_credentials WHERE tenant_id = :tid "
                    "AND channel_account_id = :caid"
                ),
                {"tid": tenant, "caid": account_id},
            )
        ).one()
        assert credential.credential_type == DISPATCHER_CREDENTIAL_TYPES[channel_type]
        assert int(credential.key_version) >= 1
        assert len(bytes(credential.nonce)) == 12
        assert len(bytes(credential.auth_tag)) == 16
        ciphertext = bytes(credential.ciphertext)
        for secret in SECRETS:
            assert secret.encode() not in ciphertext
        decrypted, _expires_at = await CredentialVaultHelper().load_secret_json(
            session,
            tenant,
            account_id,
            DISPATCHER_CREDENTIAL_TYPES[channel_type],
        )
        assert decrypted == MARKETPLACE_CREDENTIALS[channel_type]

    # The remote call really carried the priced, stock-level-aware listing.
    assert calls, "the marketplace API must have been called"
    _assert_publish_payload(channel_type, calls, external_sku=external_sku, draft=draft)


def _assert_publish_payload(
    channel_type: str,
    calls: list[httpx.Request],
    *,
    external_sku: str,
    draft: Any,
) -> None:
    """Pin the merchandising data each marketplace actually received."""
    if channel_type == AMAZON_CHANNEL:
        listing = [
            request
            for request in calls
            if request.url.host != "api.amazon.com" and request.method == "PUT"
        ]
        assert len(listing) == 1
        body = json.loads(listing[0].content)
        attributes = body["attributes"]
        assert attributes["item_name"][0]["value"] == draft.optimized_title_en
        assert attributes["fulfillment_availability"][0]["quantity"] == 77
        assert attributes["purchasable_offer"][0]["our_price"][0]["schedule"][0][
            "value_with_tax"
        ] == "10.42"
        assert attributes["hs_code"][0]["value"] == "9405429000"
        assert attributes["item_type_keyword"][0]["value"] == "LED Lighting"
        return
    if channel_type == EBAY_CHANNEL:
        offer = next(
            request for request in calls if request.url.path == "/sell/inventory/v1/offer"
        )
        body = json.loads(offer.content)
        assert body["sku"] == external_sku
        assert body["availableQuantity"] == 77
        assert body["pricingSummary"]["price"] == {"value": "10.42", "currency": "USD"}
        assert "- Category: LED Lighting" in body["listingDescription"]
        assert "- HS code: 9405429000" in body["listingDescription"]
        inventory_item = next(
            request
            for request in calls
            if request.url.path.startswith("/sell/inventory/v1/inventory_item/")
        )
        item_body = json.loads(inventory_item.content)
        assert item_body["availability"]["shipToLocationAvailability"]["quantity"] == 77
        return
    publish = calls[-1]
    params = dict(httpx.QueryParams(publish.content.decode()))
    assert params["sku_code"] == external_sku
    assert params["stock_quantity"] == "77"
    assert params["fob_price"] == "10.42"
    assert params["hs_code"] == "9405429000"
    assert params["category_id"]
    assert params["sign"]


async def test_live_push_of_many_channels_records_every_channel(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    """One publish call may target several marketplaces; every id stays distinct."""
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])
    calls: dict[str, list[httpx.Request]] = {channel: [] for channel in MARKETPLACE_CHANNELS}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.amazon.com":
            return PUBLISH_HANDLERS[AMAZON_CHANNEL](calls[AMAZON_CHANNEL])(request)
        if "amazonaws.com" in request.url.host or "amazon.com" in request.url.host:
            return PUBLISH_HANDLERS[AMAZON_CHANNEL](calls[AMAZON_CHANNEL])(request)
        if request.url.host == "api.ebay.com":
            return PUBLISH_HANDLERS[EBAY_CHANNEL](calls[EBAY_CHANNEL])(request)
        return PUBLISH_HANDLERS[ALIBABA_CHANNEL](calls[ALIBABA_CHANNEL])(request)

    async with tenant_session(runtime_session_factory, tenant) as session:
        accounts = {}
        for channel_type in MARKETPLACE_CHANNELS:
            accounts[channel_type] = await _seed_marketplace_account(
                session, tenant, channel_type
            )
        draft = await _collect_draft(session, tenant, actor)

        results = await SourcingService().publish(
            session,
            tenant,
            actor,
            draft.id,
            SourcingPublishRequest(
                channel_account_ids=list(accounts.values()),
                push_to_marketplace=True,
                price_multiplier=Decimal("1.20"),
            ),
            listing_publisher=MarketplaceListingDispatcher(
                transport=httpx.MockTransport(handler)
            ),
        )

        by_channel = {result.channel_type: result for result in results}
        assert set(by_channel) == set(MARKETPLACE_CHANNELS)
        for channel_type, result in by_channel.items():
            assert result.external_product_id == EXPECTED_EXTERNAL_PRODUCT_ID[channel_type]
            assert result.listing_state == "live_published"
            assert result.price_usd == Decimal("12.50")
            mapping = await _mapping_row(
                session, tenant, accounts[channel_type], result.external_sku
            )
            assert mapping is not None
            assert mapping.external_product_id == EXPECTED_EXTERNAL_PRODUCT_ID[channel_type]

        read = await SourcingService().get_draft(session, tenant, draft.id)
        assert read.listing_state == "live_published"
        assert len(read.publish_history) == len(MARKETPLACE_CHANNELS)
        assert {entry["external_product_id"] for entry in read.publish_history} == set(
            EXPECTED_EXTERNAL_PRODUCT_ID.values()
        )
    assert all(calls[channel] for channel in MARKETPLACE_CHANNELS)


# --------------------------------------------------------------------------------------
# 2. Fail-closed paths
# --------------------------------------------------------------------------------------


async def test_live_push_without_a_publisher_returns_503_and_never_maps(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])
    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_marketplace_account(session, tenant, AMAZON_CHANNEL)
        draft = await _collect_draft(session, tenant, actor)
        external_sku = build_external_sku(AMAZON_CHANNEL, draft.suggested_sku)

        with pytest.raises(AppError) as excinfo:
            await SourcingService().publish(
                session,
                tenant,
                actor,
                draft.id,
                SourcingPublishRequest(
                    channel_account_ids=[account_id], push_to_marketplace=True
                ),
                listing_publisher=None,
            )

        assert excinfo.value.status_code == 503
        assert excinfo.value.code == "ERR_MARKETPLACE_PUBLISHER_UNAVAILABLE"
        assert await _mapping_row(session, tenant, account_id, external_sku) is None
        row = (
            await session.execute(
                text(
                    "SELECT status, publish_history FROM sourcing_drafts "
                    "WHERE tenant_id = :tid AND id = :did"
                ),
                {"tid": tenant, "did": draft.id},
            )
        ).one()
        assert row.status == "imported"
        assert row.publish_history == []
        read = await SourcingService().get_draft(session, tenant, draft.id)
        assert read.listing_state == "imported"


async def test_live_push_rejected_by_the_marketplace_stays_unconfirmed(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.amazon.com":
            return httpx.Response(
                200,
                json={
                    "access_token": AMAZON_ACCESS_TOKEN,
                    "expires_in": 3600,
                    "token_type": "bearer",
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "INVALID",
                "submissionId": "sub-bad",
                "issues": [
                    {
                        "code": "90220",
                        "severity": "ERROR",
                        "message": "The value provided for bullet_point is invalid.",
                    }
                ],
            },
        )

    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_marketplace_account(session, tenant, AMAZON_CHANNEL)
        draft = await _collect_draft(session, tenant, actor)
        external_sku = build_external_sku(AMAZON_CHANNEL, draft.suggested_sku)

        with pytest.raises(AppError) as excinfo:
            await SourcingService().publish(
                session,
                tenant,
                actor,
                draft.id,
                SourcingPublishRequest(
                    channel_account_ids=[account_id], push_to_marketplace=True
                ),
                listing_publisher=MarketplaceListingDispatcher(
                    transport=httpx.MockTransport(handler)
                ),
            )
        assert excinfo.value.code == "ERR_AMAZON_LISTING_REJECTED"
        assert excinfo.value.status_code == 502

        assert await _mapping_row(session, tenant, account_id, external_sku) is None
        read = await SourcingService().get_draft(session, tenant, draft.id)
        assert read.listing_state == "imported"
        unpublished = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE action = 'catalog.sourcing_draft.published' "
                    "AND resource_id = :did"
                ),
                {"did": draft.id},
            )
        ).scalar()
        assert unpublished == 0


async def test_live_push_with_missing_credentials_is_rejected_before_any_call(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never hit
        raise AssertionError(f"no marketplace call may happen: {request.url}")

    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_marketplace_account(
            session, tenant, EBAY_CHANNEL, store_credentials=False
        )
        draft = await _collect_draft(session, tenant, actor)
        external_sku = build_external_sku(EBAY_CHANNEL, draft.suggested_sku)

        with pytest.raises(AppError) as excinfo:
            await SourcingService().publish(
                session,
                tenant,
                actor,
                draft.id,
                SourcingPublishRequest(
                    channel_account_ids=[account_id], push_to_marketplace=True
                ),
                listing_publisher=MarketplaceListingDispatcher(
                    transport=httpx.MockTransport(handler)
                ),
            )
        assert excinfo.value.code == "ERR_CREDENTIAL_NOT_FOUND"
        assert excinfo.value.status_code == 404
        assert await _mapping_row(session, tenant, account_id, external_sku) is None


async def test_live_push_to_shopify_persists_live_published_and_shopify_product_gid(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])
    shop_domain = "qzled-erp-sandbox.myshopify.com"
    client_id = "shopify-live-client-id"
    client_secret = "shpss_live_client_secret"
    refreshed_token = "shpat_refreshed_live_token"
    expected_product_id = "gid://shopify/Product/990012345"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/admin/oauth/access_token":
            return httpx.Response(
                200,
                json={"access_token": refreshed_token, "expires_in": 86399},
            )
        if request.url.path == "/admin/api/2026-01/graphql.json":
            assert request.headers["x-shopify-access-token"] == refreshed_token
            gql_body = json.loads(request.content)
            assert gql_body["variables"]["synchronous"] is True
            product_input = gql_body["variables"]["productSet"]
            assert product_input["status"] == "ACTIVE"
            assert product_input["productType"] == "LED Lighting"
            assert len(product_input["files"]) == 2
            return httpx.Response(
                200,
                json={
                    "data": {
                        "productSet": {
                            "product": {
                                "id": expected_product_id,
                                "title": product_input["title"],
                                "handle": "solar-led-flood-light-100w",
                                "status": "ACTIVE",
                                "variants": {
                                    "nodes": [
                                        {
                                            "id": "gid://shopify/ProductVariant/990012346",
                                            "sku": product_input["variants"][0]["sku"],
                                            "price": product_input["variants"][0]["price"],
                                        }
                                    ]
                                },
                            },
                            "userErrors": [],
                        }
                    }
                },
            )
        return httpx.Response(404, json={"unexpected": str(request.url)})

    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name, status, config) VALUES "
                "(:id, :tid, 'shopify', :ext, 'QZLED Shopify Sandbox', 'active', '{}'::jsonb)"
            ),
            {"id": account_id, "tid": tenant, "ext": shop_domain},
        )
        await CredentialVaultHelper().save_secret_json(
            session,
            tenant,
            account_id,
            "shopify_custom_app",
            {
                "shop_domain": shop_domain,
                "auth_mode": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "access_token": "",
            },
        )
        draft = await _collect_draft(session, tenant, actor)
        external_sku = build_external_sku("shopify", draft.suggested_sku)

        results = await SourcingService().publish(
            session,
            tenant,
            actor,
            draft.id,
            SourcingPublishRequest(
                channel_account_ids=[account_id],
                push_to_marketplace=True,
                initial_stock_quantity=77,
            ),
            listing_publisher=MarketplaceListingDispatcher(
                transport=httpx.MockTransport(handler)
            ),
        )

        assert len(results) == 1
        result = results[0]
        assert result.channel_type == "shopify"
        assert result.external_sku == external_sku
        assert result.external_product_id == expected_product_id
        assert result.listing_state == "live_published"
        assert result.external_listing_confirmed is True
        assert result.publish_mode == "live_marketplace_api"

        mapping = await _mapping_row(session, tenant, account_id, external_sku)
        assert mapping is not None
        assert mapping.external_product_id == expected_product_id

        draft_read = await SourcingService().get_draft(session, tenant, draft.id)
        assert draft_read.listing_state == "live_published"
        assert draft_read.status == "published"

        # Client-credentials refresh must have persisted the fresh token back to the vault.
        refreshed_creds, _ = await CredentialVaultHelper().load_secret_json(
            session, tenant, account_id, "shopify_custom_app"
        )
        assert refreshed_creds["access_token"] == refreshed_token

    graphql_calls = [req for req in calls if req.url.path.endswith("/graphql.json")]
    assert len(graphql_calls) == 1
    sent_product = json.loads(graphql_calls[0].content)["variables"]["productSet"]
    assert sent_product["variants"][0]["sku"] == external_sku
    assert sent_product["variants"][0]["price"] == "10.42"
    assert "<ul>" in sent_product["descriptionHtml"]


async def test_shopify_live_push_rejection_rolls_back_without_mapping(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/api/2026-01/graphql.json":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "productSet": {
                            "product": None,
                            "userErrors": [
                                {
                                    "field": ["productSet", "variants", "sku"],
                                    "message": "SKU has already been taken.",
                                }
                            ],
                        }
                    }
                },
            )
        return httpx.Response(404)

    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name, status, config) VALUES "
                "(:id, :tid, 'shopify', 'qzled-erp-sandbox.myshopify.com', "
                "'QZLED Shopify Sandbox', 'active', '{}'::jsonb)"
            ),
            {"id": account_id, "tid": tenant},
        )
        await CredentialVaultHelper().save_secret_json(
            session,
            tenant,
            account_id,
            "oauth_token",
            {
                "shop_domain": "qzled-erp-sandbox.myshopify.com",
                "auth_mode": "oauth",
                "access_token": "shpat_static_oauth_token",
            },
        )
        draft = await _collect_draft(session, tenant, actor)
        external_sku = build_external_sku("shopify", draft.suggested_sku)

        with pytest.raises(AppError) as excinfo:
            await SourcingService().publish(
                session,
                tenant,
                actor,
                draft.id,
                SourcingPublishRequest(
                    channel_account_ids=[account_id], push_to_marketplace=True
                ),
                listing_publisher=MarketplaceListingDispatcher(
                    transport=httpx.MockTransport(handler)
                ),
            )
        assert excinfo.value.status_code == 422
        assert excinfo.value.code == "ERR_SHOPIFY_LISTING_REJECTED"
        assert await _mapping_row(session, tenant, account_id, external_sku) is None
        read = await SourcingService().get_draft(session, tenant, draft.id)
        assert read.listing_state == "imported"
        unpublished = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE action = 'catalog.sourcing_draft.published' "
                    "AND resource_id = :did"
                ),
                {"did": draft.id},
            )
        ).scalar()
        assert unpublished == 0


async def test_live_push_to_an_unsupported_channel_type_is_a_422(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])
    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name, status, config) VALUES "
                "(:id, :tid, 'made_in_china_email', 'mic-qmled@example.com', "
                "'Made-in-China Inquiry Email', 'active', '{}'::jsonb)"
            ),
            {"id": account_id, "tid": tenant},
        )
        draft = await _collect_draft(session, tenant, actor)

        with pytest.raises(AppError) as excinfo:
            await SourcingService().publish(
                session,
                tenant,
                actor,
                draft.id,
                SourcingPublishRequest(
                    channel_account_ids=[account_id], push_to_marketplace=True
                ),
                listing_publisher=MarketplaceListingDispatcher(),
            )
        assert excinfo.value.status_code == 422
        assert excinfo.value.code == "ERR_MARKETPLACE_LIVE_PUSH_UNSUPPORTED"


class _ExplodingPublisher:
    """A publisher that must never be consulted by the local mapping path."""

    async def publish_listing(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the local SKU mapping path must not call the publisher")


async def test_local_mapping_path_never_calls_the_publisher(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])
    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_marketplace_account(session, tenant, AMAZON_CHANNEL)
        draft = await _collect_draft(session, tenant, actor)
        results = await SourcingService().publish(
            session,
            tenant,
            actor,
            draft.id,
            SourcingPublishRequest(channel_account_ids=[account_id]),
            listing_publisher=_ExplodingPublisher(),
        )
        result = results[0]
        assert result.listing_state == "mapping_ready"
        assert result.external_listing_confirmed is False
        assert result.publish_mode == "local_sku_mapping"
        assert result.external_product_id.startswith(PENDING_LISTING_PREFIX)
        read = await SourcingService().get_draft(session, tenant, draft.id)
        assert read.listing_state == "mapping_ready"


# --------------------------------------------------------------------------------------
# 3. On-demand order sync endpoint: real normalized orders into orders/order_items
# --------------------------------------------------------------------------------------


def _sync_client(
    factory: Factory,
    principal: CurrentPrincipal,
    transport: httpx.MockTransport,
) -> AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.state.marketplace_http_transport = transport
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(marketplace.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


AMAZON_ORDER_ID = "111-2223334-5556667"
EBAY_ORDER_ID = "12-11111-22222"
ALIBABA_TRADE_ID = "AT202611190001"

SYNC_EXPECTATIONS: Mapping[str, dict[str, Any]] = {
    AMAZON_CHANNEL: {
        "external_sku": "AMAZON-ORDER-SKU",
        "external_order_id": AMAZON_ORDER_ID,
        "total": Decimal("59.98"),
        "line_total": Decimal("59.98"),
        "quantity": 2,
        "fulfillment_status": FulfillmentStatus.SHIPPED.value,
        "payment_status": PaymentStatus.PAID.value,
        "order_mode": OrderMode.B2C_RETAIL.value,
    },
    EBAY_CHANNEL: {
        "external_sku": "EBAY-ORDER-SKU",
        "external_order_id": EBAY_ORDER_ID,
        "total": Decimal("44.99"),
        "line_total": Decimal("39.99"),
        "quantity": 1,
        "fulfillment_status": FulfillmentStatus.CONFIRMED_RESERVED.value,
        "payment_status": PaymentStatus.PAID.value,
        "order_mode": OrderMode.B2C_RETAIL.value,
    },
    ALIBABA_CHANNEL: {
        "external_sku": "ALIBABA-ORDER-SKU",
        "external_order_id": ALIBABA_TRADE_ID,
        "total": Decimal("110.00"),
        "line_total": Decimal("100.00"),
        "quantity": 10,
        "fulfillment_status": FulfillmentStatus.SHIPPED.value,
        "payment_status": PaymentStatus.PAID.value,
        "order_mode": OrderMode.B2B_WHOLESALE.value,
    },
}


def order_sync_handler(
    channel_type: str, calls: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve each marketplace's real order feed with a normalized single order."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if channel_type == AMAZON_CHANNEL:
            if request.url.host == "api.amazon.com":
                return httpx.Response(
                    200,
                    json={
                        "access_token": AMAZON_ACCESS_TOKEN,
                        "expires_in": 3600,
                        "token_type": "bearer",
                    },
                )
            if path == f"/orders/v0/orders/{AMAZON_ORDER_ID}/orderItems":
                return httpx.Response(
                    200,
                    json={
                        "payload": {
                            "OrderItems": [
                                {
                                    "SellerSKU": "AMAZON-ORDER-SKU",
                                    "Title": "LED Panel 40W",
                                    "QuantityOrdered": 2,
                                    "ItemPrice": {
                                        "CurrencyCode": "USD",
                                        "Amount": "59.98",
                                    },
                                }
                            ]
                        }
                    },
                )
            if path == "/orders/v0/orders":
                return httpx.Response(
                    200,
                    json={
                        "payload": {
                            "Orders": [
                                {
                                    "AmazonOrderId": AMAZON_ORDER_ID,
                                    "SellerOrderId": "SELLER-AMZ-1",
                                    "OrderStatus": "Shipped",
                                    "PaymentMethod": "other",
                                    "FulfillmentChannel": "MFN",
                                    "MarketplaceId": "ATVPDKIKX0DER",
                                    "OrderTotal": {
                                        "CurrencyCode": "USD",
                                        "Amount": "59.98",
                                    },
                                    "LastUpdateDate": "2026-11-19T10:00:00Z",
                                    "BuyerInfo": {"BuyerEmail": "amz.buyer@example.com"},
                                    "ShippingAddress": {
                                        "CountryCode": "US",
                                        "Phone": "+14155550111",
                                    },
                                }
                            ]
                        }
                    },
                )
            return httpx.Response(404, json={"errors": [{"message": path}]})

        if channel_type == EBAY_CHANNEL:
            if path == "/identity/v1/oauth2/token":
                return httpx.Response(
                    200, json={"access_token": EBAY_ACCESS_TOKEN, "expires_in": 7200}
                )
            if path == "/sell/fulfillment/v1/order":
                assert "lastmodifieddate" in request.url.params["filter"]
                return httpx.Response(
                    200,
                    json={
                        "total": 1,
                        "orders": [
                            {
                                "orderId": EBAY_ORDER_ID,
                                "orderFulfillmentStatus": "NOT_STARTED",
                                "orderPaymentStatus": "PAID",
                                "sellerId": "qz_led_official",
                                "lastModifiedDate": "2026-11-19T10:00:00.000Z",
                                "creationDate": "2026-11-19T09:00:00.000Z",
                                "pricingSummary": {
                                    "currency": "USD",
                                    "priceSubtotal": {"value": "39.99"},
                                    "deliveryCost": {"value": "5.00"},
                                    "tax": {"value": "0.00"},
                                    "total": {"value": "44.99"},
                                },
                                "buyer": {
                                    "username": "ebay_buyer",
                                    "buyerRegistrationAddress": {
                                        "email": "ebay.buyer@example.com",
                                        "countryCode": "US",
                                    },
                                },
                                "lineItems": [
                                    {
                                        "sku": "EBAY-ORDER-SKU",
                                        "title": "LED Strip 5m",
                                        "quantity": 1,
                                        "lineItemCost": {"value": "39.99"},
                                    }
                                ],
                            }
                        ],
                    },
                )
            return httpx.Response(404, json={"errors": [{"message": path}]})

        # Alibaba.com ICBU trade feed (form-encoded, signed request).
        data = dict(httpx.QueryParams(request.content.decode()))
        assert data["sign"] == data["sign"].upper()
        assert data["app_key"] == ALIBABA_APP_KEY
        assert data["session"] == ALIBABA_ACCESS_TOKEN
        return httpx.Response(
            200,
            json={
                "result": {
                    "success": True,
                    "code": "0",
                    "data": {
                        "total_count": "1",
                        "orders": [
                            {
                                "trade_id": ALIBABA_TRADE_ID,
                                "order_no": f"NO-{ALIBABA_TRADE_ID}",
                                "status": "waitbuyerreceive",
                                "currency": "USD",
                                "subtotal": "100.00",
                                "shipping_fee": "10.00",
                                "discount_amount": "0.00",
                                "tax_amount": "0.00",
                                "total_amount": "110.00",
                                "gmt_modified": "2026-11-19 10:00:00",
                                "buyer_company_name": "Nordic Lights AB",
                                "buyer_company_id": "ALB-778001",
                                "buyer_country": "SE",
                                "buyer_email": "ops@nordiclights.se",
                                "buyer_login_id": "nordiclights",
                                "products": [
                                    {
                                        "product_id": "6601999001",
                                        "sku_code": "ALIBABA-ORDER-SKU",
                                        "product_name": "Nordic Pendant Light 220V",
                                        "quantity": "10",
                                        "unit_price": "10.0000",
                                        "amount": "100.00",
                                    }
                                ],
                            }
                        ],
                    },
                }
            },
        )

    return handler


@pytest.mark.parametrize("channel_type", MARKETPLACE_CHANNELS)
async def test_on_demand_sync_orders_ingests_normalized_orders(
    channel_type: str,
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    expected = SYNC_EXPECTATIONS[channel_type]
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(order_sync_handler(channel_type, calls))

    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_marketplace_account(session, tenant, channel_type)
        await _seed_mapped_variant(
            session,
            tenant,
            account_id,
            external_sku=expected["external_sku"],
            warehouse_id=workspace["warehouse_id"],
        )

    client = _sync_client(runtime_session_factory, workspace["manager"], transport)
    async with client:
        response = await client.post(
            f"/api/v1/channels/marketplace/{account_id}/sync-orders",
            params={"lookback_hours": 48},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "channel_account_id": str(account_id),
        "channel_type": channel_type,
        "scanned": 1,
        "recovered": 1,
        "updated": 0,
        "failed": 0,
    }

    async with tenant_session(runtime_session_factory, tenant) as session:
        order = (
            await session.execute(
                text(
                    "SELECT id, external_order_id, order_mode, currency, total_amount, "
                    "subtotal_amount, fulfillment_status, payment_status, channel_metadata "
                    "FROM orders WHERE tenant_id = :tid AND channel_account_id = :caid"
                ),
                {"tid": tenant, "caid": account_id},
            )
        ).one()
        assert order.external_order_id == expected["external_order_id"]
        assert order.order_mode == expected["order_mode"]
        assert order.currency == "USD"
        assert Decimal(str(order.total_amount)) == expected["total"]
        assert order.fulfillment_status == expected["fulfillment_status"]
        assert order.payment_status == expected["payment_status"]
        # The order -> tenant-base FX rate is locked at ingestion, never guessed as 1.0.
        assert order.channel_metadata["fx_source"] == "fallback_fx"
        assert Decimal(order.channel_metadata["fx_rate_order_to_base"]) == Decimal("7.240000")

        items = (
            await session.execute(
                text(
                    "SELECT sku_snapshot, quantity, unit_price, line_total FROM order_items "
                    "WHERE tenant_id = :tid AND order_id = :oid"
                ),
                {"tid": tenant, "oid": order.id},
            )
        ).all()
        assert len(items) == 1
        assert items[0].quantity == expected["quantity"]
        assert Decimal(str(items[0].line_total)) == expected["line_total"]
        assert items[0].sku_snapshot.startswith("INT-")

        reserved = await session.scalar(
            text(
                "SELECT reserved_qty FROM inventory_balances WHERE tenant_id = :tid "
                "AND warehouse_id = :wid AND variant_id = ("
                "SELECT variant_id FROM order_items WHERE order_id = :oid)"
            ),
            {"tid": tenant, "wid": workspace["warehouse_id"], "oid": order.id},
        )
        assert reserved == expected["quantity"]

        if channel_type == ALIBABA_CHANNEL:
            company = (
                await session.execute(
                    text(
                        "SELECT legal_name, country_code FROM b2b_companies "
                        "WHERE tenant_id = :tid"
                    ),
                    {"tid": tenant},
                )
            ).one()
            assert company.legal_name == "Nordic Lights AB"
            assert company.country_code == "SE"

    # A second sync is idempotent: the same order is refreshed, never duplicated.
    client = _sync_client(runtime_session_factory, workspace["owner"], transport)
    async with client:
        again = await client.post(
            f"/api/v1/channels/marketplace/{account_id}/sync-orders",
        )
    assert again.status_code == 200, again.text
    assert again.json()["recovered"] == 0
    assert again.json()["updated"] == 1
    async with tenant_session(runtime_session_factory, tenant) as session:
        order_count = await session.scalar(
            text(
                "SELECT count(*) FROM orders WHERE tenant_id = :tid AND channel_account_id = :caid"
            ),
            {"tid": tenant, "caid": account_id},
        )
        assert order_count == 1


async def test_sync_orders_rejects_unknown_accounts_and_unsupported_channels(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    async with tenant_session(runtime_session_factory, tenant) as session:
        shopify_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name, status, config) VALUES "
                "(:id, :tid, 'shopify', 'qm-led.myshopify.com', 'Shopify Store', "
                "'active', '{}'::jsonb)"
            ),
            {"id": shopify_id, "tid": tenant},
        )

    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, json={"unexpected": str(request.url)})
    )
    client = _sync_client(runtime_session_factory, workspace["owner"], transport)
    async with client:
        missing = await client.post(
            f"/api/v1/channels/marketplace/{uuid4()}/sync-orders"
        )
        unsupported = await client.post(
            f"/api/v1/channels/marketplace/{shopify_id}/sync-orders"
        )
        bad_lookback = await client.post(
            f"/api/v1/channels/marketplace/{shopify_id}/sync-orders",
            params={"lookback_hours": 0},
        )
    assert missing.status_code == 404
    assert missing.json()["code"] == "ERR_CHANNEL_ACCOUNT_NOT_FOUND"
    assert unsupported.status_code == 422
    assert unsupported.json()["code"] == "ERR_MARKETPLACE_ORDER_SYNC_UNSUPPORTED"
    assert bad_lookback.status_code == 422


async def test_sync_orders_requires_a_manager_or_above(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant"]
    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_marketplace_account(session, tenant, AMAZON_CHANNEL)

    agent = CurrentPrincipal(
        user_id=uuid4(),
        tenant_id=tenant,
        role=UserRole.AGENT,
        email="agent@marketplace-live.example.com",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, json={"unexpected": str(request.url)})
    )
    client = _sync_client(runtime_session_factory, agent, transport)
    async with client:
        response = await client.post(
            f"/api/v1/channels/marketplace/{account_id}/sync-orders"
        )
    assert response.status_code == 403
    assert response.json()["code"] == "ERR_INSUFFICIENT_ROLE"


# --------------------------------------------------------------------------------------
# 4. Reconciler wiring: the cron recognizes and builds all three marketplaces
# --------------------------------------------------------------------------------------


def test_reconciliation_registry_covers_the_marketplaces() -> None:
    from erp_core.shared.enums import ChannelType

    for channel in (
        ChannelType.AMAZON_SP,
        ChannelType.EBAY,
        ChannelType.ALIBABA_EMAIL,
    ):
        assert channel in ECOMMERCE_CHANNELS
    assert RECONCILIATION_CREDENTIAL_TYPES[ChannelType.SHOPIFY] == (
        "shopify_custom_app",
        "oauth_token",
    )
    assert RECONCILIATION_CREDENTIAL_TYPES[ChannelType.AMAZON_SP] == ("amazon_sp_api",)
    assert RECONCILIATION_CREDENTIAL_TYPES[ChannelType.EBAY] == ("ebay_api",)
    assert RECONCILIATION_CREDENTIAL_TYPES[ChannelType.ALIBABA_EMAIL] == ("alibaba_intl_app",)
    # The listing dispatcher covers all 8 live marketplaces whose keys match the reconciler.
    assert set(DISPATCHER_CREDENTIAL_TYPES.values()) == {
        "amazon_sp_api",
        "ebay_api",
        "alibaba_intl_app",
        "shopify_custom_app",
        "woocommerce_keys",
        "shopline_app",
        "shoplazza_app",
        "tiktok_shop_app",
    }


@pytest.mark.parametrize(
    ("channel_type", "expected_class"),
    [
        ("amazon_sp", AmazonSpApiAdapter),
        ("ebay", EbayConnectorAdapter),
        ("alibaba_email", AlibabaIntlAdapter),
    ],
)
async def test_instantiate_adapter_builds_the_right_marketplace_adapter(
    channel_type: str, expected_class: type
) -> None:
    from erp_core.shared.enums import ChannelType

    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    adapter = await _instantiate_adapter(
        ChannelType(channel_type),
        dict(MARKETPLACE_CREDENTIALS[channel_type]),
        uuid4(),
        transport=transport,
    )
    assert isinstance(adapter, expected_class)
    if isinstance(adapter, AmazonSpApiAdapter):
        assert adapter.seller_id == AMAZON_SELLER_ID
        assert adapter.marketplace_id == "ATVPDKIKX0DER"
    elif isinstance(adapter, EbayConnectorAdapter):
        assert adapter.seller_username == EBAY_SELLER
        assert adapter.site_id == "EBAY_US"
        assert adapter.sandbox is False
        assert EBAY_CERT_ID not in repr(adapter)
    else:
        assert adapter.store_id == ALIBABA_STORE_ID
        assert adapter.app_key == ALIBABA_APP_KEY
        assert ALIBABA_APP_SECRET not in repr(adapter)


async def test_instantiate_adapter_defaults_marketplace_and_site_ids() -> None:
    from erp_core.shared.enums import ChannelType

    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    amazon_credentials = dict(MARKETPLACE_CREDENTIALS[AMAZON_CHANNEL])
    amazon_credentials.pop("marketplace_id")
    amazon = await _instantiate_adapter(
        ChannelType.AMAZON_SP, amazon_credentials, None, transport=transport
    )
    assert isinstance(amazon, AmazonSpApiAdapter)
    assert amazon.marketplace_id == "ATVPDKIKX0DER"

    ebay_credentials = dict(MARKETPLACE_CREDENTIALS[EBAY_CHANNEL])
    ebay_credentials.pop("site_id")
    ebay = await _instantiate_adapter(
        ChannelType.EBAY, ebay_credentials, None, transport=transport
    )
    assert isinstance(ebay, EbayConnectorAdapter)
    assert ebay.site_id == "EBAY_US"


async def test_reconcile_channel_account_orders_runs_for_every_marketplace(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    """The 15-minute worker ingests a marketplace order through the shared path."""
    from erp_core.orders.reconciliation_worker import reconcile_channel_account_orders
    from erp_core.shared.enums import ChannelType

    tenant = workspace["tenant"]
    expected = SYNC_EXPECTATIONS[AMAZON_CHANNEL]
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(order_sync_handler(AMAZON_CHANNEL, calls))
    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_marketplace_account(session, tenant, AMAZON_CHANNEL)
        await _seed_mapped_variant(
            session,
            tenant,
            account_id,
            external_sku=expected["external_sku"],
            warehouse_id=workspace["warehouse_id"],
        )
        adapter = await _instantiate_adapter(
            ChannelType.AMAZON_SP,
            dict(MARKETPLACE_CREDENTIALS[AMAZON_CHANNEL]),
            workspace["warehouse_id"],
            transport=transport,
        )
        stats = await reconcile_channel_account_orders(
            session,
            tenant_id=tenant,
            channel_account_id=account_id,
            channel_type=ChannelType.AMAZON_SP,
            adapter=adapter,
            default_warehouse_id=workspace["warehouse_id"],
            since=datetime.now(UTC) - timedelta(minutes=30),
        )
        assert stats == {"scanned": 1, "recovered": 1, "updated": 0, "failed": 0}
        order = (
            await session.execute(
                text(
                    "SELECT order_no, total_amount FROM orders WHERE tenant_id = :tid "
                    "AND external_order_id = :ext"
                ),
                {"tid": tenant, "ext": AMAZON_ORDER_ID},
            )
        ).one()
        assert Decimal(str(order.total_amount)) == Decimal("59.98")
        audit = (
            await session.execute(
                text(
                    "SELECT after_state FROM audit_events "
                    "WHERE action = 'ecommerce.orders_reconciled' AND resource_id = :rid"
                ),
                {"rid": account_id},
            )
        ).one()
        assert audit.after_state["recovered"] == 1
