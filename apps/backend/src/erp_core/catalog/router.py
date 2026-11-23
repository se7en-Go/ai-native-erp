from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.catalog.media_service import (
    MAX_MEDIA_BYTES,
    CatalogMediaService,
    MediaAssetRead,
    MediaUrlIngestRequest,
)
from erp_core.catalog.schemas import (
    CatalogCsvImportRequest,
    CatalogCsvImportResponse,
    LiveSkuCheckRead,
    ProductCreate,
    ProductRead,
    SkuMappingCreate,
    SkuMappingRead,
    SkuMappingUpdate,
    VariantCreate,
    VariantMappingsBundleRead,
    VariantRead,
    VariantUpdate,
)
from erp_core.catalog.service import CatalogService
from erp_core.catalog.sourcing_service import (
    SourcingPublishRequest,
    SourcingPublishResult,
    SourcingService,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/catalog", tags=["catalog"])
_service = CatalogService()
_sourcing = SourcingService()

_READER_ROLES = (
    UserRole.OWNER,
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.AGENT,
    UserRole.FINANCE,
)
_WRITER_ROLES = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)

ReaderPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_READER_ROLES))]
WriterPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_WRITER_ROLES))]


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    ) or getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


@router.get("/products", response_model=PaginatedResponse[ProductRead])
async def list_products(
    request: Request,
    principal: ReaderPrincipal,
    search: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[ProductRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_products(
            session, principal.tenant_id, limit=limit, offset=offset, search=search
        )


@router.post("/products", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_product(
    request: Request,
    payload: ProductCreate,
    principal: WriterPrincipal,
) -> ProductRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_product(
            session, principal.tenant_id, str(principal.user_id), payload
        )


@router.get("/variants", response_model=PaginatedResponse[VariantRead])
async def list_variants(
    request: Request,
    principal: ReaderPrincipal,
    product_id: UUID | None = None,
    sku: str | None = None,
    q: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[VariantRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_variants(
            session,
            principal.tenant_id,
            role=principal.role,
            product_id=product_id,
            sku=sku,
            q=q,
            limit=limit,
            offset=offset,
        )


@router.get("/live-check", response_model=LiveSkuCheckRead)
async def live_check_sku(
    request: Request,
    principal: ReaderPrincipal,
    sku: str = Query(..., min_length=1),
    qty: int = Query(default=1, ge=1),
    incoterm: str = Query(default="FOB", min_length=3, max_length=8),
) -> LiveSkuCheckRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.live_check_sku(
            session, principal.tenant_id, sku=sku, qty=qty, incoterm=incoterm
        )


@router.post("/variants", response_model=VariantRead, status_code=status.HTTP_201_CREATED)
async def create_variant(
    request: Request,
    payload: VariantCreate,
    principal: WriterPrincipal,
) -> VariantRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_variant(
            session, principal.tenant_id, str(principal.user_id), payload, role=principal.role
        )


@router.post("/variants/import-csv", response_model=CatalogCsvImportResponse)
async def import_variants_csv(
    request: Request,
    payload: CatalogCsvImportRequest,
    principal: WriterPrincipal,
) -> CatalogCsvImportResponse:
    """Bulk upsert products and variants from a case-insensitive CSV sheet."""
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.import_variants_csv(
            session, principal.tenant_id, str(principal.user_id), payload.csv_text
        )


@router.patch("/variants/{variant_id}", response_model=VariantRead)
async def update_variant(
    request: Request,
    variant_id: UUID,
    payload: VariantUpdate,
    principal: WriterPrincipal,
) -> VariantRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.update_variant(
            session,
            principal.tenant_id,
            str(principal.user_id),
            variant_id,
            payload,
            role=principal.role,
        )


@router.get("/variants/{variant_id}/mappings", response_model=VariantMappingsBundleRead)
async def list_variant_mappings(
    request: Request,
    variant_id: UUID,
    principal: ReaderPrincipal,
) -> VariantMappingsBundleRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_variant_mappings(
            session, principal.tenant_id, variant_id, role=principal.role
        )


@router.get("/sku-mappings", response_model=PaginatedResponse[SkuMappingRead])
async def list_sku_mappings(
    request: Request,
    principal: ReaderPrincipal,
    channel_account_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[SkuMappingRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_sku_mappings(
            session,
            principal.tenant_id,
            channel_account_id=channel_account_id,
            limit=limit,
            offset=offset,
        )


@router.post("/sku-mappings", response_model=SkuMappingRead, status_code=status.HTTP_201_CREATED)
async def create_sku_mapping(
    request: Request,
    payload: SkuMappingCreate,
    principal: WriterPrincipal,
) -> SkuMappingRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_sku_mapping(
            session, principal.tenant_id, str(principal.user_id), payload
        )


@router.put("/mappings/{mapping_id}", response_model=SkuMappingRead)
async def update_sku_mapping(
    request: Request,
    mapping_id: UUID,
    payload: SkuMappingUpdate,
    principal: WriterPrincipal,
) -> SkuMappingRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.update_sku_mapping(
            session, principal.tenant_id, str(principal.user_id), mapping_id, payload
        )


def _media_service(request: Request) -> CatalogMediaService:
    storage = getattr(request.app.state, "storage", None)
    return CatalogMediaService(storage=storage)


@router.post("/media/upload", response_model=MediaAssetRead, status_code=status.HTTP_201_CREATED)
async def upload_catalog_media(
    request: Request,
    principal: WriterPrincipal,
    file: Annotated[UploadFile, File(...)],
    product_id: Annotated[UUID | None, Form()] = None,
    variant_id: Annotated[UUID | None, Form()] = None,
    sourcing_draft_id: Annotated[UUID | None, Form()] = None,
    alt_text: Annotated[str, Form()] = "",
    sort_order: Annotated[int, Form()] = 0,
) -> MediaAssetRead:
    raw = await file.read(MAX_MEDIA_BYTES + 1)
    media = _media_service(request)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await media.upload_image_bytes(
            session,
            principal.tenant_id,
            filename=file.filename or "upload.jpg",
            data=raw,
            product_id=product_id,
            variant_id=variant_id,
            sourcing_draft_id=sourcing_draft_id,
            alt_text=alt_text,
            sort_order=sort_order,
        )


@router.post(
    "/media/ingest-urls",
    response_model=list[MediaAssetRead],
    status_code=status.HTTP_201_CREATED,
)
async def ingest_catalog_media_urls(
    request: Request,
    payload: MediaUrlIngestRequest,
    principal: WriterPrincipal,
) -> list[MediaAssetRead]:
    media = _media_service(request)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await media.ingest_external_urls(
            session,
            principal.tenant_id,
            urls=payload.urls,
            product_id=payload.product_id,
            variant_id=payload.variant_id,
            sourcing_draft_id=payload.sourcing_draft_id,
            alt_text=payload.alt_text,
        )


@router.get("/media", response_model=list[MediaAssetRead])
async def list_catalog_media(
    request: Request,
    principal: ReaderPrincipal,
    product_id: UUID | None = None,
    sourcing_draft_id: UUID | None = None,
) -> list[MediaAssetRead]:
    media = _media_service(request)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await media.list_assets(
            session,
            principal.tenant_id,
            product_id=product_id,
            sourcing_draft_id=sourcing_draft_id,
        )


@router.get("/media/{asset_id}/content", response_class=Response)
async def get_catalog_media_content(
    request: Request,
    asset_id: UUID,
    principal: ReaderPrincipal,
) -> Response:
    media = _media_service(request)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        data, mime_type, filename = await media.read_asset_content(
            session, principal.tenant_id, asset_id
        )
    return Response(
        content=data,
        media_type=mime_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/products/{product_id}/publish", response_model=list[SourcingPublishResult])
async def publish_master_product(
    request: Request,
    product_id: UUID,
    payload: SourcingPublishRequest,
    principal: WriterPrincipal,
) -> list[SourcingPublishResult]:
    publisher = getattr(request.app.state, "marketplace_listing_publisher", None)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _sourcing.publish_master_product(
            session,
            principal.tenant_id,
            str(principal.user_id),
            product_id,
            payload,
            listing_publisher=publisher,
        )
