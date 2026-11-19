from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.shared.enums import ConfidenceLevel, CustomerType, IdentityType, ProposalType


class BuyerLocalTimeInfo(BaseModel):
    iana_timezone: str | None = None
    local_iso: str | None = None
    formatted_time: str | None = None
    local_hour: int | None = None
    is_quiet_hours: bool = False
    recommended_send_at_utc: datetime | None = None


class CustomerIdentityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    identity_type: IdentityType
    identity_value: str
    confidence_level: ConfidenceLevel
    is_active: bool
    created_at: datetime


class MergeProposalCandidate(BaseModel):
    proposal_type: ProposalType = ProposalType.IDENTITY_MERGE
    source_customer_id: UUID
    target_customer_id: UUID
    target_display_name: str
    similarity_score: float
    reason: str
    created_at: datetime


class CustomerSummaryRead(BaseModel):
    id: UUID
    display_name: str
    customer_type: CustomerType
    primary_email: str | None = None
    primary_phone_e164: str | None = None
    country_code: str | None = None
    preferred_language: str = "en"
    buyer_local_time: BuyerLocalTimeInfo


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    customer_type: CustomerType
    company_id: UUID | None = None
    display_name: str
    primary_email: str | None = None
    primary_phone_e164: str | None = None
    country_code: str | None = None
    postal_code: str | None = None
    preferred_language: str
    iana_timezone: str | None = None
    timezone_source: str | None = None
    lifecycle_stage: str
    ltv_usd: Decimal
    merged_into_customer_id: UUID | None = None
    created_at: datetime
    buyer_local_time: BuyerLocalTimeInfo
    identities: list[CustomerIdentityRead] = Field(default_factory=list)
    merge_proposals: list[MergeProposalCandidate] = Field(default_factory=list)


class CustomerUpdateRequest(BaseModel):
    display_name: str | None = None
    customer_type: CustomerType | None = None
    company_id: UUID | None = None
    preferred_language: str | None = None
    iana_timezone: str | None = None
    postal_code: str | None = None
    lifecycle_stage: str | None = None


class MergeCustomersRequest(BaseModel):
    source_customer_id: UUID
    target_customer_id: UUID
    reason: str = Field(min_length=2, max_length=500)


class MergeCustomersResponse(BaseModel):
    merge_event_id: UUID
    source_customer_id: UUID
    target_customer_id: UUID


class SplitCustomerRequest(BaseModel):
    merge_event_id: UUID
    reason: str = Field(min_length=2, max_length=500)


class SplitCustomerResponse(BaseModel):
    split_event_id: UUID
    merge_event_id: UUID
