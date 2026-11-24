from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from typing import Any, cast

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import AdapterRegistry, MessagingChannelAdapter
from erp_core.channels.adapters.gateway import GatewayIngressAdapter
from erp_core.channels.adapters.marketplace_im import (
    CALLBACK_URL_HEADER,
    IM_EVENT_HEADER,
    MARKETPLACE_IM_CHANNELS,
    build_marketplace_im_adapters,
)
from erp_core.channels.adapters.telegram import TELEGRAM_ROUTING_HEADER
from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.channels.messaging_accounts import MESSAGING_CREDENTIAL
from erp_core.channels.webhook_store import (
    accept_raw_webhook_event,
    set_raw_webhook_processing_state,
)
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import ChannelType
from erp_core.shared.errors import AppError

logger = logging.getLogger(__name__)
webhook_router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

# Redis stream contract shared by webhook ingress, the outbox dispatcher and the
# background worker. `STREAM_MAXLEN` bounds growth (~50k entries is weeks of traffic) so
# a stalled consumer cannot fill Redis memory with an unbounded backlog.
OUTBOUND_STREAM_KEY = "stream:outbound:messages"
STREAM_MAXLEN = 50_000

_CHANNEL_ALIASES: dict[str, ChannelType] = {
    "whatsapp": ChannelType.WHATSAPP_CLOUD,
    "whatsapp_cloud": ChannelType.WHATSAPP_CLOUD,
    "whatsapp_coexistence": ChannelType.WHATSAPP_COEXISTENCE,
    "email": ChannelType.EMAIL,
    "resend": ChannelType.EMAIL,
    "postmark": ChannelType.EMAIL,
    "web_livechat": ChannelType.WEB_LIVECHAT,
    "messenger": ChannelType.MESSENGER,
    "instagram": ChannelType.INSTAGRAM_DM,
    "instagram_dm": ChannelType.INSTAGRAM_DM,
    "telegram": ChannelType.TELEGRAM,
}
_META_CHANNELS = (
    ChannelType.MESSENGER,
    ChannelType.INSTAGRAM_DM,
    "messenger",
    "instagram",
    "instagram_dm",
)
TELEGRAM_BOT_KEY = re.compile(r"[1-9][0-9]{0,19}")

#: Buyer-IM platform slug -> canonical channel. Both the underscore and hyphen shape of
#: every channel is accepted (`mercado_libre`/`mercado-libre`, `tiktok_shop`/`tiktok-shop`,
#: `amazon_sp`/`amazon-sp`) plus the documented Alibaba International spellings.
_MARKETPLACE_IM_PLATFORM_ALIASES: dict[str, ChannelType] = {
    **{
        alias: channel_type
        for channel_type in MARKETPLACE_IM_CHANNELS
        for alias in (
            channel_type.value,
            channel_type.value.replace("_", "-"),
        )
    },
    "alibaba-intl": ChannelType.ALIBABA_EMAIL,
    "alibaba_intl": ChannelType.ALIBABA_EMAIL,
    "alibaba-email": ChannelType.ALIBABA_EMAIL,
}

#: Credential bundle that carries each marketplace's inbound webhook secret.
_MARKETPLACE_IM_CREDENTIAL_TYPES: dict[ChannelType, str] = {
    ChannelType.SHOPEE: "shopee_app",
    ChannelType.LAZADA: "lazada_app",
    ChannelType.TEMU: "temu_app",
    ChannelType.SHEIN: "shein_app",
    ChannelType.ALIEXPRESS: "aliexpress_app",
    ChannelType.OZON: "ozon_app",
    ChannelType.WALMART: "walmart_app",
    ChannelType.MERCADO_LIBRE: "mercado_libre_app",
    ChannelType.TIKTOK_SHOP: "tiktok_shop_app",
    ChannelType.EBAY: "ebay_api",
    ChannelType.AMAZON_SP: "amazon_sp_api",
    ChannelType.ALIBABA_EMAIL: "alibaba_intl_app",
}

