"""Storefront connector onboarding and signed webhook ingress for non-Shopify platforms."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.ecommerce.base import (
    CredentialVaultHelper,
    register_channel_routing,
    resolve_routing_tenant,
)
from erp_core.channels.ecommerce.shoplazza import (
    verify_shoplazza_oauth_hmac,
    verify_shoplazza_webhook,
)
from erp_core.channels.ecommerce.shopline import verify_shopline_webhook
from erp_core.channels.ecommerce.tiktok_shop import (
    enforce_custom_app_shop_limit,
    verify_tiktok_webhook_signature,
)
from erp_core.channels.ecommerce.woocommerce import (
    build_wc_authorize_url,
    verify_woocommerce_webhook,
)
from erp_core.channels.webhook_delivery import accept_delivery
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(tags=["connectors"])
_admin_roles = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))

CREDENTIAL_TYPES = {
    "woocommerce": "woocommerce_keys",
    "shopline": "shopline_app",
    "shoplazza": "shoplazza_app",
    "tiktok_shop": "tiktok_shop_app",
}
SECRET_ENV = {
    "woocommerce": "WOOCOMMERCE_WEBHOOK_SECRET",
    "shopline": "SHOPLINE_WEBHOOK_SECRET",
    "shoplazza": "SHOPLAZZA_CLIENT_SECRET",
    "tiktok_shop": "TIKTOK_SHOP_APP_SECRET",
}
DEDUP_HEADERS = {
    "woocommerce": "X-WC-Webhook-Delivery-ID",
    "shopline": "X-Shopline-Event-Id",
    "shoplazza": "X-Shoplazza-Deduplication-ID",
    "tiktok_shop": "X-Tts-Event-Id",
}
TOPIC_HEADERS = {
    "woocommerce": "X-WC-Webhook-Topic",
    "shopline": "X-Shopline-Topic",
    "shoplazza": "X-Shoplazza-Topic",
    "tiktok_shop": "X-Tts-Topic",
}
DOMAIN_HEADERS = {
    "woocommerce": "X-WC-Webhook-Source",
    "shopline": "X-Shopline-Shop-Domain",
    "shoplazza": "X-Shoplazza-Shop-Domain",
    "tiktok_shop": "X-Tts-Shop-Domain",
}


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _header(headers: dict[str, str], name: str) -> str:
    lowered = name.lower()
    return headers.get(lowered) or headers.get(name) or ""


async def _webhook_secret(request: Request, platform: str, domain: str) -> str:
    factory = _session_factory(request)
    async with factory() as lookup:
        route = await resolve_routing_tenant(lookup, platform, domain)
    if route is not None:
        tenant_id, account_id = route
        try:
            async with tenant_session(factory, tenant_id) as session:
                data, _ = await CredentialVaultHelper().load_secret_json(
                    session, tenant_id, account_id, CREDENTIAL_TYPES[platform]
                )
        except AppError:
            data = {}
        for key in ("webhook_secret", "client_secret", "app_secret"):
            if data.get(key):
                return str(data[key])
    return os.getenv(SECRET_ENV[platform], "")


def _verify(platform: str, raw: bytes, headers: dict[str, str], secret: str) -> bool:
    if platform == "woocommerce":
        return verify_woocommerce_webhook(raw, headers, secret)
    if platform == "shopline":
        return verify_shopline_webhook(raw, headers, secret)
    return verify_shoplazza_webhook(raw, headers, secret)


async def receive_signed_storefront_webhook(
    request: Request, platform: str
) -> dict[str, int | str | bool]:
    raw = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    domain = _header(headers, DOMAIN_HEADERS[platform]).strip().lower()
    secret = await _webhook_secret(request, platform, domain)
    if not secret or not _verify(platform, raw, headers, secret):
        raise AppError(
            slug="invalid-webhook-signature",
            status_code=401,
            code="ERR_INVALID_WEBHOOK_SIGNATURE",
            title=f"Invalid {platform} Signature",
            detail="Webhook signature verification failed.",
        )
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Payload is not an object")
    except ValueError as exc:
        raise AppError(
            slug="invalid-storefront-delivery",
            status_code=400,
            code="ERR_INVALID_STOREFRONT_DELIVERY",
            title="Invalid Storefront Delivery",
            detail="Expected a JSON object.",
        ) from exc
    dedup_id = _header(headers, DEDUP_HEADERS[platform]) or hashlib.sha256(raw).hexdigest()
    inserted = await accept_delivery(
        request,
        channel=platform,
        external_event_id=dedup_id,
        payload=payload,
        external_account_id=domain or None,
        headers={
            "topic": _header(headers, TOPIC_HEADERS[platform]),
            "shop_domain": domain,
        },
    )
    return {"code": 200, "status": "accepted", "deduplicated": not inserted}


async def _connect_storefront(
    session: AsyncSession,
    tenant_id: UUID,
    platform: str,
    domain: str,
    display_name: str,
    warehouse_id: UUID | None,
    credential_payload: dict[str, Any],
) -> UUID:
    config = {"warehouse_id": str(warehouse_id)} if warehouse_id else {}
    await session.execute(
        text(
            """
            INSERT INTO channel_accounts (
                id, tenant_id, channel_type, external_account_id, display_name, status, config
            ) VALUES (:id, :tid, :ctype, :ext, :name, 'active', CAST(:cfg AS jsonb))
            ON CONFLICT (tenant_id, channel_type, external_account_id)
            DO UPDATE SET status = 'active', config = EXCLUDED.config, updated_at = now()
            """
        ),
        {
            "id": uuid4(),
            "tid": tenant_id,
            "ctype": platform,
            "ext": domain,
            "name": display_name,
            "cfg": json.dumps(config),
        },
    )
    account_id = await session.scalar(
        text(
            "SELECT id FROM channel_accounts WHERE tenant_id = :tid "
            "AND channel_type = :ctype AND external_account_id = :ext"
        ),
        {"tid": tenant_id, "ctype": platform, "ext": domain},
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
        channel_type=platform,
        external_account_id=domain,
        tenant_id=tenant_id,
        channel_account_id=UUID(str(account_id)),
    )
    await CredentialVaultHelper().save_secret_json(
        session,
        tenant_id=tenant_id,
        channel_account_id=UUID(str(account_id)),
        credential_type=CREDENTIAL_TYPES[platform],
        payload=credential_payload,
    )
    return UUID(str(account_id))


class ConnectResponse(BaseModel):
    channel_account_id: UUID
    external_account_id: str


class WooAuthUrlRequest(BaseModel):
    store_url: str = Field(min_length=8, max_length=2048)
    return_url: str = Field(min_length=8, max_length=2048)
    callback_url: str = Field(min_length=8, max_length=2048)


class WooAuthCallbackPayload(BaseModel):
    store_url: str = Field(min_length=8, max_length=2048)
    consumer_key: str = Field(min_length=1, max_length=255)
    consumer_secret: str = Field(min_length=1, max_length=255)
    webhook_secret: str | None = Field(default=None, max_length=255)
    warehouse_id: UUID | None = None


class ShoplineConnectRequest(BaseModel):
    store_domain: str = Field(min_length=4, max_length=255)
    access_token: str = Field(min_length=1, max_length=512)
    app_secret: str = Field(min_length=1, max_length=255)
    warehouse_id: UUID | None = None


class ShoplazzaConnectRequest(BaseModel):
    store_domain: str = Field(min_length=4, max_length=255)
    access_token: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=1, max_length=255)
    warehouse_id: UUID | None = None


@router.post("/connectors/woocommerce/auth-url")
async def get_woocommerce_auth_url(
    payload: WooAuthUrlRequest, principal: CurrentPrincipal = _admin_roles
) -> dict[str, str]:
    return {
        "auth_url": build_wc_authorize_url(
            store_url=payload.store_url,
            app_name="OmniERP",
            user_id=str(principal.tenant_id),
            return_url=payload.return_url,
            callback_url=payload.callback_url,
        )
    }


@router.post(
    "/connectors/woocommerce/auth-callback",
    response_model=ConnectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def woocommerce_auth_callback(
    request: Request, payload: WooAuthCallbackPayload, principal: CurrentPrincipal = _admin_roles
) -> ConnectResponse:
    domain = payload.store_url.replace("https://", "").replace("http://", "").split("/")[0].lower()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        account_id = await _connect_storefront(
            session,
            principal.tenant_id,
            "woocommerce",
            domain,
            f"WooCommerce {domain}",
            payload.warehouse_id,
            {
                "store_url": payload.store_url,
                "consumer_key": payload.consumer_key,
                "consumer_secret": payload.consumer_secret,
                "webhook_secret": payload.webhook_secret or "",
            },
        )
    return ConnectResponse(channel_account_id=account_id, external_account_id=domain)


@router.post("/webhooks/woocommerce")
async def receive_woocommerce_webhook(request: Request) -> dict[str, int | str | bool]:
    return await receive_signed_storefront_webhook(request, "woocommerce")


@router.post(
    "/connectors/shopline/connect",
    response_model=ConnectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def connect_shopline(
    request: Request, payload: ShoplineConnectRequest, principal: CurrentPrincipal = _admin_roles
) -> ConnectResponse:
    domain = payload.store_domain.strip().lower()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        account_id = await _connect_storefront(
            session,
            principal.tenant_id,
            "shopline",
            domain,
            f"SHOPLINE {domain}",
            payload.warehouse_id,
            {
                "store_domain": domain,
                "access_token": payload.access_token,
                "webhook_secret": payload.app_secret,
            },
        )
    return ConnectResponse(channel_account_id=account_id, external_account_id=domain)


@router.post("/webhooks/shopline")
async def receive_shopline_webhook(request: Request) -> dict[str, int | str | bool]:
    return await receive_signed_storefront_webhook(request, "shopline")


@router.get("/connectors/shoplazza/oauth/callback", response_model=ConnectResponse)
async def shoplazza_oauth_callback(
    request: Request,
    code: Annotated[str, Query(min_length=1, max_length=512)],
    shop: Annotated[str, Query(min_length=4, max_length=255)],
    timestamp: Annotated[str, Query(min_length=1, max_length=32)],
    hmac: Annotated[str, Query(min_length=1, max_length=128)],
    principal: CurrentPrincipal = _admin_roles,
) -> ConnectResponse:
    secret = os.getenv("SHOPLAZZA_CLIENT_SECRET", "")
    params = {"code": code, "shop": shop, "timestamp": timestamp, "hmac": hmac}
    if not verify_shoplazza_oauth_hmac(params, secret):
        raise AppError(
            slug="invalid-shoplazza-oauth-hmac",
            status_code=401,
            code="ERR_INVALID_SHOPLAZZA_OAUTH_HMAC",
            title="Invalid Shoplazza OAuth HMAC",
            detail="OAuth callback verification failed.",
        )
    domain = shop.strip().lower()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        account_id = await _connect_storefront(
            session,
            principal.tenant_id,
            "shoplazza",
            domain,
            f"Shoplazza {domain}",
            None,
            {"store_domain": domain, "access_token": code, "client_secret": secret},
        )
    return ConnectResponse(channel_account_id=account_id, external_account_id=domain)


@router.post(
    "/connectors/shoplazza/connect",
    response_model=ConnectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def connect_shoplazza(
    request: Request,
    payload: ShoplazzaConnectRequest,
    principal: CurrentPrincipal = _admin_roles,
) -> ConnectResponse:
    domain = payload.store_domain.strip().lower()
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        account_id = await _connect_storefront(
            session,
            principal.tenant_id,
            "shoplazza",
            domain,
            f"Shoplazza {domain}",
            payload.warehouse_id,
            {
                "store_domain": domain,
                "access_token": payload.access_token,
                "client_secret": payload.client_secret,
            },
        )
    return ConnectResponse(channel_account_id=account_id, external_account_id=domain)


class TikTokConnectRequest(BaseModel):
    shop_id: str = Field(min_length=1, max_length=64)
    shop_cipher: str = Field(min_length=1, max_length=255)
    app_key: str = Field(min_length=1, max_length=128)
    app_secret: str = Field(min_length=8, max_length=255)
    access_token: str = Field(min_length=1, max_length=512)
    app_mode: str = Field(default="seller_byok", pattern="^(seller_byok|public)$")
    warehouse_id: UUID | None = None


@router.post(
    "/connectors/tiktok/connect",
    response_model=ConnectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def connect_tiktok_shop(
    request: Request,
    payload: TikTokConnectRequest,
    principal: CurrentPrincipal = _admin_roles,
) -> ConnectResponse:
    async with tenant_session(_session_factory(request), principal.tenant_id) as session:
        await enforce_custom_app_shop_limit(session, payload.app_mode, payload.app_key)
        account_id = await _connect_storefront(
            session,
            principal.tenant_id,
            "tiktok_shop",
            payload.shop_id,
            f"TikTok Shop {payload.shop_id}",
            payload.warehouse_id,
            {
                "app_key": payload.app_key,
                "app_secret": payload.app_secret,
                "access_token": payload.access_token,
                "shop_cipher": payload.shop_cipher,
                "app_mode": payload.app_mode,
                "webhook_secret": payload.app_secret,
            },
        )
    return ConnectResponse(channel_account_id=account_id, external_account_id=payload.shop_id)


@router.post("/webhooks/tiktok")
async def receive_tiktok_webhook(request: Request) -> dict[str, int | str | bool]:
    raw = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    domain = _header(headers, DOMAIN_HEADERS["tiktok_shop"]).strip().lower()
    secret = await _webhook_secret(request, "tiktok_shop", domain)
    app_key = os.getenv("TIKTOK_SHOP_APP_KEY", "")
    if not verify_tiktok_webhook_signature(
        raw, {"Authorization": _header(headers, "Authorization")}, app_key, secret
    ):
        raise AppError(
            slug="invalid-webhook-signature",
            status_code=401,
            code="ERR_INVALID_WEBHOOK_SIGNATURE",
            title="Invalid TikTok Shop Signature",
            detail="Webhook signature verification failed.",
        )
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Payload is not an object")
    except ValueError as exc:
        raise AppError(
            slug="invalid-storefront-delivery",
            status_code=400,
            code="ERR_INVALID_STOREFRONT_DELIVERY",
            title="Invalid Storefront Delivery",
            detail="Expected a JSON object.",
        ) from exc
    dedup_id = str(payload.get("tts_notification_id") or hashlib.sha256(raw).hexdigest())
    inserted = await accept_delivery(
        request,
        channel="tiktok_shop",
        external_event_id=dedup_id,
        payload=payload,
        external_account_id=str(payload.get("shop_id") or domain) or None,
        headers={"topic": str(payload.get("type") or ""), "shop_domain": domain},
    )
    return {"code": 200, "status": "accepted", "deduplicated": not inserted}


@router.post("/webhooks/shoplazza")
async def receive_shoplazza_webhook(request: Request) -> dict[str, int | str | bool]:
    return await receive_signed_storefront_webhook(request, "shoplazza")


__all__ = ["router", "receive_signed_storefront_webhook"]
