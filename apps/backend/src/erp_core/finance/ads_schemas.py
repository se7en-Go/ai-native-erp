"""Request/response contracts for the read-only ads spend router.

Every money and rate value crosses the wire as an exact decimal **string** - the
web app never recomputes ROAS, TACoS or CTR from floats. Credentials are exposed
only as `has_credentials` plus an opaque masked `token_preview`; the plaintext
token is never part of any response model.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

AdsPlatform = Literal["meta_ads", "amazon_ads", "sandbox_ads"]
AdsAccountStatus = Literal["active", "paused", "error"]


class AdAccountConnect(BaseModel):
    """Connect one read-only ad account. `access_token` is encrypted at rest."""

    platform: AdsPlatform
    account_name: str = Field(min_length=1, max_length=200)
    external_account_id: str = Field(min_length=1, max_length=200)
    access_token: str = Field(min_length=1, max_length=8192)
    currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("access_token")
    @classmethod
    def _strip_token(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("access_token must not be blank")
        return stripped

    @field_validator("currency")
    @classmethod
    def _normalise_currency(cls, value: str) -> str:
        return value.strip().upper()


class AdAccountRead(BaseModel):
    """Public account projection. Never carries the token or the ciphertext."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    platform: AdsPlatform
    account_name: str
    external_account_id: str
    currency: str
    status: AdsAccountStatus
    last_synced_at: datetime | None = None
    created_at: datetime
    has_credentials: bool = True
    token_preview: str


class AdSpendRowRead(BaseModel):
    """One `(spend_date, campaign_name)` fact row, money as decimal strings."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ad_account_id: UUID
    spend_date: date
    campaign_name: str
    sku: str | None = None
    impressions: int
    clicks: int
    ad_spend: str
    attributed_sales: str
    total_store_sales: str
    currency: str


class AdsSyncResultRead(BaseModel):
    """Outcome of one read-only pull."""

    account: AdAccountRead
    rows_upserted: int
    days: int
    synced_at: datetime


class AdsSpendSummaryRead(BaseModel):
    """Windowed totals plus the exact ratios derived from them.

    * `roas` = attributed_sales / ad_spend, quantized `0.01`
    * `tacos_pct` = (ad_spend / total_store_sales) * 100, quantized `0.01`
    * `ctr_pct` = (clicks / impressions) * 100, quantized `0.01`
    """

    tenant_id: UUID
    days: int
    currency: str
    total_ad_spend: str
    total_attributed_sales: str
    total_store_sales: str
    total_impressions: int
    total_clicks: int
    roas: str
    tacos_pct: str
    ctr_pct: str
    rows: list[AdSpendRowRead]
