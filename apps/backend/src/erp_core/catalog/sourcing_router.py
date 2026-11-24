"""HTTP surface for Miaoshou-style sourcing drafts (collect/import/publish)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.catalog.collector_token_service import (
    CollectorTokenCreateRequest,
    CollectorTokenCreateResponse,
    CollectorTokenRead,
    CollectorTokenService,
)
from erp_core.catalog.media_service import CatalogMediaService
from erp_core.catalog.sourcing_copy_protocol import SourcingAiRewriteRequest, SourcingCopyRewriter
from erp_core.catalog.sourcing_service import (
    SourcingAiRewriteResponse,
    SourcingCollectRequest,
    SourcingDeleteResult,
    SourcingDraftRead,
    SourcingDraftUpdateRequest,
    SourcingImportResult,
    SourcingPublishRequest,
    SourcingPublishResult,
    SourcingService,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal, require_roles

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/catalog/sourcing", tags=["catalog-sourcing"])


class ExtensionCollectResponse(BaseModel):
    draft: SourcingDraftRead
    catalog_url: str
    mirrored_images_count: int

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


def _token_service() -> CollectorTokenService:
    return CollectorTokenService()


@router.post(
    "/collector-tokens",
    response_model=CollectorTokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_collector_token(
    request: Request,
    principal: WriterPrincipal,
    body: CollectorTokenCreateRequest,
) -> CollectorTokenCreateResponse:
    service = _token_service()
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await service.create_token(session, principal.tenant_id, principal.user_id, body)


@router.get("/collector-tokens", response_model=list[CollectorTokenRead])
async def list_collector_tokens(
    request: Request,
    principal: ReaderPrincipal,
) -> list[CollectorTokenRead]:
    service = _token_service()
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await service.list_tokens(session, principal.tenant_id)


@router.delete("/collector-tokens/{token_id}", response_model=CollectorTokenRead)
async def revoke_collector_token(
    request: Request,
    token_id: UUID,
    principal: WriterPrincipal,
) -> CollectorTokenRead:
    service = _token_service()
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await service.revoke_token(
            session, principal.tenant_id, token_id, str(principal.user_id)
        )


@router.post(
    "/collect-from-extension",
    response_model=ExtensionCollectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def collect_sourcing_draft_from_extension(
    request: Request,
    body: SourcingCollectRequest,
    x_collector_token: Annotated[str | None, Header(alias="X-Collector-Token")] = None,
) -> ExtensionCollectResponse:
    session_factory = _get_session_factory(request)
    token_service = _token_service()
    resolved = await token_service.authenticate_token(session_factory, x_collector_token)

    sourcing_service = SourcingService()
    async with tenant_session(session_factory, resolved.tenant_id) as session:
        draft = await sourcing_service.collect(
            session,
            resolved.tenant_id,
            str(resolved.created_by_user_id),
            body,
            skip_remote_fetch=True,
        )

    mirrored_count = 0
    if body.images:
        media_service = CatalogMediaService(storage=getattr(request.app.state, "storage", None))
        async with tenant_session(session_factory, resolved.tenant_id) as media_session:
            for img_url in body.images[:8]:
                try:
                    async with media_session.begin_nested():
                        ingested = await media_service.ingest_external_urls(
                            media_session,
                            resolved.tenant_id,
                            urls=[img_url],
                            sourcing_draft_id=draft.id,
                            alt_text=draft.optimized_title_en[:120],
                        )
                        mirrored_count += len(ingested)
                except Exception as exc:
                    logger.warning(
                        "collector.image_mirror_failed",
                        draft_id=str(draft.id),
                        image_url=img_url,
                        error=str(exc),
                    )

    return ExtensionCollectResponse(
        draft=draft,
        catalog_url="/catalog?tab=sourcing",
        mirrored_images_count=mirrored_count,
    )


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


@router.patch("/drafts/{draft_id}", response_model=SourcingDraftRead)
async def update_sourcing_draft(
    request: Request,
    draft_id: UUID,
    payload: SourcingDraftUpdateRequest,
    principal: WriterPrincipal,
) -> SourcingDraftRead:
    """Patch a sourcing draft's copy, price and per-marketplace category attributes."""
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.update_draft(
            session, principal.tenant_id, str(principal.user_id), draft_id, payload
        )


@router.post("/drafts/{draft_id}/ai-rewrite", response_model=SourcingAiRewriteResponse)
async def rewrite_sourcing_draft_copy(
    request: Request,
    draft_id: UUID,
    payload: SourcingAiRewriteRequest,
    principal: WriterPrincipal,
) -> SourcingAiRewriteResponse:
    """Rewrite a draft into a localized, platform-shaped listing copy deck.

    The LLM rewriter is injected by the composition root
    (``app.state.sourcing_copy_rewriter``); when it is absent the catalog-side deterministic
    localized copywriter runs, so the endpoint is always usable and always honest about
    which mode produced the copy. ``erp_core.catalog`` never imports the AI engine.
    """
    copy_rewriter: SourcingCopyRewriter | None = getattr(
        request.app.state, "sourcing_copy_rewriter", None
    )
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.ai_rewrite_draft(
            session,
            principal.tenant_id,
            str(principal.user_id),
            draft_id,
            payload,
            copy_rewriter=copy_rewriter,
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
