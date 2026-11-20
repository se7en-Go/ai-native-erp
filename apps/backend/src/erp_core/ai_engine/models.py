from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class KbDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_hash: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'en'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(
            "source_type IN ('markdown', 'pdf', 'csv', 'faq_json', 'policy_doc')",
            name="ck_kb_documents_source_type",
        ),
        Index("idx_kb_documents_tenant_active", "tenant_id", "is_active", desc("created_at")),
        Index("idx_kb_documents_tenant_hash", "tenant_id", "doc_hash"),
    )


class KbChunk(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            r"to_tsvector('simple', regexp_replace(content, '([一-龯])', ' \1 ', 'g'))",
            persisted=True,
        ),
        nullable=False,
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    has_dynamic_price_disclaimer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["kb_documents.tenant_id", "kb_documents.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("chunk_index >= 0"),
        UniqueConstraint("tenant_id", "document_id", "chunk_index", name="uq_kb_chunks_doc_idx"),
        Index("idx_kb_chunks_tenant_doc", "tenant_id", "document_id", "chunk_index"),
        Index("idx_kb_chunks_tsv_gin", "tsv", postgresql_using="gin"),
        Index(
            "idx_kb_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )


class AiProposal(Base):
    __tablename__ = "ai_proposals"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    capability: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_type: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    target_entity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_entity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    proposed_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Verified ErpToolRegistry provenance; NULL means the claims are ungrounded.
    source_tool_call_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    estimated_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    edited_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(
            "idx_ai_proposals_inbox",
            "tenant_id",
            "status",
            desc("created_at"),
            postgresql_where=text("status = 'pending'"),
        ),
        Index("idx_ai_proposals_tenant_capability", "tenant_id", "capability", desc("created_at")),
        Index(
            "idx_ai_proposals_tenant_source_tool",
            "tenant_id",
            "source_tool_call_id",
            postgresql_where=text("source_tool_call_id IS NOT NULL"),
        ),
    )


class AiEvalFeedback(Base):
    __tablename__ = "ai_eval_feedback"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    proposal_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    capability: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_type: Mapped[str] = mapped_column(Text, nullable=False)
    original_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    edited_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    diff_patch: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    feedback_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    created_by_user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "proposal_id"],
            ["ai_proposals.tenant_id", "ai_proposals.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "proposal_id", name="uq_ai_eval_feedback_proposal"),
        Index("idx_ai_eval_feedback_tenant_cap", "tenant_id", "capability", desc("created_at")),
    )


class AiEmployee(Base):
    """A configured digital employee: persona, capability scope and autonomy ceiling."""

    __tablename__ = "ai_employees"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role_title: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str] = mapped_column(Text, nullable=False)
    avatar_emoji: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    capability_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    autonomy_level: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'L1_proposal'")
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    tool_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    system_persona: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "code"),
        CheckConstraint(
            "autonomy_level IN ('L0_copilot','L1_proposal','L2_auto')",
            name="ck_ai_employees_autonomy_level",
        ),
        Index("ix_ai_employees_is_enabled", "tenant_id", "is_enabled", "autonomy_level"),
    )


class QuickReplyTemplate(Base):
    """Reusable canned reply the inbox assistant inserts into a draft (never auto-sends)."""

    __tablename__ = "quick_reply_templates"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    shortcut_code: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "shortcut_code"),
        CheckConstraint("language IN ('zh','en')"),
        Index("ix_quick_reply_templates_category", "tenant_id", "category", "language"),
    )


class AiCreativeAsset(Base):
    """A generated or human-authored marketing asset kept for review and reuse."""

    __tablename__ = "ai_creative_assets"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    asset_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    sku_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_by: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(
            "asset_type IN ('product_copy','listing','email_template','social_caption','ad_hook')",
            name="ck_ai_creative_assets_asset_type",
        ),
        CheckConstraint(
            "status IN ('draft','approved','archived')", name="ck_ai_creative_assets_status"
        ),
        Index(
            "ix_ai_creative_assets_type",
            "tenant_id",
            "asset_type",
            "status",
            desc("created_at"),
        ),
    )
