from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.kb_gap_schemas import (
    AiAdoptionMetrics,
    KbGapCreateRequest,
    KbGapPromoteRequest,
    KbGapPromoteResult,
    KbGapRead,
    RedlineCheckRequest,
    RedlineCheckResult,
    RedlineRuleCreate,
    RedlineRuleRead,
)
from erp_core.ai_engine.kb_gap_service import KbGapService
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import (
    CurrentPrincipal,
    get_tenant_db,
    require_roles,
)

router = APIRouter(prefix="/api/v1/ai", tags=["ai-kb-gaps"])

_editor = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER))
_reader = Depends(require_roles(*UserRole))


def _service() -> KbGapService:
    return KbGapService()


# --------------------------------------------------------------- red lines


@router.get("/redline-rules", response_model=list[RedlineRuleRead])
async def list_redline_rules(
    principal: Annotated[CurrentPrincipal, _reader],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> list[RedlineRuleRead]:
    return await _service().list_redline_rules(session, principal.tenant_id)


@router.post(
    "/redline-rules",
    response_model=RedlineRuleRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_redline_rule(
    payload: RedlineRuleCreate,
    principal: Annotated[CurrentPrincipal, _editor],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> RedlineRuleRead:
    return await _service().create_redline_rule(
        session, principal.tenant_id, payload, actor_id=str(principal.user_id)
    )


@router.delete("/redline-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_redline_rule(
    rule_id: UUID,
    principal: Annotated[CurrentPrincipal, _editor],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> None:
    await _service().delete_redline_rule(
        session, principal.tenant_id, rule_id, actor_id=str(principal.user_id)
    )


@router.post("/redline-rules/check", response_model=RedlineCheckResult)
async def check_outbound_text(
    payload: RedlineCheckRequest,
    principal: Annotated[CurrentPrincipal, _reader],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> RedlineCheckResult:
    return await _service().check_outbound_text(session, principal.tenant_id, payload.content)


# ----------------------------------------------------------------- KB gaps


@router.get("/kb-gaps", response_model=PaginatedResponse[KbGapRead])
async def list_kb_gaps(
    principal: Annotated[CurrentPrincipal, _reader],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    gap_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedResponse[KbGapRead]:
    return await _service().list_kb_gaps(
        session, principal.tenant_id, status=gap_status, limit=limit, offset=offset
    )


@router.post(
    "/kb-gaps",
    response_model=KbGapRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_kb_gap(
    payload: KbGapCreateRequest,
    principal: Annotated[CurrentPrincipal, _reader],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> KbGapRead:
    return await _service().create_or_bump_kb_gap(session, principal.tenant_id, payload)


@router.post("/kb-gaps/{gap_id}/promote", response_model=KbGapPromoteResult)
async def promote_kb_gap(
    gap_id: UUID,
    payload: KbGapPromoteRequest,
    principal: Annotated[CurrentPrincipal, _editor],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> KbGapPromoteResult:
    return await _service().promote_kb_gap(
        session,
        principal.tenant_id,
        gap_id,
        payload.resolved_answer,
        actor_id=str(principal.user_id),
    )


@router.post("/kb-gaps/{gap_id}/dismiss", response_model=KbGapRead)
async def dismiss_kb_gap(
    gap_id: UUID,
    principal: Annotated[CurrentPrincipal, _editor],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> KbGapRead:
    return await _service().dismiss_kb_gap(
        session, principal.tenant_id, gap_id, actor_id=str(principal.user_id)
    )


# -------------------------------------------------------- adoption metrics


@router.get("/adoption-metrics", response_model=AiAdoptionMetrics)
async def get_adoption_metrics(
    principal: Annotated[CurrentPrincipal, _reader],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> AiAdoptionMetrics:
    return await _service().get_ai_adoption_metrics(session, principal.tenant_id, days=days)
