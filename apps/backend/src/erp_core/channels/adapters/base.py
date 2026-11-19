from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.enums import (
    ChannelType,
    ConversationMode,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)


class NormalizedAttachment(BaseModel):
    filename: str
    mime_type: str
    size_bytes: int = 0
    r2_key: str | None = None
    external_url: str | None = None
    external_media_id: str | None = None


class NormalizedInboundEvent(BaseModel):
    external_event_id: str
    external_account_id: str
    external_message_id: str | None = None
    external_thread_id: str | None = None
    subject: str | None = None
    sender_identity_type: IdentityType | None
    sender_identity_value: str
    sender_display_name: str
    country_hint: str | None = None
    direction: MessageDirection = MessageDirection.INBOUND
    sender_type: MessageSenderType = MessageSenderType.CUSTOMER
    content_type: MessageContentType = MessageContentType.TEXT
    body_text: str | None = None
    body_sanitized_html: str | None = None
    attachments: list[NormalizedAttachment] = Field(default_factory=list)
    source_timestamp: datetime
    conversation_mode: ConversationMode = ConversationMode.B2C_SUPPORT
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    ctwa_clid: str | None = None
    referral_ad_id: str | None = None
    extra_identities: list[tuple[IdentityType, str]] = Field(default_factory=list)
    b2b_inquiry_data: dict[str, Any] | None = None
    is_stop_command: bool = False
    opens_csw: bool = True
    parent_external_id: str | None = None
    resolved_conversation_id: UUID | None = None
    override_channel_type: ChannelType | None = None


class NormalizedStatusEvent(BaseModel):
    external_message_id: str
    external_account_id: str
    delivery_status: DeliveryStatus
    timestamp: datetime
    error_code: int | None = None
    error_message: str | None = None
    pricing_metadata: dict[str, Any] = Field(default_factory=dict)
    require_existing_message: bool = False
    source_external_event_id: str | None = None
    receipt_recipient_id: str | None = None
    read_through_recipient_id: str | None = None


class NormalizedWebhookBatch(BaseModel):
    inbound_events: list[NormalizedInboundEvent] = Field(default_factory=list)
    status_events: list[NormalizedStatusEvent] = Field(default_factory=list)
    account_updates: list[dict[str, Any]] = Field(default_factory=list)


class OutboundMessageRequest(BaseModel):
    tenant_id: UUID
    channel_account_id: UUID
    channel_type: ChannelType
    external_account_id: str
    recipient_id: str
    conversation_id: UUID
    message_id: UUID
    content_type: MessageContentType = MessageContentType.TEXT
    body_text: str | None = None
    body_html: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_components: list[dict[str, Any]] = Field(default_factory=list)
    reply_to_address: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    subject: str | None = None
    from_address: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutboundSendResult(BaseModel):
    success: bool
    external_message_id: str | None = None
    error_code: int | None = None
    error_message: str | None = None
    retryable: bool = False
    estimated_cost_usd: Decimal = Decimal("0.000000")
    pricing_category: str | None = None


class MessagingChannelAdapter(ABC):
    """Canonical interface implemented by every messaging channel adapter."""

    channel_types: ClassVar[tuple[ChannelType, ...]]

    @abstractmethod
    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        raise NotImplementedError

    @abstractmethod
    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        channel_account_id: UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        raise NotImplementedError

    @abstractmethod
    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        raise NotImplementedError


class AdapterRegistry:
    """In-process registry resolving channel slugs and ChannelType values to adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, MessagingChannelAdapter] = {}

    def register(self, key: str, adapter: MessagingChannelAdapter) -> None:
        self._adapters[key.lower()] = adapter

    def get(self, key: str) -> MessagingChannelAdapter | None:
        return self._adapters.get(key.lower())
