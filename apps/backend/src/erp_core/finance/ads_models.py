"""Read-only ads spend models: account connections and daily spend snapshots.

`ad_account_connections.encrypted_credentials_b64` holds the AES-256-GCM
envelope produced by `EnvelopeCryptoService` bound to
``AAD = f"{tenant_id}:{id}:ads_oauth"``. The plaintext token never reaches
PostgreSQL and is never returned by an API response - callers get
`has_credentials` plus an opaque masked fingerprint only.

`ad_spend_daily_snapshots` is the immutable-shaped fact table (`NUMERIC(14, 2)`
money, never a float) that ROAS / TACoS / CTR are computed from with `Decimal`
`ROUND_HALF_UP`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class AdAccountConnection(Base):
    __tablename__ = "ad_account_connections"
    __table_args__ = (
        sa.CheckConstraint(
            "platform IN ('meta_ads', 'amazon_ads', 'sandbox_ads')",
            name="ck_ad_account_connections_platform",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'error')",
            name="ck_ad_account_connections_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "platform",
            "external_account_id",
            name="uq_ad_account_connections_tenant_platform_ext",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    account_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    external_account_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    encrypted_credentials_b64: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="active")
    last_synced_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class AdSpendDailySnapshot(Base):
    __tablename__ = "ad_spend_daily_snapshots"
    __table_args__ = (
        sa.CheckConstraint("impressions >= 0", name="ck_ad_spend_daily_snapshots_impressions"),
        sa.CheckConstraint("clicks >= 0", name="ck_ad_spend_daily_snapshots_clicks"),
        sa.CheckConstraint("ad_spend >= 0", name="ck_ad_spend_daily_snapshots_ad_spend"),
        sa.CheckConstraint(
            "attributed_sales >= 0", name="ck_ad_spend_daily_snapshots_attributed"
        ),
        sa.CheckConstraint(
            "total_store_sales >= 0", name="ck_ad_spend_daily_snapshots_total_sales"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "ad_account_id",
            "spend_date",
            "campaign_name",
            name="uq_ad_spend_daily_snapshots_grain",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    ad_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("ad_account_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    spend_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    campaign_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    sku: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    impressions: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    ad_spend: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    attributed_sales: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    total_store_sales: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="USD")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
