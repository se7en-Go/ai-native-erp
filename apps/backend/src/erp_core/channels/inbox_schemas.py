from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

# Conversation pinning/tagging lives inside `conversations.channel_policy_state`
# (JSONB), so the frozen Phase 1–2 schema needs no Alembic revision for it.
CONVERSATION_PINNED_STATE_KEY = "is_pinned"
CONVERSATION_TAGS_STATE_KEY = "tags"
MAX_CONVERSATION_TAGS = 10
MAX_CONVERSATION_TAG_LENGTH = 32


def normalize_conversation_tags(values: Iterable[str]) -> list[str]:
    """Strip, drop blanks, deduplicate case-insensitively, then cap count and length."""
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in values:
        tag = str(raw).strip()[:MAX_CONVERSATION_TAG_LENGTH].strip()
        if not tag:
            continue
        fingerprint = tag.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        normalized.append(tag)
        if len(normalized) == MAX_CONVERSATION_TAGS:
            break
    return normalized


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
    is_pinned: bool = False
    tags: list[str] = Field(default_factory=list)


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
    is_pinned: bool | None = None
    tags: list[str] | None = None

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_conversation_tags(value)
