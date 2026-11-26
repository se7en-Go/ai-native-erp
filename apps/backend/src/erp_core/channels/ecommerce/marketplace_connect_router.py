"""Marketplace onboarding for the global marketplace fleet.

Covers Alibaba International, Amazon SP-API, eBay plus the eight global marketplaces
(Shopee, Lazada, Temu, SHEIN, AliExpress, Ozon, Walmart, Mercado Libre).

Every endpoint follows the same fail-closed contract as ``connectors_router``:

* the caller must be ``OWNER`` or ``ADMIN`` of the active tenant;
* channel credentials are envelope-encrypted with
  ``AAD = f"{tenant_id}:{channel_account_id}:{cred_type}"`` before they touch
  ``channel_credentials``;
* the response DTO never carries a plaintext secret back to the browser.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.ecommerce.base import CredentialVaultHelper, register_channel_routing
from erp_core.channels.ecommerce.listing_dispatcher import build_marketplace_adapter
from erp_core.orders.reconciliation_worker import (
    reconcile_channel_account_orders,
    resolve_default_warehouse_id,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/api/v1/channels", tags=["marketplace-connect"])

_ADMIN_ROLES = (UserRole.OWNER, UserRole.ADMIN)
AdminPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_ADMIN_ROLES))]

_SYNC_ROLES = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)
SyncPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_SYNC_ROLES))]

ALIBABA_INTL_CREDENTIAL_TYPE = "alibaba_intl_app"
AMAZON_CREDENTIAL_TYPE = "amazon_sp_api"
EBAY_CREDENTIAL_TYPE = "ebay_api"
SHOPEE_CREDENTIAL_TYPE = "shopee_app"
LAZADA_CREDENTIAL_TYPE = "lazada_app"
TEMU_CREDENTIAL_TYPE = "temu_app"
SHEIN_CREDENTIAL_TYPE = "shein_app"
ALIEXPRESS_CREDENTIAL_TYPE = "aliexpress_app"
OZON_CREDENTIAL_TYPE = "ozon_app"
WALMART_CREDENTIAL_TYPE = "walmart_app"
MERCADO_LIBRE_CREDENTIAL_TYPE = "mercado_libre_app"

ALIBABA_INTL_CHANNEL_TYPE = "alibaba_email"
AMAZON_CHANNEL_TYPE = "amazon_sp"
EBAY_CHANNEL_TYPE = "ebay"
SHOPEE_CHANNEL_TYPE = "shopee"
LAZADA_CHANNEL_TYPE = "lazada"
TEMU_CHANNEL_TYPE = "temu"
SHEIN_CHANNEL_TYPE = "shein"
ALIEXPRESS_CHANNEL_TYPE = "aliexpress"
OZON_CHANNEL_TYPE = "ozon"
WALMART_CHANNEL_TYPE = "walmart"
MERCADO_LIBRE_CHANNEL_TYPE = "mercado_libre"

DEFAULT_AMAZON_MARKETPLACE_ID = "ATVPDKIKX0DER"
DEFAULT_EBAY_SITE_ID = "EBAY_US"
DEFAULT_SHOPEE_REGION = "GLOBAL"
DEFAULT_LAZADA_REGION = "SG"
DEFAULT_TEMU_REGION = "US"
DEFAULT_SHEIN_REGION = "US"
DEFAULT_MERCADO_LIBRE_SITE_ID = "MLM"
DEFAULT_OZON_FULFILLMENT_MODE: Final = "fbs"

#: Channels with a real order-feed adapter behind the on-demand sync endpoint.
SYNC_SUPPORTED_CHANNEL_TYPES: frozenset[str] = frozenset(
    {
        ALIBABA_INTL_CHANNEL_TYPE,
        AMAZON_CHANNEL_TYPE,
        EBAY_CHANNEL_TYPE,
        SHOPEE_CHANNEL_TYPE,
        LAZADA_CHANNEL_TYPE,
        TEMU_CHANNEL_TYPE,
        SHEIN_CHANNEL_TYPE,
        ALIEXPRESS_CHANNEL_TYPE,
        OZON_CHANNEL_TYPE,
        WALMART_CHANNEL_TYPE,
        MERCADO_LIBRE_CHANNEL_TYPE,
    }
)

CREDENTIAL_TYPES: dict[str, str] = {
    ALIBABA_INTL_CHANNEL_TYPE: ALIBABA_INTL_CREDENTIAL_TYPE,
    AMAZON_CHANNEL_TYPE: AMAZON_CREDENTIAL_TYPE,
    EBAY_CHANNEL_TYPE: EBAY_CREDENTIAL_TYPE,
    SHOPEE_CHANNEL_TYPE: SHOPEE_CREDENTIAL_TYPE,
    LAZADA_CHANNEL_TYPE: LAZADA_CREDENTIAL_TYPE,
    TEMU_CHANNEL_TYPE: TEMU_CREDENTIAL_TYPE,
    SHEIN_CHANNEL_TYPE: SHEIN_CREDENTIAL_TYPE,
    ALIEXPRESS_CHANNEL_TYPE: ALIEXPRESS_CREDENTIAL_TYPE,
    OZON_CHANNEL_TYPE: OZON_CREDENTIAL_TYPE,
    WALMART_CHANNEL_TYPE: WALMART_CREDENTIAL_TYPE,
    MERCADO_LIBRE_CHANNEL_TYPE: MERCADO_LIBRE_CREDENTIAL_TYPE,
}


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _credential_vault() -> CredentialVaultHelper:
    """Indirection seam so credential storage can be asserted in unit tests."""
    return CredentialVaultHelper()


class AlibabaIntlConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    app_key: str = Field(min_length=1, max_length=256)
    app_secret: str = Field(min_length=8, max_length=512)
    inquiry_email: str | None = Field(default=None, max_length=320)
    visibility_scope: Literal["shared", "private"] = "shared"


class AmazonConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    seller_id: str = Field(min_length=1, max_length=128)
    marketplace_id: str = Field(
        default=DEFAULT_AMAZON_MARKETPLACE_ID, min_length=1, max_length=64
    )
    lwa_client_id: str = Field(min_length=1, max_length=256)
    lwa_client_secret: str = Field(min_length=8, max_length=512)
    refresh_token: str = Field(min_length=8, max_length=4096)


class EbayConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    seller_username: str = Field(min_length=1, max_length=128)
    site_id: str = Field(default=DEFAULT_EBAY_SITE_ID, min_length=1, max_length=32)
    app_id: str = Field(min_length=1, max_length=256)
    cert_id: str = Field(min_length=8, max_length=512)
    refresh_token: str = Field(min_length=8, max_length=4096)
    sandbox: bool = False


class ShopeeConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    shop_id: str = Field(min_length=1, max_length=128)
    partner_id: str = Field(min_length=1, max_length=64)
    partner_key: str = Field(min_length=8, max_length=512)
    access_token: str = Field(min_length=8, max_length=4096)
    region: str = Field(default=DEFAULT_SHOPEE_REGION, min_length=2, max_length=32)
    fulfillment_mode: Literal["standard", "full_managed", "light_export"] = "standard"


class LazadaConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    seller_id: str = Field(min_length=1, max_length=128)
    app_key: str = Field(min_length=1, max_length=256)
    app_secret: str = Field(min_length=8, max_length=512)
    access_token: str = Field(min_length=8, max_length=4096)
    region: str = Field(default=DEFAULT_LAZADA_REGION, min_length=2, max_length=32)
    fulfillment_mode: Literal["standard", "full_managed"] = "standard"


class TemuConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    mall_id: str = Field(min_length=1, max_length=128)
    app_key: str = Field(min_length=1, max_length=256)
    app_secret: str = Field(min_length=8, max_length=512)
    access_token: str = Field(min_length=8, max_length=4096)
    region: str = Field(default=DEFAULT_TEMU_REGION, min_length=2, max_length=32)
    fulfillment_mode: Literal["standard", "semi_managed", "local"] = "standard"


class SheinConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    seller_id: str = Field(min_length=1, max_length=128)
    open_key_id: str = Field(min_length=1, max_length=256)
    secret_key: str = Field(min_length=8, max_length=512)
    region: str = Field(default=DEFAULT_SHEIN_REGION, min_length=2, max_length=32)
    fulfillment_mode: Literal["standard", "semi_managed"] = "standard"


class AliExpressConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    seller_id: str = Field(min_length=1, max_length=128)
    app_key: str = Field(min_length=1, max_length=256)
    app_secret: str = Field(min_length=8, max_length=512)
    access_token: str = Field(min_length=8, max_length=4096)
    fulfillment_mode: Literal["standard", "managed"] = "standard"


class OzonConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    client_id: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=8, max_length=512)
    warehouse_id: str = Field(default="", max_length=128)
    fulfillment_mode: Literal["fbs", "fbo"] = DEFAULT_OZON_FULFILLMENT_MODE


class WalmartConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    seller_id: str = Field(min_length=1, max_length=128)
    client_id: str = Field(min_length=1, max_length=256)
    client_secret: str = Field(min_length=8, max_length=512)
    access_token: str | None = Field(default=None, max_length=4096)
    fulfillment_mode: Literal["standard", "wfs"] = "standard"


class MercadoLibreConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    seller_id: str = Field(min_length=1, max_length=128)
    access_token: str = Field(min_length=8, max_length=4096)
    site_id: str = Field(default=DEFAULT_MERCADO_LIBRE_SITE_ID, min_length=3, max_length=8)
    refresh_token: str = Field(default="", max_length=4096)
    fulfillment_mode: Literal["standard", "full_managed"] = "standard"


class MarketplaceConnectionRead(BaseModel):
    """Non-secret projection of a freshly connected marketplace account."""

    channel_account_id: UUID
    channel_type: str
    external_account_id: str
    display_name: str
    credential_type: str
    status: str = "active"
    visibility_scope: str | None = None


#: Public name of the connect contract — never carries a plaintext secret.
ConnectResponse = MarketplaceConnectionRead


class MarketplaceOrderSyncResult(BaseModel):
    """Outcome of one on-demand order reconciliation for a marketplace account."""

    channel_account_id: UUID
    channel_type: str
    scanned: int
    recovered: int
    updated: int
    failed: int


async def _upsert_channel_account(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    channel_type: str,
    external_account_id: str,
    display_name: str,
    config: dict[str, Any],
) -> UUID:
    await session.execute(
        text(
            """
            INSERT INTO channel_accounts (
                id, tenant_id, channel_type, external_account_id, display_name, status, config
            ) VALUES (:id, :tid, :ctype, :ext, :name, 'active', CAST(:cfg AS jsonb))
            ON CONFLICT (tenant_id, channel_type, external_account_id)
            DO UPDATE SET
                display_name = EXCLUDED.display_name,
                status = 'active',
                config = EXCLUDED.config,
                updated_at = now()
            """
        ),
        {
            "id": uuid4(),
            "tid": tenant_id,
            "ctype": channel_type,
            "ext": external_account_id,
            "name": display_name,
            "cfg": json.dumps(config, default=str),
        },
    )
    account_id = await session.scalar(
        text(
            "SELECT id FROM channel_accounts WHERE tenant_id = :tid "
            "AND channel_type = :ctype AND external_account_id = :ext"
        ),
        {"tid": tenant_id, "ctype": channel_type, "ext": external_account_id},
    )
    if account_id is None:
        raise AppError(
            slug="channel-account-unavailable",
            status_code=500,
            code="ERR_CHANNEL_ACCOUNT_UNAVAILABLE",
            title="Channel Account Unavailable",
            detail="Marketplace channel account could not be provisioned.",
        )
    return UUID(str(account_id))


async def provision_marketplace_account(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    channel_type: str,
    external_account_id: str,
    display_name: str,
    secret_payload: dict[str, Any],
    config: dict[str, Any],
    actor_id: str,
    visibility_scope: str | None = None,
) -> MarketplaceConnectionRead:
    """Create/refresh a channel account and seal its credentials under an AAD scope."""
    credential_type = CREDENTIAL_TYPES[channel_type]
    account_id = await _upsert_channel_account(
        session,
        tenant_id=tenant_id,
        channel_type=channel_type,
        external_account_id=external_account_id,
        display_name=display_name,
        config=config,
    )
    await register_channel_routing(
        session,
        channel_type=channel_type,
        external_account_id=external_account_id,
        tenant_id=tenant_id,
        channel_account_id=account_id,
    )
    await _credential_vault().save_secret_json(
        session,
        tenant_id=tenant_id,
        channel_account_id=account_id,
        credential_type=credential_type,
        payload=secret_payload,
    )
    await record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_type="human_user",
        actor_id=actor_id,
        action="channel.marketplace.connected",
        resource_type="channel_account",
        resource_id=account_id,
        after_state={
            "channel_type": channel_type,
            "external_account_id": external_account_id,
            "credential_type": credential_type,
        },
    )
    return MarketplaceConnectionRead(
        channel_account_id=account_id,
        channel_type=channel_type,
        external_account_id=external_account_id,
        display_name=display_name,
        credential_type=credential_type,
        visibility_scope=visibility_scope,
    )


async def _load_marketplace_account(
    session: AsyncSession, tenant_id: UUID, channel_account_id: UUID
) -> tuple[str, dict[str, Any]]:
    """Load one tenant-scoped channel account or fail closed with a 404."""
    row = (
        await session.execute(
            text(
                "SELECT channel_type, config FROM channel_accounts "
                "WHERE tenant_id = :tid AND id = :caid"
            ),
            {"tid": tenant_id, "caid": channel_account_id},
        )
    ).one_or_none()
    if row is None:
        raise AppError(
            slug="channel-account-not-found",
            status_code=404,
            code="ERR_CHANNEL_ACCOUNT_NOT_FOUND",
            title="Channel Account Not Found",
            detail=f"Channel account '{channel_account_id}' does not exist for this tenant.",
        )
    config = row.config if isinstance(row.config, dict) else {}
    return str(row.channel_type), config


@router.post(
    "/marketplace/{channel_account_id}/sync-orders",
    response_model=MarketplaceOrderSyncResult,
)
async def sync_marketplace_orders(
    request: Request,
    channel_account_id: UUID,
    principal: SyncPrincipal,
    lookback_hours: int = Query(default=24, ge=1, le=720),
) -> MarketplaceOrderSyncResult:
    """Pull a marketplace's order feed on demand and ingest missing/drifted orders.

    Used to repair a missed webhook without waiting for the 15-minute cron: the same
    ``reconcile_channel_account_orders`` worker is reused so ingestion stays idempotent and
    fully audited.
    """
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        channel_type, account_config = await _load_marketplace_account(
            session, principal.tenant_id, channel_account_id
        )
        if channel_type not in SYNC_SUPPORTED_CHANNEL_TYPES:
            raise AppError(
                slug="marketplace-order-sync-unsupported",
                status_code=422,
                code="ERR_MARKETPLACE_ORDER_SYNC_UNSUPPORTED",
                title="Marketplace Order Sync Unsupported",
                detail=(
                    f"Channel type '{channel_type}' has no live order feed integration; "
                    f"supported channels are {', '.join(sorted(SYNC_SUPPORTED_CHANNEL_TYPES))}."
                ),
            )
        credentials, _expires_at = await _credential_vault().load_secret_json(
            session,
            principal.tenant_id,
            channel_account_id,
            CREDENTIAL_TYPES[channel_type],
        )
        transport = getattr(request.app.state, "marketplace_http_transport", None)
        adapter = build_marketplace_adapter(channel_type, credentials, transport=transport)
        if adapter is None:  # pragma: no cover - guarded by SYNC_SUPPORTED_CHANNEL_TYPES
            raise AppError(
                slug="marketplace-order-sync-unsupported",
                status_code=422,
                code="ERR_MARKETPLACE_ORDER_SYNC_UNSUPPORTED",
                title="Marketplace Order Sync Unsupported",
                detail=f"Channel type '{channel_type}' has no order adapter.",
            )
        warehouse_id = await resolve_default_warehouse_id(
            session,
            principal.tenant_id,
            channel_account_id,
            account_config,
        )
        stats = await reconcile_channel_account_orders(
            session,
            tenant_id=principal.tenant_id,
            channel_account_id=channel_account_id,
            channel_type=ChannelType(channel_type),
            adapter=adapter,
            default_warehouse_id=warehouse_id,
            since=datetime.now(UTC) - timedelta(hours=lookback_hours),
        )
    return MarketplaceOrderSyncResult(
        channel_account_id=channel_account_id,
        channel_type=channel_type,
        scanned=stats["scanned"],
        recovered=stats["recovered"],
        updated=stats["updated"],
        failed=stats["failed"],
    )


@router.post(
    "/alibaba-intl/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_alibaba_intl(
    request: Request,
    payload: AlibabaIntlConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    store_id = payload.store_id.strip()
    display_name = payload.display_name.strip()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=ALIBABA_INTL_CHANNEL_TYPE,
            external_account_id=store_id,
            display_name=display_name,
            secret_payload={
                "store_id": store_id,
                "app_key": payload.app_key,
                "app_secret": payload.app_secret,
                "inquiry_email": payload.inquiry_email or "",
                "visibility_scope": payload.visibility_scope,
            },
            config={
                "store_id": store_id,
                "inquiry_email": payload.inquiry_email or "",
                "visibility_scope": payload.visibility_scope,
            },
            actor_id=str(principal.user_id),
            visibility_scope=payload.visibility_scope,
        )


@router.post(
    "/amazon/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_amazon(
    request: Request,
    payload: AmazonConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    seller_id = payload.seller_id.strip()
    marketplace_id = payload.marketplace_id.strip().upper()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=AMAZON_CHANNEL_TYPE,
            external_account_id=seller_id,
            display_name=payload.display_name.strip(),
            secret_payload={
                "seller_id": seller_id,
                "marketplace_id": marketplace_id,
                "lwa_client_id": payload.lwa_client_id,
                "lwa_client_secret": payload.lwa_client_secret,
                "refresh_token": payload.refresh_token,
            },
            config={"seller_id": seller_id, "marketplace_id": marketplace_id},
            actor_id=str(principal.user_id),
        )


@router.post(
    "/ebay/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_ebay(
    request: Request,
    payload: EbayConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    seller_username = payload.seller_username.strip()
    site_id = payload.site_id.strip().upper().replace("-", "_")
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=EBAY_CHANNEL_TYPE,
            external_account_id=seller_username,
            display_name=payload.display_name.strip(),
            secret_payload={
                "seller_username": seller_username,
                "site_id": site_id,
                "app_id": payload.app_id,
                "cert_id": payload.cert_id,
                "refresh_token": payload.refresh_token,
                "sandbox": payload.sandbox,
            },
            config={
                "seller_username": seller_username,
                "site_id": site_id,
                "sandbox": payload.sandbox,
            },
            actor_id=str(principal.user_id),
        )


@router.post(
    "/shopee/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_shopee(
    request: Request,
    payload: ShopeeConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    shop_id = payload.shop_id.strip()
    partner_id = payload.partner_id.strip()
    region = payload.region.strip().upper()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=SHOPEE_CHANNEL_TYPE,
            external_account_id=shop_id,
            display_name=payload.display_name.strip(),
            secret_payload={
                "shop_id": shop_id,
                "partner_id": partner_id,
                "partner_key": payload.partner_key,
                "access_token": payload.access_token,
                "region": region,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            config={
                "shop_id": shop_id,
                "partner_id": partner_id,
                "region": region,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            actor_id=str(principal.user_id),
        )


@router.post(
    "/lazada/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_lazada(
    request: Request,
    payload: LazadaConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    seller_id = payload.seller_id.strip()
    region = payload.region.strip().upper()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=LAZADA_CHANNEL_TYPE,
            external_account_id=seller_id,
            display_name=payload.display_name.strip(),
            secret_payload={
                "seller_id": seller_id,
                "app_key": payload.app_key,
                "app_secret": payload.app_secret,
                "access_token": payload.access_token,
                "region": region,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            config={
                "seller_id": seller_id,
                "region": region,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            actor_id=str(principal.user_id),
        )


@router.post(
    "/temu/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_temu(
    request: Request,
    payload: TemuConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    mall_id = payload.mall_id.strip()
    region = payload.region.strip().upper()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=TEMU_CHANNEL_TYPE,
            external_account_id=mall_id,
            display_name=payload.display_name.strip(),
            secret_payload={
                "mall_id": mall_id,
                "app_key": payload.app_key,
                "app_secret": payload.app_secret,
                "access_token": payload.access_token,
                "region": region,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            config={
                "mall_id": mall_id,
                "region": region,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            actor_id=str(principal.user_id),
        )


@router.post(
    "/shein/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_shein(
    request: Request,
    payload: SheinConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    seller_id = payload.seller_id.strip()
    region = payload.region.strip().upper()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=SHEIN_CHANNEL_TYPE,
            external_account_id=seller_id,
            display_name=payload.display_name.strip(),
            secret_payload={
                "seller_id": seller_id,
                "open_key_id": payload.open_key_id,
                "secret_key": payload.secret_key,
                "region": region,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            config={
                "seller_id": seller_id,
                "region": region,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            actor_id=str(principal.user_id),
        )


@router.post(
    "/aliexpress/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_aliexpress(
    request: Request,
    payload: AliExpressConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    seller_id = payload.seller_id.strip()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=ALIEXPRESS_CHANNEL_TYPE,
            external_account_id=seller_id,
            display_name=payload.display_name.strip(),
            secret_payload={
                "seller_id": seller_id,
                "app_key": payload.app_key,
                "app_secret": payload.app_secret,
                "access_token": payload.access_token,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            config={"seller_id": seller_id, "fulfillment_mode": payload.fulfillment_mode},
            actor_id=str(principal.user_id),
        )


@router.post(
    "/ozon/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_ozon(
    request: Request,
    payload: OzonConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    client_id = payload.client_id.strip()
    marketplace_warehouse_id = payload.warehouse_id.strip()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=OZON_CHANNEL_TYPE,
            external_account_id=client_id,
            display_name=payload.display_name.strip(),
            secret_payload={
                "client_id": client_id,
                "api_key": payload.api_key,
                "warehouse_id": marketplace_warehouse_id,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            # Ozon's ``warehouse_id`` is a marketplace-side identifier, so it must never
            # shadow ``config.warehouse_id`` (the tenant's own ERP warehouse UUID).
            config={
                "client_id": client_id,
                "ozon_warehouse_id": marketplace_warehouse_id,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            actor_id=str(principal.user_id),
        )


@router.post(
    "/walmart/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_walmart(
    request: Request,
    payload: WalmartConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    seller_id = payload.seller_id.strip()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=WALMART_CHANNEL_TYPE,
            external_account_id=seller_id,
            display_name=payload.display_name.strip(),
            secret_payload={
                "seller_id": seller_id,
                "client_id": payload.client_id,
                "client_secret": payload.client_secret,
                "access_token": (payload.access_token or "").strip(),
                "fulfillment_mode": payload.fulfillment_mode,
            },
            config={"seller_id": seller_id, "fulfillment_mode": payload.fulfillment_mode},
            actor_id=str(principal.user_id),
        )


@router.post(
    "/mercado-libre/connect",
    response_model=MarketplaceConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def connect_mercado_libre(
    request: Request,
    payload: MercadoLibreConnectRequest,
    principal: AdminPrincipal,
) -> MarketplaceConnectionRead:
    seller_id = payload.seller_id.strip()
    site_id = payload.site_id.strip().upper()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        return await provision_marketplace_account(
            session,
            tenant_id=principal.tenant_id,
            channel_type=MERCADO_LIBRE_CHANNEL_TYPE,
            external_account_id=seller_id,
            display_name=payload.display_name.strip(),
            secret_payload={
                "seller_id": seller_id,
                "access_token": payload.access_token,
                "site_id": site_id,
                "refresh_token": payload.refresh_token.strip(),
                "fulfillment_mode": payload.fulfillment_mode,
            },
            config={
                "seller_id": seller_id,
                "site_id": site_id,
                "fulfillment_mode": payload.fulfillment_mode,
            },
            actor_id=str(principal.user_id),
        )


__all__ = [
    "ALIBABA_INTL_CREDENTIAL_TYPE",
    "ALIEXPRESS_CREDENTIAL_TYPE",
    "AMAZON_CREDENTIAL_TYPE",
    "CREDENTIAL_TYPES",
    "ConnectResponse",
    "EBAY_CREDENTIAL_TYPE",
    "LAZADA_CREDENTIAL_TYPE",
    "MERCADO_LIBRE_CREDENTIAL_TYPE",
    "MarketplaceConnectionRead",
    "MarketplaceOrderSyncResult",
    "OZON_CREDENTIAL_TYPE",
    "SHEIN_CREDENTIAL_TYPE",
    "SHOPEE_CREDENTIAL_TYPE",
    "SYNC_SUPPORTED_CHANNEL_TYPES",
    "TEMU_CREDENTIAL_TYPE",
    "WALMART_CREDENTIAL_TYPE",
    "provision_marketplace_account",
    "router",
]
