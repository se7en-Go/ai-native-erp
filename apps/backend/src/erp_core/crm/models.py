from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base
from erp_core.shared.enums import Incoterm, LeadStatus, OpportunityStage, QuoteStatus


def _str_enum_col(enum_cls: type) -> sa.Enum:
    return sa.Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=64,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    customer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False
    )
    source_channel: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    utm_campaign: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    lead_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    qualification_score: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    extracted_requirements: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    status: Mapped[LeadStatus] = mapped_column(
        _str_enum_col(LeadStatus), nullable=False, default=LeadStatus.NEW
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    lead_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("leads.id"), nullable=True
    )
    company_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("b2b_companies.id"), nullable=True
    )
    customer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    stage: Mapped[OpportunityStage] = mapped_column(
        _str_enum_col(OpportunityStage), nullable=False, default=OpportunityStage.DISCOVERY
    )
    expected_amount: Mapped[Decimal] = mapped_column(
        sa.Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="USD")
    expected_close_date: Mapped[date | None] = mapped_column(sa.Date(), nullable=True)
    owner_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "quote_no", "version", name="uq_quotes_tenant_no_version"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    opportunity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("opportunities.id"), nullable=False
    )
    quote_no: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=1)
    is_proforma_invoice: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    incoterm: Mapped[Incoterm] = mapped_column(
        _str_enum_col(Incoterm), nullable=False, default=Incoterm.FOB
    )
    port_of_loading: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    port_of_destination: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    payment_terms: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    base_currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="CNY")
    quote_currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="USD")
    fx_rate_snapshot: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False)
    fx_locked_until: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    freight_fee: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    insurance_fee: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    customs_duty_estimate: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    total_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    pdf_r2_key: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    status: Mapped[QuoteStatus] = mapped_column(
        _str_enum_col(QuoteStatus), nullable=False, default=QuoteStatus.DRAFT
    )
    converted_order_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class QuoteItem(Base):
    __tablename__ = "quote_items"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    quote_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("product_variants.id"), nullable=False
    )
    sku_snapshot: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    description_i18n: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    quantity: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    moq_tier_applied: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    unit_price_quote_ccy: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    unit_cost_base_snapshot: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    line_total_quote_ccy: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
