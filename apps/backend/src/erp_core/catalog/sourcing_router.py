"""HTTP surface for Miaoshou-style sourcing drafts (collect/import/publish)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.catalog.sourcing_service import (
    SourcingCollectRequest,
    SourcingDeleteResult,
    SourcingDraftRead,
    SourcingImportResult,
    SourcingPublishRequest,
    SourcingPublishResult,
    SourcingService,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal, require_roles

router = APIRouter(prefix="/api/v1/catalog/sourcing", tags=["catalog-sourcing"])

_service = SourcingService()

_WRITER_ROLES = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)

ReaderPrincipal = Annotated[CurrentPrincipal, Depends(get_current_principal)]
WriterPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_WRITER_ROLES))]


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    ) or getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


@router.post("/collect", response_model=SourcingDraftRead, status_code=status.HTTP_201_CREATED)
async def collect_sourcing_draft(
    request: Request,
    payload: SourcingCollectRequest,
    principal: WriterPrincipal,
) -> SourcingDraftRead:
    """Collect a 1688 URL / supplier page / pasted copy into a priced English draft."""
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.collect(
            session, principal.tenant_id, str(principal.user_id), payload
        )


@router.get("/drafts", response_model=list[SourcingDraftRead])
async def list_sourcing_drafts(
    request: Request,
    principal: ReaderPrincipal,
) -> list[SourcingDraftRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_drafts(session, principal.tenant_id)


@router.get("/drafts/{draft_id}", response_model=SourcingDraftRead)
async def get_sourcing_draft(
    request: Request,
    draft_id: UUID,
    principal: ReaderPrincipal,
) -> SourcingDraftRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.get_draft(session, principal.tenant_id, draft_id)


@router.delete("/drafts/{draft_id}", response_model=SourcingDeleteResult)
async def delete_sourcing_draft(
    request: Request,
    draft_id: UUID,
    principal: WriterPrincipal,
) -> SourcingDeleteResult:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.delete_draft(
            session, principal.tenant_id, str(principal.user_id), draft_id
        )


@router.post(
    "/drafts/{draft_id}/import-to-catalog",
    response_model=SourcingImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_sourcing_draft_to_catalog(
    request: Request,
    draft_id: UUID,
    principal: WriterPrincipal,
) -> SourcingImportResult:
    """Materialize the draft into real products + product_variants rows."""
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.import_to_catalog(
            session, principal.tenant_id, str(principal.user_id), draft_id
        )


@router.post(
    "/drafts/{draft_id}/publish",
    response_model=list[SourcingPublishResult],
)
async def publish_sourcing_draft(
    request: Request,
    draft_id: UUID,
    payload: SourcingPublishRequest,
    principal: WriterPrincipal,
) -> list[SourcingPublishResult]:
    """Publish a draft to the selected channel accounts via platform SKU mappings.

    With ``push_to_marketplace=True`` the live publisher injected by the composition root
    (``app.state.marketplace_listing_publisher``) performs the real marketplace call. The
    catalog module never imports ``erp_core.channels``: it only sees the structural
    ``MarketplaceListingPublisher`` protocol.
    """
    publisher = getattr(request.app.state, "marketplace_listing_publisher", None)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.publish(
            session,
            principal.tenant_id,
            str(principal.user_id),
            draft_id,
            payload,
            listing_publisher=publisher,
        )


__all__ = ["router"]
