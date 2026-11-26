from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator

Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Currency = Annotated[str, StringConstraints(to_upper=True, pattern=r"^[A-Za-z]{3}$")]
Country = Annotated[str, StringConstraints(to_upper=True, pattern=r"^[A-Za-z]{2}$")]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Rating = Annotated[Decimal, Field(ge=0, le=5, max_digits=3, decimal_places=2)]
LeadTime = Annotated[int, Field(ge=0, le=3650)]
Quantity = Annotated[int, Field(gt=0, le=1_000_000_000)]
UnitCost = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=4)]
SupplierStatus = Literal["active", "inactive"]
PurchaseStatus = Literal["draft", "ordered", "partially_received", "received", "cancelled"]
MovementType = Literal[
    "inbound_po",
    "outbound_order",
    "transfer",
    "transfer_out",
    "transfer_in",
    "stocktake_adjust",
]


class SupplierFields(BaseModel):
    contact_name: Name | None = None
    contact_email: EmailStr | None = None
    contact_phone: Code | None = None
    country: Country | None = None
    payment_terms: Note | None = None
    rating: Rating | None = None


class SupplierCreate(SupplierFields):
    code: Code
    name: Name
    lead_time_days: LeadTime = 0
    status: SupplierStatus = "active"


class SupplierUpdate(SupplierFields):
    code: Code | None = None
    name: Name | None = None
    lead_time_days: LeadTime | None = None
    status: SupplierStatus | None = None

    @model_validator(mode="after")
    def validate_required_fields(self) -> SupplierUpdate:
        for field in self.model_fields_set & {"code", "name", "lead_time_days", "status"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class SupplierRead(SupplierCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PurchaseOrderItemInput(BaseModel):
    variant_id: UUID
    qty_ordered: Quantity
    unit_cost: UnitCost


class PurchaseOrderItemRead(PurchaseOrderItemInput):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sku: str
    qty_received: int


PurchaseItems = Annotated[list[PurchaseOrderItemInput], Field(min_length=1, max_length=100)]


class PurchaseOrderCreate(BaseModel):
    po_no: Code | None = None
    supplier_id: UUID
    warehouse_id: UUID
    currency: Currency = "USD"
    status: Literal["draft", "ordered"] = "draft"
    expected_arrival_date: date | None = None
    notes: Note | None = None
    items: PurchaseItems


class PurchaseOrderUpdate(BaseModel):
    supplier_id: UUID | None = None
    warehouse_id: UUID | None = None
    currency: Currency | None = None
    status: Literal["ordered", "cancelled"] | None = None
    expected_arrival_date: date | None = None
    notes: Note | None = None
    items: PurchaseItems | None = None

    @model_validator(mode="after")
    def validate_required_fields(self) -> PurchaseOrderUpdate:
        for field in self.model_fields_set - {"expected_arrival_date", "notes"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class PurchaseOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    po_no: str
    supplier_id: UUID
    warehouse_id: UUID
    currency: str
    total_amount: Decimal
    status: PurchaseStatus
    expected_arrival_date: date | None
    notes: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    items: list[PurchaseOrderItemRead]


class ReceiptLine(BaseModel):
    item_id: UUID
    qty: Quantity


class ReceivePurchaseOrder(BaseModel):
    receipt_id: UUID
    items: list[ReceiptLine] = Field(
        default_factory=list,
        max_length=100,
        description="Empty means receive all outstanding quantities.",
    )


class CreateStocktake(BaseModel):
    movement_type: Literal["stocktake_adjust"]
    request_id: UUID
    warehouse_id: UUID
    variant_id: UUID
    qty_delta: int = Field(ge=-1_000_000_000, le=1_000_000_000)
    notes: Note | None = None

    @model_validator(mode="after")
    def validate_delta(self) -> CreateStocktake:
        if self.qty_delta == 0:
            raise ValueError("qty_delta cannot be zero")
        return self


class MovementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    movement_no: str
    warehouse_id: UUID
    variant_id: UUID
    movement_type: MovementType
    qty_delta: int
    reference_type: str | None
    reference_id: UUID | None
    operator_user_id: UUID | None
    notes: str | None
    created_at: datetime


class StockRead(BaseModel):
    warehouse_id: UUID
    variant_id: UUID
    sku: str
    on_hand_qty: int
    reserved_qty: int
    available_qty: int


class LowStockItemRead(BaseModel):
    """One SKU/warehouse pair sitting below its configured safety stock."""

    variant_id: UUID
    sku: str
    product_name: str
    warehouse_id: UUID
    warehouse_name: str
    on_hand_qty: int
    reserved_qty: int
    available_qty: int
    safety_stock_qty: int
    suggested_reorder_qty: int
    sourcing_url: str | None = None


class LowStockListRead(BaseModel):
    items: list[LowStockItemRead]
    total: int = 0


class LowStockCreatePoRequest(BaseModel):
    """One-click replenishment: convert a low-stock SKU into a real purchase order."""

    supplier_id: UUID
    variant_id: UUID
    quantity: Quantity
    unit_cost: UnitCost
    currency: Currency = "USD"
    notes: Note | None = None


class WarehouseCreateRequest(BaseModel):
    code: Code
    name: Name
    country_code: Country = "CN"
    city: Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)] = ""


class WmsWarehouseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    country_code: str
    city: str
