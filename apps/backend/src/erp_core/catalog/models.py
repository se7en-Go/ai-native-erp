from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from erp_core.shared.db import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "spu_code", name="uq_products_tenant_spu"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    spu_code: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    category: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    hs_code: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    origin_country: Mapped[str] = mapped_column(sa.CHAR(2), nullable=False, default="CN")
    i18n_specs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    certifications: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text()), nullable=False, default=list
    )
    ai_faq_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class ProductVariant(Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "sku", name="uq_product_variants_tenant_sku"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    product_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    sku: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cost_price_base: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    base_currency: Mapped[str] = mapped_column(sa.CHAR(3), nullable=False, default="CNY")
    min_margin_floor_pct: Mapped[Decimal] = mapped_column(
        sa.Numeric(5, 2), nullable=False, default=Decimal("15.00")
    )
    retail_price_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    b2b_moq_tiers: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    weight_grams: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    carton_dims_cm: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    lead_time_days: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=7)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class PlatformSkuMapping(Base):
    __tablename__ = "platform_sku_mappings"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id", "channel_account_id", "external_sku", name="uq_sku_map_channel_ext"
        ),
        sa.CheckConstraint("bundle_multiplier >= 1", name="chk_bundle_multiplier_positive"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )
    variant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("product_variants.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("channel_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_product_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    external_sku: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    bundle_multiplier: Mapped[int] = mapped_column(sa.Integer(), nullable=False, default=1)
    sync_inventory_enabled: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
