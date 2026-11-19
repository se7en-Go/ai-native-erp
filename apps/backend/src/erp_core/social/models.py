"""Phase 3D social marketing ORM models: accounts, calendar posts and mentions.

The tables are created by `0008_phase3d_social_matrix` and every one of them is
`ENABLE`/`FORCE` row-level secured on `tenant_id`. Enum-like columns are plain
text with database CHECK constraints, so this module never becomes the source of
truth for the allowed values. OAuth tokens never live in a plain column: the
envelope (`*_ciphertext`/`*_nonce`/`*_auth_tag`/`*_key_version`) is written only
through `EnvelopeCryptoService` with `AAD = f"{tenant_id}:{account_id}:social_oauth"`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class SocialAccount(Base):
    """One authorized brand account on one of the fourteen supported platforms."""

    __tablename__ = "social_accounts"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "id", name="uq_social_accounts_id"),
        sa.UniqueConstraint(
            "tenant_id", "platform", "handle", name="uq_social_accounts_platform_handle"
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    platform: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    region: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    handle: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="connected")
    followers_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    token_ciphertext: Mapped[bytes | None] = mapped_column(sa.LargeBinary(), nullable=True)
    token_nonce: Mapped[bytes | None] = mapped_column(sa.LargeBinary(), nullable=True)
    token_auth_tag: Mapped[bytes | None] = mapped_column(sa.LargeBinary(), nullable=True)
    token_key_version: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    created_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SocialPost(Base):
    """One calendar entry targeting exactly one platform (a multi-platform post is N rows)."""

    __tablename__ = "social_posts"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "id", name="uq_social_posts_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    social_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("social_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    platform: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    title: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    caption: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    hashtags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    media_urls: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    sku_ref: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="draft")
    scheduled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    external_post_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SocialMention(Base):
    """A public mention/comment used for listening and one-click CRM lead conversion."""

    __tablename__ = "social_mentions"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "id", name="uq_social_mentions_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    social_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("social_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    platform: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    author_handle: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    content_text: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    sentiment: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="neutral")
    converted_lead_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
