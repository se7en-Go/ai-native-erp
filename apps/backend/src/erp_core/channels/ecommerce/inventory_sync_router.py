"""HTTP endpoints for multi-platform real-time inventory sync logs & triggers."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.channels.ecommerce.inventory_sync_dispatcher import (
    InventorySyncDispatcher,
    InventorySyncDispatchSummary,
    InventorySyncJobRead,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal, require_roles

router = APIRouter(prefix="/api/v1/orders/inventory", tags=["inventory-sync"])

_default_dispatcher = InventorySyncDispatcher()
_reader = Depends(get_current_principal)
_writer = Depends(require_roles(UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER))


class InventorySyncNowRequest(BaseModel):
    variant_id: UUID | None = None
    channel_account_id: UUID | None = None


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _get_dispatcher(request: Request) -> InventorySyncDispatcher:
    dispatcher: InventorySyncDispatcher | None = getattr(
        request.app.state, "inventory_sync_dispatcher", None
    )
    return dispatcher or _default_dispatcher


@router.post("/sync-now", response_model=InventorySyncDispatchSummary)
async def trigger_inventory_sync_now(
    request: Request,
    variant_id: Annotated[UUID | None, Query()] = None,
    channel_account_id: Annotated[UUID | None, Query()] = None,
    payload: Annotated[InventorySyncNowRequest | None, Body()] = None,
    principal: CurrentPrincipal = _writer,
) -> InventorySyncDispatchSummary:
    """Enqueue and immediately push ERP sellable inventory to active ecommerce stores."""
    target_variant = (payload.variant_id if payload else None) or variant_id
    target_account = (payload.channel_account_id if payload else None) or channel_account_id
    dispatcher = _get_dispatcher(request)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await dispatcher.sync_inventory_now(
            session,
            principal.tenant_id,
            variant_id=target_variant,
            channel_account_id=target_account,
            trigger_source="manual",
        )


@router.get("/sync-logs", response_model=PaginatedResponse[InventorySyncJobRead])
async def list_inventory_sync_logs(
    request: Request,
    variant_id: Annotated[UUID | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    principal: CurrentPrincipal = _reader,
) -> PaginatedResponse[InventorySyncJobRead]:
    """List cross-channel inventory sync jobs ordered newest first."""
    dispatcher = _get_dispatcher(request)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        items, total = await dispatcher.list_sync_jobs(
            session,
            principal.tenant_id,
            variant_id=variant_id,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + len(items)) < total,
        )


@router.post("/sync-jobs/{job_id}/retry", response_model=InventorySyncJobRead)
async def retry_inventory_sync_job(
    request: Request,
    job_id: UUID,
    principal: CurrentPrincipal = _writer,
) -> InventorySyncJobRead:
    """Retry a failed or dead-letter channel inventory sync job using latest stock."""
    dispatcher = _get_dispatcher(request)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await dispatcher.retry_sync_job(session, principal.tenant_id, job_id)


__all__ = ["InventorySyncNowRequest", "router"]
