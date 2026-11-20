from __future__ import annotations

import hmac
import os
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedAttachment,
    NormalizedInboundEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.channels.messaging_accounts import (
    MESSAGING_CREDENTIAL,
    ChannelConnectorPrincipal,
    ConnectMessagingResponse,
    MessagingConnectRoute,
    invalid_connection,
    load_outbound_context,
    messaging_sessions,
    provider_token,
    provider_transport,
    register_messaging_account,
    resolve_connect_ownership,
    save_messaging_credentials,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, MessageContentType, UserRole
from erp_core.shared.errors import AppError

TELEGRAM_SECRET_HEADER = "x-telegram-bot-api-secret-token"
# Internal header written by the ingress router from the validated URL path.
TELEGRAM_ROUTING_HEADER = "x-erp-telegram-bot-key"
TELEGRAM_BOT_KEY = re.compile(r"[1-9][0-9]{0,19}")
_BOT_TOKEN = re.compile(r"([1-9][0-9]{0,19}):[A-Za-z0-9_-]{10,200}")


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _positive_id(value: Any) -> int | None:
    return value if type(value) is int and 0 < value < 2**63 else None


def _attachment(obj: dict[str, Any], *, photo: bool) -> NormalizedAttachment | None:
    file_id = _string(obj.get("file_id"))
    if not file_id:
        return None
    return NormalizedAttachment(
        filename=_string(obj.get("file_name")) or ("photo.jpg" if photo else "document"),
        mime_type=_string(obj.get("mime_type"))
        or ("image/jpeg" if photo else "application/octet-stream"),
        external_media_id=file_id,
        size_bytes=_positive_id(obj.get("file_size")) or 0,
    )


class TelegramAPIError(Exception):
    def __init__(self, status: int = 502) -> None:
        super().__init__("Telegram Bot API request failed")
        self.status = status


class TelegramAdapter(MessagingChannelAdapter):
    channel_types = (ChannelType.TELEGRAM,)

    def __init__(self, *, http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.http_transport = http_transport

    def verify_webhook_signature(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
        secret: str,
    ) -> bool:
        supplied = next(
            (value for name, value in headers.items() if name.lower() == TELEGRAM_SECRET_HEADER), ""
        )
        return bool(secret) and hmac.compare_digest(supplied.encode(), secret.encode())

    def _normalize(
        self,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedInboundEvent | None:
        bot = headers.get(TELEGRAM_ROUTING_HEADER, "")
        update_id = _positive_id(payload.get("update_id"))
        if not TELEGRAM_BOT_KEY.fullmatch(bot) or update_id is None:
            return None
        business = "business_message" in payload
        msg = _object(payload.get("business_message" if business else "message"))
        if not msg or msg.get("sender_business_bot") is not None:
            return None
        business_id = _string(msg.get("business_connection_id")) if business else None
        if business and (
            not business_id or len(business_id) > 256 or not business_id.isprintable()
        ):
            return None
        chat = _object(msg.get("chat"))
        author = _object(msg.get("from"))
        peer = _positive_id(chat.get("id"))
        if (
            chat.get("type") != "private"
            or peer is None
            or author.get("is_bot")
            or _positive_id(author.get("id")) != peer
        ):
            return None  # One-to-one inbox; also reject customer-invisible outbound/echo updates.
        msg_id = _positive_id(msg.get("message_id"))
        timestamp = _positive_id(msg.get("date"))
        if not msg_id or not timestamp:
            return None
        try:
            when = datetime.fromtimestamp(timestamp, UTC)
        except (OverflowError, OSError, ValueError):
            return None

        attachments = []
        if document := _attachment(_object(msg.get("document")), photo=False):
            attachments.append(document)
        photos = msg.get("photo")
        if isinstance(photos, list) and photos and (
            photo := _attachment(_object(photos[-1]), photo=True)
        ):
            attachments.append(photo)
        body_text = _string(msg.get("text")) or _string(msg.get("caption"))
        if not body_text and not attachments:
            return None

        thread = f"business:{business_id}:{peer}" if business_id else str(peer)
        display_name = " ".join(
            filter(None, (_string(author.get("first_name")), _string(author.get("last_name"))))
        ) or str(peer)
        metadata = {"chat_id": peer, "telegram_user_id": peer, "telegram_message_id": msg_id}
        return NormalizedInboundEvent(
            external_event_id=f"{bot}:update:{update_id}",
            external_account_id=bot,
            external_message_id=f"{bot}:{thread}:{msg_id}",
            external_thread_id=thread,
            sender_identity_type=None,
            sender_identity_value=str(peer),
            sender_display_name=display_name,
            body_text=body_text,
            content_type=MessageContentType.MEDIA if attachments else MessageContentType.TEXT,
            attachments=attachments,
            source_timestamp=when,
            opens_csw=False,
            channel_metadata={**metadata, "business_connection_id": business_id}
            if business_id
            else metadata,
            is_stop_command=bool(body_text and body_text.strip().upper() == "STOP"),
        )

    def extract_external_event_ids(
        self,
        payload: dict[str, Any],
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> list[tuple[str, str | None]]:
        event = self._normalize(payload, headers)
        return [(event.external_event_id, event.external_account_id)] if event else []

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        event = self._normalize(payload, headers)
        return NormalizedWebhookBatch(inbound_events=[event] if event else [])

    async def _bot_api(self, token: str, method: str, data: dict[str, Any]) -> Any:
        # The Bot API embeds the token in its URL. Use the HTTP transport directly
        # to avoid httpx.AsyncClient's default INFO log of the full request URL.
        request = httpx.Request(
            "POST",
            f"https://api.telegram.org/bot{token}/{method}",
            json=data,
            extensions={"timeout": httpx.Timeout(10).as_dict()},
        )
        transport = self.http_transport or httpx.AsyncHTTPTransport(trust_env=False)
        try:
            async with transport:
                response = await transport.handle_async_request(request)
                try:
                    await response.aread()
                    if not response.is_success:
                        raise TelegramAPIError(response.status_code)
                    payload = _object(response.json())
                finally:
                    await response.aclose()
        except (httpx.HTTPError, ValueError):
            raise TelegramAPIError() from None
        if payload.get("ok") is not True:
            raise TelegramAPIError(_positive_id(payload.get("error_code")) or 502)
        return payload.get("result")

    async def identify_bot_profile(
        self, token: str
    ) -> tuple[str, str | None, str | None]:
        """Return ``(bot_id, username, first_name)`` for a single-paste connect flow."""
        match = _BOT_TOKEN.fullmatch(token)
        if not match:
            raise invalid_connection("The Telegram bot token is invalid.")
        try:
            bot = _object(await self._bot_api(token, "getMe", {}))
        except TelegramAPIError as exc:
            if exc.status in (400, 401, 403, 404):
                raise invalid_connection("Telegram rejected the bot token.") from None
            raise _provider_unavailable() from None
        bot_id = _positive_id(bot.get("id"))
        if not bot.get("is_bot") or bot_id is None or str(bot_id) != match[1]:
            raise invalid_connection("The token does not match a Telegram bot account.")
        return str(bot_id), _string(bot.get("username")), _string(bot.get("first_name"))

    async def identify_bot(self, token: str) -> str:
        bot_id, _, _ = await self.identify_bot_profile(token)
        return bot_id

    async def set_webhook(self, token: str, url: str, secret: str) -> None:
        try:
            ok = await self._bot_api(
                token,
                "setWebhook",
                {
                    "url": url,
                    "secret_token": secret,
                    "allowed_updates": ["message", "business_message"],
                },
            )
        except TelegramAPIError:
            raise _provider_unavailable() from None
        if ok is not True:
            raise _provider_unavailable()

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        context = await load_outbound_context(session, tenant_id, request, ChannelType.TELEGRAM)
        thread = context["external_thread_id"] if context else None
        if not context or not isinstance(thread, str):
            return OutboundSendResult(success=False, error_message="ERR_INVALID_OUTBOUND_CONTEXT")
        business_id, peer = None, thread
        if thread.startswith("business:"):
            business_id, _, peer = thread.removeprefix("business:").rpartition(":")
            if not business_id:
                return OutboundSendResult(
                    success=False, error_message="ERR_INVALID_OUTBOUND_CONTEXT"
                )
        if not TELEGRAM_BOT_KEY.fullmatch(peer):
            return OutboundSendResult(success=False, error_message="ERR_INVALID_OUTBOUND_CONTEXT")
        if await ConsentAndSuppressionService().is_suppressed(session, tenant_id, "telegram", peer):
            return OutboundSendResult(success=False, error_message="ERR_RECIPIENT_SUPPRESSED")
        if context["content_type"] != MessageContentType.TEXT or not context["body_text"]:
            return OutboundSendResult(
                success=False, error_message="ERR_UNSUPPORTED_MESSAGE_CONTENT"
            )
        raw_token = credentials.get("bot_token")
        token = raw_token if isinstance(raw_token, str) else ""
        match = _BOT_TOKEN.fullmatch(token)
        if not match or match[1] != request.external_account_id:
            return OutboundSendResult(success=False, error_message="ERR_CREDENTIAL_NOT_FOUND")
        data: dict[str, Any] = {"chat_id": int(peer), "text": context["body_text"]}
        if business_id:
            data["business_connection_id"] = business_id
        try:
            result = _object(await self._bot_api(token, "sendMessage", data))
        except TelegramAPIError as exc:
            return OutboundSendResult(
                success=False,
                error_code=exc.status,
                error_message="ERR_TELEGRAM_SEND_FAILED",
                retryable=exc.status >= 500 or exc.status == 429,
            )
        msg_id = _positive_id(result.get("message_id"))
        if not msg_id:
            return OutboundSendResult(success=False, error_message="ERR_TELEGRAM_SEND_FAILED")
        return OutboundSendResult(
            success=True, external_message_id=f"{request.external_account_id}:{thread}:{msg_id}"
        )


def _provider_unavailable() -> AppError:
    return AppError(
        slug="telegram-provider-unavailable",
        title="Telegram API Unavailable",
        status_code=502,
        code="ERR_TELEGRAM_PROVIDER_UNAVAILABLE",
        detail="Telegram did not complete the requested operation.",
    )


def _webhook_url(bot: str) -> str:
    try:
        base = httpx.URL(os.environ.get("TELEGRAM_WEBHOOK_BASE_URL", ""))
        if (
            base.scheme == "https"
            and base.host
            and not base.userinfo
            and not base.query
            and not base.fragment
            and base.path == "/"
        ):
            return str(base.copy_with(path=f"/api/v1/webhooks/telegram/{bot}"))
    except httpx.InvalidURL:
        pass
    raise AppError(
        slug="telegram-webhook-not-configured",
        title="Telegram Webhook Not Configured",
        status_code=503,
        code="ERR_TELEGRAM_WEBHOOK_NOT_CONFIGURED",
        detail="Set TELEGRAM_WEBHOOK_BASE_URL to the public HTTPS origin.",
    )


class TelegramConnectRequest(BaseModel):
    bot_token: SecretStr
    display_name: str | None = Field(default=None, max_length=150)
    visibility_scope: Literal["shared", "personal"] | None = None
    owner_user_id: UUID | None = None


telegram_router = APIRouter(
    prefix="/api/v1/channels/telegram", tags=["channels"], route_class=MessagingConnectRoute
)


def _display_name(payload: TelegramConnectRequest, username: str | None, first: str | None,
                  bot_id: str) -> str:
    """One-paste connect: never force a human to type a name Telegram already knows."""
    requested = (payload.display_name or "").strip()
    if requested:
        return requested
    if first:
        return first[:150]
    if username:
        return f"@{username}"[:150]
    return f"Telegram Bot {bot_id}"[:150]


@telegram_router.post("/connect")
async def connect_telegram(
    payload: TelegramConnectRequest,
    request: Request,
    principal: ChannelConnectorPrincipal,
) -> ConnectMessagingResponse:
    token = provider_token(payload.bot_token)
    adapter = TelegramAdapter(http_transport=provider_transport(request))
    bot, username, first_name = await adapter.identify_bot_profile(token)
    url = _webhook_url(bot)
    tenant = principal.tenant_id
    effective_display_name = _display_name(payload, username, first_name, bot)
    async with tenant_session(messaging_sessions(request), tenant) as session:
        owner_uid, vis_scope = await resolve_connect_ownership(
            session,
            principal,
            payload.visibility_scope,
            payload.owner_user_id,
            allow_agent_self_bind=True,
        )
        aid = await register_messaging_account(
            session,
            tenant,
            ChannelType.TELEGRAM,
            bot,
            effective_display_name,
            owner_user_id=owner_uid,
            visibility_scope=vis_scope,
            actor_user_id=principal.user_id,
            actor_is_admin=principal.role in (UserRole.OWNER, UserRole.ADMIN),
            config_patch={"bot_username": username} if username else None,
        )
        vault = CredentialVaultHelper()
        try:
            old, _ = await vault.load_secret_json(session, tenant, aid, MESSAGING_CREDENTIAL)
        except AppError as exc:
            if exc.code != "ERR_CREDENTIAL_NOT_FOUND":
                raise
            old = {}
        secret = _string(old.get("webhook_secret")) or secrets.token_urlsafe(32)
        await save_messaging_credentials(
            session, tenant, aid, {"bot_token": token, "webhook_secret": secret}
        )
        # Provider mutation must occur only after this transaction has proven routing ownership.
        await adapter.set_webhook(token, url, secret)
    return ConnectMessagingResponse(
        channel_account_id=aid,
        external_account_id=bot,
        display_name=effective_display_name,
        owner_user_id=owner_uid,
        visibility_scope=vis_scope,
        bot_username=username,
    )
