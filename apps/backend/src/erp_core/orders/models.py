from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base
from erp_core.shared.enums import (
    FulfillmentStatus,
    InventoryEventType,
    OrderMode,
    PaymentStatus,
    TrackingProvider,
)


def _str_enum_col(enum_cls: type) -> sa.Enum:
    return sa.Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=64,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "order_no", name="uq_orders_tenant_order_no"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    order_no: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    order_mode: Mapped[OrderMode] = mapped_column(_str_enum_col(OrderMode), nullable=False)
    channel_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("channel_accounts.id"), nullable=True
    )
    external_order_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    customer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False
    )
    quote_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("quotes.id"), nullable=True
    )
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False)
    fx_rate_to_base: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False)
    subtotal_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    shipping_charged: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    tax_collected: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    total_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)
    fulfillment_status: Mapped[FulfillmentStatus] = mapped_column(
        _str_enum_col(FulfillmentStatus), nullable=False, default=FulfillmentStatus.PENDING_PAYMENT
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        _str_enum_col(PaymentStatus), nullable=False, default=PaymentStatus.UNPAID
    )
    channel_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("product_variants.id"), nullable=False
    )
    sku_snapshot: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    quantity: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    unit_cost_base_snapshot: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), nullable=False)


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "tracking_number", name="uq_shipments_tenant_tracking"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False
    )
    tracking_provider: Mapped[TrackingProvider] = mapped_column(
        _str_enum_col(TrackingProvider), nullable=False, default=TrackingProvider("17track")
    )
    tracking_number: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    carrier_code: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    shipment_mode: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="parcel")
    standardized_status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="not_found")
    sub_status: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    estimated_delivery_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    tracking_events: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    actual_shipping_cost_base: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    exception_alert_sent: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class InventoryBalance(Base):
    __tablename__ = "inventory_balances"
    __table_args__ = (
        sa.CheckConstraint("on_hand_qty >= reserved_qty", name="chk_inventory_non_negative"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True
    )
    warehouse_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    variant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("product_variants.id"), primary_key=True
    )
    on_hand_qty: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    reserved_qty: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)


class InventoryLedger(Base):
    __tablename__ = "inventory_ledger"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_inventory_ledger_idempotency"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger(), sa.Identity(always=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True
    )
    warehouse_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    variant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("product_variants.id"), nullable=False
    )
    event_type: Mapped[InventoryEventType] = mapped_column(
        _str_enum_col(InventoryEventType), nullable=False
    )
    qty_delta_on_hand: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    qty_delta_reserved: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    reference_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    reference_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class InventorySyncJob(Base):
    __tablename__ = "inventory_sync_jobs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("product_variants.id", ondelete="CASCADE"),
        nullable=False,
    )
    mapping_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("platform_sku_mappings.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("channel_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    external_sku: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    requested_qty: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    synced_qty: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    trigger_source: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
