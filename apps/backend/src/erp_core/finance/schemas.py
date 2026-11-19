from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from erp_core.shared.enums import FulfillmentStatus, OrderMode


class ReceivableRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    order_id: UUID
    direction: str
    milestone_label: str
    due_amount: Decimal
    paid_amount: Decimal
    currency: str
    due_date: date | None
    status: str
    created_at: datetime


def _parse_exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("Use a decimal string for monetary values and FX rates")
    return value


ExactDecimal = Annotated[Decimal, BeforeValidator(_parse_exact_decimal)]


class PaymentCreate(BaseModel):
    rp_id: UUID
    payment_method: str = Field(min_length=1, max_length=64)
    external_tx_ref: str | None = Field(default=None, max_length=128)
    amount_received: ExactDecimal = Field(ge=Decimal("0.01"), max_digits=14, decimal_places=2)
    currency: str = Field(pattern="^[A-Za-z]{3}$")
    actual_fx_rate: ExactDecimal = Field(
        ge=Decimal("0.000001"),
        max_digits=12,
        decimal_places=6,
    )
    gateway_fee: ExactDecimal = Field(
        default=Decimal("0.00"),
        ge=Decimal("0"),
        max_digits=12,
        decimal_places=2,
    )
    receipt_r2_key: str | None = None
    paid_at: datetime | None = None


class PaymentRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    rp_id: UUID
    payment_method: str
    external_tx_ref: str | None
    amount_received: Decimal
    currency: str
    actual_fx_rate: Decimal
    gateway_fee: Decimal
    receipt_r2_key: str | None
    verified_by_user_id: UUID | None
    paid_at: datetime
    created_at: datetime


class OrderProfitSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: UUID
    tenant_id: UUID
    base_currency: str
    gross_revenue_base: Decimal
    cogs_base: Decimal
    platform_commission_base: Decimal
    payment_gateway_fee_base: Decimal
    shipping_and_duty_base: Decimal
    channel_msg_cost_base: Decimal
    ad_attribution_cost_base: Decimal
    fx_gain_loss_base: Decimal
    net_contribution_margin_base: Decimal
    contribution_margin_pct: Decimal
    recalculated_at: datetime


class ProfitSummaryRead(BaseModel):
    base_currency: str
    total_orders: int
    gross_revenue_base: Decimal
    cogs_base: Decimal
    total_variable_expenses_base: Decimal
    fx_gain_loss_base: Decimal
    net_contribution_margin_base: Decimal
    contribution_margin_pct: Decimal
    order_mode: OrderMode | None = None


class FinanceOverviewSummaryRead(BaseModel):
    total_gross_revenue_base: Decimal
    total_net_margin_base: Decimal
    avg_margin_pct: Decimal
    base_currency: str


class FinanceOverviewReceivableItem(BaseModel):
    id: UUID
    order_id: UUID
    order_no: str
    order_mode: OrderMode
    milestone_label: str
    due_amount: Decimal
    paid_amount: Decimal
    currency: str
    status: str
    shipment_blocked_70_unpaid: bool
    pending_payment_id: UUID | None = None
    pending_tx_ref: str | None = None


class FinanceOverviewSnapshotItem(BaseModel):
    order_id: UUID
    order_no: str
    base_currency: str
    gross_revenue_base: Decimal
    cogs_base: Decimal
    platform_commission_base: Decimal
    payment_gateway_fee_base: Decimal
    shipping_and_duty_base: Decimal
    channel_msg_cost_base: Decimal
    ad_attribution_cost_base: Decimal
    fx_gain_loss_base: Decimal
    net_contribution_margin_base: Decimal


class FinanceOverviewRead(BaseModel):
    summary: FinanceOverviewSummaryRead
    receivables: list[FinanceOverviewReceivableItem]
    cvp_snapshots: list[FinanceOverviewSnapshotItem]


class PaymentVerifyResponse(BaseModel):
    ok: bool = True
    payment: PaymentRecordRead
    order_fulfillment_status: FulfillmentStatus
