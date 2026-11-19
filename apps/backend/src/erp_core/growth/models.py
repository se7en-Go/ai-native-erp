"""Phase 3C growth ORM models: compliant lead collection, prospect pool, campaigns.

The tables are created by `0007_phase3c_growth_leads` and every one of them is
`ENABLE`/`FORCE` row-level secured on `tenant_id`. Enum-like columns are plain
text with database CHECK constraints, so this module never becomes the source of
truth for the allowed values.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class LeadCollectionJob(Base):
    """One queued/completed collection run against a compliant data source."""

    __tablename__ = "lead_collection_jobs"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "id", name="uq_lead_collection_jobs_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    source_provider: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    target_region: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    max_results: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=100)
    min_rating: Mapped[Decimal | None] = mapped_column(sa.Numeric(3, 2), nullable=True)
    require_phone: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="pending")
    collected_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    cleaned_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    synced_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class CollectedProspect(Base):
    """A deduplicated, graded prospect produced by a collection job or CSV import."""

    __tablename__ = "collected_prospects"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "id", name="uq_collected_prospects_id"),
        sa.UniqueConstraint(
            "tenant_id", "dedupe_fingerprint", name="uq_collected_prospects_fingerprint"
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    job_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("lead_collection_jobs.id", ondelete="CASCADE"),
        nullable=True,
    )
    company_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    phone_e164: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    email: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    website: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    country: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    city: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    latitude: Mapped[float | None] = mapped_column(sa.Double(), nullable=True)
    longitude: Mapped[float | None] = mapped_column(sa.Double(), nullable=True)
    rating: Mapped[Decimal | None] = mapped_column(sa.Numeric(3, 2), nullable=True)
    grade: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="C")
    whatsapp_reachable: Mapped[bool | None] = mapped_column(sa.Boolean(), nullable=True)
    dedupe_fingerprint: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    sync_status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="raw")
    crm_lead_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class MarketingCampaign(Base):
    """An outbound email/WhatsApp campaign definition and its delivery counters."""

    __tablename__ = "marketing_campaigns"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "id", name="uq_marketing_campaigns_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    channel_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    subject: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    body_template: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="draft")
    total_targets: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    suppressed_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    replied_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    created_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class CampaignRecipient(Base):
    """Per-address launch outcome; the audit trail behind the campaign counters."""

    __tablename__ = "campaign_recipients"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "id", name="uq_campaign_recipients_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "campaign_id",
            "address",
            name="uq_campaign_recipients_tenant_address",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    campaign_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("marketing_campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    prospect_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    address: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="queued")
    error_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
