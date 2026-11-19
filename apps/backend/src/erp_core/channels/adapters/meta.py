"""Shared Messenger/Instagram signature, inbox normalization and outbound policy."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedAttachment,
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.channels.messaging_accounts import invalid_connection, load_outbound_context
from erp_core.shared.enums import (
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageSenderType,
)
from erp_core.shared.errors import AppError

GRAPH_BASE_URL = "https://graph.facebook.com/v24.0"
_MEDIA = {"image": "image/*", "video": "video/*", "audio": "audio/*"}


def _objects(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        yield from (item for item in value if isinstance(item, dict))


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nonempty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(ms: Any) -> datetime | None:
    if type(ms) is not int or ms < 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


class MetaMessagingAdapter(MessagingChannelAdapter):
    identity_type: ClassVar[IdentityType]
    object_type: ClassVar[str]
    token_key: ClassVar[str]
    expired_code: ClassVar[str]

    def __init__(
        self,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.http_transport = http_transport
        self.clock = clock

    def verify_webhook_signature(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
        secret: str,
    ) -> bool:
        signature = next((v for k, v in headers.items() if k.lower() == "x-hub-signature-256"), "")
        expected = (
            b"sha256=" + hmac.digest(secret.encode(), raw_body, hashlib.sha256).hex().encode()
        )
        return bool(secret) and hmac.compare_digest(signature.encode(), expected)

    def _entries(self, payload: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        if payload.get("object") != self.object_type:
            return
        for entry in _objects(payload.get("entry")):
            account = _nonempty_string(entry.get("id"))
            if account and account.isascii() and account.isdecimal() and len(account) <= 32:
                for event in _objects(entry.get("messaging")):
                    yield account, event

    def _metadata(self, message: dict[str, Any]) -> dict[str, Any]:
        return {}

    def _normalize(self, payload: dict[str, Any]) -> NormalizedWebhookBatch:
        batch = NormalizedWebhookBatch()
        for account, event in self._entries(payload):
            sender = _nonempty_string(_object(event.get("sender")).get("id"))
            recipient = _object(event.get("recipient")).get("id")
            message = _object(event.get("message"))
            if not sender or sender == account or recipient != account or message.get("is_echo"):
                continue
            ts = _timestamp(event.get("timestamp"))
            mid = _nonempty_string(message.get("mid"))
            if mid and ts:
                body = _nonempty_string(message.get("text"))
                attachments = []
                for attachment in _objects(message.get("attachments")):
                    kind = _nonempty_string(attachment.get("type")) or "file"
                    data = _object(attachment.get("payload"))
                    url = _nonempty_string(data.get("url"))
                    media_id = _nonempty_string(data.get("attachment_id"))
                    if url or media_id:
                        attachments.append(
                            NormalizedAttachment(
                                filename=kind,
                                mime_type=_MEDIA.get(kind, "application/octet-stream"),
                                external_url=url,
                                external_media_id=media_id,
                            )
                        )
                metadata = self._metadata(message)
                if not (body or attachments or metadata):
                    continue
                batch.inbound_events.append(
                    NormalizedInboundEvent(
                        external_event_id=f"{account}:message:{mid}",
                        external_account_id=account,
                        external_message_id=mid,
                        external_thread_id=sender,
                        sender_identity_type=self.identity_type,
                        sender_identity_value=sender,
                        sender_display_name=sender,
                        body_text=body,
                        source_timestamp=ts,
                        attachments=attachments,
                        content_type=MessageContentType.MEDIA
                        if attachments
                        else MessageContentType.TEXT,
                        channel_metadata=metadata,
                        is_stop_command=bool(body and body.strip().upper() == "STOP"),
                    )
                )
            read = _object(event.get("read"))
            read_mid = _nonempty_string(read.get("mid"))
            watermark = _timestamp(read.get("watermark"))
            if read_mid and (read_time := ts or watermark):
                batch.status_events.append(
                    NormalizedStatusEvent(
                        external_message_id=read_mid,
                        external_account_id=account,
                        delivery_status=DeliveryStatus.READ,
                        timestamp=read_time,
                        source_external_event_id=f"{account}:read:{sender}:{read_mid}",
                        receipt_recipient_id=sender,
                        require_existing_message=True,
                    )
                )
            elif watermark:
                event_id = f"{account}:read:{sender}:{read['watermark']}"
                batch.status_events.append(
                    NormalizedStatusEvent(
                        external_message_id=event_id,
                        external_account_id=account,
                        delivery_status=DeliveryStatus.READ,
                        timestamp=watermark,
                        source_external_event_id=event_id,
                        receipt_recipient_id=sender,
                        read_through_recipient_id=sender,
                    )
                )
        return batch

    def extract_external_event_ids(
        self,
        payload: dict[str, Any],
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> list[tuple[str, str | None]]:
        batch = self._normalize(payload)
        return [
            *((ev.external_event_id, ev.external_account_id) for ev in batch.inbound_events),
            *(
                (ev.source_external_event_id, ev.external_account_id)
                for ev in batch.status_events
                if ev.source_external_event_id
            ),
        ]

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        return self._normalize(payload)

    def _send_path(self, external_account_id: str) -> str:
        return "/me/messages"

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        channel = self.channel_types[0]
        context = await load_outbound_context(session, tenant_id, request, channel)
        if context is None or not context["external_thread_id"]:
            return OutboundSendResult(success=False, error_message="ERR_INVALID_OUTBOUND_CONTEXT")
        recipient = context["external_thread_id"]
        if await ConsentAndSuppressionService().is_suppressed(
            session,
            tenant_id,
            channel.value,
            recipient,
        ):
            return OutboundSendResult(success=False, error_message="ERR_RECIPIENT_SUPPRESSED")

        now = self.clock()
        csw = context["csw_expires_at"]
        human = context["human_agent_window_expires_at"]
        body: dict[str, Any] = {"recipient": {"id": recipient}, "messaging_type": "RESPONSE"}
        if csw is None or now >= csw:
            if (
                human is None
                or now > human
                or context["sender_type"] != MessageSenderType.HUMAN_AGENT
            ):
                return OutboundSendResult(success=False, error_message=self.expired_code)
            body.update(messaging_type="MESSAGE_TAG", tag="HUMAN_AGENT")

        if context["content_type"] != MessageContentType.TEXT or not context["body_text"]:
            return OutboundSendResult(
                success=False, error_message="ERR_UNSUPPORTED_MESSAGE_CONTENT"
            )
        body["message"] = {"text": context["body_text"]}
        token = credentials.get(self.token_key)
        if not isinstance(token, str) or not token:
            return OutboundSendResult(success=False, error_message="ERR_CREDENTIAL_NOT_FOUND")
        try:
            async with httpx.AsyncClient(transport=self.http_transport, timeout=10) as client:
                response = await client.post(
                    GRAPH_BASE_URL + self._send_path(request.external_account_id),
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
            response.raise_for_status()
            message_id = _nonempty_string(_object(response.json()).get("message_id"))
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return OutboundSendResult(
                success=False,
                error_code=code,
                error_message="ERR_META_SEND_FAILED",
                retryable=code >= 500 or code == 429,
            )
        except (httpx.RequestError, ValueError):
            return OutboundSendResult(
                success=False, error_message="ERR_META_SEND_FAILED", retryable=True
            )
        if not message_id:
            return OutboundSendResult(success=False, error_message="ERR_META_SEND_FAILED")
        return OutboundSendResult(success=True, external_message_id=message_id)


async def validate_graph_account(
    path: str,
    account: str,
    token: str,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            response = await client.get(
                GRAPH_BASE_URL + path,
                params={"fields": "id"},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code in (400, 401, 403, 404):
            raise invalid_connection("The token cannot access this provider account.")
        response.raise_for_status()
        actual = _object(response.json()).get("id")
    except (httpx.HTTPError, ValueError):
        raise AppError(
            slug="meta-provider-unavailable",
            title="Meta API Unavailable",
            status_code=502,
            code="ERR_META_PROVIDER_UNAVAILABLE",
            detail="Meta did not return a valid account response.",
        ) from None
    if actual != account:
        raise invalid_connection("The token does not match this provider account.")
