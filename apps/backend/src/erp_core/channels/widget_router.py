from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.identity.service import IdentityResolverService
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.enums import ChannelType, ConversationMode, ConversationStatus, IdentityType

widget_router = APIRouter(prefix="/api/v1/widget", tags=["widget"])

# Short-lived visitor bearer token: a leaked widget token must not stay valid for a week.
_WIDGET_JWT_TTL = timedelta(hours=2)
# Long conversations keep only the newest page of messages, not the oldest.
_WIDGET_MESSAGE_PAGE_LIMIT = 100
# 96 bits of HMAC-SHA256 in hex is plenty to make a visitor id unpredictable/forge-proof.
_VISITOR_SIGNATURE_HEX_CHARS = 24


class PreChatOptInPayload(BaseModel):
    display_name: str | None = None
    email: str | None = None
    whatsapp_phone: str | None = None
    marketing_opt_in: bool = False


class WidgetSessionRequest(BaseModel):
    site_key: str = Field(min_length=3)
    visitor_id: str | None = None
    page_url: str | None = None
    sku: str | None = None
    cart_value_usd: str | None = None
    utm_params: dict[str, str] = Field(default_factory=dict)
    prechat_opt_in: PreChatOptInPayload | None = None


class WidgetMessageItem(BaseModel):
    id: uuid.UUID
    direction: str
    sender_type: str
    body_text: str | None
    created_at: datetime


class WidgetSessionResponse(BaseModel):
    session_token: str
    visitor_id: str
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    business_name: str
    messages: list[WidgetMessageItem]


class WidgetSendMessageRequest(BaseModel):
    body_text: str = Field(min_length=1, max_length=4000)
    page_url: str | None = None
    sku: str | None = None


class WidgetIdentifyRequest(BaseModel):
    email: str | None = None
    phone_e164: str | None = None
    external_customer_id: str | None = None
    display_name: str | None = None
    timestamp: int
    hmac_signature: str


class WidgetTokenClaims(BaseModel):
    visitor_id: str
    tenant_id: uuid.UUID
    channel_account_id: uuid.UUID
    customer_id: uuid.UUID
    conversation_id: uuid.UUID


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        request.app.state.session_factory = factory
    return factory


def _get_redis(request: Request) -> aioredis.Redis:
    client = getattr(request.app.state, "redis", None)
    if client is None:
        client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        request.app.state.redis = client
    return client


def _widget_signing_secret() -> str:
    """Single secret source for visitor-id HMAC and for the widget bearer JWT."""
    return get_settings().JWT_SECRET


def _jwt_secret() -> str:
    return _widget_signing_secret()


def _jwt_alg() -> str:
    return get_settings().JWT_ALGORITHM


def _visitor_signature(
    raw_vid: str, *, tenant_id: uuid.UUID, account_id: uuid.UUID, secret: str
) -> str:
    message = f"{tenant_id}:{account_id}:{raw_vid}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()[
        :_VISITOR_SIGNATURE_HEX_CHARS
    ]


def sign_visitor_id(
    raw_vid: str, *, tenant_id: uuid.UUID, account_id: uuid.UUID, secret: str
) -> str:
    """Return ``'<uuid>.<hmac_sha256_hex[:24]>'`` bound to one tenant + channel account."""
    normalized = str(uuid.UUID(raw_vid.strip()))
    signature = _visitor_signature(
        normalized, tenant_id=tenant_id, account_id=account_id, secret=secret
    )
    return f"{normalized}.{signature}"


def verify_signed_visitor_id(
    token: str | None, *, tenant_id: uuid.UUID, account_id: uuid.UUID, secret: str
) -> str | None:
    """Return the raw visitor UUID only when the signature is valid for this account.

    A bare (unsigned) UUID, a tampered MAC, or a token minted for another tenant/account
    all return ``None`` so the caller mints a fresh anonymous visitor instead of resuming
    somebody else's conversation.
    """
    if not token:
        return None
    raw_vid, separator, signature = token.strip().partition(".")
    if separator != "." or not raw_vid or not signature:
        return None
    try:
        normalized = str(uuid.UUID(raw_vid))
    except ValueError:
        return None
    expected = _visitor_signature(
        normalized, tenant_id=tenant_id, account_id=account_id, secret=secret
    )
    if not hmac.compare_digest(signature.lower(), expected):
        return None
    return normalized


