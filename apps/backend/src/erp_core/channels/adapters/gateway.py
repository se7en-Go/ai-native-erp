"""Minimal ingress adapter used until a channel-specific adapter is installed.

Verifies HMAC and normalizes the WhatsApp Cloud webhook shape far enough for
the inbox pipeline to persist one inbound message. It does not call Meta.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.adapters.base import (
    MessagingChannelAdapter,
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.shared.enums import (
    ChannelType,
    ConversationMode,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)


class GatewayIngressAdapter(MessagingChannelAdapter):
    channel_types: ClassVar[tuple[ChannelType, ...]] = (
        ChannelType.WHATSAPP_CLOUD,
        ChannelType.WHATSAPP_COEXISTENCE,
        ChannelType.EMAIL,
        ChannelType.WEB_LIVECHAT,
    )

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        provided = headers.get("x-hub-signature-256", "")
        if not secret or not provided:
            return False
        expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        events: list[tuple[str, str | None]] = []
        for entry in payload.get("entry", []):
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes", []):
                if not isinstance(change, dict):
                    continue
                value = change.get("value", {})
                if not isinstance(value, dict):
                    continue
                metadata = value.get("metadata", {})
                phone_id = metadata.get("phone_number_id") if isinstance(metadata, dict) else None
                for message in value.get("messages", []):
                    if isinstance(message, dict) and message.get("id"):
                        account_id = None if phone_id is None else str(phone_id)
                        events.append((str(message["id"]), account_id))
                for status in value.get("statuses", []):
                    if isinstance(status, dict) and status.get("id") and status.get("status"):
                        events.append(
                            (
                                f"status:{status['id']}:{status['status']}",
                                None if phone_id is None else str(phone_id),
                            )
                        )
        if not events:
            events.append((hashlib.sha256(raw_body).hexdigest(), None))
        return events

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        del session, tenant_id, channel_account_id, headers
        inbound: list[NormalizedInboundEvent] = []
        statuses: list[NormalizedStatusEvent] = []
        for entry in payload.get("entry", []):
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes", []):
                if not isinstance(change, dict):
                    continue
                value = change.get("value", {})
                if not isinstance(value, dict):
                    continue
                metadata = value.get("metadata", {})
                phone_id = ""
                if isinstance(metadata, dict) and metadata.get("phone_number_id"):
                    phone_id = str(metadata["phone_number_id"])
                for message in value.get("messages", []):
                    if not isinstance(message, dict) or not message.get("id"):
                        continue
                    text_body = ""
                    text_obj = message.get("text")
                    if isinstance(text_obj, dict):
                        text_body = str(text_obj.get("body") or "")
                    inbound.append(
                        NormalizedInboundEvent(
                            external_event_id=str(message["id"]),
                            external_account_id=phone_id,
                            external_message_id=str(message["id"]),
                            external_thread_id=str(message.get("from") or ""),
                            sender_identity_type=IdentityType.WHATSAPP_WAID,
                            sender_identity_value=str(message.get("from") or ""),
                            sender_display_name=str(message.get("from") or "buyer"),
                            direction=MessageDirection.INBOUND,
                            sender_type=MessageSenderType.CUSTOMER,
                            content_type=MessageContentType.TEXT,
                            body_text=text_body,
                            source_timestamp=datetime.fromtimestamp(
                                int(message.get("timestamp") or 0), tz=UTC
                            ),
                            conversation_mode=ConversationMode.B2C_SUPPORT,
                        )
                    )
                for status in value.get("statuses", []):
                    if not isinstance(status, dict) or not status.get("id"):
                        continue
                    statuses.append(
                        NormalizedStatusEvent(
                            external_message_id=str(status["id"]),
                            external_account_id=phone_id,
                            delivery_status=DeliveryStatus(str(status["status"])),
                            timestamp=datetime.fromtimestamp(
                                int(status.get("timestamp") or 0), tz=UTC
                            ),
                            require_existing_message=True,
                        )
                    )
        return NormalizedWebhookBatch(inbound_events=inbound, status_events=statuses)

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        del session, tenant_id, credentials, request
        return OutboundSendResult(
            success=False,
            error_message="Gateway ingress adapter does not send outbound messages",
            retryable=False,
        )
