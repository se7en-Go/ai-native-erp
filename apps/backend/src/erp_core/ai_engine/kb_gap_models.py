from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class OutboundRedlineRule(Base):
    """Operator-authored red line evaluated before any outbound message is sent."""

    __tablename__ = "outbound_redline_rules"
    __table_args__ = (
        CheckConstraint(
            "match_type IN ('keyword', 'regex')",
            name="ck_outbound_redline_rules_match_type",
        ),
        CheckConstraint("action IN ('block', 'warn')", name="ck_outbound_redline_rules_action"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    match_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="keyword")
    action: Mapped[str] = mapped_column(Text, nullable=False, server_default="block")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KbGapItem(Base):
    """A customer question the knowledge base could not answer, and its promotion."""

    __tablename__ = "kb_gap_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')", name="ck_kb_gap_items_status"
        ),
        CheckConstraint("frequency_count >= 1", name="ck_kb_gap_items_frequency_count"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    customer_question: Mapped[str] = mapped_column(Text, nullable=False)
    detected_language: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    frequency_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    resolved_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    promoted_kb_document_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("kb_documents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
