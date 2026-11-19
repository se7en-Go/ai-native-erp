from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import TypeAdapter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.models import ProductVariant
from erp_core.orders.inventory_service import InventoryLedgerService
from erp_core.shared.pagination import PaginatedResponse
from erp_core.shared.query import conflicting_change, record_not_found
from erp_core.supply_chain.models import PurchaseOrder, PurchaseOrderItem, Supplier, WmsMovement
from erp_core.supply_chain.schemas import (
    PurchaseOrderCreate,
    PurchaseOrderItemInput,
    PurchaseOrderItemRead,
    PurchaseOrderRead,
    PurchaseOrderUpdate,
    PurchaseStatus,
    ReceivePurchaseOrder,
)

PurchaseStatusAdapter: TypeAdapter[PurchaseStatus] = TypeAdapter(PurchaseStatus)


async def get_supplier(
    session: AsyncSession,
    tenant_id: UUID,
    supplier_id: UUID,
    *,
    lock: bool = False,
) -> Supplier:
    stmt = sa.select(Supplier).where(Supplier.tenant_id == tenant_id, Supplier.id == supplier_id)
    row = await session.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise record_not_found("Supplier")
    return row


async def get_purchase_order(
    session: AsyncSession,
    tenant_id: UUID,
    po_id: UUID,
    *,
    lock: bool = False,
) -> PurchaseOrder:
    stmt = sa.select(PurchaseOrder).where(
        PurchaseOrder.tenant_id == tenant_id,
        PurchaseOrder.id == po_id,
    )
    order = await session.scalar(stmt.with_for_update() if lock else stmt)
    if order is None:
        raise record_not_found("Purchase order")
    return order


async def purchase_items(
    session: AsyncSession,
    tenant_id: UUID,
    po_ids: Sequence[UUID],
) -> dict[UUID, list[PurchaseOrderItem]]:
    result: dict[UUID, list[PurchaseOrderItem]] = defaultdict(list)
    if po_ids:
        rows = await session.scalars(
            sa.select(PurchaseOrderItem)
            .where(
                PurchaseOrderItem.tenant_id == tenant_id,
                PurchaseOrderItem.purchase_order_id.in_(po_ids),
            )
            .order_by(PurchaseOrderItem.variant_id)
        )
        for row in rows:
            result[row.purchase_order_id].append(row)
    return result


def order_response(order: PurchaseOrder, items: Sequence[PurchaseOrderItem]) -> PurchaseOrderRead:
    return PurchaseOrderRead(
        id=order.id,
        po_no=order.po_no,
        supplier_id=order.supplier_id,
        warehouse_id=order.warehouse_id,
        currency=order.currency,
        total_amount=order.total_amount,
        status=PurchaseStatusAdapter.validate_python(order.status),
        expected_arrival_date=order.expected_arrival_date,
        notes=order.notes,
        created_by=order.created_by,
        created_at=order.created_at,
        updated_at=order.updated_at,
        items=[PurchaseOrderItemRead.model_validate(item) for item in items],
    )