def _sign_widget_token(claims: WidgetTokenClaims) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": claims.visitor_id,
        "tenant_id": str(claims.tenant_id),
        "channel_account_id": str(claims.channel_account_id),
        "customer_id": str(claims.customer_id),
        "conversation_id": str(claims.conversation_id),
        "scope": "widget_visitor",
        "iat": int(now.timestamp()),
        "exp": int((now + _WIDGET_JWT_TTL).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_jwt_alg())


def _decode_widget_token(authorization: str | None = Header(default=None)) -> WidgetTokenClaims:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing widget session bearer token",
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        decoded = jwt.decode(token, _jwt_secret(), algorithms=[_jwt_alg()])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired widget session token",
        ) from exc
    if decoded.get("scope") != "widget_visitor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token scope mismatch",
        )
    return WidgetTokenClaims(
        visitor_id=str(decoded["sub"]),
        tenant_id=uuid.UUID(decoded["tenant_id"]),
        channel_account_id=uuid.UUID(decoded["channel_account_id"]),
        customer_id=uuid.UUID(decoded["customer_id"]),
        conversation_id=uuid.UUID(decoded["conversation_id"]),
    )


def _enforce_origin(origin: str | None, allowed_origins: list[str]) -> dict[str, str]:
    if not origin:
        return {}
    if "*" in allowed_origins or origin in allowed_origins:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Vary": "Origin",
        }
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Origin {origin} is not in channel allowed_origins",
    )


@widget_router.options("/{path:path}")
async def widget_cors_preflight(request: Request) -> Response:
    """Answer the preflight without ever granting cookie/credentialed cross-origin access.

    Preflight carries no site_key, so the Origin cannot be validated against a channel
    account here. The widget authenticates with a Bearer header (never cookies), therefore
    the unchecked preflight omits ``Access-Control-Allow-Credentials`` entirely instead of
    echoing an unvalidated Origin as credentialed.
    """
    origin = request.headers.get("origin")
    headers = (
        {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Vary": "Origin",
        }
        if origin
        else {}
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=headers)


