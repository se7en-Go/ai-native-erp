"""Request/response contracts for `/api/v1/supply-chain` Track 7 endpoints.

Money and quantity decimals are serialised as exact strings (`"12.3400"`), never
as IEEE-754 floats, so the workbench can render a rolled-up BOM cost or a DOC
figure without re-deriving it in JavaScript.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
TransferNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
TrackingNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
QtyPerUnit = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=4)]
WasteRatePct = Annotated[Decimal, Field(ge=0, le=100, max_digits=6, decimal_places=2)]
UnitCost = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=4)]
LineQty = Annotated[int, Field(gt=0, le=1_000_000_000)]
KitQty = Annotated[int, Field(gt=0, le=1_000_000)]
EtaDays = Annotated[int, Field(ge=0, le=3650)]
TargetDocDays = Annotated[int, Field(ge=1, le=365)]

ShippingMethod = Literal["sea_freight", "air_freight", "express", "rail", "truck"]
TransferStatus = Literal["draft", "in_transit", "received", "cancelled"]
RiskLevel = Literal["critical", "warning", "healthy"]


# ---------------------------------------------------------------------------
# 1-level light BOM
# ---------------------------------------------------------------------------


class BomComponentUpsert(BaseModel):
    """Add or update one component line of a 1-level kit."""

    parent_variant_id: UUID
    component_variant_id: UUID
    qty_per_unit: QtyPerUnit
    waste_rate_pct: WasteRatePct = Decimal("0")
    notes: Note | None = None


class BomComponentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    parent_variant_id: UUID
    parent_sku: str
    component_variant_id: UUID
    component_sku: str
    qty_per_unit: str
    waste_rate_pct: str
    effective_qty_per_unit: str
    component_unit_cost_base: str
    line_unit_cost_base: str
    notes: str | None
    created_at: datetime
    updated_at: datetime


class BomKitRead(BaseModel):
    """One parent SKU and every component that makes it."""

    parent_variant_id: UUID
    parent_sku: str
    components: list[BomComponentRead]
    component_count: int
    rolled_up_unit_cost: str


class BomListRead(BaseModel):
    items: list[BomKitRead]
    total: int


class BomCostRollupLineRead(BaseModel):
    component_id: UUID
    component_variant_id: UUID
    component_sku: str
    qty_per_unit: str
    waste_rate_pct: str
    effective_qty_per_unit: str
    component_unit_cost_base: str
    line_unit_cost_base: str


class BomCostRollupRead(BaseModel):
    parent_variant_id: UUID
    parent_sku: str
    base_currency: str
    component_count: int
    unit_cost_base: str
    lines: list[BomCostRollupLineRead]


class BomAssembleRequest(BaseModel):
    """Assemble `qty_kits` kits, deducting every component from one warehouse."""

    warehouse_id: UUID
    qty_kits: KitQty
    notes: Note | None = None
    request_id: UUID


class BomAssemblyConsumptionRead(BaseModel):
    component_variant_id: UUID
    component_sku: str
    qty_consumed: int


class BomAssembleRead(BaseModel):
    request_id: UUID
    warehouse_id: UUID
    parent_variant_id: UUID
    parent_sku: str
    qty_kits: int
    parent_on_hand_qty: int
    consumed: list[BomAssemblyConsumptionRead]
    replayed: bool


# ---------------------------------------------------------------------------
# In-transit headhaul stock transfers
# ---------------------------------------------------------------------------


class StockTransferLineInput(BaseModel):
    variant_id: UUID
    qty: LineQty
    unit_freight_cost_base: UnitCost = Decimal("0")


class StockTransferCreate(BaseModel):
    transfer_no: TransferNo | None = None
    source_warehouse_id: UUID
    destination_warehouse_id: UUID
    shipping_method: ShippingMethod = "sea_freight"
    tracking_no: TrackingNo | None = None
    eta_days: EtaDays = 25
    notes: Note | None = None
    lines: list[StockTransferLineInput] = Field(min_length=1, max_length=200)


class StockTransferLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    variant_id: UUID
    sku: str
    qty: int
    received_qty: int
    unit_freight_cost_base: str
    line_freight_cost_base: str


class StockTransferRead(BaseModel):
    id: UUID
    tenant_id: UUID
    transfer_no: str
    source_warehouse_id: UUID
    source_warehouse_name: str
    destination_warehouse_id: UUID
    destination_warehouse_name: str
    status: TransferStatus
    shipping_method: ShippingMethod
    tracking_no: str | None
    eta_days: int
    dispatched_at: datetime | None
    received_at: datetime | None
    notes: str | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    total_qty: int
    total_freight_cost_base: str
    lines: list[StockTransferLineRead]


class StockTransferListRead(BaseModel):
    items: list[StockTransferRead]
    total: int


# ---------------------------------------------------------------------------
# Days-of-cover (DOC) weighted replenishment
# ---------------------------------------------------------------------------


class DocReplenishmentRowRead(BaseModel):
    variant_id: UUID
    sku: str
    product_name: str
    local_available_qty: int
    overseas_available_qty: int
    in_transit_qty: int
    total_available_qty: int
    units_sold_7d: int
    units_sold_30d: int
    units_sold_90d: int
    weighted_daily_sales: str
    doc_days: str | None
    suggested_reorder_qty: int
    risk_level: RiskLevel


class DocReplenishmentRead(BaseModel):
    target_doc_days: int
    generated_at: datetime
    items: list[DocReplenishmentRowRead]
    total: int
    critical_count: int
    warning_count: int
