from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class ReceivablePayable(Base):
    __tablename__ = "receivables_payables"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False
    )
    direction: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    milestone_label: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    due_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    paid_amount: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    due_date: Mapped[date | None] = mapped_column(sa.Date(), nullable=True)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class PaymentRecord(Base):
    __tablename__ = "payment_records"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    rp_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("receivables_payables.id"), nullable=False
    )
    payment_method: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    external_tx_ref: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    amount_received: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    actual_fx_rate: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False)
    gateway_fee: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    receipt_r2_key: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    verified_by_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    paid_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class OrderProfitSnapshot(Base):
    __tablename__ = "order_profit_snapshots"

    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    base_currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="CNY")
    gross_revenue_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    cogs_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    platform_commission_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    payment_gateway_fee_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    shipping_and_duty_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    channel_msg_cost_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    ad_attribution_cost_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    fx_gain_loss_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    net_contribution_margin_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2),
        sa.Computed(
            "gross_revenue_base - cogs_base - platform_commission_base "
            "- payment_gateway_fee_base - shipping_and_duty_base "
            "- channel_msg_cost_base - ad_attribution_cost_base + fx_gain_loss_base",
            persisted=True,
        ),
        nullable=False,
    )
    recalculated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class ChannelDailyMetric(Base):
    __tablename__ = "channel_daily_metrics"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("channel_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    variant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("product_variants.id", ondelete="SET NULL"),
        nullable=True,
    )
    metric_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    impressions: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    ad_spend_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    external_sessions: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    source: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
