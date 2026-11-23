"""15-minute incremental reconciliation of storefront + marketplace orders.

Every channel in :data:`ECOMMERCE_CHANNELS` (the 16 live ecommerce integrations) is pulled
on the same cadence so a missed webhook or a drifted order is repaired without human
action; :func:`_instantiate_adapter` builds the real connector for each of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.ecommerce.base import CommercePlatformAdapter, EcommerceOrderSyncService
from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_migrator_engine,
    create_runtime_engine,
    create_session_factory,
    system_session,
    tenant_session,
)
from erp_core.shared.enums import ChannelType
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event

logger = structlog.get_logger(__name__)

ECOMMERCE_CHANNELS = (
    ChannelType.SHOPIFY,
    ChannelType.WOOCOMMERCE,
    ChannelType.SHOPLINE,
    ChannelType.SHOPLAZZA,
    ChannelType.TIKTOK_SHOP,
    ChannelType.AMAZON_SP,
    ChannelType.EBAY,
    ChannelType.ALIBABA_EMAIL,
    ChannelType.SHOPEE,
    ChannelType.LAZADA,
    ChannelType.TEMU,
    ChannelType.SHEIN,
    ChannelType.ALIEXPRESS,
    ChannelType.OZON,
    ChannelType.WALMART,
    ChannelType.MERCADO_LIBRE,
)
CREDENTIAL_TYPES = {
    ChannelType.SHOPIFY: ("shopify_custom_app", "oauth_token"),
    ChannelType.WOOCOMMERCE: ("woocommerce_keys",),
    ChannelType.SHOPLINE: ("shopline_app",),
    ChannelType.SHOPLAZZA: ("shoplazza_app",),
    ChannelType.TIKTOK_SHOP: ("tiktok_shop_app",),
    ChannelType.AMAZON_SP: ("amazon_sp_api",),
    ChannelType.EBAY: ("ebay_api",),
    ChannelType.ALIBABA_EMAIL: ("alibaba_intl_app",),
    ChannelType.SHOPEE: ("shopee_app",),
    ChannelType.LAZADA: ("lazada_app",),
    ChannelType.TEMU: ("temu_app",),
    ChannelType.SHEIN: ("shein_app",),
    ChannelType.ALIEXPRESS: ("aliexpress_app",),
    ChannelType.OZON: ("ozon_app",),
    ChannelType.WALMART: ("walmart_app",),
    ChannelType.MERCADO_LIBRE: ("mercado_libre_app",),
}
REPLAY_WINDOW = timedelta(minutes=30)


async def resolve_default_warehouse_id(
    session: AsyncSession,
    tenant_id: UUID,
    channel_account_id: UUID,
    account_config: Mapping[str, Any] | None = None,
    warehouse_override: UUID | None = None,
) -> UUID:
    """Resolve the warehouse a channel account's imported orders should reserve stock in.

    Precedence: explicit ``channel_accounts.config.warehouse_id`` -> caller override -> the
    tenant's oldest active warehouse. The historical ``channel_account_id`` fallback is only
    kept for legacy tenants that have no ``warehouses`` row at all, so an imported order can
    never silently reserve against a warehouse that does not exist.
    """
    configured = (account_config or {}).get("warehouse_id")
    if configured:
        try:
            return UUID(str(configured))
        except ValueError:
            logger.warning(
                "reconciliation_warehouse_config_invalid",
                tenant_id=str(tenant_id),
                channel_account_id=str(channel_account_id),
                configured_warehouse_id=str(configured),
            )
    if warehouse_override is not None:
        return warehouse_override
    active_warehouse = await session.scalar(
        text(
            "SELECT id FROM warehouses WHERE tenant_id = :tid AND is_active = true "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        ),
        {"tid": tenant_id},
    )
    if active_warehouse is not None:
        return UUID(str(active_warehouse))
    logger.warning(
        "reconciliation_warehouse_legacy_fallback",
        tenant_id=str(tenant_id),
        channel_account_id=str(channel_account_id),
        fallback_warehouse_id=str(channel_account_id),
    )
    return channel_account_id


async def reconcile_channel_account_orders(
    session: AsyncSession,
    tenant_id: UUID,
    channel_account_id: UUID,
    channel_type: ChannelType,
    adapter: CommercePlatformAdapter,
    default_warehouse_id: UUID,
    since: datetime,
) -> dict[str, int]:
    """Pull orders since `since`, import missing ones and refresh drifted ones.

    Every order is ingested inside its own savepoint: an oversold or otherwise rejected order
    rolls back to the savepoint (no orphan ``orders``/``order_items`` rows) while the rest of
    the batch keeps committing.
    """
    stats = {"recovered": 0, "updated": 0, "failed": 0, "scanned": 0}
    sync = EcommerceOrderSyncService()
    cursor: str | None = None
    while True:
        orders, cursor = await adapter.fetch_orders_updated_since(since, cursor)
        for order in orders:
            stats["scanned"] += 1
            try:
                async with session.begin_nested():
                    _, created = await sync.ingest_normalized_order(
                        session,
                        tenant_id=tenant_id,
                        channel_account_id=channel_account_id,
                        default_warehouse_id=default_warehouse_id,
                        order_dto=order,
                        actor_id=f"reconciliation:{channel_type.value}",
                    )
            except AppError as exc:
                stats["failed"] += 1
                logger.warning(
                    "reconciliation_order_rolled_back",
                    tenant_id=str(tenant_id),
                    channel_account_id=str(channel_account_id),
                    external_order_id=order.external_order_id,
                    code=exc.code,
                    status_code=exc.status_code,
                )
                continue
            except IntegrityError as exc:
                stats["failed"] += 1
                logger.warning(
                    "reconciliation_order_rolled_back",
                    tenant_id=str(tenant_id),
                    channel_account_id=str(channel_account_id),
                    external_order_id=order.external_order_id,
                    code="ERR_ORDER_INTEGRITY_CONFLICT",
                    error=type(exc.orig).__name__,
                )
                continue
            stats["recovered" if created else "updated"] += 1
        if not cursor:
            break
    await record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_type="system_cron",
        actor_id=f"reconciliation.{channel_type.value}",
        action="ecommerce.orders_reconciled",
        resource_type="channel_account",
        resource_id=channel_account_id,
        after_state=dict(stats),
    )
    return stats


async def _adapter_credentials(
    session: AsyncSession, tenant_id: UUID, channel_account_id: UUID, channel_type: ChannelType
) -> dict[str, Any]:
    from erp_core.channels.ecommerce.base import CredentialVaultHelper

    vault = CredentialVaultHelper()
    for credential_type in CREDENTIAL_TYPES[channel_type]:
        try:
            data, _ = await vault.load_secret_json(
                session, tenant_id, channel_account_id, credential_type
            )
            return data
        except AppError:
            continue
    raise AppError(
        slug="credential-not-found",
        status_code=404,
        code="ERR_CREDENTIAL_NOT_FOUND",
        title="Credential Not Found",
        detail=f"No stored credential for {channel_type.value} account '{channel_account_id}'.",
    )


async def _instantiate_adapter(
    channel_type: ChannelType,
    credentials: dict[str, Any],
    warehouse_id: UUID | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CommercePlatformAdapter:
    from erp_core.channels.ecommerce.listing_dispatcher import build_marketplace_adapter
    from erp_core.channels.ecommerce.shopify import ShopifyGraphqlAdapter
    from erp_core.channels.ecommerce.shoplazza import ShoplazzaAdapter
    from erp_core.channels.ecommerce.shopline import ShoplineAdapter
    from erp_core.channels.ecommerce.tiktok_shop import TikTokShopCoreAdapter
    from erp_core.channels.ecommerce.woocommerce import WooCommerceAdapter

    del warehouse_id
    domain = str(credentials.get("shop_domain") or credentials.get("store_domain") or "")
    if channel_type is ChannelType.SHOPIFY:
        return ShopifyGraphqlAdapter(
            shop_domain=domain,
            access_token=str(credentials.get("access_token") or ""),
            webhook_secret=str(credentials.get("client_secret") or ""),
            auth_mode=str(credentials.get("auth_mode") or "oauth"),
            client_id=credentials.get("client_id"),
            client_secret=credentials.get("client_secret"),
        )
    if channel_type is ChannelType.WOOCOMMERCE:
        return WooCommerceAdapter(
            store_url=str(credentials.get("store_url") or f"https://{domain}"),
            consumer_key=str(credentials.get("consumer_key") or ""),
            consumer_secret=str(credentials.get("consumer_secret") or ""),
            webhook_secret=str(credentials.get("webhook_secret") or ""),
        )
    if channel_type is ChannelType.SHOPLINE:
        return ShoplineAdapter(
            store_domain=domain,
            access_token=str(credentials.get("access_token") or ""),
            webhook_secret=str(credentials.get("webhook_secret") or ""),
        )
    if channel_type is ChannelType.SHOPLAZZA:
        return ShoplazzaAdapter(
            store_domain=domain,
            access_token=str(credentials.get("access_token") or ""),
            webhook_secret=str(credentials.get("client_secret") or ""),
        )
    if channel_type is ChannelType.TIKTOK_SHOP:
        return TikTokShopCoreAdapter(
            app_key=str(credentials.get("app_key") or ""),
            app_secret=str(credentials.get("app_secret") or ""),
            access_token=str(credentials.get("access_token") or ""),
            shop_cipher=str(credentials.get("shop_cipher") or ""),
            app_mode=str(credentials.get("app_mode") or "seller_byok"),
        )
    # The eight global marketplaces (Shopee / Lazada / Temu / SHEIN / AliExpress / Ozon /
    # Walmart / Mercado Libre) share one credential-to-adapter mapping with the live listing
    # dispatcher — together with Amazon SP-API / eBay Sell / Alibaba.com ICBU — so the same
    # vaulted secret drives both publishing and order reconciliation.
    marketplace_adapter = build_marketplace_adapter(
        channel_type.value, credentials, transport=transport
    )
    if marketplace_adapter is not None:
        return marketplace_adapter
    raise AppError(
        slug="unsupported-ecommerce-channel",
        status_code=422,
        code="ERR_UNSUPPORTED_ECOMMERCE_CHANNEL",
        title="Unsupported Ecommerce Channel",
        detail=f"Channel '{channel_type.value}' has no reconciliation adapter.",
    )


async def reconcile_tenant_orders(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    warehouse_override: UUID | None = None,
) -> dict[str, int]:
    totals = {"recovered": 0, "updated": 0, "failed": 0, "scanned": 0, "accounts": 0}
    async with tenant_session(session_factory, tenant_id) as session:
        accounts = (
            await session.execute(
                text(
                    "SELECT id, channel_type, config FROM channel_accounts "
                    "WHERE tenant_id = :tid AND status = 'active' "
                    "AND channel_type = ANY(CAST(:types AS text[])) ORDER BY id"
                ),
                {"tid": tenant_id, "types": [channel.value for channel in ECOMMERCE_CHANNELS]},
            )
        ).all()
        for account in accounts:
            channel_type = ChannelType(str(account.channel_type))
            try:
                credentials = await _adapter_credentials(
                    session, tenant_id, account.id, channel_type
                )
            except AppError as exc:
                logger.warning(
                    "reconciliation_credentials_missing",
                    tenant_id=str(tenant_id),
                    channel_account_id=str(account.id),
                    code=exc.code,
                )
                continue
            warehouse_id = await resolve_default_warehouse_id(
                session,
                tenant_id=tenant_id,
                channel_account_id=account.id,
                account_config=account.config,
                warehouse_override=warehouse_override,
            )
            try:
                adapter = await _instantiate_adapter(channel_type, credentials, warehouse_id)
            except AppError as exc:
                # An incomplete credential blob must not abort the whole tenant sweep.
                logger.warning(
                    "reconciliation_adapter_unavailable",
                    tenant_id=str(tenant_id),
                    channel_account_id=str(account.id),
                    channel_type=channel_type.value,
                    code=exc.code,
                )
                continue
            stats = await reconcile_channel_account_orders(
                session,
                tenant_id=tenant_id,
                channel_account_id=account.id,
                channel_type=channel_type,
                adapter=adapter,
                default_warehouse_id=warehouse_id,
                since=datetime.now(UTC) - REPLAY_WINDOW,
            )
            totals["accounts"] += 1
            for key in ("recovered", "updated", "failed", "scanned"):
                totals[key] += stats[key]
    return totals


async def reconcile_ecommerce_orders_cron() -> dict[str, int]:
    """Every 15 minutes: sweep all tenants and repair missed or drifted storefront orders."""
    settings = get_settings()
    directory_engine = create_migrator_engine(settings)
    runtime_engine = create_runtime_engine(settings)
    totals = {
        "recovered": 0,
        "updated": 0,
        "failed": 0,
        "scanned": 0,
        "accounts": 0,
        "tenants": 0,
    }
    try:
        runtime_factory = create_session_factory(runtime_engine)
        async with system_session(create_session_factory(directory_engine)) as directory:
            tenant_ids = [
                row.id
                for row in (
                    await directory.execute(text("SELECT id FROM tenants WHERE is_active"))
                ).all()
            ]
        for tenant_id in tenant_ids:
            stats = await reconcile_tenant_orders(runtime_factory, tenant_id)
            totals["tenants"] += 1
            for key in ("recovered", "updated", "failed", "scanned", "accounts"):
                totals[key] += stats[key]
        return totals
    finally:
        await runtime_engine.dispose()
        await directory_engine.dispose()
