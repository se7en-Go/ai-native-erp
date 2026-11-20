from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.identity.schemas import CustomerSummaryRead
from erp_core.shared.enums import (
    AutonomyLevel,
    ChannelType,
    ConversationMode,
    ConversationStatus,
    DeliveryStatus,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)

InboxTabFilter = Literal["all", "unread", "pending_human", "sla_urgent"]


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    customer_id: UUID
    channel_account_id: UUID
    channel_type: ChannelType
    external_thread_id: str | None = None
    subject: str | None = None
    conversation_mode: ConversationMode
    status: ConversationStatus
    assigned_user_id: UUID | None = None
    ai_autonomy_override: AutonomyLevel | None = None
    csw_expires_at: datetime | None = None
    fep_expires_at: datetime | None = None
    human_agent_window_expires_at: datetime | None = None
    sla_due_at: datetime | None = None
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    last_message_preview: str | None = None
    unread_count: int
    updated_at: datetime
    customer_name: str | None = None
    country_code: str | None = None
    iana_timezone: str | None = None
    customer: CustomerSummaryRead | None = None
    assigned_user_name: str | None = None
    channel_account_display_name: str | None = None
    channel_visibility_scope: Literal["shared", "personal"] | None = None
    channel_owner_user_id: UUID | None = None


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    channel_type: ChannelType
    external_message_id: str | None = None
    direction: MessageDirection
    sender_type: MessageSenderType
    sender_id: str | None = None
    content_type: MessageContentType
    body_text: str | None = None
    body_sanitized_html: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_status: DeliveryStatus
    channel_msg_cost: Decimal = Decimal("0.000000")
    source_timestamp: datetime
    created_at: datetime


class SendInboxMessageRequest(BaseModel):
    direction: Literal["outbound", "internal_note"] = "outbound"
    content_type: MessageContentType = MessageContentType.TEXT
    body_text: str | None = None
    body_html: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_components: list[dict[str, Any]] = Field(default_factory=list)
    translate_to: str | None = None
    target_language: str | None = None
    translate_before_send: bool = False
    use_human_agent_tag: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SendInboxMessageResponse(BaseModel):
    message: MessageRead
    translated_preview: str | None = None
    queued_for_delivery: bool


class UpdateConversationRequest(BaseModel):
    status: ConversationStatus | None = None
    assigned_user_id: UUID | None = None
    conversation_mode: ConversationMode | None = None
    ai_autonomy_override: AutonomyLevel | None = None
    mark_read: bool | None = None
