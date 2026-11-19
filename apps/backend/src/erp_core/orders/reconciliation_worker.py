"""15-minute incremental reconciliation of storefront orders (missed webhooks/drift)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
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
)
CREDENTIAL_TYPES = {
    ChannelType.SHOPIFY: ("shopify_custom_app", "oauth_token"),
    ChannelType.WOOCOMMERCE: ("woocommerce_keys",),
    ChannelType.SHOPLINE: ("shopline_app",),
    ChannelType.SHOPLAZZA: ("shoplazza_app",),
    ChannelType.TIKTOK_SHOP: ("tiktok_shop_app",),
}
REPLAY_WINDOW = timedelta(minutes=30)


async def reconcile_channel_account_orders(
    session: AsyncSession,
    tenant_id: UUID,
    channel_account_id: UUID,
    channel_type: ChannelType,
    adapter: CommercePlatformAdapter,
    default_warehouse_id: UUID,
    since: datetime,
) -> dict[str, int]:
    """Pull orders since `since`, import missing ones and refresh drifted ones."""
    stats = {"recovered": 0, "updated": 0, "scanned": 0}
    sync = EcommerceOrderSyncService()
    cursor: str | None = None
    while True:
        orders, cursor = await adapter.fetch_orders_updated_since(since, cursor)
        for order in orders:
            stats["scanned"] += 1
            try:
                _, created = await sync.ingest_normalized_order(
                    session,
                    tenant_id=tenant_id,
                    channel_account_id=channel_account_id,
                    default_warehouse_id=default_warehouse_id,
                    order_dto=order,
                    actor_id=f"reconciliation:{channel_type.value}",
                )
            except AppError as exc:
                logger.warning(
                    "reconciliation_order_skipped",
                    tenant_id=str(tenant_id),
                    external_order_id=order.external_order_id,
                    code=exc.code,
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
    channel_type: ChannelType, credentials: dict[str, Any], warehouse_id: UUID | None
) -> CommercePlatformAdapter:
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
    totals = {"recovered": 0, "updated": 0, "scanned": 0, "accounts": 0}
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
            configured = (account.config or {}).get("warehouse_id")
            warehouse_id = (
                UUID(str(configured)) if configured else (warehouse_override or account.id)
            )
            adapter = await _instantiate_adapter(channel_type, credentials, warehouse_id)
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
            for key in ("recovered", "updated", "scanned"):
                totals[key] += stats[key]
    return totals


async def reconcile_ecommerce_orders_cron() -> dict[str, int]:
    """Every 15 minutes: sweep all tenants and repair missed or drifted storefront orders."""
    settings = get_settings()
    directory_engine = create_migrator_engine(settings)
    runtime_engine = create_runtime_engine(settings)
    totals = {"recovered": 0, "updated": 0, "scanned": 0, "accounts": 0, "tenants": 0}
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
            for key in ("recovered", "updated", "scanned", "accounts"):
                totals[key] += stats[key]
        return totals
    finally:
        await runtime_engine.dispose()
        await directory_engine.dispose()
