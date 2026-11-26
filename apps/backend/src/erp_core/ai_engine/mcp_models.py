"""SQLAlchemy model for tenant-scoped external MCP (Model Context Protocol) tokens."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, desc, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class McpApiToken(Base):
    """A hashed bearer token that lets an external MCP client call ERP read tools.

    The plaintext token is shown exactly once at creation time; the row keeps only
    the SHA-256 hex digest (`token_hash`) plus a short display prefix
    (`token_prefix`). `scopes` gates read (`mcp:read`) versus proposal drafting
    (`mcp:propose`); `allowed_tools` is an optional whitelist of tool names.
    """

    __tablename__ = "mcp_api_tokens"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY['mcp:read']::TEXT[]")
    )
    allowed_tools: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_mcp_api_tokens_hash"),
        Index("ix_mcp_api_tokens_tenant_created", "tenant_id", desc("created_at")),
    )
