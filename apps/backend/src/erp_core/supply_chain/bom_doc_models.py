"""ORM models for the Track 7 supply-chain tables (migration 0026).

`bom_components` is a strictly one-level light bill of materials, and
`stock_transfers` / `stock_transfer_lines` carry the headhaul (first-leg)
transfer whose units are in transit between the domestic and overseas
warehouse. Every table is tenant scoped and FORCE RLS protected in PostgreSQL;
these models only ever describe the shape, the isolation lives in the database.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class BomComponent(Base):
    """One component line of a 1-level light BOM kit."""

    __tablename__ = "bom_components"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "parent_variant_id",
            "component_variant_id",
            name="uq_bom_components_tenant_parent_component",
        ),
        CheckConstraint("qty_per_unit > 0", name="ck_bom_components_qty_per_unit"),
        CheckConstraint(
            "waste_rate_pct >= 0 AND waste_rate_pct <= 100",
            name="ck_bom_components_waste_rate_pct",
        ),
        CheckConstraint(
            "parent_variant_id <> component_variant_id",
            name="ck_bom_components_no_self_reference",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    parent_variant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("product_variants.id", ondelete="CASCADE"),
        nullable=False,
    )
    component_variant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("product_variants.id", ondelete="CASCADE"),
        nullable=False,
    )
    qty_per_unit: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    waste_rate_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, server_default="0"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StockTransfer(Base):
    """Header of a headhaul transfer: draft -> in_transit -> received / cancelled."""

    __tablename__ = "stock_transfers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "transfer_no", name="uq_stock_transfers_tenant_transfer_no"
        ),
        CheckConstraint(
            "status IN ('draft', 'in_transit', 'received', 'cancelled')",
            name="ck_stock_transfers_status",
        ),
        CheckConstraint(
            "shipping_method IN ('sea_freight', 'air_freight', 'express', 'rail', 'truck')",
            name="ck_stock_transfers_shipping_method",
        ),
        CheckConstraint("eta_days >= 0", name="ck_stock_transfers_eta_days"),
        CheckConstraint(
            "source_warehouse_id <> destination_warehouse_id",
            name="ck_stock_transfers_distinct_warehouses",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    transfer_no: Mapped[str] = mapped_column(Text, nullable=False)
    source_warehouse_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False
    )
    destination_warehouse_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("warehouses.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    shipping_method: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="sea_freight"
    )
    tracking_no: Mapped[str | None] = mapped_column(Text, nullable=True)
    eta_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="25")
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StockTransferLine(Base):
    """One SKU quantity riding on a headhaul transfer."""

    __tablename__ = "stock_transfer_lines"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "transfer_id",
            "variant_id",
            name="uq_stock_transfer_lines_tenant_transfer_variant",
        ),
        CheckConstraint("qty > 0", name="ck_stock_transfer_lines_qty"),
        CheckConstraint(
            "received_qty >= 0 AND received_qty <= qty",
            name="ck_stock_transfer_lines_received_qty",
        ),
        CheckConstraint(
            "unit_freight_cost_base >= 0",
            name="ck_stock_transfer_lines_unit_freight_cost_base",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    transfer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("stock_transfers.id", ondelete="CASCADE"),
        nullable=False,
    )
    variant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("product_variants.id", ondelete="CASCADE"),
        nullable=False,
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    received_qty: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    unit_freight_cost_base: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, server_default="0"
    )
