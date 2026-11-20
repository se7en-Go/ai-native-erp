from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class _TenantRow:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(sa.ForeignKey("tenants.id", ondelete="CASCADE"))


class Supplier(_TenantRow, Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "code"),
    )

    code: Mapped[str] = mapped_column(sa.Text)
    name: Mapped[str] = mapped_column(sa.Text)
    contact_name: Mapped[str | None] = mapped_column(sa.Text)
    contact_email: Mapped[str | None] = mapped_column(sa.Text)
    contact_phone: Mapped[str | None] = mapped_column(sa.Text)
    country: Mapped[str | None] = mapped_column(sa.CHAR(2))
    lead_time_days: Mapped[int] = mapped_column(server_default="0")
    payment_terms: Mapped[str | None] = mapped_column(sa.Text)
    rating: Mapped[Decimal | None] = mapped_column(sa.Numeric(3, 2))
    status: Mapped[str] = mapped_column(sa.Text, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class PurchaseOrder(_TenantRow, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "po_no"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "supplier_id"], ["suppliers.tenant_id", "suppliers.id"]
        ),
    )

    po_no: Mapped[str] = mapped_column(sa.Text)
    supplier_id: Mapped[UUID]
    warehouse_id: Mapped[UUID]
    currency: Mapped[str] = mapped_column(sa.CHAR(3))
    total_amount: Mapped[Decimal] = mapped_column(sa.Numeric(14, 2), server_default="0")
    status: Mapped[str] = mapped_column(sa.Text, server_default="draft")
    expected_arrival_date: Mapped[date | None]
    notes: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class PurchaseOrderItem(_TenantRow, Base):
    __tablename__ = "purchase_order_items"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "purchase_order_id", "variant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "purchase_order_id"],
            ["purchase_orders.tenant_id", "purchase_orders.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "variant_id"],
            ["product_variants.tenant_id", "product_variants.id"],
        ),
    )

    purchase_order_id: Mapped[UUID]
    variant_id: Mapped[UUID]
    sku: Mapped[str] = mapped_column(sa.Text)
    qty_ordered: Mapped[int]
    qty_received: Mapped[int] = mapped_column(server_default="0")
    unit_cost: Mapped[Decimal] = mapped_column(sa.Numeric(14, 4))


class Warehouse(_TenantRow, Base):
    __tablename__ = "warehouses"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "code"),
    )

    code: Mapped[str] = mapped_column(sa.Text)
    name: Mapped[str] = mapped_column(sa.Text)
    country_code: Mapped[str] = mapped_column(sa.CHAR(2), server_default="CN")
    city: Mapped[str] = mapped_column(sa.Text, server_default="")
    is_active: Mapped[bool] = mapped_column(server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class WmsMovement(_TenantRow, Base):
    __tablename__ = "wms_movements"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "movement_no"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "variant_id"],
            ["product_variants.tenant_id", "product_variants.id"],
        ),
    )

    movement_no: Mapped[str] = mapped_column(sa.Text)
    warehouse_id: Mapped[UUID]
    variant_id: Mapped[UUID]
    movement_type: Mapped[str] = mapped_column(sa.Text)
    qty_delta: Mapped[int]
    reference_type: Mapped[str | None] = mapped_column(sa.Text)
    reference_id: Mapped[UUID | None]
    operator_user_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    notes: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
