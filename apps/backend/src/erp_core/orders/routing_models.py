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


class OrderRoutingRule(Base):
    """Ordered condition/action pair that routes an order or holds it for review.

    ``conditions_json`` keys: ``destination_countries`` (list of ISO-3166 alpha-2),
    ``channel_types`` (list), ``order_modes`` (list), ``min_margin_pct`` (decimal
    string anti-loss floor), ``min_total_amount`` (decimal string scope filter),
    ``max_weight_kg`` (decimal string scope filter).

    ``actions_json`` keys: ``warehouse_id`` (uuid|null), ``carrier_code`` (str|null),
    ``service_code`` (str|null), ``add_tags`` (list of str), ``hold_for_review``
    (bool), ``hold_reason`` (str|null).
    """

    __tablename__ = "order_routing_rules"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    priority: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=100)
    is_enabled: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=True)
    conditions_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    actions_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class OrderRoutingEvaluation(Base):
    """One immutable routing decision for an order (append-only history)."""

    __tablename__ = "order_routing_evaluations"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    matched_rule_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    recommended_warehouse_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("warehouses.id", ondelete="SET NULL"),
        nullable=True,
    )
    recommended_carrier: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    recommended_service: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    estimated_margin_pct: Mapped[Decimal | None] = mapped_column(
        sa.Numeric(8, 4), nullable=True
    )
    is_held: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    hold_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    hold_released_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    hold_released_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    applied_tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    evaluated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class CarrierShippingLabel(Base):
    """A booked carrier waybill plus the object-storage key of its 4x6 thermal PDF."""

    __tablename__ = "carrier_shipping_labels"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    shipment_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("shipments.id", ondelete="SET NULL"), nullable=True
    )
    carrier_code: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    service_code: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    tracking_number: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    waybill_number: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    label_format: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, default="pdf_4x6"
    )
    label_r2_key: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    mode: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="sandbox")
    shipping_cost_estimate: Mapped[Decimal | None] = mapped_column(
        sa.Numeric(12, 2), nullable=True
    )
    currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="created")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
