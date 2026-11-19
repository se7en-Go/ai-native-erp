from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from redis.asyncio import Redis
from sqlalchemy import text
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


class WebLiveChatAdapter(MessagingChannelAdapter):
    """Self-hosted Web LiveChat adapter emitting live SSE replies back to storefront visitors."""

    channel_types: ClassVar[tuple[ChannelType, ...]] = (ChannelType.WEB_LIVECHAT,)

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        return True

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
        """Deliver outbound reply directly to visitor widget stream via Redis Pub/Sub."""
        ext_id = f"wc_{request.message_id}"
        await session.execute(
            text(
                "UPDATE messages SET delivery_status = 'delivered', external_message_id = :emid "
                "WHERE tenant_id = :tid AND id = :mid"
            ),
            {"tid": tenant_id, "mid": request.message_id, "emid": ext_id},
        )
        if self._redis is not None:
            await self._redis.publish(
                f"widget:conv:{request.conversation_id}",
                json.dumps(
                    {
                        "id": str(request.message_id),
                        "direction": "outbound",
                        "sender_type": "human_agent",
                        "body_text": request.body_text,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                ),
            )
        return OutboundSendResult(success=True, external_message_id=ext_id)
