from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from erp_core.shared.enums import UserRole


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=120)
    tenant_name: str = Field(min_length=1, max_length=120)
    tenant_slug: str | None = Field(default=None, min_length=2, max_length=80)
    base_currency: str = Field(default="USD", min_length=3, max_length=3)
    timezone: str = Field(default="Asia/Shanghai", min_length=2, max_length=64)
    preset_type: str = Field(default="hybrid_factory_b2b_b2c", min_length=2, max_length=64)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    tenant_slug: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class PrincipalResponse(BaseModel):
    user_id: UUID
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    role: UserRole
    email: EmailStr
    full_name: str
    can_view_cost: bool


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    principal: PrincipalResponse


class TenantMembershipOption(BaseModel):
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    role: UserRole
    is_active: bool


class MembershipItem(BaseModel):
    membership_id: UUID
    tenant_id: UUID
    user_id: UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime


class InviteMemberRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    role: UserRole


class UpdateMemberRoleRequest(BaseModel):
    role: UserRole
    is_active: bool | None = None


class UpdateTenantSettingsRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_currency: str | None = Field(default=None, min_length=3, max_length=3)
    timezone: str | None = Field(default=None, min_length=2, max_length=64)


class TenantSettingsResponse(BaseModel):
    tenant_id: UUID
    slug: str
    name: str
    business_preset: str
    base_currency: str
    timezone: str


class CostVisibilitySampleResponse(BaseModel):
    sku: str
    currency: str
    unit_sale_price: Decimal
    cost_price_base: Decimal | None = None
    cogs_base: Decimal | None = None
    net_profit_base: Decimal | None = None
    net_margin_pct: Decimal | None = None
    cost_masked: bool = False
