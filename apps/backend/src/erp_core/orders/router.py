from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.orders.order_service import OrderService
from erp_core.orders.schemas import (
    CancelOrderRequest,
    InventoryBalanceItemRead,
    InventoryLedgerRead,
    OrderCreateRequest,
    OrderRead,
    ShipmentRead,
    ShipOrderRequest,
    WarehouseListRead,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus, UserRole
from erp_core.shared.pagination import PaginatedResponse
from erp_core.tenancy.dependencies import CurrentPrincipal, require_roles

router = APIRouter(prefix="/orders", tags=["orders"])
_service = OrderService()

_READER_ROLES = (
    UserRole.OWNER,
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.AGENT,
    UserRole.FINANCE,
)
_WRITER_ROLES = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER, UserRole.AGENT)
_MANAGER_ROLES = (UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER)

ReaderPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_READER_ROLES))]
WriterPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_WRITER_ROLES))]
ManagerPrincipal = Annotated[CurrentPrincipal, Depends(require_roles(*_MANAGER_ROLES))]


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    ) or getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not initialized on app.state")
    return factory


@router.get("", response_model=PaginatedResponse[OrderRead])
async def list_orders(
    request: Request,
    principal: ReaderPrincipal,
    order_mode: OrderMode | None = None,
    fulfillment_status: FulfillmentStatus | None = None,
    payment_status: PaymentStatus | None = None,
    customer_id: UUID | None = None,
    search: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[OrderRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_orders(
            session,
            principal.tenant_id,
            order_mode=order_mode,
            fulfillment_status=fulfillment_status,
            payment_status=payment_status,
            customer_id=customer_id,
            search=search,
            limit=limit,
            offset=offset,
        )


@router.get("/inventory/balances", response_model=PaginatedResponse[InventoryBalanceItemRead])
async def list_inventory_balances(
    request: Request,
    principal: ReaderPrincipal,
    variant_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[InventoryBalanceItemRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.inventory.list_balances(
            session, principal.tenant_id, variant_id=variant_id, limit=limit, offset=offset
        )


@router.get("/inventory/ledger", response_model=PaginatedResponse[InventoryLedgerRead])
async def list_inventory_ledger(
    request: Request,
    principal: ReaderPrincipal,
    variant_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[InventoryLedgerRead]:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.inventory.list_ledger(
            session, principal.tenant_id, variant_id=variant_id, limit=limit, offset=offset
        )


@router.get("/warehouses", response_model=WarehouseListRead)
async def list_warehouses(request: Request, principal: ReaderPrincipal) -> WarehouseListRead:
    """List warehouse IDs visible to this tenant without creating a fallback default."""
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.list_warehouses(session, principal.tenant_id)


@router.get("/{order_id}", response_model=OrderRead)
async def get_order(
    request: Request,
    principal: ReaderPrincipal,
    order_id: UUID,
) -> OrderRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.get_order(session, principal.tenant_id, order_id)


@router.post("", response_model=OrderRead, status_code=status.HTTP_201_CREATED)
async def create_order(
    request: Request,
    principal: ManagerPrincipal,
    payload: OrderCreateRequest,
) -> OrderRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.create_manual_order(session, principal.tenant_id, principal, payload)


@router.post("/{order_id}/cancel", response_model=OrderRead)
async def cancel_order(
    request: Request,
    principal: ManagerPrincipal,
    order_id: UUID,
    payload: CancelOrderRequest,
) -> OrderRead:
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.cancel_order(
            session,
            principal.tenant_id,
            actor_id=str(principal.user_id),
            order_id=order_id,
            warehouse_id=payload.warehouse_id,
            reason=payload.reason,
        )


@router.post("/{order_id}/ship", response_model=ShipmentRead, status_code=status.HTTP_201_CREATED)
async def ship_order(
    request: Request,
    principal: ReaderPrincipal,
    order_id: UUID,
    payload: ShipOrderRequest,
) -> ShipmentRead:
    tracking_adapter = getattr(request.app.state, "tracking_adapter", None)
    async with tenant_session(_get_session_factory(request), principal.tenant_id) as session:
        return await _service.ship_order(
            session,
            principal.tenant_id,
            principal=principal,
            order_id=order_id,
            payload=payload,
            tracking_adapter=tracking_adapter,
        )