#: Platform-wide signing key env var used when the tenant has no stored credential.
_MARKETPLACE_IM_SECRET_ENV: dict[ChannelType, str] = {
    ChannelType.SHOPEE: "SHOPEE_PARTNER_KEY",
    ChannelType.LAZADA: "LAZADA_APP_SECRET",
    ChannelType.TEMU: "TEMU_APP_SECRET",
    ChannelType.SHEIN: "SHEIN_SECRET_KEY",
    ChannelType.ALIEXPRESS: "ALIEXPRESS_APP_SECRET",
    ChannelType.OZON: "OZON_API_KEY",
    ChannelType.WALMART: "WALMART_CLIENT_SECRET",
    ChannelType.MERCADO_LIBRE: "MERCADO_LIBRE_WEBHOOK_SECRET",
    ChannelType.TIKTOK_SHOP: "TIKTOK_SHOP_APP_SECRET",
    ChannelType.EBAY: "EBAY_WEBHOOK_SECRET",
    ChannelType.AMAZON_SP: "AMAZON_SP_WEBHOOK_SECRET",
    ChannelType.ALIBABA_EMAIL: "ALIBABA_INTL_APP_SECRET",
}

#: Credential-blob keys that may carry the inbound webhook secret, in precedence order.
_MARKETPLACE_IM_SECRET_KEYS: tuple[str, ...] = (
    "webhook_secret",
    "client_secret",
    "app_secret",
    "partner_key",
    "secret_key",
    "api_key",
    "access_token",
)


def _invalid_signature_response(request: Request, *, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        media_type="application/problem+json",
        content={
            "type": "https://erp.local/errors/webhook-signature-invalid",
            "title": "Invalid Webhook Signature",
            "status": 401,
            "detail": detail,
            "instance": str(request.url.path),
            "code": "ERR_WEBHOOK_SIGNATURE_INVALID",
        },
    )


# Auth material a caller may send with a webhook. Signature verification consumes these
# in memory only; they must never be persisted with the raw webhook row.
_SENSITIVE_WEBHOOK_HEADERS: frozenset[str] = frozenset(
    {
        "x-telegram-bot-api-secret-token",
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-webhook-secret",
        "x-secret-token",
    }
)


