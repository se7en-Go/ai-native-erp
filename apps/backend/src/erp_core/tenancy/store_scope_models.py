from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class MemberChannelScope(Base):
    """Per-member grant over one connected store / channel account.

    Tenancy owns this table (a membership-governance concern, not a channel
    concern), so the read/write paths in `store_scope_service` reach
    `channel_accounts` and the other tenant business tables exclusively through
    parameterised `sqlalchemy.text(...)` statements. That keeps the module graph
    acyclic: `erp_core.tenancy` never imports a downstream domain module.
    """

    __tablename__ = "member_channel_scopes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "channel_account_id",
            name="uq_member_channel_scopes_unique",
        ),
        CheckConstraint(
            "access_level IN ('read', 'operate', 'manage')",
            name="ck_member_channel_scopes_access_level",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel_account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("channel_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    access_level: Mapped[str] = mapped_column(Text, nullable=False, server_default="operate")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
