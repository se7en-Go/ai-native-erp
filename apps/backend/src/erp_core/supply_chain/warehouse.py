from __future__ import annotations

from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.models import ProductVariant
from erp_core.orders.inventory_service import InventoryLedgerService
from erp_core.orders.order_service import OrderService
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.query import conflicting_change, record_not_found
from erp_core.supply_chain import procurement
from erp_core.supply_chain.models import Warehouse, WmsMovement
from erp_core.supply_chain.schemas import (
    CreateStocktake,
    LowStockCreatePoRequest,
    LowStockItemRead,
    LowStockListRead,
    MovementRead,
    PurchaseOrderCreate,
    PurchaseOrderItemInput,
    PurchaseOrderRead,
    StockRead,
    WarehouseCreateRequest,
    WmsWarehouseRead,
)

_SAFETY_STOCK_EXPR = "COALESCE((pv.attributes->>'safety_stock_qty')::int, 50)"


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


async def list_low_stock(
    session: AsyncSession,
    tenant_id: UUID,
) -> LowStockListRead:
    """Warehouse-level safety-stock breaches with a restock suggestion per row."""
    rows = (
        await session.execute(
            sa.text(f"""
        SELECT ib.variant_id,
               pv.sku AS sku,
               COALESCE(
                   p.i18n_specs->'en'->>'title',
                   p.i18n_specs->'zh'->>'title',
                   p.spu_code
               ) AS product_name,
               ib.warehouse_id,
               COALESCE(w.name, 'Warehouse ' || left(ib.warehouse_id::text, 8)) AS warehouse_name,
               ib.on_hand_qty,
               ib.reserved_qty,
               (ib.on_hand_qty - ib.reserved_qty) AS available_qty,
               {_SAFETY_STOCK_EXPR} AS safety_stock_qty,
               sd.source_url AS sourcing_url
        FROM inventory_balances ib
        JOIN product_variants pv
            ON pv.tenant_id = ib.tenant_id AND pv.id = ib.variant_id
        JOIN products p
            ON p.tenant_id = pv.tenant_id AND p.id = pv.product_id
        LEFT JOIN warehouses w
            ON w.tenant_id = ib.tenant_id AND w.id = ib.warehouse_id
        LEFT JOIN LATERAL (
            SELECT source_url
            FROM sourcing_drafts
            WHERE tenant_id = ib.tenant_id
              AND imported_product_id = p.id
              AND source_url IS NOT NULL
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
        ) sd ON true
        WHERE ib.tenant_id = :tid
          AND (ib.on_hand_qty - ib.reserved_qty) < {_SAFETY_STOCK_EXPR}
        ORDER BY available_qty ASC, pv.sku, ib.warehouse_id
    """),
            {"tid": tenant_id},
        )
    ).mappings().all()

    items: list[LowStockItemRead] = []
    for row in rows:
        safety_stock = int(row["safety_stock_qty"])
        available = int(row["available_qty"])
        items.append(
            LowStockItemRead(
                variant_id=row["variant_id"],
                sku=row["sku"],
                product_name=row["product_name"],
                warehouse_id=row["warehouse_id"],
                warehouse_name=row["warehouse_name"],
                on_hand_qty=int(row["on_hand_qty"]),
                reserved_qty=int(row["reserved_qty"]),
                available_qty=available,
                safety_stock_qty=safety_stock,
                suggested_reorder_qty=max(safety_stock * 2 - available, safety_stock),
                sourcing_url=row["sourcing_url"],
            )
        )
    return LowStockListRead(items=items, total=len(items))


async def _replenishment_warehouse(
    session: AsyncSession,
    tenant_id: UUID,
    variant_id: UUID,
) -> UUID:
    """Pick the warehouse that actually needs the stock, else the tenant's first warehouse."""
    balance_id = await session.scalar(
        sa.text(
            """
            SELECT warehouse_id FROM inventory_balances
            WHERE tenant_id = :tid AND variant_id = :vid
            ORDER BY (on_hand_qty - reserved_qty) ASC, warehouse_id
            LIMIT 1
            """
        ),
        {"tid": tenant_id, "vid": variant_id},
    )
    if balance_id is not None:
        return UUID(str(balance_id))
    known = (
        await session.scalars(
            sa.select(Warehouse.id)
            .where(Warehouse.tenant_id == tenant_id)
            .order_by(Warehouse.created_at, Warehouse.id)
            .limit(1)
        )
    ).first()
    if known is None:
        raise conflicting_change(
            "Create a warehouse before converting low stock into a purchase order."
        )
    return UUID(str(known))


async def _linked_sourcing_url(
    session: AsyncSession, tenant_id: UUID, variant_id: UUID
) -> str | None:
    url = await session.scalar(
        sa.text(
            """
            SELECT sd.source_url
            FROM product_variants pv
            JOIN sourcing_drafts sd
              ON sd.tenant_id = pv.tenant_id
             AND sd.imported_product_id = pv.product_id
            WHERE pv.tenant_id = :tid
              AND pv.id = :vid
              AND sd.source_url IS NOT NULL
            ORDER BY sd.updated_at DESC, sd.created_at DESC
            LIMIT 1
            """
        ),
        {"tid": tenant_id, "vid": variant_id},
    )
    return str(url).strip() if url else None


async def create_purchase_order_from_low_stock(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    payload: LowStockCreatePoRequest,
) -> PurchaseOrderRead:
    """One-click low-stock restock: a single-line draft purchase order with 1688 link."""
    warehouse_id = await _replenishment_warehouse(session, tenant_id, payload.variant_id)
    sourcing_url = await _linked_sourcing_url(session, tenant_id, payload.variant_id)
    notes = (payload.notes or "").strip() or None
    if sourcing_url and (not notes or sourcing_url not in notes):
        suffix = f"1688 Sourcing URL: {sourcing_url}"
        notes = f"{notes}\n{suffix}"[:5000] if notes else suffix[:5000]
    request = PurchaseOrderCreate(
        supplier_id=payload.supplier_id,
        warehouse_id=warehouse_id,
        currency=payload.currency,
        status="draft",
        notes=notes,
        items=[
            PurchaseOrderItemInput(
                variant_id=payload.variant_id,
                qty_ordered=payload.quantity,
                unit_cost=payload.unit_cost,
            )
        ],
    )
    return await procurement.create_purchase_order(session, tenant_id, user_id, request)


async def create_warehouse(
    session: AsyncSession,
    tenant_id: UUID,
    payload: WarehouseCreateRequest,
) -> WmsWarehouseRead:
    warehouse = Warehouse(
        tenant_id=tenant_id,
        code=payload.code,
        name=payload.name,
        country_code=payload.country_code,
        city=payload.city,
        is_active=True,
    )
    session.add(warehouse)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise conflicting_change("This warehouse code is already in use.") from exc
    return WmsWarehouseRead.model_validate(warehouse)