async def _fetch_latest_messages(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    after_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Return the newest (optionally cursor-bounded) 100 messages in ascending order.

    Long conversations must show the most recent page of history, so the query orders
    DESC and the page is reversed into chronological order for the widget transcript.
    """
    params: dict[str, Any] = {
        "tid": tenant_id,
        "cid": conversation_id,
        "limit": _WIDGET_MESSAGE_PAGE_LIMIT,
    }
    after_clause = ""
    if after_id is not None:
        cursor = (
            await session.execute(
                text(
                    "SELECT source_timestamp, id FROM messages "
                    "WHERE tenant_id = :tid AND conversation_id = :cid AND id = :mid"
                ),
                {"tid": tenant_id, "cid": conversation_id, "mid": after_id},
            )
        ).first()
        if cursor is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Widget message cursor not found in this conversation",
            )
        after_clause = " AND (source_timestamp, id) > (:cur_ts, :cur_id)"
        params["cur_ts"] = cursor[0]
        params["cur_id"] = cursor[1]
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, direction, sender_type, body_text, created_at "
                    "FROM messages WHERE tenant_id = :tid AND conversation_id = :cid "
                    "AND direction != 'internal_note'" + after_clause + " "
                    "ORDER BY source_timestamp DESC, id DESC LIMIT :limit"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": row["id"],
            "direction": row["direction"],
            "sender_type": row["sender_type"],
            "body_text": row["body_text"],
            "created_at": row["created_at"],
        }
        for row in reversed(rows)
    ]


@widget_router.post("/session", response_model=WidgetSessionResponse)
async def create_or_resume_widget_session(
    payload: WidgetSessionRequest,
    request: Request,
    response: Response,
) -> WidgetSessionResponse:
    session_factory = _get_session_factory(request)
    origin = request.headers.get("origin")

    async with system_session(session_factory) as sys_sess:
        dir_row = (
            await sys_sess.execute(
                text(
                    "SELECT tenant_id, channel_account_id "
                    "FROM resolve_tenant_by_channel_external_id('web_livechat', CAST(:skey AS "
                    "text))"
                ),
                {"skey": payload.site_key},
            )
        ).first()
    if dir_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown site_key")

    tenant_id, channel_account_id = dir_row[0], dir_row[1]
    signing_secret = _widget_signing_secret()
    # Only an HMAC-signed visitor id minted for THIS tenant + channel account may be
    # resumed. Anything else (missing, raw UUID, tampered, other account) gets a brand
    # new anonymous visitor, so guessing a UUID cannot hijack a stranger's session.
    resumed_visitor_id = verify_signed_visitor_id(
        payload.visitor_id,
        tenant_id=tenant_id,
        account_id=channel_account_id,
        secret=signing_secret,
    )
    visitor_id = resumed_visitor_id or str(uuid.uuid4())
    signed_visitor_id = sign_visitor_id(
        visitor_id, tenant_id=tenant_id, account_id=channel_account_id, secret=signing_secret
    )
    identity_svc = IdentityResolverService()
    consent_svc = ConsentAndSuppressionService()

    async with tenant_session(session_factory, tenant_id) as session:
        acct = (
            (
                await session.execute(
                    text(
                        "SELECT display_name, config FROM channel_accounts "
                        "WHERE tenant_id = :tid AND id = :aid AND status = 'active'"
                    ),
                    {"tid": tenant_id, "aid": channel_account_id},
                )
            )
            .mappings()
            .first()
        )
        if acct is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Widget channel inactive"
            )

        cfg = acct["config"] or {}
        cors_headers = _enforce_origin(origin, list(cfg.get("allowed_origins") or []))
        for k, v in cors_headers.items():
            response.headers[k] = v

        business_name = str(cfg.get("business_name") or acct["display_name"])
        prechat = payload.prechat_opt_in
        display_name = (
            prechat.display_name
            if prechat and prechat.display_name
            else f"Visitor {visitor_id[:6]}"
        )

        # SECURITY: the visitor id is the ONLY identity this unauthenticated endpoint may
        # resolve. Pre-chat email/phone are stored as unverified leads on the conversation
        # and are never linked into customer_identities (nor into an existing customer's
        # thread) until the HMAC-verified POST /identify proves ownership of them.
        customer_id, _ = await identity_svc.resolve_or_create_customer(
            session, tenant_id, IdentityType.WEB_VISITOR_ID, visitor_id, display_name
        )

        unverified_prechat: dict[str, Any] = {}
        if prechat is not None:
            if prechat.email:
                unverified_prechat["email"] = prechat.email
            if prechat.whatsapp_phone:
                unverified_prechat["whatsapp_phone"] = prechat.whatsapp_phone
            if prechat.marketing_opt_in:
                unverified_prechat["marketing_opt_in"] = True

        await session.execute(
            text(
                "INSERT INTO customer_identities "
                "(id, tenant_id, customer_id, identity_type, identity_value, confidence_level) "
                "VALUES (:id, :tid, :cid, 'web_visitor_id', :vid, 'deterministic') "
                "ON CONFLICT (tenant_id, identity_type, identity_value) DO NOTHING"
            ),
            {"id": uuid.uuid4(), "tid": tenant_id, "cid": customer_id, "vid": visitor_id},
        )

        if prechat and prechat.marketing_opt_in:
            client_ip = request.client.host if request.client else None
            # The opt-in is recorded as UNVERIFIED proof only: no normalized address is
            # passed, so an attacker cannot use a forged pre-chat form to clear a victim's
            # STOP suppression_list entry or attach consent to a victim's address.
            if prechat.whatsapp_phone:
                await consent_svc.record_opt_in(
                    session=session,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    channel_type="whatsapp",
                    proof_source="web_livechat_prechat",
                    disclosed_business_name=business_name,
                    proof_ip=client_ip,
                    evidence_ref="unverified_prechat",
                )
            if prechat.email:
                await consent_svc.record_opt_in(
                    session=session,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    channel_type="email",
                    proof_source="web_livechat_prechat",
                    disclosed_business_name=business_name,
                    proof_ip=client_ip,
                    evidence_ref="unverified_prechat",
                )

        conv_id = (
            await session.execute(
                text(
                    "SELECT id FROM conversations "
                    "WHERE tenant_id = :tid AND customer_id = :cid "
                    "AND channel_account_id = :aid AND status != 'resolved' "
                    "ORDER BY updated_at DESC LIMIT 1"
                ),
                {"tid": tenant_id, "cid": customer_id, "aid": channel_account_id},
            )
        ).scalar_one_or_none()
        if conv_id is None:
            conv_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO conversations ("
                    "id, tenant_id, customer_id, channel_account_id, channel_type, "
                    "external_thread_id, conversation_mode, status, channel_policy_state"
                    ") VALUES ("
                    ":id, :tid, :cid, :aid, :ctype, :thread, :mode, :status, CAST(:policy AS jsonb)"
                    ")"
                ),
                {
                    "id": conv_id,
                    "tid": tenant_id,
                    "cid": customer_id,
                    "aid": channel_account_id,
                    "ctype": ChannelType.WEB_LIVECHAT.value,
                    "thread": visitor_id,
                    "mode": ConversationMode.B2C_SUPPORT.value,
                    "status": ConversationStatus.OPEN.value,
                    "policy": json.dumps(
                        {
                            "page_url": payload.page_url,
                            "sku": payload.sku,
                            "cart_value_usd": payload.cart_value_usd,
                            "utm_params": payload.utm_params,
                            "unverified_prechat": unverified_prechat,
                        }
                    ),
                },
            )
        elif unverified_prechat:
            # Refresh the unverified lead snapshot on the resumed thread (never an identity).
            await session.execute(
                text(
                    "UPDATE conversations SET channel_policy_state = "
                    "COALESCE(channel_policy_state, CAST('{}' AS jsonb)) || "
                    "CAST(:patch AS jsonb), updated_at = now() "
                    "WHERE tenant_id = :tid AND id = :cid"
                ),
                {
                    "tid": tenant_id,
                    "cid": conv_id,
                    "patch": json.dumps({"unverified_prechat": unverified_prechat}),
                },
            )

        msg_rows = await _fetch_latest_messages(session, tenant_id, conv_id)

    token = _sign_widget_token(
        WidgetTokenClaims(
            visitor_id=visitor_id,
            tenant_id=tenant_id,
            channel_account_id=channel_account_id,
            customer_id=customer_id,
            conversation_id=conv_id,
        )
    )
    return WidgetSessionResponse(
        session_token=token,
        visitor_id=signed_visitor_id,
        customer_id=customer_id,
        conversation_id=conv_id,
        business_name=business_name,
        messages=[WidgetMessageItem(**dict(r)) for r in msg_rows],
    )


async def _validate_account_cors(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    origin: str | None,
    response: Response,
) -> None:
    cfg = (
        await session.execute(
            text("SELECT config FROM channel_accounts WHERE tenant_id = :tid AND id = :aid"),
            {"tid": tenant_id, "aid": account_id},
        )
    ).scalar_one_or_none()
    allowed = list((cfg or {}).get("allowed_origins") or [])
    for k, v in _enforce_origin(origin, allowed).items():
        response.headers[k] = v


@widget_router.get("/messages")
async def list_widget_messages(
    request: Request,
    response: Response,
    claims: Annotated[WidgetTokenClaims, Depends(_decode_widget_token)],
    after_id: uuid.UUID | None = None,
) -> dict[str, list[dict[str, Any]]]:
    session_factory = _get_session_factory(request)
    async with tenant_session(session_factory, claims.tenant_id) as session:
        await _validate_account_cors(
            session,
            claims.tenant_id,
            claims.channel_account_id,
            request.headers.get("origin"),
            response,
        )
        rows = await _fetch_latest_messages(
            session, claims.tenant_id, claims.conversation_id, after_id=after_id
        )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "direction": r["direction"],
                "sender_type": r["sender_type"],
                "body_text": r["body_text"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@widget_router.post("/messages", status_code=status.HTTP_201_CREATED)
async def send_widget_message(
    payload: WidgetSendMessageRequest,
    request: Request,
    response: Response,
    claims: Annotated[WidgetTokenClaims, Depends(_decode_widget_token)],
) -> dict[str, Any]:
    session_factory = _get_session_factory(request)
    redis = _get_redis(request)
    msg_id = uuid.uuid4()
    now_ts = datetime.now(UTC)

    async with tenant_session(session_factory, claims.tenant_id) as session:
        await _validate_account_cors(
            session,
            claims.tenant_id,
            claims.channel_account_id,
            request.headers.get("origin"),
            response,
        )
        await session.execute(
            text(
                "INSERT INTO messages ("
                "id, tenant_id, conversation_id, customer_id, channel_type, "
                "external_message_id, external_sender_id, direction, sender_type, "
                "sender_id, content_type, body_text, channel_metadata, delivery_status, "
                "source_timestamp"
                ") VALUES ("
                ":id, :tid, :conv_id, :cid, 'web_livechat', :emid, :vid, 'inbound', 'customer', "
                ":sid, 'text', :btext, CAST(:meta AS jsonb), 'delivered', :ts"
                ")"
            ),
            {
                "id": msg_id,
                "tid": claims.tenant_id,
                "conv_id": claims.conversation_id,
                "cid": claims.customer_id,
                "emid": f"wcin_{msg_id}",
                "vid": claims.visitor_id,
                "sid": str(claims.customer_id),
                "btext": payload.body_text,
                "meta": json.dumps({"page_url": payload.page_url, "sku": payload.sku}),
                "ts": now_ts,
            },
        )
        await session.execute(
            text(
                "UPDATE conversations SET last_inbound_at = :ts, last_message_preview = :preview, "
                "unread_count = unread_count + 1, updated_at = now() "
                "WHERE tenant_id = :tid AND id = :conv_id"
            ),
            {
                "tid": claims.tenant_id,
                "conv_id": claims.conversation_id,
                "ts": now_ts,
                "preview": payload.body_text[:180],
            },
        )
        scope_row = (
            (
                await session.execute(
                    text(
                        "SELECT c.assigned_user_id, a.visibility_scope "
                        "FROM conversations c JOIN channel_accounts a "
                        "ON a.tenant_id = c.tenant_id AND a.id = c.channel_account_id "
                        "WHERE c.tenant_id = :tid AND c.id = :conv_id"
                    ),
                    {"tid": claims.tenant_id, "conv_id": claims.conversation_id},
                )
            )
            .mappings()
            .one_or_none()
        )

    await redis.publish(
        f"inbox:events:{claims.tenant_id}",
        json.dumps(
            {
                "event": "message.created",
                "conversation_id": str(claims.conversation_id),
                "message_id": str(msg_id),
                "channel_type": ChannelType.WEB_LIVECHAT.value,
                "assigned_user_id": (
                    str(scope_row["assigned_user_id"])
                    if scope_row and scope_row["assigned_user_id"]
                    else None
                ),
                "visibility_scope": (
                    scope_row["visibility_scope"] if scope_row else "shared"
                ),
            }
        ),
    )
    return {
        "id": str(msg_id),
        "direction": "inbound",
        "sender_type": "customer",
        "body_text": payload.body_text,
        "created_at": now_ts.isoformat(),
    }


@widget_router.post("/identify")
async def identify_widget_visitor(
    payload: WidgetIdentifyRequest,
    request: Request,
    response: Response,
    claims: Annotated[WidgetTokenClaims, Depends(_decode_widget_token)],
) -> dict[str, Any]:
    if abs(int(time.time()) - payload.timestamp) > 300:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Stale widget identify timestamp",
        )

    session_factory = _get_session_factory(request)
    crypto = EnvelopeCryptoService(get_settings())
    identity_svc = IdentityResolverService()

    async with tenant_session(session_factory, claims.tenant_id) as session:
        await _validate_account_cors(
            session,
            claims.tenant_id,
            claims.channel_account_id,
            request.headers.get("origin"),
            response,
        )
        cred = (
            (
                await session.execute(
                    text(
                        "SELECT ciphertext, nonce, auth_tag, key_version FROM channel_credentials "
                        "WHERE tenant_id = :tid AND channel_account_id = :aid "
                        "AND credential_type = 'widget_hmac_secret' LIMIT 1"
                    ),
                    {"tid": claims.tenant_id, "aid": claims.channel_account_id},
                )
            )
            .mappings()
            .first()
        )
        if cred is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Widget identify secret not configured",
            )

        hmac_secret = await crypto.decrypt_secret(
            session=session,
            tenant_id=claims.tenant_id,
            account_id=claims.channel_account_id,
            cred_type="widget_hmac_secret",
            ciphertext=bytes(cred["ciphertext"]),
            nonce=bytes(cred["nonce"]),
            auth_tag=bytes(cred["auth_tag"]),
            key_version=int(cred["key_version"]),
        )
        # The visitor id surfaced to integrators is now server-signed, so a merchant page
        # may have signed either '<uuid>.<mac>' or the bare legacy UUID. Accept both forms
        # (both are public identifiers; only the merchant-held secret proves ownership) and
        # compare every candidate in constant time without short-circuiting.
        signed_visitor_id = sign_visitor_id(
            claims.visitor_id,
            tenant_id=claims.tenant_id,
            account_id=claims.channel_account_id,
            secret=_widget_signing_secret(),
        )
        signature_valid = False
        for candidate_vid in (claims.visitor_id, signed_visitor_id):
            candidate_canonical = (
                f"{candidate_vid}:{payload.email or ''}:{payload.phone_e164 or ''}:"
                f"{payload.external_customer_id or ''}:{payload.timestamp}"
            )
            candidate_sig = hmac.new(
                hmac_secret.encode("utf-8"),
                candidate_canonical.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            signature_valid |= hmac.compare_digest(payload.hmac_signature, candidate_sig)
        if not signature_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid HMAC signature",
            )

        display_name = (
            payload.display_name or payload.email or payload.phone_e164 or claims.visitor_id
        )
        if payload.email:
            target_cid, _ = await identity_svc.resolve_or_create_customer(
                session, claims.tenant_id, IdentityType.EMAIL, payload.email, display_name
            )
        elif payload.phone_e164:
            target_cid, _ = await identity_svc.resolve_or_create_customer(
                session, claims.tenant_id, IdentityType.PHONE_E164, payload.phone_e164, display_name
            )
        elif payload.external_customer_id:
            target_cid, _ = await identity_svc.resolve_or_create_customer(
                session,
                claims.tenant_id,
                IdentityType.SHOPIFY_CUSTOMER_ID,
                payload.external_customer_id,
                display_name,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one identifier is required",
            )

        merged = False
        if target_cid != claims.customer_id:
            await identity_svc.merge_customers(
                session=session,
                tenant_id=claims.tenant_id,
                source_customer_id=claims.customer_id,
                target_customer_id=target_cid,
                performed_by="widget_hmac_identify",
                reason="authenticated_storefront_identify",
            )
            merged = True

    upgraded_token = _sign_widget_token(
        WidgetTokenClaims(
            visitor_id=claims.visitor_id,
            tenant_id=claims.tenant_id,
            channel_account_id=claims.channel_account_id,
            customer_id=target_cid,
            conversation_id=claims.conversation_id,
        )
    )
    return {
        "session_token": upgraded_token,
        "customer_id": str(target_cid),
        "merged": merged,
    }
