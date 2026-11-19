from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.shared.enums import UsageMetricType


class CreditWalletRead(BaseModel):
    tenant_id: UUID
    plan_tier: str
    included_credits_remaining: int
    topup_credits_balance: int
    total_credits_available: int
    fractional_credit_carry: Decimal
    overage_spend_cap_cents: int
    overage_spent_cents: int
    auto_downgrade_to_l0: bool
    billing_cycle_end: datetime
    updated_at: datetime


class UsageLedgerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    idempotency_key: str
    metric_type: UsageMetricType
    units_consumed: Decimal
    raw_provider_cost_usd: Decimal
    model_or_provider: str
    stripe_meter_synced: bool
    stripe_meter_event_id: str | None
    created_at: datetime


class TopupCreditsRequest(BaseModel):
    credits: int = Field(gt=0, le=1_000_000, strict=True)
    reference: str = Field(min_length=1, max_length=200)


class TopupPaymentRead(BaseModel):
    payment_intent_id: str
    client_secret: str
    amount_cents: int
    currency: str = "usd"
