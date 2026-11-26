from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class B2bPortalToken(Base):
    """Magic-link token granting a buyer read/accept/receipt access to one quote.

    Only the SHA-256 hex digest of the plain token is persisted (`token_hash`);
    `token_prefix` keeps the first characters for operator-side identification.
    """

    __tablename__ = "b2b_portal_tokens"
    __table_args__ = (
        sa.UniqueConstraint("token_hash", name="uq_b2b_portal_tokens_hash"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    quote_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_prefix: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    buyer_email: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    allow_accept: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=True)
    allow_receipt_upload: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    accepted_by_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    accepted_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
