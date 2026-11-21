"""Marketplace onboarding for Alibaba International, Amazon SP-API, and eBay sellers.

Every endpoint follows the same fail-closed contract as ``connectors_router``:

* the caller must be ``OWNER`` or ``ADMIN`` of the active tenant;
* channel credentials are envelope-encrypted with
  ``AAD = f"{tenant_id}:{channel_account_id}:{cred_type}"`` before they touch
  ``channel_credentials``;
* the response DTO never carries a plaintext secret back to the browser.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.ecommerce.base import CredentialVaultHelper, register_channel_routing
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.audit import record_audit_event
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/api/v1/channels", tags=["marketplace-connect"])

_ADMIN_ROLES = (UserRole.OWNER, UserRole.ADMIN)
AdminPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_ADMIN_ROLES))]

ALIBABA_INTL_CREDENTIAL_TYPE = "alibaba_intl_app"
AMAZON_CREDENTIAL_TYPE = "amazon_sp_api"
EBAY_CREDENTIAL_TYPE = "ebay_api"

ALIBABA_INTL_CHANNEL_TYPE = "alibaba_email"
AMAZON_CHANNEL_TYPE = "amazon_sp"
EBAY_CHANNEL_TYPE = "ebay"

DEFAULT_AMAZON_MARKETPLACE_ID = "ATVPDKIKX0DER"
DEFAULT_EBAY_SITE_ID = "EBAY_US"

CREDENTIAL_TYPES: dict[str, str] = {
    ALIBABA_INTL_CHANNEL_TYPE: ALIBABA_INTL_CREDENTIAL_TYPE,
    AMAZON_CHANNEL_TYPE: AMAZON_CREDENTIAL_TYPE,
    EBAY_CHANNEL_TYPE: EBAY_CREDENTIAL_TYPE,
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


class MarketplaceConnectionRead(BaseModel):
    """Non-secret projection of a freshly connected marketplace account."""

    channel_account_id: UUID
    channel_type: str
    external_account_id: str
    display_name: str
    credential_type: str
    status: str = "active"
    visibility_scope: str | None = None


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
    site_id = payload.site_id.strip().upper()
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
            },
            config={"seller_username": seller_username, "site_id": site_id},
            actor_id=str(principal.user_id),
        )


__all__ = [
    "ALIBABA_INTL_CREDENTIAL_TYPE",
    "AMAZON_CREDENTIAL_TYPE",
    "CREDENTIAL_TYPES",
    "EBAY_CREDENTIAL_TYPE",
    "MarketplaceConnectionRead",
    "provision_marketplace_account",
    "router",
]
