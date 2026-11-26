"""Track 7 supply-chain service: 1-level light BOM, in-transit headhaul transfers,
and days-of-cover (DOC) weighted replenishment.

Design notes that matter for correctness:

* **1-level BOM only.** A component may never be a kit itself, and a kit may
  never be somebody else's component. Both directions are rejected with HTTP 422
  so a two-hop assembly tree (and therefore a cycle) cannot be created through
  the API; `bom_components.parent_variant_id <> component_variant_id` backs the
  simple self-reference case in PostgreSQL.
* **Stock always moves through `InventoryLedgerService.adjust_stocktake`.** The
  ledger row is immutable, the balance update is `WHERE on_hand - reserved >= …`
  guarded, and the channel inventory-sync outbox is enqueued by the same call, so
  a BOM assembly or a transfer dispatch behaves exactly like a stocktake instead
  of writing a second, parallel stock path.
* **Deterministic locks.** Every multi-SKU write locks `inventory_balances` rows
  in ascending `variant_id` order before it looks at availability, which is the
  same deadlock-free order `InventoryLedgerService._normalize_sorted_items` uses.
* **Money stays `Decimal`.** Rolled-up BOM cost and DOC figures use
  `ROUND_HALF_UP` and are serialised as strings.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.catalog.models import Product, ProductVariant
from erp_core.orders.inventory_service import InsufficientStockError, InventoryLedgerService
from erp_core.shared.errors import AppError
from erp_core.shared.query import conflicting_change, record_not_found
from erp_core.supply_chain.bom_doc_models import (
    BomComponent,
    StockTransfer,
    StockTransferLine,
)
from erp_core.supply_chain.bom_doc_schemas import (
    BomAssembleRead,
    BomAssemblyConsumptionRead,
    BomComponentRead,
    BomComponentUpsert,
    BomCostRollupLineRead,
    BomCostRollupRead,
    BomKitRead,
    BomListRead,
    DocReplenishmentRead,
    DocReplenishmentRowRead,
    RiskLevel,
    ShippingMethod,
    StockTransferCreate,
    StockTransferLineRead,
    StockTransferListRead,
    StockTransferRead,
    TransferStatus,
)
from erp_core.supply_chain.models import Warehouse, WmsMovement

QTY_STEP = Decimal("0.0001")
MONEY_STEP = Decimal("0.0001")
DOC_STEP = Decimal("0.01")

#: Sales-weighting weights for the 7 / 30 / 90 day blended daily run rate.
SALES_WEIGHTS: tuple[tuple[int, Decimal], ...] = (
    (7, Decimal("0.50")),
    (30, Decimal("0.30")),
    (90, Decimal("0.20")),
)

#: `orders.fulfillment_status` values that never count as demand.
NON_DEMAND_FULFILLMENT_STATUSES = ("cancelled", "refunded")
_NON_DEMAND_SQL = ", ".join(f"'{status}'" for status in NON_DEMAND_FULFILLMENT_STATUSES)

#: A warehouse is "overseas" when it is not in the factory's home country, or when
#: its code/name says so explicitly (FBA prep centres, 3PL nodes, 海外仓).
HOME_COUNTRY_CODES = frozenset({"CN"})
OVERSEAS_MARKERS = ("overseas", "fba", "3pl", "海外", "美国", "欧洲", "日本")

_BOM_ASSEMBLE_REFERENCE = "bom_assemble"
_TRANSFER_REFERENCE = "stock_transfer"


# ---------------------------------------------------------------------------
# Pure helpers (unit tested without a database)
# ---------------------------------------------------------------------------


def effective_qty_per_unit(qty_per_unit: Decimal, waste_rate_pct: Decimal) -> Decimal:
    """`qty_per_unit * (1 + waste_rate_pct / 100)`, quantized to 4 dp HALF_UP."""
    gross = qty_per_unit * (Decimal(1) + Decimal(waste_rate_pct) / Decimal(100))
    return gross.quantize(QTY_STEP, rounding=ROUND_HALF_UP)


def ceil_units(value: Decimal) -> int:
    """Round a fractional unit requirement up to whole units (never silent shorts)."""
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def weighted_daily_sales(
    units_7d: int,
    units_30d: int,
    units_90d: int,
) -> Decimal:
    """Blended daily run rate: 50% (7d) + 30% (30d) + 20% (90d), 4 dp HALF_UP."""
    total = Decimal(0)
    for window_days, weight in SALES_WEIGHTS:
        units = {7: units_7d, 30: units_30d, 90: units_90d}[window_days]
        total += Decimal(units) / Decimal(window_days) * weight
    return total.quantize(QTY_STEP, rounding=ROUND_HALF_UP)


def doc_days(total_available_qty: int, daily_sales: Decimal) -> Decimal | None:
    """Days of cover, or `None` when there is no demand to cover."""
    if daily_sales <= 0:
        return None
    return (Decimal(total_available_qty) / daily_sales).quantize(DOC_STEP, rounding=ROUND_HALF_UP)


def suggested_reorder_qty(
    total_available_qty: int,
    daily_sales: Decimal,
    target_doc_days: int,
) -> int:
    """`max(0, ceil(daily_sales * target_doc_days) - total_available)`."""
    target_units = ceil_units((daily_sales * Decimal(target_doc_days)).quantize(QTY_STEP))
    return max(0, target_units - total_available_qty)


def risk_level(doc: Decimal | None) -> RiskLevel:
    """Critical below 7 days of cover, warning below 15, otherwise healthy."""
    if doc is None:
        return "healthy"
    if doc < Decimal(7):
        return "critical"
    if doc < Decimal(15):
        return "warning"
    return "healthy"


def is_overseas_warehouse(country_code: str | None, code: str | None, name: str | None) -> bool:
    """Classify a warehouse as overseas (FBA/3PL/海外仓) versus local factory stock."""
    haystack = " ".join(part for part in (code, name) if part).lower()
    if any(marker in haystack for marker in OVERSEAS_MARKERS):
        return True
    if not country_code:
        return False
    return country_code.strip().upper() not in HOME_COUNTRY_CODES


def _unprocessable(code: str, detail: str, slug: str) -> AppError:
    return AppError(
        slug=slug,
        status_code=422,
        code=code,
        title="Invalid Bill of Materials",
        detail=detail,
    )


def _dec_text(value: Decimal, step: Decimal = MONEY_STEP) -> str:
    return format(Decimal(value).quantize(step, rounding=ROUND_HALF_UP), "f")


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------


VariantRow = sa.Row[UUID, str, Decimal, str]
BomRow = sa.Row[BomComponent, str, str, Decimal]
TransferRow = sa.Row[StockTransfer, str | None, str | None]


async def _variant_rows(
    session: AsyncSession,
    tenant_id: UUID,
    variant_ids: list[UUID],
) -> dict[UUID, VariantRow]:
    if not variant_ids:
        return {}
    stmt = sa.select(
        ProductVariant.id,
        ProductVariant.sku,
        ProductVariant.cost_price_base,
        ProductVariant.base_currency,
    ).where(
        ProductVariant.tenant_id == tenant_id,
        ProductVariant.id.in_(variant_ids),
    )
    rows: list[VariantRow] = list((await session.execute(stmt)).all())
    return {row[0]: row for row in rows}


async def _component_rows(
    session: AsyncSession,
    tenant_id: UUID,
    parent_variant_id: UUID | None = None,
) -> list[BomRow]:
    parent = sa.orm.aliased(ProductVariant)
    component = sa.orm.aliased(ProductVariant)
    stmt = (
        sa.select(
            BomComponent,
            parent.sku.label("parent_sku"),
            component.sku.label("component_sku"),
            component.cost_price_base.label("component_unit_cost_base"),
        )
        .select_from(BomComponent)
        .join(
            parent,
            sa.and_(
                parent.tenant_id == BomComponent.tenant_id,
                parent.id == BomComponent.parent_variant_id,
            ),
        )
        .join(
            component,
            sa.and_(
                component.tenant_id == BomComponent.tenant_id,
                component.id == BomComponent.component_variant_id,
            ),
        )
        .where(BomComponent.tenant_id == tenant_id)
        .order_by(parent.sku, component.sku)
    )
    if parent_variant_id is not None:
        stmt = stmt.where(BomComponent.parent_variant_id == parent_variant_id)
    return list((await session.execute(stmt)).all())


def _component_read(row: BomRow) -> BomComponentRead:
    component = row[0]
    effective = effective_qty_per_unit(component.qty_per_unit, component.waste_rate_pct)
    unit_cost = Decimal(row[3])
    return BomComponentRead(
        id=component.id,
        parent_variant_id=component.parent_variant_id,
        parent_sku=row[1],
        component_variant_id=component.component_variant_id,
        component_sku=row[2],
        qty_per_unit=_dec_text(component.qty_per_unit),
        waste_rate_pct=_dec_text(component.waste_rate_pct, Decimal("0.01")),
        effective_qty_per_unit=_dec_text(effective),
        component_unit_cost_base=_dec_text(unit_cost),
        line_unit_cost_base=_dec_text(effective * unit_cost),
        notes=component.notes,
        created_at=component.created_at,
        updated_at=component.updated_at,
    )


def _rolled_up_unit_cost(components: list[BomComponentRead]) -> Decimal:
    total = sum(
        (Decimal(item.line_unit_cost_base) for item in components),
        start=Decimal(0),
    )
    return total.quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


async def list_bom_kits(
    session: AsyncSession,
    tenant_id: UUID,
    parent_variant_id: UUID | None = None,
) -> BomListRead:
    rows = await _component_rows(session, tenant_id, parent_variant_id)
    grouped: dict[UUID, list[BomComponentRead]] = defaultdict(list)
    parent_skus: dict[UUID, str] = {}
    for row in rows:
        read = _component_read(row)
        grouped[read.parent_variant_id].append(read)
        parent_skus[read.parent_variant_id] = read.parent_sku

    kits = [
        BomKitRead(
            parent_variant_id=parent_id,
            parent_sku=parent_skus[parent_id],
            components=components,
            component_count=len(components),
            rolled_up_unit_cost=_dec_text(_rolled_up_unit_cost(components)),
        )
        for parent_id, components in sorted(grouped.items(), key=lambda item: parent_skus[item[0]])
    ]
    return BomListRead(items=kits, total=len(kits))


async def upsert_component(
    session: AsyncSession,
    tenant_id: UUID,
    payload: BomComponentUpsert,
) -> BomComponentRead:
    """Add or update a component, refusing anything that would nest or cycle."""
    if payload.parent_variant_id == payload.component_variant_id:
        raise _unprocessable(
            "ERR_BOM_SELF_REFERENCE",
            "A kit cannot be its own component.",
            "bom-self-reference",
        )
    variants = await _variant_rows(
        session,
        tenant_id,
        sorted({payload.parent_variant_id, payload.component_variant_id}),
    )
    if payload.parent_variant_id not in variants:
        raise record_not_found("Parent SKU")
    if payload.component_variant_id not in variants:
        raise record_not_found("Component SKU")

    # Strictly one level: the component may not already be a kit, and the kit may
    # not already be somebody else's component.
    nested = await session.scalar(
        sa.select(sa.func.count())
        .select_from(BomComponent)
        .where(
            BomComponent.tenant_id == tenant_id,
            sa.or_(
                BomComponent.parent_variant_id == payload.component_variant_id,
                BomComponent.component_variant_id == payload.parent_variant_id,
            ),
        )
    )
    if nested:
        raise _unprocessable(
            "ERR_BOM_MULTI_LEVEL",
            (
                "This light BOM is strictly one level deep: the component is already a kit, "
                "or the kit is already a component of another kit."
            ),
            "bom-multi-level",
        )

    await session.scalar(
        insert(BomComponent)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            parent_variant_id=payload.parent_variant_id,
            component_variant_id=payload.component_variant_id,
            qty_per_unit=payload.qty_per_unit,
            waste_rate_pct=payload.waste_rate_pct,
            notes=payload.notes,
        )
        .on_conflict_do_update(
            index_elements=[
                BomComponent.tenant_id,
                BomComponent.parent_variant_id,
                BomComponent.component_variant_id,
            ],
            set_={
                "qty_per_unit": payload.qty_per_unit,
                "waste_rate_pct": payload.waste_rate_pct,
                "notes": payload.notes,
                "updated_at": sa.func.now(),
            },
        )
        .returning(BomComponent.id)
    )
    rows = await _component_rows(session, tenant_id, payload.parent_variant_id)
    for row in rows:
        if row[0].component_variant_id == payload.component_variant_id:
            return _component_read(row)
    raise record_not_found("BOM component")


async def delete_component(session: AsyncSession, tenant_id: UUID, component_id: UUID) -> None:
    component = await session.scalar(
        sa.select(BomComponent).where(
            BomComponent.tenant_id == tenant_id,
            BomComponent.id == component_id,
        )
    )
    if component is None:
        raise record_not_found("BOM component")
    await session.delete(component)


async def bom_cost_rollup(
    session: AsyncSession,
    tenant_id: UUID,
    parent_variant_id: UUID,
) -> BomCostRollupRead:
    variants = await _variant_rows(session, tenant_id, [parent_variant_id])
    parent = variants.get(parent_variant_id)
    if parent is None:
        raise record_not_found("Parent SKU")
    rows = await _component_rows(session, tenant_id, parent_variant_id)
    components = [_component_read(row) for row in rows]
    unit_cost = _rolled_up_unit_cost(components)
    return BomCostRollupRead(
        parent_variant_id=parent_variant_id,
        parent_sku=str(parent[1]),
        base_currency=str(parent[3]),
        component_count=len(components),
        unit_cost_base=_dec_text(unit_cost),
        lines=[
            BomCostRollupLineRead(
                component_id=item.id,
                component_variant_id=item.component_variant_id,
                component_sku=item.component_sku,
                qty_per_unit=item.qty_per_unit,
                waste_rate_pct=item.waste_rate_pct,
                effective_qty_per_unit=item.effective_qty_per_unit,
                component_unit_cost_base=item.component_unit_cost_base,
                line_unit_cost_base=item.line_unit_cost_base,
            )
            for item in components
        ],
    )


async def _lock_balances(
    session: AsyncSession,
    tenant_id: UUID,
    warehouse_id: UUID,
    variant_ids: list[UUID],
) -> dict[UUID, int]:
    """Lock the balance rows in ascending `variant_id` order and return availability."""
    params: dict[str, object] = {"tid": tenant_id, "wid": warehouse_id}
    placeholders = []
    for index, variant_id in enumerate(sorted(variant_ids, key=str)):
        key = f"v{index}"
        placeholders.append(f":{key}")
        params[key] = variant_id
    rows = (
        await session.execute(
            sa.text(
                "SELECT variant_id, on_hand_qty - reserved_qty AS available "
                "FROM inventory_balances "
                "WHERE tenant_id = :tid AND warehouse_id = :wid "
                f"AND variant_id IN ({', '.join(placeholders)}) "
                "ORDER BY variant_id FOR UPDATE"
            ),
            params,
        )
    ).all()
    return {UUID(str(row.variant_id)): int(row.available) for row in rows}


async def _movement_exists(
    session: AsyncSession,
    tenant_id: UUID,
    movement_no: str,
) -> bool:
    return (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(WmsMovement)
            .where(
                WmsMovement.tenant_id == tenant_id,
                WmsMovement.movement_no == movement_no,
            )
        )
        or 0
    ) > 0


async def _post_movement(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    warehouse_id: UUID,
    variant_id: UUID,
    movement_no: str,
    qty_delta: int,
    movement_type: str,
    reference_type: str,
    reference_id: UUID,
    operator_user_id: UUID | None,
    notes: str | None,
) -> WmsMovement:
    movement = await session.scalar(
        insert(WmsMovement)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            movement_no=movement_no,
            warehouse_id=warehouse_id,
            variant_id=variant_id,
            movement_type=movement_type,
            qty_delta=qty_delta,
            reference_type=reference_type,
            reference_id=reference_id,
            operator_user_id=operator_user_id,
            notes=notes,
        )
        .on_conflict_do_nothing(
            index_elements=[WmsMovement.tenant_id, WmsMovement.movement_no]
        )
        .returning(WmsMovement)
    )
    if movement is None:
        raise conflicting_change(
            f"Movement '{movement_no}' already exists with different details."
        )
    return cast(WmsMovement, movement)


async def _parent_on_hand(
    session: AsyncSession, tenant_id: UUID, warehouse_id: UUID, variant_id: UUID
) -> int:
    balance = await session.scalar(
        sa.text(
            "SELECT on_hand_qty FROM inventory_balances "
            "WHERE tenant_id = :tid AND warehouse_id = :wid AND variant_id = :vid"
        ),
        {"tid": tenant_id, "wid": warehouse_id, "vid": variant_id},
    )
    return int(balance or 0)


async def assemble_kit(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    parent_variant_id: UUID,
    warehouse_id: UUID,
    qty_kits: int,
    request_id: UUID,
    notes: str | None,
) -> BomAssembleRead:
    """Deduct every component and credit the kit, idempotently per `request_id`."""
    variants = await _variant_rows(session, tenant_id, [parent_variant_id])
    parent = variants.get(parent_variant_id)
    if parent is None:
        raise record_not_found("Parent SKU")
    parent_sku = str(parent[1])

    components = await _component_rows(session, tenant_id, parent_variant_id)
    if not components:
        raise conflicting_change("This SKU has no BOM components to assemble.")

    consumed_by_sku: dict[UUID, tuple[str, int]] = {}
    for row in components:
        component = row[0]
        effective = effective_qty_per_unit(component.qty_per_unit, component.waste_rate_pct)
        consumed_by_sku[component.component_variant_id] = (
            row[2],
            ceil_units((effective * Decimal(qty_kits)).quantize(QTY_STEP)),
        )
    plan = sorted(consumed_by_sku.items(), key=lambda item: str(item[0]))

    warehouse_exists = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Warehouse)
        .where(Warehouse.tenant_id == tenant_id, Warehouse.id == warehouse_id)
    )
    if not warehouse_exists:
        raise record_not_found("Warehouse")

    ledger = InventoryLedgerService()
    parent_movement_no = f"BOM-{request_id.hex}-{parent_variant_id}"

    # Replay: the identical `request_id` must never deduct stock twice.
    if await _movement_exists(session, tenant_id, parent_movement_no):
        recorded = (
            await session.execute(
                sa.select(WmsMovement)
                .where(
                    WmsMovement.tenant_id == tenant_id,
                    WmsMovement.reference_type == _BOM_ASSEMBLE_REFERENCE,
                    WmsMovement.reference_id == request_id,
                )
                .order_by(WmsMovement.variant_id)
            )
        ).scalars().all()
        recorded_plan = {
            movement.variant_id: -movement.qty_delta
            for movement in recorded
            if movement.variant_id != parent_variant_id
        }
        expected_plan = {variant_id: qty for variant_id, (_, qty) in plan}
        parent_recorded = next(
            (movement for movement in recorded if movement.variant_id == parent_variant_id), None
        )
        if (
            recorded_plan != expected_plan
            or parent_recorded is None
            or parent_recorded.qty_delta != qty_kits
            or parent_recorded.warehouse_id != warehouse_id
        ):
            raise conflicting_change(
                "This request_id was already used to assemble a different BOM payload."
            )
        return BomAssembleRead(
            request_id=request_id,
            warehouse_id=warehouse_id,
            parent_variant_id=parent_variant_id,
            parent_sku=parent_sku,
            qty_kits=qty_kits,
            parent_on_hand_qty=await _parent_on_hand(
                session, tenant_id, warehouse_id, parent_variant_id
            ),
            consumed=[
                BomAssemblyConsumptionRead(
                    component_variant_id=variant_id, component_sku=sku, qty_consumed=qty
                )
                for variant_id, (sku, qty) in plan
            ],
            replayed=True,
        )

    availability = await _lock_balances(
        session, tenant_id, warehouse_id, [variant_id for variant_id, _ in plan]
    )
    for variant_id, (_sku, qty) in plan:
        available = availability.get(variant_id, 0)
        if available < qty:
            raise InsufficientStockError(variant_id=variant_id, requested=qty, available=available)

    for variant_id, (_, qty) in plan:
        movement_no = f"BOM-{request_id.hex}-{variant_id}"
        movement = await _post_movement(
            session,
            tenant_id=tenant_id,
            warehouse_id=warehouse_id,
            variant_id=variant_id,
            movement_no=movement_no,
            qty_delta=-qty,
            movement_type="stocktake_adjust",
            reference_type=_BOM_ASSEMBLE_REFERENCE,
            reference_id=request_id,
            operator_user_id=user_id,
            notes=notes,
        )
        await ledger.adjust_stocktake(
            session,
            tenant_id,
            warehouse_id,
            variant_id,
            -qty,
            movement.id,
            movement_no,
        )

    parent_movement = await _post_movement(
        session,
        tenant_id=tenant_id,
        warehouse_id=warehouse_id,
        variant_id=parent_variant_id,
        movement_no=parent_movement_no,
        qty_delta=qty_kits,
        movement_type="stocktake_adjust",
        reference_type=_BOM_ASSEMBLE_REFERENCE,
        reference_id=request_id,
        operator_user_id=user_id,
        notes=notes,
    )
    await ledger.adjust_stocktake(
        session,
        tenant_id,
        warehouse_id,
        parent_variant_id,
        qty_kits,
        parent_movement.id,
        parent_movement_no,
    )

    return BomAssembleRead(
        request_id=request_id,
        warehouse_id=warehouse_id,
        parent_variant_id=parent_variant_id,
        parent_sku=parent_sku,
        qty_kits=qty_kits,
        parent_on_hand_qty=await _parent_on_hand(
            session, tenant_id, warehouse_id, parent_variant_id
        ),
        consumed=[
            BomAssemblyConsumptionRead(
                component_variant_id=variant_id, component_sku=sku, qty_consumed=qty
            )
            for variant_id, (sku, qty) in plan
        ],
        replayed=False,
    )


# ---------------------------------------------------------------------------
# In-transit headhaul stock transfers
# ---------------------------------------------------------------------------


async def _transfer_lines(
    session: AsyncSession,
    tenant_id: UUID,
    transfer_ids: list[UUID],
) -> dict[UUID, list[StockTransferLineRead]]:
    if not transfer_ids:
        return {}
    stmt = (
        sa.select(StockTransferLine, ProductVariant.sku)
        .select_from(StockTransferLine)
        .join(
            ProductVariant,
            sa.and_(
                ProductVariant.tenant_id == StockTransferLine.tenant_id,
                ProductVariant.id == StockTransferLine.variant_id,
            ),
        )
        .where(
            StockTransferLine.tenant_id == tenant_id,
            StockTransferLine.transfer_id.in_(transfer_ids),
        )
        .order_by(ProductVariant.sku)
    )
    grouped: dict[UUID, list[StockTransferLineRead]] = defaultdict(list)
    for line, sku in (await session.execute(stmt)).all():
        grouped[line.transfer_id].append(
            StockTransferLineRead(
                id=line.id,
                variant_id=line.variant_id,
                sku=str(sku),
                qty=line.qty,
                received_qty=line.received_qty,
                unit_freight_cost_base=_dec_text(line.unit_freight_cost_base),
                line_freight_cost_base=_dec_text(
                    Decimal(line.unit_freight_cost_base) * Decimal(line.qty)
                ),
            )
        )
    return grouped


async def list_transfers(
    session: AsyncSession,
    tenant_id: UUID,
    transfer_id: UUID | None = None,
    *,
    for_update: bool = False,
) -> StockTransferListRead:
    source = sa.orm.aliased(Warehouse)
    destination = sa.orm.aliased(Warehouse)
    stmt = (
        sa.select(
            StockTransfer,
            source.name.label("source_name"),
            destination.name.label("destination_name"),
        )
        .select_from(StockTransfer)
        .outerjoin(
            source,
            sa.and_(
                source.tenant_id == StockTransfer.tenant_id,
                source.id == StockTransfer.source_warehouse_id,
            ),
        )
        .outerjoin(
            destination,
            sa.and_(
                destination.tenant_id == StockTransfer.tenant_id,
                destination.id == StockTransfer.destination_warehouse_id,
            ),
        )
        .where(StockTransfer.tenant_id == tenant_id)
        .order_by(StockTransfer.created_at.desc(), StockTransfer.id)
    )
    if transfer_id is not None:
        stmt = stmt.where(StockTransfer.id == transfer_id)
    if for_update:
        stmt = stmt.with_for_update(of=StockTransfer)
    rows: list[TransferRow] = list((await session.execute(stmt)).all())
    lines = await _transfer_lines(session, tenant_id, [row[0].id for row in rows])

    items: list[StockTransferRead] = []
    for transfer, source_name, destination_name in rows:
        transfer_lines = lines.get(transfer.id, [])
        items.append(
            StockTransferRead(
                id=transfer.id,
                tenant_id=transfer.tenant_id,
                transfer_no=transfer.transfer_no,
                source_warehouse_id=transfer.source_warehouse_id,
                source_warehouse_name=str(
                    source_name or f"Warehouse {transfer.source_warehouse_id.hex[:8]}"
                ),
                destination_warehouse_id=transfer.destination_warehouse_id,
                destination_warehouse_name=str(
                    destination_name or f"Warehouse {transfer.destination_warehouse_id.hex[:8]}"
                ),
                status=cast(TransferStatus, transfer.status),
                shipping_method=cast(ShippingMethod, transfer.shipping_method),
                tracking_no=transfer.tracking_no,
                eta_days=transfer.eta_days,
                dispatched_at=transfer.dispatched_at,
                received_at=transfer.received_at,
                notes=transfer.notes,
                created_by=transfer.created_by,
                created_at=transfer.created_at,
                updated_at=transfer.updated_at,
                total_qty=sum(line.qty for line in transfer_lines),
                total_freight_cost_base=_dec_text(
                    sum(
                        (Decimal(line.line_freight_cost_base) for line in transfer_lines),
                        start=Decimal(0),
                    )
                ),
                lines=transfer_lines,
            )
        )
    return StockTransferListRead(items=items, total=len(items))


async def _single_transfer(
    session: AsyncSession,
    tenant_id: UUID,
    transfer_id: UUID,
    *,
    for_update: bool = False,
) -> StockTransferRead:
    page = await list_transfers(session, tenant_id, transfer_id, for_update=for_update)
    if not page.items:
        raise record_not_found("Stock transfer")
    return page.items[0]


async def create_transfer(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    payload: StockTransferCreate,
) -> StockTransferRead:
    if payload.source_warehouse_id == payload.destination_warehouse_id:
        raise _unprocessable(
            "ERR_TRANSFER_SAME_WAREHOUSE",
            "A headhaul transfer needs two different warehouses.",
            "transfer-same-warehouse",
        )
    warehouses = set(
        (
            await session.scalars(
                sa.select(Warehouse.id).where(
                    Warehouse.tenant_id == tenant_id,
                    Warehouse.id.in_(
                        [payload.source_warehouse_id, payload.destination_warehouse_id]
                    ),
                )
            )
        ).all()
    )
    if payload.source_warehouse_id not in warehouses:
        raise record_not_found("Source warehouse")
    if payload.destination_warehouse_id not in warehouses:
        raise record_not_found("Destination warehouse")

    variant_ids = [line.variant_id for line in payload.lines]
    if len(set(variant_ids)) != len(variant_ids):
        raise _unprocessable(
            "ERR_TRANSFER_DUPLICATE_SKU",
            "Each SKU may appear only once per transfer.",
            "transfer-duplicate-sku",
        )
    known = await _variant_rows(session, tenant_id, variant_ids)
    for variant_id in variant_ids:
        if variant_id not in known:
            raise record_not_found("SKU")

    transfer = StockTransfer(
        id=uuid4(),
        tenant_id=tenant_id,
        transfer_no=payload.transfer_no or f"TRF-{uuid4().hex[:12].upper()}",
        source_warehouse_id=payload.source_warehouse_id,
        destination_warehouse_id=payload.destination_warehouse_id,
        status="draft",
        shipping_method=payload.shipping_method,
        tracking_no=payload.tracking_no,
        eta_days=payload.eta_days,
        notes=payload.notes,
        created_by=user_id,
    )
    session.add(transfer)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise conflicting_change("This transfer number is already in use.") from exc

    for line in payload.lines:
        session.add(
            StockTransferLine(
                id=uuid4(),
                tenant_id=tenant_id,
                transfer_id=transfer.id,
                variant_id=line.variant_id,
                qty=line.qty,
                received_qty=0,
                unit_freight_cost_base=line.unit_freight_cost_base,
            )
        )
    await session.flush()
    return await _single_transfer(session, tenant_id, transfer.id)


async def dispatch_transfer(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    transfer_id: UUID,
) -> StockTransferRead:
    """Deduct every line from the source warehouse and mark the load in transit."""
    current = await _single_transfer(session, tenant_id, transfer_id, for_update=True)
    if current.status == "in_transit":
        # Idempotent retry: the stock already left the source warehouse once.
        return current
    if current.status != "draft":
        raise conflicting_change(
            f"Only draft stock transfers can be dispatched (this one is '{current.status}')."
        )

    plan = sorted(
        ((line.variant_id, line.qty) for line in current.lines),
        key=lambda item: str(item[0]),
    )
    ledger = InventoryLedgerService()
    availability = await _lock_balances(
        session, tenant_id, current.source_warehouse_id, [variant_id for variant_id, _ in plan]
    )
    for variant_id, qty in plan:
        available = availability.get(variant_id, 0)
        if available < qty:
            raise InsufficientStockError(variant_id=variant_id, requested=qty, available=available)

    for variant_id, qty in plan:
        movement_no = f"TRFOUT-{transfer_id.hex}-{variant_id}"
        movement = await _post_movement(
            session,
            tenant_id=tenant_id,
            warehouse_id=current.source_warehouse_id,
            variant_id=variant_id,
            movement_no=movement_no,
            qty_delta=-qty,
            movement_type="transfer_out",
            reference_type=_TRANSFER_REFERENCE,
            reference_id=transfer_id,
            operator_user_id=user_id,
            notes=current.notes,
        )
        await ledger.adjust_stocktake(
            session,
            tenant_id,
            current.source_warehouse_id,
            variant_id,
            -qty,
            movement.id,
            movement_no,
        )

    transfer = await session.get(StockTransfer, transfer_id)
    if transfer is None:
        raise record_not_found("Stock transfer")
    transfer.status = "in_transit"
    transfer.dispatched_at = datetime.now(UTC)
    transfer.updated_at = datetime.now(UTC)
    await session.flush()
    return await _single_transfer(session, tenant_id, transfer_id)


async def receive_transfer(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    transfer_id: UUID,
) -> StockTransferRead:
    """Credit the destination warehouse and close the in-transit leg."""
    current = await _single_transfer(session, tenant_id, transfer_id, for_update=True)
    if current.status == "received":
        return current
    if current.status != "in_transit":
        raise conflicting_change(
            f"Only in-transit stock transfers can be received (this one is '{current.status}')."
        )

    ledger = InventoryLedgerService()
    plan = sorted(
        ((line.variant_id, line.qty) for line in current.lines),
        key=lambda item: str(item[0]),
    )
    for variant_id, qty in plan:
        movement_no = f"TRFIN-{transfer_id.hex}-{variant_id}"
        movement = await _post_movement(
            session,
            tenant_id=tenant_id,
            warehouse_id=current.destination_warehouse_id,
            variant_id=variant_id,
            movement_no=movement_no,
            qty_delta=qty,
            movement_type="transfer_in",
            reference_type=_TRANSFER_REFERENCE,
            reference_id=transfer_id,
            operator_user_id=user_id,
            notes=current.notes,
        )
        await ledger.adjust_stocktake(
            session,
            tenant_id,
            current.destination_warehouse_id,
            variant_id,
            qty,
            movement.id,
            movement_no,
        )

    lines = (
        await session.scalars(
            sa.select(StockTransferLine).where(
                StockTransferLine.tenant_id == tenant_id,
                StockTransferLine.transfer_id == transfer_id,
            )
        )
    ).all()
    for line in lines:
        line.received_qty = line.qty

    transfer = await session.get(StockTransfer, transfer_id)
    if transfer is None:
        raise record_not_found("Stock transfer")
    transfer.status = "received"
    transfer.received_at = datetime.now(UTC)
    transfer.updated_at = datetime.now(UTC)
    await session.flush()
    return await _single_transfer(session, tenant_id, transfer_id)


async def cancel_transfer(
    session: AsyncSession,
    tenant_id: UUID,
    transfer_id: UUID,
) -> StockTransferRead:
    """Cancel a draft transfer; a load that already left the dock cannot be cancelled."""
    current = await _single_transfer(session, tenant_id, transfer_id, for_update=True)
    if current.status == "cancelled":
        return current
    if current.status != "draft":
        raise conflicting_change(
            f"Only draft stock transfers can be cancelled (this one is '{current.status}')."
        )
    transfer = await session.get(StockTransfer, transfer_id)
    if transfer is None:
        raise record_not_found("Stock transfer")
    transfer.status = "cancelled"
    transfer.updated_at = datetime.now(UTC)
    await session.flush()
    return await _single_transfer(session, tenant_id, transfer_id)


# ---------------------------------------------------------------------------
# Days-of-cover (DOC) weighted replenishment
# ---------------------------------------------------------------------------


async def doc_replenishment(
    session: AsyncSession,
    tenant_id: UUID,
    target_doc_days: int,
) -> DocReplenishmentRead:
    """Blend 7/30/90 day sales into a DOC per SKU and suggest a replenishment qty."""
    product_name = sa.func.coalesce(
        Product.i18n_specs["en"]["title"].astext,
        Product.i18n_specs["zh"]["title"].astext,
        Product.spu_code,
    )
    variant_rows = (
        await session.execute(
            sa.select(
                ProductVariant.id,
                ProductVariant.sku,
                product_name.label("product_name"),
            )
            .select_from(ProductVariant)
            .join(
                Product,
                sa.and_(
                    Product.tenant_id == ProductVariant.tenant_id,
                    Product.id == ProductVariant.product_id,
                ),
            )
            .where(ProductVariant.tenant_id == tenant_id)
            .order_by(ProductVariant.sku)
        )
    ).all()

    balance_rows = (
        await session.execute(
            sa.text(
                """
                SELECT ib.variant_id,
                       COALESCE(SUM(ib.on_hand_qty - ib.reserved_qty), 0) AS available,
                       w.country_code,
                       w.code,
                       w.name
                FROM inventory_balances ib
                LEFT JOIN warehouses w
                    ON w.tenant_id = ib.tenant_id AND w.id = ib.warehouse_id
                WHERE ib.tenant_id = :tid
                GROUP BY ib.variant_id, w.country_code, w.code, w.name
                """
            ),
            {"tid": tenant_id},
        )
    ).all()

    local_qty: dict[UUID, int] = defaultdict(int)
    overseas_qty: dict[UUID, int] = defaultdict(int)
    for row in balance_rows:
        variant_id = UUID(str(row.variant_id))
        available = int(row.available)
        if is_overseas_warehouse(
            None if row.country_code is None else str(row.country_code),
            None if row.code is None else str(row.code),
            None if row.name is None else str(row.name),
        ):
            overseas_qty[variant_id] += available
        else:
            local_qty[variant_id] += available

    window_start = datetime.now(UTC) - timedelta(days=max(7, 30, 90))
    sales_rows = (
        await session.execute(
            sa.text(
                f"""
                SELECT oi.variant_id,
                       COALESCE(SUM(oi.quantity) FILTER (
                           WHERE o.created_at >= now() - interval '7 days'), 0) AS units_7d,
                       COALESCE(SUM(oi.quantity) FILTER (
                           WHERE o.created_at >= now() - interval '30 days'), 0) AS units_30d,
                       COALESCE(SUM(oi.quantity), 0) AS units_90d
                FROM order_items oi
                JOIN orders o
                    ON o.tenant_id = oi.tenant_id AND o.id = oi.order_id
                WHERE oi.tenant_id = :tid
                  AND o.fulfillment_status NOT IN ({_NON_DEMAND_SQL})
                  AND o.created_at >= :window_start
                GROUP BY oi.variant_id
                """
            ),
            {"tid": tenant_id, "window_start": window_start},
        )
    ).all()
    sales: dict[UUID, tuple[int, int, int]] = {
        UUID(str(row.variant_id)): (int(row.units_7d), int(row.units_30d), int(row.units_90d))
        for row in sales_rows
    }

    in_transit = {
        UUID(str(row[0])): int(row[1])
        for row in (
            await session.execute(
                sa.text(
                    """
                    SELECT stl.variant_id, COALESCE(SUM(stl.qty), 0) AS in_transit_qty
                    FROM stock_transfer_lines stl
                    JOIN stock_transfers st
                        ON st.tenant_id = stl.tenant_id AND st.id = stl.transfer_id
                    WHERE stl.tenant_id = :tid AND st.status = 'in_transit'
                    GROUP BY stl.variant_id
                    """
                ),
                {"tid": tenant_id},
            )
        ).all()
    }

    items: list[DocReplenishmentRowRead] = []
    for variant_id, sku, name in variant_rows:
        units_7d, units_30d, units_90d = sales.get(variant_id, (0, 0, 0))
        local = local_qty.get(variant_id, 0)
        overseas = overseas_qty.get(variant_id, 0)
        transit = in_transit.get(variant_id, 0)
        total_available = local + overseas + transit
        daily = weighted_daily_sales(units_7d, units_30d, units_90d)
        cover = doc_days(total_available, daily)
        items.append(
            DocReplenishmentRowRead(
                variant_id=variant_id,
                sku=str(sku),
                product_name=str(name),
                local_available_qty=local,
                overseas_available_qty=overseas,
                in_transit_qty=transit,
                total_available_qty=total_available,
                units_sold_7d=units_7d,
                units_sold_30d=units_30d,
                units_sold_90d=units_90d,
                weighted_daily_sales=_dec_text(daily),
                doc_days=None if cover is None else _dec_text(cover, DOC_STEP),
                suggested_reorder_qty=suggested_reorder_qty(
                    total_available, daily, target_doc_days
                ),
                risk_level=risk_level(cover),
            )
        )

    rank = {"critical": 0, "warning": 1, "healthy": 2}
    items.sort(
        key=lambda item: (
            rank[item.risk_level],
            Decimal(item.doc_days) if item.doc_days is not None else Decimal("1e12"),
            item.sku,
        )
    )
    return DocReplenishmentRead(
        target_doc_days=target_doc_days,
        generated_at=datetime.now(UTC),
        items=items,
        total=len(items),
        critical_count=sum(1 for item in items if item.risk_level == "critical"),
        warning_count=sum(1 for item in items if item.risk_level == "warning"),
    )
