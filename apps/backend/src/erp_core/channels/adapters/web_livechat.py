from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedInboundEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.shared.enums import (
    ChannelType,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)

logger = logging.getLogger(__name__)

# Integrator-facing webhook signature headers, most specific first.
_SIGNATURE_HEADERS: tuple[str, ...] = ("x-widget-signature", "x-webhook-signature")
_SHA256_PREFIX = "sha256="


def _provided_signature(headers: Mapping[str, str]) -> str:
    """Return the lowercase hex digest from the first non-empty signature header."""
    lowered = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    for header in _SIGNATURE_HEADERS:
        value = lowered.get(header, "")
        if not value:
            continue
        if value[: len(_SHA256_PREFIX)].lower() == _SHA256_PREFIX:
            value = value[len(_SHA256_PREFIX) :].strip()
        return value.lower()
    return ""


class WebLiveChatAdapter(MessagingChannelAdapter):
    """Self-hosted Web LiveChat adapter emitting live SSE replies back to storefront visitors."""

    channel_types: ClassVar[tuple[ChannelType, ...]] = (ChannelType.WEB_LIVECHAT,)

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        """Verify ``hex(HMAC-SHA256(secret, raw_body))`` from the widget signature header.

        Accepts both a bare hex digest and the ``sha256=<hex>`` form. A missing/empty
        secret or signature, or any mismatch, fails closed instead of accepting the
        webhook, so an unsigned visitor payload can never be replayed into ingest.
        """
        if not secret or not secret.strip():
            return False
        provided = _provided_signature(headers)
        if not provided:
            return False
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("ascii"))

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        return [(str(payload.get("event_id") or uuid.uuid4()), payload.get("site_key"))]

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        visitor_id = str(payload["visitor_id"])
        return NormalizedWebhookBatch(
            inbound_events=[
                NormalizedInboundEvent(
                    external_event_id=str(payload.get("event_id") or uuid.uuid4()),
                    external_account_id=str(payload["site_key"]),
                    external_message_id=str(payload.get("message_id") or uuid.uuid4()),
                    external_thread_id=visitor_id,
                    sender_identity_type=IdentityType.WEB_VISITOR_ID,
                    sender_identity_value=visitor_id,
                    sender_display_name=str(
                        payload.get("display_name") or f"Visitor {visitor_id[:6]}"
                    ),
                    direction=MessageDirection.INBOUND,
                    sender_type=MessageSenderType.CUSTOMER,
                    content_type=MessageContentType.TEXT,
                    body_text=str(payload.get("body_text") or ""),
                    source_timestamp=datetime.now(UTC),
                )
            ]
        )

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        """Acknowledge a widget reply; visitor clients pull it from the messages table.

        A self-hosted widget has no external HTTP push. The agent-side outbound worker
        already persists ``delivery_status = 'sent'`` and this ``external_message_id``
        immediately after a successful send, so the adapter must not race that write.
        Widget visitors read the committed reply through ``GET /api/v1/widget/messages``.
        """
        external_message_id = f"web:{request.message_id}"
        await self._publish_widget_reply(request, external_message_id)
        return OutboundSendResult(success=True, external_message_id=external_message_id)

    async def _publish_widget_reply(
        self, request: OutboundMessageRequest, external_message_id: str
    ) -> None:
        """Best-effort SSE nudge; a Redis hiccup must never fail the outbound send."""
        if self._redis is None:
            return
        try:
            await self._redis.publish(
                f"widget:conv:{request.conversation_id}",
                json.dumps(
                    {
                        "event": "message.created",
                        "id": str(request.message_id),
                        "external_message_id": external_message_id,
                        "conversation_id": str(request.conversation_id),
                        "direction": "outbound",
                        "body_text": request.body_text,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                ),
            )
        except Exception:  # noqa: BLE001 - widget push must not fail delivery
            logger.warning(
                "widget reply publish failed for conversation %s",
                request.conversation_id,
                exc_info=True,
            )
