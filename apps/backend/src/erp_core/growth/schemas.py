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
# How the collection run picks its provider. `auto` and `synthetic` are the only
# modes allowed to fall back to the deterministic catalogue, so a demo run can
# never crash while an explicit provider choice still fails honestly.
LeadProviderMode = Literal["auto", "overpass", "google_places", "synthetic"]
JobStatus = Literal["pending", "running", "completed", "failed"]
ProspectGrade = Literal["A", "B", "C"]
ProspectSyncStatus = Literal["raw", "cleaned", "synced_to_crm", "discarded"]
CampaignChannelType = Literal["email", "whatsapp_cloud"]
CampaignStatus = Literal["draft", "scheduled", "running", "paused", "completed"]
RecipientStatus = Literal["queued", "sent", "suppressed", "failed", "replied"]

Keyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Region = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
City = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
CityList = Annotated[list[City], Field(min_length=1, max_length=10)]
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
    """Create a collection job.

    Two shapes are accepted and they stay fully backwards compatible:

    * legacy — `source_provider` pinned, e.g. `overpass_osm` + `target_region`; the
      job stays `pending` until `POST /jobs/{id}/run` (unchanged behaviour);
    * batch — `cities`/`city` (one or many), `keywords`/`keyword`, and
      `provider_mode`; the recipe is request-scoped, so it executes immediately.
    """

    source_provider: LeadSourceProvider | None = None
    provider_mode: LeadProviderMode = "auto"
    keywords: Annotated[list[Keyword], Field(max_length=20)] = Field(default_factory=list)
    keyword: Keyword | None = None
    target_region: Region | None = None
    city: City | None = None
    cities: CityList | None = None
    max_results: MaxResults = 100
    min_rating: Rating | None = None
    require_phone: bool = False
    # Compliant CSV/trade-show import: the caller supplies the bytes it is allowed to use.
    csv_content: CsvText | None = None

    @property
    def effective_keywords(self) -> tuple[str, ...]:
        keywords = list(self.keywords)
        if self.keyword is not None and self.keyword not in keywords:
            keywords.insert(0, self.keyword)
        return tuple(keyword for keyword in keywords if keyword.strip())

    @property
    def effective_regions(self) -> tuple[str, ...]:
        regions: list[str] = []
        if self.city is not None:
            regions.append(self.city)
        for city in self.cities or []:
            if city not in regions:
                regions.append(city)
        if not regions and (self.target_region or "").strip():
            regions.append((self.target_region or "").strip())
        return tuple(regions)

    @property
    def resolved_source_provider(self) -> LeadSourceProvider:
        """The provider recorded on the job row (the requested primary source)."""
        if self.source_provider is not None:
            return self.source_provider
        if self.csv_content:
            return "csv_import"
        if self.provider_mode == "google_places":
            return "google_places"
        return "overpass_osm"

    @property
    def runs_immediately(self) -> bool:
        """A batch recipe cannot be replayed later, so it runs in this request."""
        return bool(self.csv_content) or self.source_provider is None or (
            self.provider_mode != "auto"
            or self.cities is not None
            or self.city is not None
            or self.keyword is not None
        )

    @model_validator(mode="after")
    def validate_source_requirements(self) -> LeadCollectionJobCreate:
        is_csv = self.source_provider == "csv_import" or (
            self.source_provider is None and bool((self.csv_content or "").strip())
        )
        if is_csv:
            if not (self.csv_content or "").strip():
                raise ValueError("csv_content is required for csv_import")
            if self.provider_mode != "auto":
                raise ValueError("provider_mode is not supported for csv_import")
            return self
        if not self.effective_keywords:
            raise ValueError("keywords is required for map/POI collection")
        if not self.effective_regions:
            raise ValueError("target_region or cities is required for map/POI collection")
        if self.source_provider is not None and self.provider_mode != "auto":
            pinned = {
                "overpass": "overpass_osm",
                "google_places": "google_places",
            }.get(self.provider_mode)
            if pinned is not None and pinned != self.source_provider:
                raise ValueError(
                    "provider_mode conflicts with source_provider; send only one of them"
                )
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
    # Actual per-record sources of this job (`overpass_osm`, `google_places`,
    # `fallback_catalog`, ...) with their record counts, so the UI can show whether
    # the leads are live map data or the deterministic fallback catalogue.
    sources: dict[str, int] = Field(default_factory=dict)


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


EnrichProspectIds = Annotated[list[UUID], Field(min_length=1, max_length=50)]
EnrichmentStatus = Literal[
    "enriched",
    "no_email_found",
    "blocked",
    "invalid_website",
    "skipped_no_website",
    "already_has_email",
    "not_found",
]


class EnrichProspectEmailsRequest(BaseModel):
    """Enrich selected prospects from the e-mail addresses their website publishes."""

    prospect_ids: EnrichProspectIds
    force: bool = False


class EnrichedProspectItem(BaseModel):
    prospect_id: UUID
    company_name: str
    website: str | None
    email: str | None
    previous_email: str | None
    grade: ProspectGrade
    status: EnrichmentStatus


class EnrichProspectEmailsResponse(BaseModel):
    checked_count: int
    enriched_count: int
    items: list[EnrichedProspectItem]


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


class SendRealCampaignRequest(BaseModel):
    """Really deliver a queued email campaign through the tenant's own mailbox."""

    channel_account_id: UUID | None = None
    dry_run: bool = False
    max_send: Annotated[int, Field(ge=1, le=50)] = 20


class SendRealCampaignResponse(BaseModel):
    """Counts of one real delivery run.

    On a `dry_run` nothing is persisted and `sent_count` reports how many messages
    *would* have been delivered.
    """

    campaign_id: UUID
    provider: str
    from_email: str
    sent_count: int
    suppressed_count: int
    failed_count: int
    dry_run: bool