async def list_purchase_orders(
    session: AsyncSession,
    tenant_id: UUID,
    order_status: PurchaseStatus | None,
    limit: int,
    offset: int,
) -> PaginatedResponse[PurchaseOrderRead]:
    stmt = sa.select(PurchaseOrder).where(PurchaseOrder.tenant_id == tenant_id)
    if order_status is not None:
        stmt = stmt.where(PurchaseOrder.status == order_status)
    count = await session.scalar(sa.select(sa.func.count()).select_from(stmt.subquery()))
    rows = (
        await session.scalars(
            stmt.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = await purchase_items(session, tenant_id, [r.id for r in rows])
    return PaginatedResponse.build(
        items=[order_response(r, items[r.id]) for r in rows],
        total=count or 0,
        limit=limit,
        offset=offset,
    )


async def _active_supplier(session: AsyncSession, tenant_id: UUID, supplier_id: UUID) -> None:
    if (await get_supplier(session, tenant_id, supplier_id)).status != "active":
        raise conflicting_change("Inactive suppliers cannot be assigned to a purchase order.")


async def _new_items(
    session: AsyncSession,
    order: PurchaseOrder,
    incoming: list[PurchaseOrderItemInput],
) -> list[PurchaseOrderItem]:
    variant_ids = {i.variant_id for i in incoming}
    if len(variant_ids) != len(incoming):
        raise conflicting_change("A purchase order may have only one line per variant.")
    variants = await session.scalars(
        sa.select(ProductVariant).where(
            ProductVariant.tenant_id == order.tenant_id,
            ProductVariant.id.in_(variant_ids),
        )
    )
    skus = {v.id: v.sku for v in variants}
    if skus.keys() != variant_ids:
        raise record_not_found("SKU")
    total = sum((i.qty_ordered * i.unit_cost for i in incoming), Decimal(0)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    if total > Decimal("999999999999.99"):
        raise conflicting_change("Purchase-order total exceeds the supported currency amount.")
    order.total_amount = total
    return [
        PurchaseOrderItem(
            tenant_id=order.tenant_id,
            purchase_order_id=order.id,
            variant_id=i.variant_id,
            sku=skus[i.variant_id],
            qty_ordered=i.qty_ordered,
            qty_received=0,
            unit_cost=i.unit_cost,
        )
        for i in incoming
    ]


async def create_purchase_order(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    payload: PurchaseOrderCreate,
) -> PurchaseOrderRead:
    await _active_supplier(session, tenant_id, payload.supplier_id)
    order = PurchaseOrder(
        id=uuid4(),
        tenant_id=tenant_id,
        created_by=user_id,
        po_no=payload.po_no or f"PO-{uuid4().hex.upper()}",
        **payload.model_dump(exclude={"po_no", "items"}),
    )
    items = await _new_items(session, order, payload.items)
    session.add(order)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise conflicting_change("This purchase-order number is already in use.") from exc
    session.add_all(items)
    await session.flush()
    return order_response(order, items)


async def update_purchase_order(
    session: AsyncSession,
    tenant_id: UUID,
    po_id: UUID,
    payload: PurchaseOrderUpdate,
) -> PurchaseOrderRead:
    order = await get_purchase_order(session, tenant_id, po_id, lock=True)
    if order.status not in ("draft", "ordered"):
        raise conflicting_change("Only unreceived, non-cancelled purchase orders can be edited.")
    if payload.supplier_id is not None:
        await _active_supplier(session, tenant_id, payload.supplier_id)
    items = (await purchase_items(session, tenant_id, [po_id]))[po_id]
    if payload.items is not None:
        new_items = await _new_items(session, order, payload.items)
        for item in items:
            await session.delete(item)
        await session.flush()
        session.add_all(new_items)
        items = new_items
    for field, value in payload.model_dump(exclude_unset=True, exclude={"items"}).items():
        setattr(order, field, value)
    order.updated_at = datetime.now(UTC)
    await session.flush()
    return order_response(order, items)


async def receive_purchase_order(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    po_id: UUID,
    payload: ReceivePurchaseOrder,
) -> PurchaseOrderRead:
    # One lock per PO serializes against both another receipt and a concurrent edit/delete.
    # Sort by variant across POs to match the InventoryLedgerService lock order.
    order = await get_purchase_order(session, tenant_id, po_id, lock=True)
    items = (await purchase_items(session, tenant_id, [po_id]))[po_id]
    requested = {line.item_id: line.qty for line in payload.items}
    if len(requested) != len(payload.items):
        raise conflicting_change("Duplicate purchase-order lines in receipt.")
    prefix = f"RCV-{po_id.hex}-{payload.receipt_id.hex}-"
    received = await session.scalars(
        sa.select(WmsMovement).where(
            WmsMovement.tenant_id == tenant_id,
            WmsMovement.reference_id == po_id,
            WmsMovement.movement_no.like(f"{prefix}%"),
        )
    )
    previous = {row.variant_id: row.qty_delta for row in received}
    if previous:
        if requested and (
            {item.id: previous[item.variant_id] for item in items if item.variant_id in previous}
            != requested
        ):
            raise conflicting_change("This receipt ID was already used with different quantities.")
        return order_response(order, items)
    if order.status in ("received", "cancelled"):
        raise conflicting_change("This purchase order is closed to receiving.")
    if not requested:
        requested = {
            i.id: i.qty_ordered - i.qty_received for i in items if i.qty_ordered > i.qty_received
        }
    if not requested.keys() <= {item.id for item in items}:
        raise record_not_found("Purchase-order line")
    for item in items:
        if requested.get(item.id, 0) > item.qty_ordered - item.qty_received:
            raise conflicting_change(f"Receipt exceeds outstanding quantity for {item.sku}.")
    ledger = InventoryLedgerService()
    for item in items:
        qty = requested.get(item.id, 0)
        if not qty:
            continue
        key = f"{prefix}{item.variant_id.hex}"
        posted = await ledger.inbound_stock(
            session,
            tenant_id,
            order.warehouse_id,
            item.variant_id,
            qty,
            po_id,
            key,
        )
        if posted is None:
            raise conflicting_change("This receipt already exists in the inventory ledger.")
        item.qty_received += qty
        session.add(
            WmsMovement(
                tenant_id=tenant_id,
                movement_no=key,
                warehouse_id=order.warehouse_id,
                variant_id=item.variant_id,
                movement_type="inbound_po",
                qty_delta=qty,
                reference_type="purchase_order",
                reference_id=po_id,
                operator_user_id=user_id,
            )
        )
    order.status = (
        "received" if all(i.qty_received == i.qty_ordered for i in items) else "partially_received"
    )
    order.updated_at = datetime.now(UTC)
    await session.flush()
    return order_response(order, items)
