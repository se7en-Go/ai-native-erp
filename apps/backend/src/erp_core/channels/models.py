from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class ChannelRoutingDirectory(Base):
    """Non-PII global directory mapping external channel identifiers to tenants."""

    __tablename__ = "channel_routing_directory"
    __table_args__ = (PrimaryKeyConstraint("channel_type", "external_account_id"),)

    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_account_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ChannelAccount(Base):
    """Tenant-owned messaging channel connection metadata."""

    __tablename__ = "channel_accounts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_type",
            "external_account_id",
            name="uq_channel_accounts_tenant_type_ext",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    visibility_scope: Mapped[str] = mapped_column(
        Text, nullable=False, default="shared", server_default=text("'shared'")
    )
    rate_limit_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: {"mps": 80, "coexistence": False}
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class RawWebhookEvent(Base):
    """Immutable ingress webhook store deduplicating upstream retry storms."""

    __tablename__ = "raw_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "channel_type",
            "external_event_id",
            name="uq_raw_webhook_events_channel_ext_event",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    processing_status: Mapped[str] = mapped_column(Text, nullable=False, default="received")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeadLetterEvent(Base):
    """Failed inbound or outbound webhook/delivery event queued for operator replay."""

    __tablename__ = "dead_letter_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    raw_webhook_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("raw_webhook_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False, default="inbound")
    external_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Conversation(Base):
    """Unified omnichannel conversation thread with channel compliance windows."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("channel_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_mode: Mapped[str] = mapped_column(Text, nullable=False, default="b2c_support")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    ai_autonomy_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    csw_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fep_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ctwa_clid: Mapped[str | None] = mapped_column(Text, nullable=True)
    human_agent_window_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_message_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    channel_policy_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Message(Base):
    """Normalized omnichannel message with partial unique idempotency guard."""

    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "uq_messages_idempotency",
            "tenant_id",
            "channel_type",
            "external_message_id",
            unique=True,
            postgresql_where=text("external_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    customer_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_sender_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    sender_type: Mapped[str] = mapped_column(Text, nullable=False)
    sender_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_sanitized_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    channel_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    delivery_status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    channel_msg_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ConsentRecord(Base):
    """Append-only opt-in and opt-out proof ledger."""

    __tablename__ = "consent_records"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    consent_status: Mapped[str] = mapped_column(Text, nullable=False)
    opt_in_scope: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=lambda: ["order_updates"]
    )
    disclosed_business_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_source: Mapped[str] = mapped_column(Text, nullable=False)
    proof_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SuppressionEntry(Base):
    """Hard outbound suppression list checked prior to every outbound dispatch."""

    __tablename__ = "suppression_list"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_type",
            "normalized_address",
            name="uq_suppression_list_tenant_channel_addr",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_type: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_address: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
