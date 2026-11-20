from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/catalog", tags=["catalog"])
_service = CatalogService()

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
