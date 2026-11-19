from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.crm.quote_service import QuoteService
from erp_core.crm.schemas import (
    ConvertQuoteRequest,
    ConvertQuoteResponse,
    LeadCreate,
    LeadRead,
    OpportunityCreate,
    OpportunityRead,
    OpportunityStageUpdate,
    QuoteCreate,
    QuotePdfResponse,
    QuoteRead,
    QuoteRepriceRequest,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/b2b", tags=["b2b"])
_service = QuoteService()

_SALES_ROLES = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
_MANAGER_ROLES = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)
_READER_ROLES = (
    UserRole.OWNER,
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.AGENT,
    UserRole.FINANCE,
)

SalesPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_SALES_ROLES))]
ManagerPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_MANAGER_ROLES))]
ReaderPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_READER_ROLES))]


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    ) or getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


@router.get("/leads", response_model=PaginatedResponse[LeadRead])
async def list_leads(
    request: Request,
    principal: SalesPrincipal,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[LeadRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_leads(session, principal.tenant_id, limit=limit, offset=offset)


@router.post("/leads", response_model=LeadRead, status_code=status.HTTP_201_CREATED)
async def create_lead(
    request: Request,
    principal: SalesPrincipal,
    payload: LeadCreate,
) -> LeadRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_lead(session, principal.tenant_id, payload)


@router.get("/opportunities", response_model=PaginatedResponse[OpportunityRead])
async def list_opportunities(
    request: Request,
    principal: SalesPrincipal,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[OpportunityRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_opportunities(
            session, principal.tenant_id, limit=limit, offset=offset
        )


@router.post("/opportunities", response_model=OpportunityRead, status_code=status.HTTP_201_CREATED)
async def create_opportunity(
    request: Request,
    principal: SalesPrincipal,
    payload: OpportunityCreate,
) -> OpportunityRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_opportunity(session, principal.tenant_id, payload)


@router.patch("/opportunities/{opportunity_id}", response_model=OpportunityRead)
async def update_opportunity_stage(
    request: Request,
    principal: SalesPrincipal,
    opportunity_id: UUID,
    payload: OpportunityStageUpdate,
) -> OpportunityRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.update_opportunity_stage(
            session, principal.tenant_id, str(principal.user_id), opportunity_id, payload.stage
        )


@router.get("/quotes", response_model=PaginatedResponse[QuoteRead])
async def list_quotes(
    request: Request,
    principal: ReaderPrincipal,
    opportunity_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[QuoteRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_quotes(
            session, principal.tenant_id, opportunity_id=opportunity_id, limit=limit, offset=offset
        )


@router.post("/quotes", response_model=QuoteRead, status_code=status.HTTP_201_CREATED)
async def create_quote(
    request: Request,
    principal: SalesPrincipal,
    payload: QuoteCreate,
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_quote(session, principal.tenant_id, principal, payload)


@router.post(
    "/quotes/{quote_id}/reprice-version",
    response_model=QuoteRead,
    status_code=status.HTTP_201_CREATED,
)
async def reprice_quote_version(
    request: Request,
    principal: SalesPrincipal,
    quote_id: UUID,
    payload: QuoteRepriceRequest,
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.reprice_quote_version(
            session, principal.tenant_id, principal, quote_id, payload
        )


@router.post("/quotes/{quote_id}/send", response_model=QuoteRead)
async def send_quote(
    request: Request,
    principal: SalesPrincipal,
    quote_id: UUID,
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.send_quote(session, principal.tenant_id, principal, quote_id)


@router.post("/quotes/{quote_id}/accept", response_model=QuoteRead)
async def accept_quote(
    request: Request,
    principal: SalesPrincipal,
    quote_id: UUID,
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.accept_quote(session, principal.tenant_id, principal, quote_id)


@router.post("/quotes/{quote_id}/pdf", response_model=QuotePdfResponse)
async def generate_quote_pdf(
    request: Request,
    principal: ReaderPrincipal,
    quote_id: UUID,
    language: str = Query(default="en", min_length=2, max_length=5),
) -> QuotePdfResponse:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.generate_pi_pdf(
            session, principal.tenant_id, quote_id, language=language
        )


@router.post("/quotes/{quote_id}/convert-to-order", response_model=ConvertQuoteResponse)
async def convert_quote_to_order(
    request: Request,
    principal: ManagerPrincipal,
    quote_id: UUID,
    payload: ConvertQuoteRequest,
) -> ConvertQuoteResponse:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.convert_quote_to_order(
            session, principal.tenant_id, principal, quote_id, payload
        )
