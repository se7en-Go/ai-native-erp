"""ORM models for the dual-basis settlement reconciliation vertical.

`settlement_batches` / `settlement_lines` / `payout_statements` are created by the
locked Alembic revision `0022_finance_dual_settlement`. Every monetary column is
`NUMERIC`, mirrored here as `sa.Numeric`, and every attribute is typed
`Decimal | None` where the column is nullable - a `float` can never enter or
leave this model layer.

The models intentionally carry no relationship() graph: the reporting queries in
`settlement_service` are explicit, tenant-scoped SQL, so the RESTRICTIVE RLS
policy stays the second line of defence behind the `tenant_id = :tid` predicate.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class SettlementBatch(Base):
    """One imported marketplace payout statement header."""

    __tablename__ = "settlement_batches"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id", "channel_type", "batch_ref", name="uq_settlement_batches_tenant_ref"
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    channel_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("channel_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    batch_ref: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    period_start: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    period_end: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    gross_sales_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    commission_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    fulfillment_fee_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    storage_fee_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    ad_fee_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    refund_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    net_payout_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    fx_rate_to_base: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="imported")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SettlementLine(Base):
    """One per-order (optionally per-SKU) row inside a settlement batch."""

    __tablename__ = "settlement_lines"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("settlement_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_order_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    order_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    sku: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    gross_sales_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    commission_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    fulfillment_fee_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    storage_fee_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    ad_fee_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    refund_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    net_settlement_ccy: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    fx_rate_to_base: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False)
    net_settlement_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class PayoutStatement(Base):
    """One cross-border collection statement line (PingPong / LianLian / ...)."""

    __tablename__ = "payout_statements"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id", "provider", "external_tx_ref", name="uq_payout_statements_tenant_ref"
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    external_tx_ref: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    payout_currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    payout_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    settled_base_currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    settled_base_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    actual_fx_rate: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False)
    expected_fx_rate: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False)
    fx_variance_base: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    matched_settlement_batch_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("settlement_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    matched_order_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    match_status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="unmatched")
    paid_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
