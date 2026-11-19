from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MoqTierInput(BaseModel):
    min_qty: int = Field(ge=1)
    unit_price_usd: Decimal = Field(gt=Decimal("0"))


class ProductCreate(BaseModel):
    spu_code: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    hs_code: str | None = Field(default=None, max_length=32)
    origin_country: str = Field(default="CN", min_length=2, max_length=2)
    i18n_specs: dict[str, Any] = Field(default_factory=dict)
    certifications: list[str] = Field(default_factory=list)
    ai_faq_facts: dict[str, Any] = Field(default_factory=dict)


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    spu_code: str
    category: str
    hs_code: str | None
    origin_country: str
    i18n_specs: dict[str, Any]
    certifications: list[str]
    ai_faq_facts: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class VariantCreate(BaseModel):
    product_id: UUID
    sku: str = Field(min_length=1, max_length=128)
    attributes: dict[str, Any] = Field(default_factory=dict)
    cost_price_base: Decimal = Field(ge=Decimal("0"))
    base_currency: str = Field(default="CNY", min_length=3, max_length=3)
    min_margin_floor_pct: Decimal = Field(
        default=Decimal("15.00"), ge=Decimal("0"), le=Decimal("99.99")
    )
    retail_price_usd: Decimal = Field(gt=Decimal("0"))
    b2b_moq_tiers: list[MoqTierInput] = Field(default_factory=list)
    weight_grams: int = Field(ge=0)
    carton_dims_cm: dict[str, Any] | None = None
    lead_time_days: int = Field(default=7, ge=0)


class VariantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    product_id: UUID
    sku: str
    attributes: dict[str, Any]
    cost_price_base: Decimal | None = None
    base_currency: str
    min_margin_floor_pct: Decimal | None = None
    retail_price_usd: Decimal
    b2b_moq_tiers: list[MoqTierInput]
    weight_grams: int
    carton_dims_cm: dict[str, Any] | None
    lead_time_days: int
    created_at: datetime


class VariantPricingInternal(BaseModel):
    """Internal service-to-service DTO carrying full cost and margin floor fields."""

    id: UUID
    product_id: UUID
    sku: str
    cost_price_base: Decimal
    base_currency: str
    min_margin_floor_pct: Decimal
    retail_price_usd: Decimal
    b2b_moq_tiers: list[dict[str, Any]]
    i18n_specs: dict[str, Any]
    hs_code: str | None


class SkuMappingCreate(BaseModel):
    variant_id: UUID
    channel_account_id: UUID
    external_product_id: str = Field(min_length=1)
    external_sku: str = Field(min_length=1)
    bundle_multiplier: int = Field(default=1, ge=1)
    sync_inventory_enabled: bool = True


class SkuMappingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    variant_id: UUID
    channel_account_id: UUID
    external_product_id: str
    external_sku: str
    bundle_multiplier: int
    sync_inventory_enabled: bool
    created_at: datetime


class VariantUpdate(BaseModel):
    retail_price_usd: Decimal | None = Field(default=None, gt=Decimal("0"))
    b2b_moq_tiers: list[MoqTierInput] | None = None
    min_margin_floor_pct: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("99.99"))
    lead_time_days: int | None = Field(default=None, ge=0)


class SkuMappingUpdate(BaseModel):
    external_sku: str | None = Field(default=None, min_length=1)
    bundle_multiplier: int | None = Field(default=None, ge=1)
    sync_inventory_enabled: bool | None = None


class VariantMappingWithLabelRead(SkuMappingRead):
    channel_label: str | None = None


class VariantMappingsBundleRead(BaseModel):
    variant: VariantRead
    mappings: list[VariantMappingWithLabelRead]


class LiveSkuCheckRead(BaseModel):
    sku: str
    available_qty: int
    unit_price_quote_ccy: Decimal
    moq_tier_applied: int | None
    incoterm: str
    source_tool_call_id: str


class ResolvedChannelSku(BaseModel):
    variant_id: UUID
    internal_sku: str
    bundle_multiplier: int
    deduct_qty: int
    cost_price_base: Decimal
    base_currency: str
