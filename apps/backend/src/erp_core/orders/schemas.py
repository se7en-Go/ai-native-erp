from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from erp_core.shared.enums import FulfillmentStatus, OrderMode, PaymentStatus, TrackingProvider


class InventoryBalanceRead(BaseModel):
    tenant_id: UUID
    warehouse_id: UUID
    variant_id: UUID
    on_hand_qty: int
    reserved_qty: int
    available_qty: int


class OrderItemInput(BaseModel):
    variant_id: UUID
    quantity: int = Field(gt=0)
    unit_price: Decimal | None = Field(default=None, gt=Decimal("0"))


class OrderItemInternal(BaseModel):
    variant_id: UUID
    sku_snapshot: str
    quantity: int = Field(gt=0)
    unit_price: Decimal
    unit_cost_base_snapshot: Decimal
    line_total: Decimal


class OrderItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_id: UUID
    variant_id: UUID
    sku_snapshot: str
    quantity: int
    unit_price: Decimal
    unit_cost_base_snapshot: Decimal
    line_total: Decimal


class OrderCreateRequest(BaseModel):
    order_mode: OrderMode = OrderMode.B2C_RETAIL
    customer_id: UUID
    warehouse_id: UUID
    currency: str = Field(default="USD", min_length=3, max_length=3)
    fx_rate_to_base: Decimal = Field(default=Decimal("7.200000"), gt=Decimal("0"))
    shipping_charged: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    discount_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    tax_collected: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    items: list[OrderItemInput] = Field(min_length=1)


class OrderCreateInternal(BaseModel):
    order_mode: OrderMode
    customer_id: UUID
    warehouse_id: UUID
    currency: str
    fx_rate_to_base: Decimal
    subtotal_amount: Decimal
    shipping_charged: Decimal = Decimal("0.00")
    discount_amount: Decimal = Decimal("0.00")
    tax_collected: Decimal = Decimal("0.00")
    total_amount: Decimal
    channel_account_id: UUID | None = None
    external_order_id: str | None = None
    quote_id: UUID | None = None
    initial_fulfillment_status: FulfillmentStatus = FulfillmentStatus.CONFIRMED_RESERVED
    initial_payment_status: PaymentStatus = PaymentStatus.UNPAID
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    items: list[OrderItemInternal] = Field(min_length=1)
    order_no_override: str | None = None


class ShipmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    order_id: UUID
    tracking_provider: TrackingProvider
    tracking_number: str
    carrier_code: str | None
    shipment_mode: str
    standardized_status: str
    sub_status: str | None
    last_event_at: datetime | None
    last_synced_at: datetime | None
    estimated_delivery_at: datetime | None
    tracking_events: list[dict[str, Any]]
    actual_shipping_cost_base: Decimal
    exception_alert_sent: bool
    is_stagnant_72h: bool = False
    created_at: datetime


class InventoryBalanceItemRead(BaseModel):
    warehouse_id: UUID
    variant_id: UUID
    sku: str
    on_hand_qty: int
    reserved_qty: int
    available_qty: int


class InventoryLedgerRead(BaseModel):
    id: int
    variant_id: UUID
    event_type: str
    qty_delta_on_hand: int
    qty_delta_reserved: int
    reference_type: str
    reference_id: UUID
    idempotency_key: str
    created_at: datetime


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    order_no: str
    order_mode: OrderMode
    channel_account_id: UUID | None
    external_order_id: str | None
    customer_id: UUID
    customer_name: str | None = None
    quote_id: UUID | None
    currency: str
    fx_rate_to_base: Decimal
    subtotal_amount: Decimal
    shipping_charged: Decimal
    discount_amount: Decimal
    tax_collected: Decimal
    total_amount: Decimal
    fulfillment_status: FulfillmentStatus
    payment_status: PaymentStatus
    channel_metadata: dict[str, Any]
    has_72h_stagnation_alert: bool = False
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemRead] = Field(default_factory=list)
    shipments: list[ShipmentRead] = Field(default_factory=list)


class CancelOrderRequest(BaseModel):
    warehouse_id: UUID
    reason: str = Field(min_length=1, max_length=500)


class ShipOrderRequest(BaseModel):
    warehouse_id: UUID
    tracking_number: str = Field(min_length=3, max_length=128)
    carrier_code: str | None = None
    tracking_provider: TrackingProvider = TrackingProvider("17track")
    shipment_mode: str = Field(default="parcel", pattern="^(parcel|air_freight|sea_lcl|sea_fcl)$")
    actual_shipping_cost_base: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    override_unpaid_balance: bool = False
    override_reason: str | None = None
