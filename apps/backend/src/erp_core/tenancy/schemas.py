from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
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


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=10)


class LogoutResponse(BaseModel):
    ok: bool = True


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


class PiTemplateSettings(BaseModel):
    """Tenant-configurable export Proforma Invoice letterhead, bank wire and terms."""

    company_legal_name: str = Field(default="", max_length=200)
    company_address: str = Field(default="", max_length=400)
    contact_phone: str = Field(default="", max_length=80)
    contact_email: str = Field(default="", max_length=160)
    website: str = Field(default="", max_length=200)
    tax_or_reg_no: str = Field(default="", max_length=100)
    bank_beneficiary_name: str = Field(default="", max_length=200)
    bank_name: str = Field(default="", max_length=200)
    bank_account_no: str = Field(default="", max_length=120)
    bank_swift_code: str = Field(default="", max_length=60)
    bank_address: str = Field(default="", max_length=300)
    default_terms_notes: str = Field(default="", max_length=2000)
    footer_signature_title: str = Field(
        default="Authorized Signature & Company Stamp", max_length=160
    )


def parse_pi_template(tenant_name: str, raw: object) -> PiTemplateSettings:
    """Layer stored overrides on top of export-ready defaults; bad input falls back."""
    defaults = default_pi_template(tenant_name)
    if not isinstance(raw, dict):
        return defaults
    merged = defaults.model_dump()
    for key in PiTemplateSettings.model_fields:
        value = raw.get(key)
        if isinstance(value, str):
            merged[key] = value
    try:
        return PiTemplateSettings.model_validate(merged)
    except ValueError:
        return defaults


def default_pi_template(tenant_name: str) -> PiTemplateSettings:
    """Generic, export-ready defaults. Never embeds tenant-private or user-specific data."""
    company = tenant_name.strip()
    return PiTemplateSettings(
        company_legal_name=company,
        company_address="Industrial Park, Export Processing Zone",
        contact_phone="+00 000 0000 0000",
        contact_email="",
        website="",
        tax_or_reg_no="",
        bank_beneficiary_name=company,
        bank_name="Bank of China (Configure in Settings)",
        bank_account_no="",
        bank_swift_code="BKCHCNBJ",
        bank_address="Configure the receiving bank address in Settings",
        default_terms_notes=(
            "1. Validity: 15 days from the issue date unless otherwise agreed in writing.\n"
            "2. Payment: 30% T/T deposit, balance before shipment, bank charges at buyer's "
            "account.\n"
            "3. Lead time: confirmed on order acknowledgement after deposit receipt.\n"
            "4. Packaging: export carton with buyer-approved marks; pallets on request.\n"
            "5. Quality: manufacturer standard warranty applies; certificates per quotation.\n"
            "6. Force majeure: delivery dates extend for events beyond reasonable control."
        ),
        footer_signature_title="Authorized Signature & Company Stamp",
    )


class CostVisibilitySampleResponse(BaseModel):
    sku: str
    currency: str
    unit_sale_price: Decimal
    cost_price_base: Decimal | None = None
    cogs_base: Decimal | None = None
    net_profit_base: Decimal | None = None
    net_margin_pct: Decimal | None = None
    cost_masked: bool = False


class AuditEventRead(BaseModel):
    id: int
    tenant_id: UUID
    created_at: datetime
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: UUID
    proposal_id: UUID | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    ip_address: str | None = None
    user_agent: str | None = None


class GdprExportResult(BaseModel):
    export_id: UUID
    tenant_id: UUID
    customer_id: UUID
    object_key: str
    download_url: str
    expires_in_seconds: int
    tables_included: list[str]


class GdprAnonymizeResult(BaseModel):
    tenant_id: UUID
    customer_id: UUID
    redacted_identifier: str
    identities_deleted: int
    financial_ledger_preserved: bool


class GdprTenantRedactResult(BaseModel):
    tenant_id: UUID
    shredded: bool
    deleted_s3_objects: int
