"""Sourcing visual-AI job records (`sourcing_image_jobs`).

One row per "去白底 / 换场景 / 图片文字翻译" request. The row is the durable job
handle: it records which provider rendered the image, its terminal status and
the resulting public URL. The rendered bytes themselves live in R2/MinIO (the
`output_r2_key` column) or — when object storage is not attached, e.g. offline
tests and local dev — inline as a deterministic `data:image/png;base64,...`
preview so a browser can still render a real PNG.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class SourcingImageJob(Base):
    __tablename__ = "sourcing_image_jobs"
    __table_args__ = (
        sa.CheckConstraint(
            "provider IN ('sandbox', 'remove_bg', 'openai_image')",
            name="ck_sourcing_image_jobs_provider",
        ),
        sa.CheckConstraint(
            "operation IN ('remove_bg', 'scene_replace', 'translate_overlay')",
            name="ck_sourcing_image_jobs_operation",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed')",
            name="ck_sourcing_image_jobs_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("product_variants.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="sandbox")
    operation: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    source_image_url: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    target_locale: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    prompt: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="completed")
    output_image_url: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    output_mime_type: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    output_r2_key: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
