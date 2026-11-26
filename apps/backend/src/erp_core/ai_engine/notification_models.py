"""SQLAlchemy 2.0 models for outbound notification channels and delivery ledger.

Mirrors migration `0020_notifications_automation`. The signing secret is stored only as
the four-part AES-256-GCM envelope tuple produced by `EnvelopeCryptoService`
(`AAD = f"{tenant_id}:{channel_id}:webhook_signing_secret"`); no column here ever holds
the plaintext secret.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class NotificationChannel(Base):
    """A Feishu / DingTalk / WeCom bot or generic webhook the tenant delivers to."""

    __tablename__ = "notification_channels"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_url: Mapped[str] = mapped_column(Text, nullable=False)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secret_auth_tag: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secret_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_priority: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'P1'"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "provider IN ('feishu', 'dingtalk', 'wecom', 'generic_webhook')",
            name="ck_notification_channels_provider",
        ),
        CheckConstraint(
            "min_priority IN ('P0', 'P1', 'P2')",
            name="ck_notification_channels_min_priority",
        ),
        Index("idx_notification_channels_tenant_enabled", "tenant_id", "is_enabled"),
        Index("idx_notification_channels_tenant_provider", "tenant_id", "provider"),
    )


class NotificationDelivery(Base):
    """One queued delivery attempt-set for a single channel and priority."""

    __tablename__ = "notification_deliveries"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("notification_channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    priority: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    deep_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "priority IN ('P0', 'P1', 'P2')", name="ck_notification_deliveries_priority"
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'retrying')",
            name="ck_notification_deliveries_status",
        ),
        Index("idx_notification_deliveries_tenant_created", "tenant_id", "created_at"),
        Index("idx_notification_deliveries_channel_created", "channel_id", "created_at"),
        Index(
            "idx_notification_deliveries_retry_due",
            "tenant_id",
            "status",
            "next_retry_at",
            postgresql_where=text("status = 'retrying'"),
        ),
    )


class AutomationRule(Base):
    """One of the seven event-driven automation templates, per tenant."""

    __tablename__ = "automation_rules"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    template_code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    autonomy_level: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'L1_proposal'")
    )
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'P1'"))
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_triggered_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "template_code IN ('low_doc_replenishment', 'negative_margin_hold', "
            "'stalled_shipment_soothe', 'cold_b2b_quote_followup', 'daily_patrol_brief', "
            "'negative_review_ticket', 'high_adoption_faq_extract')",
            name="ck_automation_rules_template_code",
        ),
        CheckConstraint(
            "autonomy_level IN ('L0_copilot', 'L1_proposal', 'L2_auto')",
            name="ck_automation_rules_autonomy_level",
        ),
        CheckConstraint(
            "priority IN ('P0', 'P1', 'P2')", name="ck_automation_rules_priority"
        ),
        UniqueConstraint("tenant_id", "template_code", name="uq_automation_rules_tenant_tpl"),
        Index("idx_automation_rules_tenant_enabled", "tenant_id", "is_enabled"),
    )
