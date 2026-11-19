from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.workforce_schemas import (
    AiEmployeeCreate,
    AiEmployeeRead,
    AiEmployeeUpdate,
    CreativeAssetCreate,
    CreativeAssetRead,
    CreativeAssetType,
    CreativeAssetUpdate,
    InboxAssistRequest,
    InboxAssistResponse,
    QuickReplyCreate,
    QuickReplyRead,
    WorkforceMatrixResponse,
)
from erp_core.ai_engine.workforce_service import WorkforceService
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import (
    CurrentPrincipal,
    get_current_principal,
    get_tenant_db,
    require_roles,
)

workforce_router = APIRouter(prefix="/api/v1/ai", tags=["ai-workforce"])

Principal = Annotated[CurrentPrincipal, Depends(get_current_principal)]
PeopleManager = Annotated[
    CurrentPrincipal, Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER))
]
DeskAgent = Annotated[
    CurrentPrincipal,
    Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)),
]
Db = Annotated[AsyncSession, Depends(get_tenant_db)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def provider_unavailable() -> AppError:
    """Map a missing/failed LLM provider onto an explicit, non-fabricating error."""
    return AppError(
        slug="ai-provider-unavailable",
        title="AI Provider Unavailable",
        status_code=503,
        detail="No LLM provider key is configured for this workspace.",
        code="ERR_AI_PROVIDER_UNAVAILABLE",
    )


def _service(request: Request) -> WorkforceService:
    runtime = getattr(request.app.state, "ai_runtime", None)
    if runtime is None:  # pragma: no cover - the app composition root always sets it
        return WorkforceService()
    return WorkforceService(
        llm_router=runtime.llm_router,
        wallet_service=runtime.wallet_service,
        tool_registry=runtime.tool_registry,
    )


Service = Annotated[WorkforceService, Depends(_service)]


@workforce_router.get("/employees")
async def list_employees(
    session: Db,
    principal: Principal,
    service: Service,
    limit: Limit = 50,
    offset: Offset = 0,
    department: str | None = None,
    is_enabled: bool | None = None,
) -> PaginatedResponse[AiEmployeeRead]:
    return await service.list_employees(
        session,
        principal.tenant_id,
        limit=limit,
        offset=offset,
        department=department,
        is_enabled=is_enabled,
    )


@workforce_router.get("/employees/matrix")
async def workforce_matrix(
    session: Db, principal: Principal, service: Service
) -> WorkforceMatrixResponse:
    return await service.matrix(session, principal.tenant_id, principal.role)


@workforce_router.post("/employees", status_code=201)
async def create_employee(
    body: AiEmployeeCreate, session: Db, principal: PeopleManager, service: Service
) -> AiEmployeeRead:
    return await service.create_employee(
        session, principal.tenant_id, body, actor_id=principal.user_id
    )


@workforce_router.patch("/employees/{employee_id}")
async def update_employee(
    employee_id: UUID,
    body: AiEmployeeUpdate,
    session: Db,
    principal: PeopleManager,
    service: Service,
) -> AiEmployeeRead:
    return await service.update_employee(
        session, principal.tenant_id, employee_id, body, actor_id=principal.user_id
    )


@workforce_router.post("/inbox/assist")
async def inbox_assist(
    body: InboxAssistRequest, session: Db, principal: DeskAgent, service: Service
) -> InboxAssistResponse:
    try:
        return await service.inbox_assist(session, principal.tenant_id, body)
    except RuntimeError:
        raise provider_unavailable() from None


@workforce_router.get("/quick-replies")
async def list_quick_replies(
    session: Db,
    principal: Principal,
    service: Service,
    limit: Limit = 100,
    offset: Offset = 0,
    language: str | None = Query(default=None, pattern="^(zh|en)$"),
    category: str | None = None,
) -> PaginatedResponse[QuickReplyRead]:
    return await service.list_quick_replies(
        session,
        principal.tenant_id,
        limit=limit,
        offset=offset,
        language=language,
        category=category,
    )


@workforce_router.post("/quick-replies", status_code=201)
async def create_quick_reply(
    body: QuickReplyCreate, session: Db, principal: DeskAgent, service: Service
) -> QuickReplyRead:
    return await service.create_quick_reply(
        session, principal.tenant_id, body, actor_id=principal.user_id
    )


@workforce_router.delete("/quick-replies/{template_id}", status_code=204)
async def delete_quick_reply(
    template_id: UUID, session: Db, principal: DeskAgent, service: Service
) -> Response:
    await service.delete_quick_reply(
        session, principal.tenant_id, template_id, actor_id=principal.user_id
    )
    return Response(status_code=204)


@workforce_router.get("/studio/assets")
async def list_assets(
    session: Db,
    principal: Principal,
    service: Service,
    limit: Limit = 50,
    offset: Offset = 0,
    asset_type: CreativeAssetType | None = None,
    status: str | None = Query(default=None, pattern="^(draft|approved|archived)$"),
) -> PaginatedResponse[CreativeAssetRead]:
    return await service.list_assets(
        session,
        principal.tenant_id,
        limit=limit,
        offset=offset,
        asset_type=asset_type.value if asset_type else None,
        status=status,
    )


@workforce_router.post("/studio/assets", status_code=201)
async def create_asset(
    body: CreativeAssetCreate, session: Db, principal: DeskAgent, service: Service
) -> CreativeAssetRead:
    try:
        return await service.create_asset(
            session, principal.tenant_id, body, actor_id=principal.user_id
        )
    except RuntimeError:
        raise provider_unavailable() from None


@workforce_router.patch("/studio/assets/{asset_id}")
async def update_asset(
    asset_id: UUID,
    body: CreativeAssetUpdate,
    session: Db,
    principal: DeskAgent,
    service: Service,
) -> CreativeAssetRead:
    return await service.update_asset(
        session, principal.tenant_id, asset_id, body, actor_id=principal.user_id
    )


router = workforce_router
