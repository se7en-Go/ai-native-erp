from __future__ import annotations

from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.models import ProductVariant
from erp_core.orders.inventory_service import InventoryLedgerService
from erp_core.orders.order_service import OrderService
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.query import conflicting_change, record_not_found
from erp_core.supply_chain.models import WmsMovement
from erp_core.supply_chain.schemas import CreateStocktake, MovementRead, StockRead


async def record_stocktake(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    payload: CreateStocktake,
) -> MovementRead:
    variant = await session.scalar(
        sa.select(ProductVariant.id).where(
            ProductVariant.tenant_id == tenant_id,
            ProductVariant.id == payload.variant_id,
        )
    )
    if variant is None:
        raise record_not_found("SKU")
    warehouses = await OrderService().list_warehouses(session, tenant_id)
    if not any(w.id == payload.warehouse_id for w in warehouses.items):
        raise record_not_found("Warehouse")
    number = f"ADJ-{payload.request_id.hex}"
    movement = await session.scalar(
        insert(WmsMovement)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            movement_no=number,
            warehouse_id=payload.warehouse_id,
            variant_id=payload.variant_id,
            movement_type="stocktake_adjust",
            qty_delta=payload.qty_delta,
            reference_type="stocktake",
            reference_id=payload.request_id,
            operator_user_id=user_id,
            notes=payload.notes,
        )
        .on_conflict_do_nothing(index_elements=[WmsMovement.tenant_id, WmsMovement.movement_no])
        .returning(WmsMovement)
    )
    if movement is None:
        existing = await session.scalar(
            sa.select(WmsMovement).where(
                WmsMovement.tenant_id == tenant_id,
                WmsMovement.movement_no == number,
            )
        )
        if existing is None or (
            existing.warehouse_id,
            existing.variant_id,
            existing.qty_delta,
            existing.notes,
        ) != (payload.warehouse_id, payload.variant_id, payload.qty_delta, payload.notes):
            raise conflicting_change("This stocktake ID was already used with different details.")
        return MovementRead.model_validate(existing)
    posted = await InventoryLedgerService().adjust_stocktake(
        session,
        tenant_id,
        payload.warehouse_id,
        payload.variant_id,
        payload.qty_delta,
        movement.id,
        number,
    )
    if posted is None:
        raise conflicting_change("This stocktake already exists in the inventory ledger.")
    return MovementRead.model_validate(movement)


async def list_movements(
    session: AsyncSession,
    tenant_id: UUID,
    limit: int,
    offset: int,
) -> PaginatedResponse[MovementRead]:
    # Shipments already append to the immutable order ledger. Project the outbound rows here
    # rather than duplicating stock logic or creating another order-to-supply module dependency.
    union = """
        SELECT id, movement_no, warehouse_id, variant_id, movement_type, qty_delta,
               reference_type, reference_id, operator_user_id, notes, created_at
        FROM wms_movements WHERE tenant_id = :tid
        UNION ALL
        SELECT CAST(md5(concat('wms:shipment:', tenant_id, ':', id)) AS uuid),
               concat('OUT-', id), warehouse_id, variant_id, 'outbound_order', qty_delta_on_hand,
               reference_type, reference_id, NULL, NULL, created_at
        FROM inventory_ledger WHERE tenant_id = :tid AND event_type = 'deduct_shipped'
    """
    params = {"tid": tenant_id, "limit": limit, "offset": offset}
    total = await session.scalar(sa.text(f"SELECT count(*) FROM ({union}) AS movements"), params)
    rows = (
        await session.execute(
            sa.text(f"""
        SELECT * FROM ({union}) AS movements ORDER BY created_at DESC, id
        LIMIT :limit OFFSET :offset
    """),
            params,
        )
    ).mappings()
    return PaginatedResponse.build(
        items=[MovementRead.model_validate(row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


async def list_stock(
    session: AsyncSession,
    tenant_id: UUID,
    limit: int,
    offset: int,
) -> PaginatedResponse[StockRead]:
    params = {"tid": tenant_id, "limit": limit, "offset": offset}
    total = await session.scalar(
        sa.text("""
        SELECT count(*) FROM vw_tenant_inventory_risk_summary WHERE tenant_id = :tid
    """),
        params,
    )
    rows = (
        await session.execute(
            sa.text("""
        SELECT warehouse_id, variant_id, sku, on_hand_qty, reserved_qty, available_qty
        FROM vw_tenant_inventory_risk_summary WHERE tenant_id = :tid
        ORDER BY sku, warehouse_id, variant_id LIMIT :limit OFFSET :offset
    """),
            params,
        )
    ).mappings()
    return PaginatedResponse.build(
        items=[StockRead.model_validate(row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )
