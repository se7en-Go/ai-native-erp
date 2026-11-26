from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.enums import UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles
from erp_core.tenancy.store_scope_schemas import (
    HandoverRequest,
    HandoverResult,
    StoreScopeAssignRequest,
    StoreScopeRead,
)
from erp_core.tenancy.store_scope_service import StoreScopeService

router = APIRouter(prefix="/api/v1/tenants", tags=["tenancy-store-scopes"])

_editor = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER))
_reader = Depends(require_roles(*UserRole))
_handover = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN))


def _service() -> StoreScopeService:
    return StoreScopeService()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/store-scopes", response_model=list[StoreScopeRead])
async def list_store_scopes(
    principal: Annotated[CurrentPrincipal, _reader],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
    user_id: UUID | None = None,
) -> list[StoreScopeRead]:
    return await _service().list_scopes(session, principal.tenant_id, principal, user_id=user_id)


@router.post(
    "/store-scopes",
    response_model=StoreScopeRead,
    status_code=status.HTTP_201_CREATED,
)
async def assign_store_scope(
    payload: StoreScopeAssignRequest,
    request: Request,
    principal: Annotated[CurrentPrincipal, _editor],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> StoreScopeRead:
    return await _service().assign_scope(
        session,
        principal.tenant_id,
        principal,
        payload,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


@router.delete("/store-scopes/{scope_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_store_scope(
    scope_id: UUID,
    request: Request,
    principal: Annotated[CurrentPrincipal, _editor],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> None:
    await _service().remove_scope(
        session,
        principal.tenant_id,
        principal,
        scope_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/handover", response_model=HandoverResult)
async def handover_member_assets(
    payload: HandoverRequest,
    principal: Annotated[CurrentPrincipal, _handover],
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
) -> HandoverResult:
    return await _service().handover_member_assets(
        session,
        principal.tenant_id,
        principal,
        from_user_id=payload.from_user_id,
        to_user_id=payload.to_user_id,
        deactivate_from_user=payload.deactivate_from_user,
        dry_run=payload.dry_run,
    )
