from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class CreditWallet(Base):
    __tablename__ = "credit_wallets"

    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    plan_tier: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'starter'"))
    included_credits_remaining: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    topup_credits_balance: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    fractional_credit_carry: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    overage_spend_cap_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("5000")
    )
    overage_spent_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    auto_downgrade_to_l0: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    billing_cycle_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UsageLedger(Base):
    __tablename__ = "usage_ledger"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    metric_type: Mapped[str] = mapped_column(Text, nullable=False)
    units_consumed: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    raw_provider_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    model_or_provider: Mapped[str] = mapped_column(Text, nullable=False)
    stripe_meter_synced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    stripe_meter_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "id", name="pk_usage_ledger"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_usage_ledger_tenant_idempotency"),
        Index(
            "idx_usage_ledger_unsynced",
            "tenant_id",
            "created_at",
            postgresql_where=text("stripe_meter_synced = false"),
        ),
        Index("idx_usage_ledger_tenant_created", "tenant_id", "created_at"),
    )
