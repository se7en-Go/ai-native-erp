from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.shared.enums import UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.query import conflicting_change, page_of
from erp_core.supply_chain import procurement, warehouse
from erp_core.supply_chain.models import Supplier
from erp_core.supply_chain.schemas import (
    CreateStocktake,
    LowStockCreatePoRequest,
    LowStockListRead,
    MovementRead,
    PurchaseOrderCreate,
    PurchaseOrderRead,
    PurchaseOrderUpdate,
    PurchaseStatus,
    ReceivePurchaseOrder,
    StockRead,
    SupplierCreate,
    SupplierRead,
    SupplierStatus,
    SupplierUpdate,
    WarehouseCreateRequest,
    WmsWarehouseRead,
)
from erp_core.tenancy.dependencies import CurrentPrincipal, get_tenant_db, require_roles

supply_router = APIRouter(prefix="/supply-chain", tags=["supply-chain"])
wms_router = APIRouter(prefix="/wms", tags=["wms"])

_MANAGERS = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)
Operator = Annotated[CurrentPrincipal, Depends(require_roles(*_MANAGERS))]
Buyer = Annotated[CurrentPrincipal, Depends(require_roles(*_MANAGERS, UserRole.FINANCE))]
Reader = Annotated[CurrentPrincipal, Depends(require_roles(*UserRole))]
AlertReader = Annotated[
    CurrentPrincipal,
    Depends(
        require_roles(
            UserRole.OWNER,
            UserRole.ADMIN,
            UserRole.MANAGER,
            UserRole.AGENT,
            UserRole.FINANCE,
        )
    ),
]
TenantDB = Annotated[AsyncSession, Depends(get_tenant_db, scope="function")]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


