"""Phase 3C growth API: collection jobs, prospect pool, CRM sync and campaigns.

Every route runs inside a tenant-scoped transaction (`get_tenant_db`) so the
`RESTRICTIVE` RLS policies do the cross-tenant isolation, and RBAC is enforced by
`require_roles`: writes/execution are Owner/Admin/Manager, reads also allow Agent.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.channels.rate_limiter import DualTokenBucketLimiter
from erp_core.growth.schemas import (
    CampaignCreate,
    CampaignLaunchResult,
    CampaignPreview,
    CampaignRead,
    CampaignRecipientRead,
    CampaignUpdate,
    CollectionSummary,
    LeadCollectionJobCreate,
    LeadCollectionJobRead,
    ProspectCleanRequest,
    ProspectCleanResult,
    ProspectRead,
    SyncToCrmRequest,
    SyncToCrmResult,
)
from erp_core.growth.service import GrowthService
from erp_core.shared.config import get_settings
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

router = APIRouter(prefix="/growth", tags=["growth"])

_MANAGERS = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)
Operator = Annotated[CurrentPrincipal, Depends(require_roles(*_MANAGERS))]
Reader = Annotated[CurrentPrincipal, Depends(require_roles(*UserRole))]
TenantDB = Annotated[AsyncSession, Depends(get_tenant_db)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def _service(request: Request) -> GrowthService:
    """Compose the service from app state so tests can inject a MockTransport client."""
    settings = get_settings()
    client = getattr(request.app.state, "growth_http_client", None)
    redis = getattr(request.app.state, "redis", None)
    limiter = DualTokenBucketLimiter(redis) if redis is not None else None
    return GrowthService(
        http_client=client,
        google_places_api_key=settings.GOOGLE_PLACES_API_KEY,
        rate_limiter=limiter,
    )


Service = Annotated[GrowthService, Depends(_service)]


@router.get("/jobs")
async def list_jobs(
    session: TenantDB, principal: Reader, service: Service, limit: Limit = 20, offset: Offset = 0
) -> PaginatedResponse[LeadCollectionJobRead]:
    return await service.list_jobs(session, principal.tenant_id, limit, offset)


@router.post("/jobs", status_code=201)
async def create_job(
    payload: LeadCollectionJobCreate,
    session: TenantDB,
    principal: Operator,
    service: Service,
) -> LeadCollectionJobRead:
    """Map/POI jobs stay `pending` until `/run`; a CSV payload runs immediately.

    The uploaded CSV bytes are request-scoped and never persisted, so the import
    has to execute while the payload is still in memory.
    """
    job = await service.create_job(session, principal.tenant_id, principal.user_id, payload)
    if payload.csv_content:
        job = await service.run_job(
            session, principal.tenant_id, job.id, csv_content=payload.csv_content
        )
    return LeadCollectionJobRead.model_validate(job)


@router.post("/jobs/{job_id}/run")
async def run_job(
    job_id: UUID, session: TenantDB, principal: Operator, service: Service
) -> LeadCollectionJobRead:
    try:
        job = await service.run_job(session, principal.tenant_id, job_id)
    except AppError:
        # The run already recorded `status='failed'` + `error_message`; commit that
        # audit trail before the problem detail rolls the request transaction back.
        await session.commit()
        raise
    return LeadCollectionJobRead.model_validate(job)


@router.get("/summary")
async def collection_summary(
    session: TenantDB, principal: Reader, service: Service
) -> CollectionSummary:
    return await service.collection_summary(session, principal.tenant_id)


@router.get("/prospects")
async def list_prospects(
    session: TenantDB,
    principal: Reader,
    service: Service,
    grade: str | None = Query(default=None, pattern="^[ABC]$"),
    sync_status: str | None = Query(
        default=None, pattern="^(raw|cleaned|synced_to_crm|discarded)$"
    ),
    job_id: UUID | None = None,
    q: str | None = Query(default=None, max_length=120),
    limit: Limit = 50,
    offset: Offset = 0,
) -> PaginatedResponse[ProspectRead]:
    return await service.list_prospects(
        session,
        principal.tenant_id,
        limit=limit,
        offset=offset,
        grade=grade,
        sync_status=sync_status,
        job_id=job_id,
        query=q,
    )


@router.post("/prospects/clean")
async def clean_prospects(
    payload: ProspectCleanRequest,
    session: TenantDB,
    principal: Operator,
    service: Service,
) -> ProspectCleanResult:
    return await service.clean_prospects(
        session, principal.tenant_id, payload.prospect_ids
    )


@router.post("/prospects/sync-crm")
async def sync_to_crm(
    payload: SyncToCrmRequest,
    session: TenantDB,
    principal: Operator,
    service: Service,
) -> SyncToCrmResult:
    return await service.sync_to_crm(
        session, principal.tenant_id, principal.user_id, payload.prospect_ids
    )


@router.get("/prospects/export.csv")
async def export_prospects(
    session: TenantDB,
    principal: Reader,
    service: Service,
    ids: Annotated[list[UUID] | None, Query()] = None,
) -> Response:
    body = await service.export_prospects_csv(session, principal.tenant_id, ids)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="growth-prospects.csv"'},
    )


@router.get("/campaigns")
async def list_campaigns(
    session: TenantDB, principal: Reader, service: Service, limit: Limit = 20, offset: Offset = 0
) -> PaginatedResponse[CampaignRead]:
    return await service.list_campaigns(session, principal.tenant_id, limit, offset)


@router.post("/campaigns", status_code=201)
async def create_campaign(
    payload: CampaignCreate, session: TenantDB, principal: Operator, service: Service
) -> CampaignRead:
    campaign = await service.create_campaign(
        session, principal.tenant_id, principal.user_id, payload
    )
    return CampaignRead.model_validate(campaign)


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(
    campaign_id: UUID,
    payload: CampaignUpdate,
    session: TenantDB,
    principal: Operator,
    service: Service,
) -> CampaignRead:
    campaign = await service.update_campaign(
        session, principal.tenant_id, campaign_id, payload
    )
    return CampaignRead.model_validate(campaign)


@router.get("/campaigns/{campaign_id}/recipients")
async def list_campaign_recipients(
    campaign_id: UUID,
    session: TenantDB,
    principal: Reader,
    service: Service,
    limit: Limit = 50,
    offset: Offset = 0,
) -> PaginatedResponse[CampaignRecipientRead]:
    return await service.list_recipients(
        session, principal.tenant_id, campaign_id, limit, offset
    )


@router.post("/campaigns/{campaign_id}/preview")
async def preview_campaign(
    campaign_id: UUID, session: TenantDB, principal: Operator, service: Service
) -> CampaignPreview:
    return await service.preview_campaign(session, principal.tenant_id, campaign_id)


@router.post("/campaigns/{campaign_id}/launch")
async def launch_campaign(
    campaign_id: UUID, session: TenantDB, principal: Operator, service: Service
) -> CampaignLaunchResult:
    return await service.launch_campaign(session, principal.tenant_id, campaign_id)
