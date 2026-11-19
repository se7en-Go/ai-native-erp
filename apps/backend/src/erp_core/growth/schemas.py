"""Phase 3C growth request/response contracts.

Money-free by design; the only numeric business values are ratings and grades.
Enum-like fields stay `Literal` so an invalid source/status fails at the schema
boundary before it reaches the database CHECK constraints.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

LeadSourceProvider = Literal["google_places", "overpass_osm", "overture_poi", "csv_import"]
JobStatus = Literal["pending", "running", "completed", "failed"]
ProspectGrade = Literal["A", "B", "C"]
ProspectSyncStatus = Literal["raw", "cleaned", "synced_to_crm", "discarded"]
CampaignChannelType = Literal["email", "whatsapp_cloud"]
CampaignStatus = Literal["draft", "scheduled", "running", "paused", "completed"]
RecipientStatus = Literal["queued", "sent", "suppressed", "failed", "replied"]

Keyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Region = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
CampaignName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
Subject = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
BodyTemplate = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)
]
Rating = Annotated[Decimal, Field(ge=0, le=5, max_digits=3, decimal_places=2)]
MaxResults = Annotated[int, Field(ge=1, le=1000)]
CsvText = Annotated[str, Field(max_length=4_000_000)]
TargetIds = Annotated[list[UUID], Field(min_length=1, max_length=500)]
ProspectIds = Annotated[list[UUID], Field(min_length=1, max_length=500)]


class LeadCollectionJobCreate(BaseModel):
    source_provider: LeadSourceProvider
    keywords: Annotated[list[Keyword], Field(max_length=20)] = Field(default_factory=list)
    target_region: Region | None = None
    max_results: MaxResults = 100
    min_rating: Rating | None = None
    require_phone: bool = False
    # Compliant CSV/trade-show import: the caller supplies the bytes it is allowed to use.
    csv_content: CsvText | None = None

    @model_validator(mode="after")
    def validate_source_requirements(self) -> LeadCollectionJobCreate:
        if self.source_provider == "csv_import":
            if not (self.csv_content or "").strip():
                raise ValueError("csv_content is required for csv_import")
            return self
        if not self.keywords:
            raise ValueError("keywords is required for map/POI collection")
        if not (self.target_region or "").strip():
            raise ValueError("target_region is required for map/POI collection")
        return self


class LeadCollectionJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    source_provider: LeadSourceProvider
    keywords: list[str]
    target_region: str | None
    max_results: int
    min_rating: Decimal | None
    require_phone: bool
    status: JobStatus
    collected_count: int
    cleaned_count: int
    synced_count: int
    error_message: str | None
    created_by: UUID
    created_at: datetime
    completed_at: datetime | None


class ProspectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    job_id: UUID | None
    company_name: str
    contact_name: str | None
    phone_e164: str | None
    email: str | None
    website: str | None
    country: str | None
    city: str | None
    latitude: float | None
    longitude: float | None
    rating: Decimal | None
    grade: ProspectGrade
    whatsapp_reachable: bool | None
    sync_status: ProspectSyncStatus
    crm_lead_id: UUID | None
    raw_metadata: dict[str, object]
    created_at: datetime


class CollectionSummary(BaseModel):
    """KPI header for the collection page; always the real tenant-wide counts."""

    collected: int
    cleaned: int
    synced: int
    jobs: int


class ProspectCleanRequest(BaseModel):
    """Omit `prospect_ids` to clean every raw prospect of the workspace."""

    prospect_ids: ProspectIds | None = None


class ProspectCleanResult(BaseModel):
    cleaned: int
    discarded_duplicates: int
    graded: dict[ProspectGrade, int]
    items: list[ProspectRead]


class SyncToCrmRequest(BaseModel):
    prospect_ids: ProspectIds


class SyncedLead(BaseModel):
    prospect_id: UUID
    crm_lead_id: UUID
    customer_id: UUID
    company_id: UUID | None


class SyncToCrmResult(BaseModel):
    synced: int
    skipped: int
    leads: list[SyncedLead]


class CampaignCreate(BaseModel):
    name: CampaignName
    channel_type: CampaignChannelType
    subject: Subject | None = None
    body_template: BodyTemplate
    target_ids: TargetIds

    @model_validator(mode="after")
    def validate_subject(self) -> CampaignCreate:
        if self.channel_type == "email" and not (self.subject or "").strip():
            raise ValueError("subject is required for email campaigns")
        return self


class CampaignUpdate(BaseModel):
    name: CampaignName | None = None
    subject: Subject | None = None
    body_template: BodyTemplate | None = None
    status: CampaignStatus | None = None
    # Replacing the target list re-resolves every address and resets the queue.
    target_ids: TargetIds | None = None


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    name: str
    channel_type: CampaignChannelType
    subject: str | None
    body_template: str
    status: CampaignStatus
    total_targets: int
    sent_count: int
    suppressed_count: int
    replied_count: int
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class SuppressionReason(BaseModel):
    reason: str
    count: int


class CampaignPreview(BaseModel):
    """Authoritative compliance snapshot; the UI must never treat it as authorization."""

    campaign_id: UUID
    channel_type: CampaignChannelType
    eligible_count: int
    suppressed_count: int
    reasons: list[SuppressionReason]
    total_targets: int
    # Present for email campaigns so the UI can show the RFC 8058 header that will be sent.
    list_unsubscribe_header: str | None = None
    # Present for WhatsApp campaigns: remaining account-level token budget for this launch.
    rate_limit_remaining: int | None = None


class CampaignLaunchResult(BaseModel):
    campaign_id: UUID
    status: CampaignStatus
    sent: int
    suppressed: int
    queued_remaining: int
    reasons: list[SuppressionReason]


class CampaignRecipientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    customer_id: UUID | None
    prospect_id: UUID | None
    address: str
    status: RecipientStatus
    error_reason: str | None
    sent_at: datetime | None