@supply_router.get("/suppliers")
async def list_suppliers(
    principal: Buyer,
    session: TenantDB,
    supplier_status: SupplierStatus | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> PaginatedResponse[SupplierRead]:
    stmt = sa.select(Supplier).where(Supplier.tenant_id == principal.tenant_id)
    if supplier_status is not None:
        stmt = stmt.where(Supplier.status == supplier_status)
    return await page_of(
        session,
        stmt.order_by(Supplier.name, Supplier.id),
        SupplierRead.model_validate,
        limit,
        offset,
    )


@supply_router.get("/suppliers/{supplier_id}")
async def read_supplier(principal: Buyer, session: TenantDB, supplier_id: UUID) -> SupplierRead:
    return SupplierRead.model_validate(
        await procurement.get_supplier(
            session,
            principal.tenant_id,
            supplier_id,
        )
    )


@supply_router.post("/suppliers", status_code=201)
async def create_supplier(
    principal: Operator,
    session: TenantDB,
    payload: SupplierCreate,
) -> SupplierRead:
    supplier = Supplier(tenant_id=principal.tenant_id, **payload.model_dump())
    session.add(supplier)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise conflicting_change("This supplier code is already in use.") from exc
    return SupplierRead.model_validate(supplier)


@supply_router.patch("/suppliers/{supplier_id}")
async def update_supplier(
    principal: Operator,
    session: TenantDB,
    supplier_id: UUID,
    payload: SupplierUpdate,
) -> SupplierRead:
    supplier = await procurement.get_supplier(session, principal.tenant_id, supplier_id, lock=True)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(supplier, field, value)
    supplier.updated_at = datetime.now(UTC)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise conflicting_change("This supplier code is already in use.") from exc
    return SupplierRead.model_validate(supplier)


@supply_router.delete("/suppliers/{supplier_id}", status_code=204)
async def deactivate_supplier(
    principal: Operator, session: TenantDB, supplier_id: UUID
) -> Response:
    """Deactivate without removing the supplier from historical orders."""
    supplier = await procurement.get_supplier(session, principal.tenant_id, supplier_id, lock=True)
    if supplier.status != "inactive":
        supplier.status, supplier.updated_at = "inactive", datetime.now(UTC)
    return Response(status_code=204)


@supply_router.get("/purchase-orders")
async def list_purchase_orders(
    principal: Buyer,
    session: TenantDB,
    order_status: PurchaseStatus | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> PaginatedResponse[PurchaseOrderRead]:
    return await procurement.list_purchase_orders(
        session, principal.tenant_id, order_status, limit, offset
    )


@supply_router.get("/purchase-orders/{po_id}")
async def read_purchase_order(
    principal: Buyer, session: TenantDB, po_id: UUID
) -> PurchaseOrderRead:
    order = await procurement.get_purchase_order(session, principal.tenant_id, po_id)
    items = await procurement.purchase_items(session, principal.tenant_id, [po_id])
    return procurement.order_response(order, items[po_id])


@supply_router.post("/purchase-orders", status_code=201)
async def create_purchase_order(
    principal: Operator,
    session: TenantDB,
    payload: PurchaseOrderCreate,
) -> PurchaseOrderRead:
    return await procurement.create_purchase_order(
        session, principal.tenant_id, principal.user_id, payload
    )


@supply_router.patch("/purchase-orders/{po_id}")
async def update_purchase_order(
    principal: Operator,
    session: TenantDB,
    po_id: UUID,
    payload: PurchaseOrderUpdate,
) -> PurchaseOrderRead:
    return await procurement.update_purchase_order(session, principal.tenant_id, po_id, payload)


@supply_router.delete("/purchase-orders/{po_id}", status_code=204)
async def delete_purchase_order(principal: Operator, session: TenantDB, po_id: UUID) -> Response:
    order = await procurement.get_purchase_order(session, principal.tenant_id, po_id, lock=True)
    if order.status != "draft":
        raise conflicting_change("Only draft purchase orders can be deleted.")
    await session.delete(order)
    return Response(status_code=204)


@supply_router.post("/purchase-orders/{po_id}/receive")
async def receive_purchase_order(
    principal: Operator,
    session: TenantDB,
    po_id: UUID,
    payload: ReceivePurchaseOrder,
) -> PurchaseOrderRead:
    return await procurement.receive_purchase_order(
        session, principal.tenant_id, principal.user_id, po_id, payload
    )


@wms_router.get("/movements")
async def list_movements(
    principal: Reader,
    session: TenantDB,
    limit: Limit = 20,
    offset: Offset = 0,
) -> PaginatedResponse[MovementRead]:
    return await warehouse.list_movements(session, principal.tenant_id, limit, offset)


@wms_router.post("/movements", status_code=201)
async def create_stocktake(
    principal: Operator,
    session: TenantDB,
    payload: CreateStocktake,
) -> MovementRead:
    return await warehouse.record_stocktake(
        session, principal.tenant_id, principal.user_id, payload
    )


@wms_router.get("/stock")
async def list_stock(
    principal: Reader,
    session: TenantDB,
    limit: Limit = 20,
    offset: Offset = 0,
) -> PaginatedResponse[StockRead]:
    return await warehouse.list_stock(session, principal.tenant_id, limit, offset)


@wms_router.get("/low-stock")
async def list_low_stock(principal: AlertReader, session: TenantDB) -> LowStockListRead:
    """Safety-stock breaches per warehouse, with a suggested reorder quantity."""
    return await warehouse.list_low_stock(session, principal.tenant_id)


@wms_router.post("/low-stock/create-po", status_code=201)
async def create_po_from_low_stock(
    principal: Operator,
    session: TenantDB,
    payload: LowStockCreatePoRequest,
) -> PurchaseOrderRead:
    """Convert one low-stock SKU into a draft purchase order in a single click."""
    return await warehouse.create_purchase_order_from_low_stock(
        session, principal.tenant_id, principal.user_id, payload
    )


@wms_router.post("/warehouses", status_code=201)
async def create_warehouse(
    principal: Operator,
    session: TenantDB,
    payload: WarehouseCreateRequest,
) -> WmsWarehouseRead:
    return await warehouse.create_warehouse(session, principal.tenant_id, payload)
