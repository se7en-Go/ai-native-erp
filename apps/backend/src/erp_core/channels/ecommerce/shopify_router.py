"""Shopify OAuth/custom-app onboarding and signed webhook ingress endpoints."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.ecommerce.base import CredentialVaultHelper, register_channel_routing
from erp_core.channels.ecommerce.shopify import (
    build_shopify_oauth_install_url,
    validate_shop_domain,
    verify_shopify_oauth_hmac,
    verify_shopify_webhook_hmac,
)
from erp_core.channels.webhook_delivery import accept_delivery
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(tags=["shopify"])
_admin_roles = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))
CREDENTIAL_TYPE = "shopify_custom_app"


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


class ShopifyCustomAppConnectRequest(BaseModel):
    shop_domain: str = Field(min_length=4, max_length=255)
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: str = Field(min_length=8, max_length=255)
    access_token: str | None = Field(default=None, max_length=255)
    warehouse_id: UUID | None = None


class ShopifyConnectResponse(BaseModel):
    channel_account_id: UUID
    shop_domain: str
    auth_mode: str


async def _upsert_channel_account(
    session: AsyncSession,
    tenant_id: UUID,
    shop_domain: str,
    warehouse_id: UUID | None,
) -> UUID:
    config = {"warehouse_id": str(warehouse_id)} if warehouse_id else {}
    await session.execute(
        text(
            """
            INSERT INTO channel_accounts (
                id, tenant_id, channel_type, external_account_id, display_name, status, config
            ) VALUES (:id, :tid, 'shopify', :ext, :ext, 'active', CAST(:cfg AS jsonb))
            ON CONFLICT (tenant_id, channel_type, external_account_id)
            DO UPDATE SET status = 'active', config = EXCLUDED.config, updated_at = now()
            """
        ),
        {"id": uuid4(), "tid": tenant_id, "ext": shop_domain, "cfg": json.dumps(config)},
    )
    account_id = await session.scalar(
        text(
            "SELECT id FROM channel_accounts WHERE tenant_id = :tid "
            "AND channel_type = 'shopify' AND external_account_id = :ext"
        ),
        {"tid": tenant_id, "ext": shop_domain},
    )
    if account_id is None:
        raise AppError(
            slug="channel-account-unavailable",
            status_code=500,
            code="ERR_CHANNEL_ACCOUNT_UNAVAILABLE",
            title="Channel Account Unavailable",
            detail="Channel account could not be provisioned.",
        )
    await register_channel_routing(
        session,
        channel_type="shopify",
        external_account_id=shop_domain,
        tenant_id=tenant_id,
        channel_account_id=UUID(str(account_id)),
    )
    return UUID(str(account_id))


@router.get("/connectors/shopify/oauth/install")
async def shopify_oauth_install(
    shop: Annotated[str, Query(min_length=4, max_length=255)],
    redirect_uri: Annotated[str, Query(min_length=8, max_length=2048)],
    principal: CurrentPrincipal = _admin_roles,
) -> dict[str, str]:
    return {
        "install_url": build_shopify_oauth_install_url(
            shop, redirect_uri, state=str(principal.tenant_id)
        )
    }


@router.get("/connectors/shopify/oauth/callback", response_model=ShopifyConnectResponse)
async def shopify_oauth_callback(
    request: Request,
    shop: Annotated[str, Query(min_length=4, max_length=255)],
    code: Annotated[str, Query(min_length=1, max_length=512)],
    hmac: Annotated[str, Query(min_length=1, max_length=128)],
    state: Annotated[str, Query(min_length=1, max_length=64)],
    principal: CurrentPrincipal = _admin_roles,
) -> ShopifyConnectResponse:
    secret = os.getenv("SHOPIFY_CLIENT_SECRET", "")
    if not verify_shopify_oauth_hmac(
        {"shop": shop, "code": code, "state": state, "hmac": hmac}, secret
    ):
        raise AppError(
            slug="invalid-shopify-oauth-hmac",
            status_code=401,
            code="ERR_INVALID_SHOPIFY_OAUTH_HMAC",
            title="Invalid Shopify OAuth HMAC",
            detail="OAuth callback verification failed.",
        )
    if state != str(principal.tenant_id):
        raise AppError(
            slug="invalid-shopify-oauth-state",
            status_code=401,
            code="ERR_INVALID_SHOPIFY_OAUTH_STATE",
            title="Invalid Shopify OAuth State",
            detail="OAuth state does not match the session tenant.",
        )
    domain = validate_shop_domain(shop)
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        account_id = await _upsert_channel_account(session, principal.tenant_id, domain, None)
        await CredentialVaultHelper().save_secret_json(
            session,
            tenant_id=principal.tenant_id,
            channel_account_id=account_id,
            credential_type=CREDENTIAL_TYPE,
            payload={
                "shop_domain": domain,
                "auth_mode": "oauth",
                "access_token": code,
                "client_secret": secret,
            },
        )
    return ShopifyConnectResponse(
        channel_account_id=account_id, shop_domain=domain, auth_mode="oauth"
    )


@router.post(
    "/connectors/shopify/custom-app",
    response_model=ShopifyConnectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def connect_shopify_custom_app(
    request: Request,
    payload: ShopifyCustomAppConnectRequest,
    principal: CurrentPrincipal = _admin_roles,
) -> ShopifyConnectResponse:
    domain = validate_shop_domain(payload.shop_domain)
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        account_id = await _upsert_channel_account(
            session, principal.tenant_id, domain, payload.warehouse_id
        )
        await CredentialVaultHelper().save_secret_json(
            session,
            tenant_id=principal.tenant_id,
            channel_account_id=account_id,
            credential_type=CREDENTIAL_TYPE,
            payload={
                "shop_domain": domain,
                "auth_mode": "client_credentials",
                "client_id": payload.client_id,
                "client_secret": payload.client_secret,
                "access_token": payload.access_token or "",
            },
        )
    return ShopifyConnectResponse(
        channel_account_id=account_id, shop_domain=domain, auth_mode="client_credentials"
    )


@router.post("/webhooks/shopify")
async def receive_shopify_webhook(
    request: Request,
    shopify_hmac: Annotated[str | None, Header(alias="X-Shopify-Hmac-Sha256")] = None,
    shopify_topic: Annotated[str | None, Header(alias="X-Shopify-Topic")] = None,
    shopify_shop_domain: Annotated[str | None, Header(alias="X-Shopify-Shop-Domain")] = None,
) -> dict[str, int | str]:
    raw = await request.body()
    if not shopify_shop_domain or not shopify_topic:
        raise AppError(
            slug="invalid-shopify-delivery",
            status_code=400,
            code="ERR_INVALID_SHOPIFY_DELIVERY",
            title="Invalid Shopify Delivery",
            detail="Missing Shopify shop or topic header.",
        )
    domain = validate_shop_domain(shopify_shop_domain)
    tenant_domain = await resolve_and_secret(request, domain)
    tenant_id, account_id, secret = tenant_domain
    if not verify_shopify_webhook_hmac(raw, shopify_hmac or "", secret):
        raise AppError(
            slug="invalid-webhook-signature",
            status_code=401,
            code="ERR_INVALID_WEBHOOK_SIGNATURE",
            title="Invalid Shopify Signature",
            detail="Webhook HMAC verification failed.",
        )
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Payload is not an object")
    except ValueError as exc:
        raise AppError(
            slug="invalid-shopify-delivery",
            status_code=400,
            code="ERR_INVALID_SHOPIFY_DELIVERY",
            title="Invalid Shopify Delivery",
            detail="Expected a JSON object.",
        ) from exc
    await accept_delivery(
        request,
        channel="shopify",
        external_event_id=hashlib.sha256(raw).hexdigest(),
        payload=payload,
        tenant_id=tenant_id,
        external_account_id=domain,
        headers={
            "x-shopify-topic": shopify_topic,
            "x-shopify-shop-domain": domain,
        },
    )
    return {"code": 200, "status": "accepted"}


async def resolve_and_secret(request: Request, domain: str) -> tuple[UUID, UUID, str]:
    """Resolve routing, then decrypt the shop's webhook secret inside its tenant context."""
    from erp_core.channels.ecommerce.base import resolve_routing_tenant

    factory = _session_factory(request)
    async with factory() as lookup:
        route = await resolve_routing_tenant(lookup, "shopify", domain)
    if route is None:
        raise AppError(
            slug="unknown-shop",
            status_code=404,
            code="ERR_UNKNOWN_SHOP",
            title="Unknown Shopify Shop",
            detail="No tenant is connected to this shop domain.",
        )
    tenant_id, account_id = route
    async with tenant_session(factory, tenant_id) as session:
        try:
            data, _ = await CredentialVaultHelper().load_secret_json(
                session, tenant_id, account_id, CREDENTIAL_TYPE
            )
        except AppError:
            data = {}
    secret = str(data.get("client_secret") or os.getenv("SHOPIFY_CLIENT_SECRET", ""))
    return tenant_id, account_id, secret


__all__ = ["router", "receive_shopify_webhook"]