def sanitize_webhook_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with auth/secret headers removed (case-insensitive).

    Raw webhook rows are long-lived, tenant-readable payload storage. Persisting
    ``Authorization``/``Cookie``/bot secret tokens there would leak live credentials to
    any reader of ``raw_webhook_events`` and into DLQ payloads, so only the safe subset
    of the request headers is stored. Verification still runs on the original mapping
    before this function is called.
    """
    sanitized: dict[str, str] = {}
    for name, value in headers.items():
        if str(name).strip().lower() in _SENSITIVE_WEBHOOK_HEADERS:
            continue
        sanitized[str(name)] = str(value)
    return sanitized


def build_default_adapter_registry(redis: aioredis.Redis | None = None) -> AdapterRegistry:
    """Register the gateway ingress adapter, then any later channel adapters.

    ``redis`` is passed to adapters that also fan out to a live client (the web widget
    reads its replies from Pub/Sub), so outbound delivery does not silently stop at the
    database write.
    """
    registry = AdapterRegistry()
    gateway = GatewayIngressAdapter()
    for key in (
        "whatsapp",
        ChannelType.WHATSAPP_CLOUD.value,
        ChannelType.WHATSAPP_COEXISTENCE.value,
        "email",
        "resend",
        "postmark",
        ChannelType.EMAIL.value,
        ChannelType.WEB_LIVECHAT.value,
    ):
        registry.register(key, gateway)
    try:
        from erp_core.channels.adapters.web_livechat import WebLiveChatAdapter

        # Registered last so it wins over the gateway stub: the gateway adapter refuses
        # outbound sends (UnsupportedOutbound), which used to dead-letter every agent
        # reply to a widget visitor.
        livechat = WebLiveChatAdapter(redis=redis)
        registry.register(ChannelType.WEB_LIVECHAT.value, livechat)
    except ImportError:
        logger.debug("WebLiveChatAdapter not yet installed; skipping registration")
    try:
        from erp_core.channels.adapters.whatsapp import (
            WhatsAppAdapter,
        )

        wa = WhatsAppAdapter()
        registry.register("whatsapp", wa)
        registry.register(ChannelType.WHATSAPP_CLOUD.value, wa)
        registry.register(ChannelType.WHATSAPP_COEXISTENCE.value, wa)
    except ImportError:
        logger.debug("WhatsAppAdapter not yet installed; skipping registration")

    try:
        from erp_core.channels.adapters.email import (
            EmailAdapter,
        )

        email_adapter = EmailAdapter()
        registry.register("email", email_adapter)
        registry.register("resend", email_adapter)
        registry.register("postmark", email_adapter)
        registry.register(ChannelType.EMAIL.value, email_adapter)
    except ImportError:
        logger.debug("EmailAdapter not yet installed; skipping registration")

    try:
        from erp_core.channels.adapters.instagram_dm import InstagramDMAdapter
        from erp_core.channels.adapters.messenger import MessengerAdapter
        from erp_core.channels.adapters.telegram import TelegramAdapter

        messenger = MessengerAdapter()
        instagram = InstagramDMAdapter()
        telegram = TelegramAdapter()
        registry.register("messenger", messenger)
        registry.register(ChannelType.MESSENGER.value, messenger)
        registry.register("instagram", instagram)
        registry.register("instagram_dm", instagram)
        registry.register(ChannelType.INSTAGRAM_DM.value, instagram)
        registry.register("telegram", telegram)
        registry.register(ChannelType.TELEGRAM.value, telegram)
    except ImportError:
        logger.debug("Meta/Telegram adapters not yet installed; skipping registration")

    # Buyer-IM adapters for the twelve marketplace chat surfaces. Registered under both
    # the canonical `ChannelType` value and the hyphen slug so `/api/v1/webhooks/im/
    # mercado-libre` and `registry.get("mercado_libre")` resolve to the same adapter.
    im_adapters = build_marketplace_im_adapters()
    for channel_type, im_adapter in im_adapters.items():
        registry.register(channel_type.value, im_adapter)
        registry.register(channel_type.value.replace("_", "-"), im_adapter)
    alibaba_im = im_adapters.get(ChannelType.ALIBABA_EMAIL)
    if alibaba_im is not None:
        for alias in ("alibaba-intl", "alibaba_intl", "alibaba-email"):
            registry.register(alias, alibaba_im)
    return registry


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


def _get_broker_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "broker_session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook broker is not configured",
        )
    return cast(async_sessionmaker[AsyncSession], factory)


def _get_redis(request: Request) -> aioredis.Redis:
    client = getattr(request.app.state, "redis", None)
    if client is None:
        client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        request.app.state.redis = client
    return client


def _get_adapter_registry(request: Request) -> AdapterRegistry:
    registry = getattr(request.app.state, "adapter_registry", None)
    if registry is None:
        registry = build_default_adapter_registry(_get_redis(request))
        request.app.state.adapter_registry = registry
    return registry


def _webhook_secret_for_channel(channel: str) -> str:
    key = channel.lower()
    if key in ("whatsapp", "whatsapp_cloud", "whatsapp_coexistence"):
        return os.environ.get("WHATSAPP_APP_SECRET", "")
    if key in ("email", "resend", "postmark"):
        return os.environ.get("RESEND_WEBHOOK_SECRET", os.environ.get("EMAIL_WEBHOOK_SECRET", ""))
    if key in ("messenger", "instagram", "instagram_dm"):
        return os.environ.get("META_APP_SECRET", "")
    return os.environ.get("WEBHOOK_SHARED_SECRET", "")


def _telegram_bot_key(channel: str) -> str | None:
    """Bot numeric id selects the tenant; it is never a usable credential by itself."""
    if channel.lower() == "telegram":
        return ""
    prefix, _, bot_key = channel.partition("/")
    if prefix.lower() == "telegram" and TELEGRAM_BOT_KEY.fullmatch(bot_key):
        return bot_key
    return None


async def _telegram_expected_secret(request: Request, bot_key: str) -> str:
    """Bot id only selects the tenant; none of it is a usable credential by itself."""
    if not bot_key:
        return os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    factory = _get_session_factory(request)
    async with system_session(factory) as lookup:
        route = (
            await lookup.execute(
                text(
                    "SELECT tenant_id, channel_account_id "
                    "FROM resolve_tenant_by_channel_external_id('telegram', :bot)"
                ),
                {"bot": bot_key},
            )
        ).first()
    if route is None or route[1] is None:
        return ""
    async with tenant_session(factory, route[0]) as session:
        try:
            payload, _ = await CredentialVaultHelper().load_secret_json(
                session, route[0], route[1], MESSAGING_CREDENTIAL
            )
        except AppError as exc:
            if exc.code == "ERR_CREDENTIAL_NOT_FOUND":
                return ""
            raise
    secret = payload.get("webhook_secret")
    return secret if isinstance(secret, str) else ""


async def _receive_telegram_webhook(request: Request, bot_key: str) -> JSONResponse:
    adapter = _get_adapter_registry(request).get(ChannelType.TELEGRAM.value)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No adapter registered for channel: telegram",
        )
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        secret = await _telegram_expected_secret(request, bot_key)
    except AppError as exc:
        if exc.code != "ERR_CREDENTIAL_NOT_FOUND":
            raise
        secret = ""
    if not secret or not adapter.verify_webhook_signature(raw_body, headers, secret):
        return _invalid_signature_response(
            request, detail="Secret token verification failed for webhook request."
        )
    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed JSON webhook body: {exc}",
        ) from exc
    if not bot_key:
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "accepted"})
    # Force the validated path bot id so a payload cannot pick a different tenant route.
    headers[TELEGRAM_ROUTING_HEADER] = bot_key
    return await _accept_events(
        request,
        source_channel=ChannelType.TELEGRAM,
        canonical_type=ChannelType.TELEGRAM,
        adapter=adapter,
        raw_body=raw_body,
        headers=headers,
        payload=payload,
    )


@webhook_router.get("/{channel}")
async def verify_webhook_subscription(
    channel: str,
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    """Handle Meta webhook subscription verification handshake."""
    key = channel.lower()
    expected_token = (
        os.environ.get("META_VERIFY_TOKEN", "")
        if key in ("messenger", "instagram", "instagram_dm")
        else os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    )
    verify_channels = (
        "whatsapp",
        "whatsapp_cloud",
        "whatsapp_coexistence",
        "messenger",
        "instagram",
        "instagram_dm",
    )
    if (
        key in verify_channels
        and hub_mode == "subscribe"
        and hub_verify_token is not None
        and expected_token
        and hmac.compare_digest(hub_verify_token, expected_token)
        and hub_challenge is not None
    ):
        return PlainTextResponse(content=hub_challenge, status_code=status.HTTP_200_OK)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid webhook verification token",
    )


async def _marketplace_im_secret(
    request: Request,
    channel_type: ChannelType,
    external_account_id: str | None,
) -> str:
    """Per-tenant signing secret for one buyer-IM marketplace push.

    The tenant credential wins (BYOK apps sign with their own key); a platform-wide env
    secret is only the fallback for single-tenant deployments. Returns ``""`` when
    neither exists, which the caller treats as fail-closed.
    """
    credential_type = _MARKETPLACE_IM_CREDENTIAL_TYPES[channel_type]
    if external_account_id:
        factory = _get_session_factory(request)
        async with system_session(factory) as lookup:
            route = (
                await lookup.execute(
                    text(
                        "SELECT tenant_id, channel_account_id "
                        "FROM resolve_tenant_by_channel_external_id("
                        "CAST(:ctype AS text), CAST(:ext_id AS text))"
                    ),
                    {"ctype": channel_type.value, "ext_id": external_account_id},
                )
            ).first()
        if route is not None and route[1] is not None:
            tenant_id = route[0]
            channel_account_id = route[1]
            try:
                async with tenant_session(factory, tenant_id) as session:
                    credential, _ = await CredentialVaultHelper().load_secret_json(
                        session, tenant_id, channel_account_id, credential_type
                    )
            except AppError as exc:
                if exc.code != "ERR_CREDENTIAL_NOT_FOUND":
                    raise
                credential = {}
            for key in _MARKETPLACE_IM_SECRET_KEYS:
                value = credential.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return os.environ.get(_MARKETPLACE_IM_SECRET_ENV[channel_type], "") or os.environ.get(
        "WEBHOOK_SHARED_SECRET", ""
    )


# NOTE: this route MUST stay declared before `/{channel:path}`, otherwise the catch-all
# storefront/messaging webhook route swallows every `/api/v1/webhooks/im/<platform>` push.
@webhook_router.post("/im/{platform}")
async def receive_marketplace_im_webhook(platform: str, request: Request) -> JSONResponse:
    """Signed buyer-IM ingress for the twelve marketplace chat surfaces.

    Buyer chat shares the marketplace's order-feed endpoint family but is answered by a
    different API, so it gets a dedicated route: verify the platform HMAC with the
    tenant's own signing secret, persist the receipt, and hand it to the Inbox pipeline
    through `stream:webhooks:{channel}`.
    """
    canonical_type = _MARKETPLACE_IM_PLATFORM_ALIASES.get(platform.strip().lower())
    if canonical_type is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unsupported buyer-IM platform: {platform}",
        )

    adapter = _get_adapter_registry(request).get(canonical_type.value)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No adapter registered for buyer-IM channel: {canonical_type.value}",
        )

    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    # Shopee signs `f"{callback_url}|{raw_body}"`, so the absolute push URL must be
    # reconstructable when the platform (or a test) did not supply it explicitly.
    if not headers.get(CALLBACK_URL_HEADER, "").strip():
        headers[CALLBACK_URL_HEADER] = (
            f"{str(request.base_url).rstrip('/')}{request.url.path}"
        )

    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Payload is not a JSON object")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed JSON webhook body: {exc}",
        ) from exc

    extracted_events = adapter.extract_external_event_ids(payload, raw_body, headers)
    external_account_id = next((ext for _, ext in extracted_events if ext), None)
    secret = await _marketplace_im_secret(request, canonical_type, external_account_id)
    if not secret or not adapter.verify_webhook_signature(raw_body, headers, secret):
        return _invalid_signature_response(
            request, detail="HMAC verification failed for webhook request."
        )

    # Internal routing markers: the IM flag lets the worker route the receipt into the
    # Inbox pipeline; the callback url is a verification input only and never stored.
    headers[IM_EVENT_HEADER] = "true"
    headers.pop(CALLBACK_URL_HEADER, None)

    return await _accept_events(
        request,
        source_channel=canonical_type,
        canonical_type=canonical_type,
        adapter=adapter,
        raw_body=raw_body,
        headers=headers,
        payload=payload,
    )


@webhook_router.post("/{channel:path}")
async def receive_webhook(channel: str, request: Request) -> JSONResponse:
    """Verify raw HMAC signature, deduplicate into PostgreSQL, and stream to Taskiq in <100ms."""
    bot_key = _telegram_bot_key(channel)
    if bot_key is not None:
        return await _receive_telegram_webhook(request, bot_key)

    canonical_type = _CHANNEL_ALIASES.get(channel.lower())
    if canonical_type is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unsupported webhook channel: {channel}",
        )

    registry = _get_adapter_registry(request)
    adapter = registry.get(channel) or registry.get(canonical_type.value)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No adapter registered for channel: {channel}",
        )

    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    secret = _webhook_secret_for_channel(channel)
    if not secret or not adapter.verify_webhook_signature(raw_body, headers, secret):
        return _invalid_signature_response(
            request, detail="HMAC verification failed for webhook request."
        )

    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed JSON webhook body: {exc}",
        ) from exc

    return await _accept_events(
        request,
        source_channel=canonical_type,
        canonical_type=canonical_type,
        adapter=adapter,
        raw_body=raw_body,
        headers=headers,
        payload=payload,
    )


async def _accept_events(
    request: Request,
    *,
    source_channel: ChannelType,
    canonical_type: ChannelType,
    adapter: MessagingChannelAdapter,
    raw_body: bytes,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> JSONResponse:
    extracted_events = adapter.extract_external_event_ids(payload, raw_body, headers)
    if not extracted_events:
        extracted_events = [(hashlib.sha256(raw_body).hexdigest(), None)]

    session_factory = _get_session_factory(request)
    broker_factory = _get_broker_factory(request)
    redis = _get_redis(request)

    inserted_ids: list[str] = []
    stream_entries: list[tuple[str, str | None]] = []

    async with system_session(session_factory) as session:
        # Resolve tenant once per distinct external_account_id in batch
        resolved_tenants: dict[str, tuple[uuid.UUID | None, ChannelType]] = {}
        for _, ext_acct_id in extracted_events:
            if not ext_acct_id or ext_acct_id in resolved_tenants:
                continue
            found_tenant: uuid.UUID | None = None
            found_type = canonical_type
            candidate_types = (
                [ChannelType.WHATSAPP_CLOUD, ChannelType.WHATSAPP_COEXISTENCE]
                if source_channel == ChannelType.WHATSAPP_CLOUD
                else [source_channel]
            )
            for candidate in candidate_types:
                row = (
                    await session.execute(
                        text(
                            "SELECT tenant_id FROM resolve_tenant_by_channel_external_id("
                            "CAST(:ctype AS text), CAST(:ext_id AS text))"
                        ),
                        {"ctype": candidate.value, "ext_id": ext_acct_id},
                    )
                ).first()
                if row is not None:
                    found_tenant = row[0]
                    found_type = candidate
                    break
            resolved_tenants[ext_acct_id] = (found_tenant, found_type)

        headers_json = json.dumps(sanitize_webhook_headers(headers))
        payload_json = json.dumps(payload)

        for ext_event_id, ext_acct_id in extracted_events:
            resolved_tid: uuid.UUID | None = None
            resolved_ctype = canonical_type
            if ext_acct_id and ext_acct_id in resolved_tenants:
                resolved_tid, resolved_ctype = resolved_tenants[ext_acct_id]

            async with system_session(broker_factory) as broker:
                inserted = await accept_raw_webhook_event(
                    broker,
                    event_id=uuid.uuid4(),
                    tenant_id=resolved_tid,
                    channel_type=resolved_ctype.value,
                    external_event_id=ext_event_id,
                    external_account_id=ext_acct_id,
                    headers_json=headers_json,
                    payload_json=payload_json,
                )
            if inserted is not None:
                inserted_str = str(inserted)
                inserted_ids.append(inserted_str)
                stream_entries.append((inserted_str, resolved_ctype.value))

    queue_failed = False
    for raw_id_str, ctype_val in stream_entries:
        stream_key = f"stream:webhooks:{ctype_val}"
        try:
            await redis.xadd(
                stream_key,
                {"raw_event_id": raw_id_str, "channel_type": str(ctype_val), "attempt": "1"},
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
        except Exception:
            logger.exception("webhook row %s committed but redis enqueue failed", raw_id_str)
            queue_failed = True
            continue
        async with system_session(broker_factory) as session:
            await set_raw_webhook_processing_state(
                session,
                raw_event_id=uuid.UUID(raw_id_str),
                status="queued",
                attempt=1,
            )
    if queue_failed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook stored but the queue is unavailable; retry to enqueue",
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "accepted",
            "deduplicated": len(inserted_ids) == 0,
            "raw_event_ids": inserted_ids,
        },
    )
