from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.shared.enums import (
    FulfillmentStatus,
    Incoterm,
    LeadStatus,
    OpportunityStage,
    OrderMode,
    PaymentStatus,
    QuoteStatus,
)


class LeadCreate(BaseModel):
    customer_id: UUID
    source_channel: str = Field(min_length=1, max_length=64)
    utm_campaign: str | None = None
    lead_type: Literal["b2b_inquiry", "b2c_vip_pre_sale"] = "b2b_inquiry"
    qualification_score: int = Field(default=0, ge=0, le=100)
    extracted_requirements: dict[str, Any] = Field(default_factory=dict)


class LeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    customer_id: UUID
    source_channel: str
    utm_campaign: str | None
    lead_type: str
    qualification_score: int
    extracted_requirements: dict[str, Any]
    status: LeadStatus
    created_at: datetime


class OpportunityCreate(BaseModel):
    lead_id: UUID | None = None
    company_id: UUID | None = None
    customer_id: UUID
    title: str = Field(min_length=1, max_length=255)
    stage: OpportunityStage = OpportunityStage.QUOTING
    expected_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    currency: str = Field(default="USD", min_length=3, max_length=3)
    expected_close_date: date | None = None
    owner_user_id: UUID


class OpportunityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    lead_id: UUID | None
    company_id: UUID | None
    customer_id: UUID
    title: str
    stage: OpportunityStage
    expected_amount: Decimal
    currency: str
    expected_close_date: date | None
    owner_user_id: UUID
    created_at: datetime
    company_name: str | None = None


class OpportunityStageUpdate(BaseModel):
    stage: OpportunityStage


class QuoteItemInput(BaseModel):
    variant_id: UUID
    quantity: int = Field(gt=0)
    unit_price_quote_ccy: Decimal | None = Field(default=None, gt=Decimal("0"))
    description_override: str | None = None
    language: str = "en"


class QuoteItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    quote_id: UUID
    variant_id: UUID
    sku_snapshot: str
    description_i18n: str
    quantity: int
    moq_tier_applied: int | None
    unit_price_quote_ccy: Decimal
    unit_cost_base_snapshot: Decimal
    line_total_quote_ccy: Decimal


class QuoteCreate(BaseModel):
    opportunity_id: UUID
    is_proforma_invoice: bool = True
    incoterm: Incoterm = Incoterm.FOB
    port_of_loading: str | None = "Xiamen, China"
    port_of_destination: str | None = None
    payment_terms: str = "30% T/T deposit, 70% before shipment"
    base_currency: str = Field(default="CNY", min_length=3, max_length=3)
    quote_currency: str = Field(default="USD", min_length=3, max_length=3)
    fx_rate_snapshot: Decimal = Field(gt=Decimal("0"))
    fx_locked_until: datetime
    freight_fee: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    insurance_fee: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    customs_duty_estimate: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    override_margin_floor: bool = False
    items: list[QuoteItemInput] = Field(min_length=1)


class QuoteRepriceRequest(BaseModel):
    fx_rate_snapshot: Decimal = Field(gt=Decimal("0"))
    fx_locked_until: datetime
    freight_fee: Decimal | None = Field(default=None, ge=Decimal("0"))
    insurance_fee: Decimal | None = Field(default=None, ge=Decimal("0"))
    customs_duty_estimate: Decimal | None = Field(default=None, ge=Decimal("0"))
    payment_terms: str | None = None
    override_margin_floor: bool = False
    items: list[QuoteItemInput] | None = None


class QuoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    opportunity_id: UUID
    quote_no: str
    version: int
    is_proforma_invoice: bool
    incoterm: Incoterm
    port_of_loading: str | None
    port_of_destination: str | None
    payment_terms: str
    base_currency: str
    quote_currency: str
    fx_rate_snapshot: Decimal
    fx_locked_until: datetime
    freight_fee: Decimal
    insurance_fee: Decimal
    customs_duty_estimate: Decimal
    total_amount: Decimal
    pdf_r2_key: str | None
    status: QuoteStatus
    converted_order_id: UUID | None
    created_at: datetime
    items: list[QuoteItemRead] = Field(default_factory=list)


class QuotePdfResponse(BaseModel):
    quote_id: UUID
    quote_no: str
    version: int
    pdf_r2_key: str
    download_url: str


class ConvertedOrderSummary(BaseModel):
    id: UUID
    order_no: str
    order_mode: OrderMode
    fulfillment_status: FulfillmentStatus
    payment_status: PaymentStatus
    total_amount: Decimal
    currency: str


class ConvertedReceivableSummary(BaseModel):
    id: UUID
    milestone_label: str
    due_amount: Decimal
    paid_amount: Decimal
    currency: str
    status: str


class ConvertQuoteRequest(BaseModel):
    warehouse_id: UUID
    order_mode: Literal[OrderMode.B2B_WHOLESALE, OrderMode.B2B_SAMPLE] = OrderMode.B2B_WHOLESALE
    deposit_ratio: Decimal = Field(default=Decimal("0.30"), gt=Decimal("0"), le=Decimal("1.00"))


class ConvertQuoteResponse(BaseModel):
    quote: QuoteRead
    order: ConvertedOrderSummary
    receivables: list[ConvertedReceivableSummary]
