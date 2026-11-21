from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.adapters.email import EmailAdapter
from erp_core.crm.models import Lead
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
    SendQuoteEmailRequest,
    SendQuoteEmailResponse,
)
from erp_core.crm.workbench import create_scoped_opportunity, require_customer, update_lead
from erp_core.crm.workbench_schemas import LeadUpdate
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import LeadStatus, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.query import page_of
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


def _mask_quote_for_role(quote: QuoteRead, role: UserRole) -> QuoteRead:
    """Strip margin/cost internals an AGENT must never see; other roles are untouched.

    AGENT is a quota-carrying sales seat: it may price quotes but must never learn the
    landed cost snapshot or the resulting margin. Masking happens at the response edge so
    the persisted Decimal snapshots (needed by finance/profit reporting) stay intact.
    """
    if role != UserRole.AGENT:
        return quote
    masked_items = [
        item.model_copy(update={"unit_cost_base_snapshot": None}) for item in quote.items
    ]
    return quote.model_copy(update={"items": masked_items})


def _mask_quote_page(
    page: PaginatedResponse[QuoteRead], role: UserRole
) -> PaginatedResponse[QuoteRead]:
    if role != UserRole.AGENT:
        return page
    return PaginatedResponse[QuoteRead](
        items=[_mask_quote_for_role(quote, role) for quote in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    ) or getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


def build_email_adapter(request: Request) -> EmailAdapter:
    """Build the delivery engine, honouring an injected HTTP transport in tests."""
    transport = getattr(request.app.state, "email_http_transport", None)
    return EmailAdapter(http_transport=transport)


@router.get("/leads", response_model=PaginatedResponse[LeadRead])
async def list_leads(
    request: Request,
    principal: SalesPrincipal,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    lead_status: LeadStatus | None = None,
) -> PaginatedResponse[LeadRead]:
    stmt = select(Lead).where(Lead.tenant_id == principal.tenant_id)
    if lead_status is not None:
        stmt = stmt.where(Lead.status == lead_status)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await page_of(
            session,
            stmt.order_by(Lead.created_at.desc(), Lead.id),
            LeadRead.model_validate,
            limit,
            offset,
        )


@router.post("/leads", response_model=LeadRead, status_code=status.HTTP_201_CREATED)
async def create_lead(
    request: Request,
    principal: SalesPrincipal,
    payload: LeadCreate,
) -> LeadRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        await require_customer(session, principal.tenant_id, payload.customer_id)
        return await _service.create_lead(session, principal.tenant_id, payload)


@router.patch("/leads/{lead_id}", response_model=LeadRead)
async def patch_lead(
    request: Request,
    principal: SalesPrincipal,
    lead_id: UUID,
    payload: LeadUpdate,
) -> LeadRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return LeadRead.model_validate(
            await update_lead(session, principal.tenant_id, lead_id, payload)
        )


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
        return await create_scoped_opportunity(session, principal.tenant_id, payload, _service)


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
        page = await _service.list_quotes(
            session, principal.tenant_id, opportunity_id=opportunity_id, limit=limit, offset=offset
        )
    return _mask_quote_page(page, principal.role)


@router.post("/quotes", response_model=QuoteRead, status_code=status.HTTP_201_CREATED)
async def create_quote(
    request: Request,
    principal: SalesPrincipal,
    payload: QuoteCreate,
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        quote = await _service.create_quote(session, principal.tenant_id, principal, payload)
    return _mask_quote_for_role(quote, principal.role)


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
        quote = await _service.reprice_quote_version(
            session, principal.tenant_id, principal, quote_id, payload
        )
    return _mask_quote_for_role(quote, principal.role)


@router.post("/quotes/{quote_id}/send", response_model=QuoteRead)
async def send_quote(
    request: Request,
    principal: SalesPrincipal,
    quote_id: UUID,
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        quote = await _service.send_quote(session, principal.tenant_id, principal, quote_id)
    return _mask_quote_for_role(quote, principal.role)


@router.post("/quotes/{quote_id}/accept", response_model=QuoteRead)
async def accept_quote(
    request: Request,
    principal: SalesPrincipal,
    quote_id: UUID,
) -> QuoteRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        quote = await _service.accept_quote(session, principal.tenant_id, principal, quote_id)
    return _mask_quote_for_role(quote, principal.role)


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


@router.get("/quotes/{quote_id}/pdf/download")
async def download_quote_pdf(
    request: Request,
    principal: ReaderPrincipal,
    quote_id: UUID,
    language: Literal["en", "zh"] = Query(default="en"),
) -> Response:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        pdf_bytes, quote = await _service.render_pi_pdf_bytes(
            session, principal.tenant_id, quote_id, language=language
        )
        filename = _service.pi_pdf_filename(quote)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/quotes/{quote_id}/send-email", response_model=SendQuoteEmailResponse)
async def send_quote_email(
    request: Request,
    principal: SalesPrincipal,
    quote_id: UUID,
    payload: SendQuoteEmailRequest,
) -> SendQuoteEmailResponse:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.send_quote_email(
            session,
            principal.tenant_id,
            principal,
            quote_id,
            payload,
            email_adapter=build_email_adapter(request),
        )


@router.post("/quotes/{quote_id}/convert-to-order", response_model=ConvertQuoteResponse)
async def convert_quote_to_order(
    request: Request,
    principal: ManagerPrincipal,
    quote_id: UUID,
    payload: ConvertQuoteRequest,
) -> ConvertQuoteResponse:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        converted = await _service.convert_quote_to_order(
            session, principal.tenant_id, principal, quote_id, payload
        )
    return converted.model_copy(
        update={"quote": _mask_quote_for_role(converted.quote, principal.role)}
    )
