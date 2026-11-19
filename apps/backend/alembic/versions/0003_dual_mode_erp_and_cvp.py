"""Dual-mode ERP tables, inventory ledger, 17TRACK shipments, and CVP profit snapshots.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19 02:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002_channels_identity_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_ERP_TABLES = [
    "products",
    "product_variants",
    "platform_sku_mappings",
    "leads",
    "opportunities",
    "quotes",
    "quote_items",
    "orders",
    "order_items",
    "shipments",
    "inventory_balances",
    "inventory_ledger",
    "receivables_payables",
    "payment_records",
    "order_profit_snapshots",
]


def _apply_tenant_rls(table_name: str, append_only: bool = False) -> None:
    """Fail-closed tenant isolation: `ENABLE` + `FORCE` RLS with a RESTRICTIVE policy."""
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")

    runtime_role_present = bool(
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime'"))
        .scalar()
    )
    if not runtime_role_present:
        return

    tenant_predicate = (
        "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
    )
    op.execute(
        f"CREATE POLICY tenant_allow_runtime ON {table_name} AS PERMISSIVE "
        "FOR ALL TO app_runtime USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"CREATE POLICY tenant_isolation_policy ON {table_name} AS RESTRICTIVE "
        f"FOR ALL TO app_runtime USING ({tenant_predicate}) "
        f"WITH CHECK ({tenant_predicate})"
    )
    if append_only:
        op.execute(f"REVOKE ALL ON {table_name} FROM app_runtime")
        op.execute(f"GRANT SELECT, INSERT ON {table_name} TO app_runtime")
    else:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table_name} TO app_runtime")


def upgrade() -> None:
    # 1. products
    op.create_table(
        "products",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("spu_code", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("hs_code", sa.Text(), nullable=True),
        sa.Column("origin_country", sa.CHAR(2), server_default=sa.text("'CN'"), nullable=False),
        sa.Column(
            "i18n_specs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "certifications",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "ai_faq_facts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "spu_code", name="uq_products_tenant_spu"),
    )
    op.create_index("idx_products_tenant_category", "products", ["tenant_id", "category"])

    # 2. product_variants
    op.create_table(
        "product_variants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("cost_price_base", sa.Numeric(12, 4), nullable=False),
        sa.Column("base_currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column(
            "min_margin_floor_pct",
            sa.Numeric(5, 2),
            server_default=sa.text("15.00"),
            nullable=False,
        ),
        sa.Column("retail_price_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "b2b_moq_tiers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("weight_grams", sa.Integer(), nullable=False),
        sa.Column("carton_dims_cm", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), server_default=sa.text("7"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "sku", name="uq_product_variants_tenant_sku"),
    )
    op.create_index(
        "idx_product_variants_tenant_product", "product_variants", ["tenant_id", "product_id"]
    )

    # 3. platform_sku_mappings
    op.create_table(
        "platform_sku_mappings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_product_id", sa.Text(), nullable=False),
        sa.Column("external_sku", sa.Text(), nullable=False),
        sa.Column("bundle_multiplier", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "sync_inventory_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("bundle_multiplier >= 1", name="chk_bundle_multiplier_positive"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["channel_account_id"], ["channel_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "channel_account_id", "external_sku", name="uq_sku_map_channel_ext"
        ),
    )

    # 4. leads
    op.create_table(
        "leads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_channel", sa.Text(), nullable=False),
        sa.Column("utm_campaign", sa.Text(), nullable=True),
        sa.Column("lead_type", sa.Text(), nullable=False),
        sa.Column("qualification_score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "extracted_requirements",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lead_type IN ('b2b_inquiry', 'b2c_vip_pre_sale')", name="chk_leads_lead_type"
        ),
        sa.CheckConstraint(
            "status IN ('new', 'contacted', 'qualified', 'disqualified', 'converted')",
            name="chk_leads_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_leads_tenant_status", "leads", ["tenant_id", "status", "created_at"])

    # 5. opportunities
    op.create_table(
        "opportunities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column(
            "expected_amount", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'USD'"), nullable=False),
        sa.Column("expected_close_date", sa.Date(), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stage IN ('discovery', 'sampling', 'quoting', 'negotiating', "
            "'closed_won', 'closed_lost')",
            name="chk_opportunities_stage",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["b2b_companies.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_opportunities_tenant_stage", "opportunities", ["tenant_id", "stage"])

    # 6. quotes
    op.create_table(
        "quotes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opportunity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_no", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "is_proforma_invoice", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("incoterm", sa.Text(), nullable=False),
        sa.Column("port_of_loading", sa.Text(), nullable=True),
        sa.Column("port_of_destination", sa.Text(), nullable=True),
        sa.Column("payment_terms", sa.Text(), nullable=False),
        sa.Column("base_currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column("quote_currency", sa.CHAR(3), server_default=sa.text("'USD'"), nullable=False),
        sa.Column("fx_rate_snapshot", sa.Numeric(12, 6), nullable=False),
        sa.Column("fx_locked_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("freight_fee", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("insurance_fee", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "customs_duty_estimate", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("pdf_r2_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("converted_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "incoterm IN ('EXW', 'FOB', 'CIF', 'DDP', 'DAP')", name="chk_quotes_incoterm"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'sent', 'accepted', 'expired', 'converted')",
            name="chk_quotes_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "quote_no", "version", name="uq_quotes_tenant_no_version"),
    )
    op.create_index("idx_quotes_tenant_opp", "quotes", ["tenant_id", "opportunity_id", "version"])

    # 7. quote_items
    op.create_table(
        "quote_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku_snapshot", sa.Text(), nullable=False),
        sa.Column("description_i18n", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("moq_tier_applied", sa.Integer(), nullable=True),
        sa.Column("unit_price_quote_ccy", sa.Numeric(12, 4), nullable=False),
        sa.Column("unit_cost_base_snapshot", sa.Numeric(12, 4), nullable=False),
        sa.Column("line_total_quote_ccy", sa.Numeric(14, 2), nullable=False),
        sa.CheckConstraint("quantity > 0", name="chk_quote_items_qty_positive"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_quote_items_tenant_quote", "quote_items", ["tenant_id", "quote_id"])

    # 8. orders
    op.create_table(
        "orders",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_no", sa.Text(), nullable=False),
        sa.Column("order_mode", sa.Text(), nullable=False),
        sa.Column("channel_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_order_id", sa.Text(), nullable=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("fx_rate_to_base", sa.Numeric(12, 6), nullable=False),
        sa.Column("subtotal_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "shipping_charged", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "discount_amount", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("tax_collected", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("fulfillment_status", sa.Text(), nullable=False),
        sa.Column("payment_status", sa.Text(), nullable=False),
        sa.Column(
            "channel_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "order_mode IN ('b2c_retail', 'b2b_sample', 'b2b_wholesale')", name="chk_orders_mode"
        ),
        sa.CheckConstraint(
            "fulfillment_status IN ('pending_payment', 'confirmed_reserved', "
            "'in_production', 'partially_shipped', 'shipped', 'delivered', "
            "'exception', 'cancelled', 'refunded')",
            name="chk_orders_fulfillment_status",
        ),
        sa.CheckConstraint(
            "payment_status IN ('unpaid', 'deposit_paid', 'partially_paid', 'paid', 'refunded')",
            name="chk_orders_payment_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["channel_account_id"], ["channel_accounts.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "order_no", name="uq_orders_tenant_order_no"),
    )
    op.create_index(
        "idx_orders_tenant_created", "orders", ["tenant_id", sa.text("created_at DESC")]
    )
    op.create_index(
        "uq_orders_tenant_channel_ext",
        "orders",
        ["tenant_id", "channel_account_id", "external_order_id"],
        unique=True,
        postgresql_where=sa.text("external_order_id IS NOT NULL"),
    )

    # 9. order_items
    op.create_table(
        "order_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku_snapshot", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("unit_cost_base_snapshot", sa.Numeric(12, 4), nullable=False),
        sa.Column("line_total", sa.Numeric(14, 2), nullable=False),
        sa.CheckConstraint("quantity > 0", name="chk_order_items_qty_positive"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_order_items_tenant_order", "order_items", ["tenant_id", "order_id"])

    # 10. shipments
    op.create_table(
        "shipments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tracking_provider", sa.Text(), server_default=sa.text("'17track'"), nullable=False
        ),
        sa.Column("tracking_number", sa.Text(), nullable=False),
        sa.Column("carrier_code", sa.Text(), nullable=True),
        sa.Column("shipment_mode", sa.Text(), server_default=sa.text("'parcel'"), nullable=False),
        sa.Column(
            "standardized_status", sa.Text(), server_default=sa.text("'not_found'"), nullable=False
        ),
        sa.Column("sub_status", sa.Text(), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "tracking_events",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "actual_shipping_cost_base",
            sa.Numeric(12, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "exception_alert_sent", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "tracking_provider IN ('17track', 'aftership', 'manual')", name="chk_shipments_provider"
        ),
        sa.CheckConstraint(
            "shipment_mode IN ('parcel', 'air_freight', 'sea_lcl', 'sea_fcl')",
            name="chk_shipments_mode",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "tracking_number", name="uq_shipments_tenant_tracking"),
    )
    op.create_index(
        "idx_shipments_tenant_stalled",
        "shipments",
        ["tenant_id", "standardized_status", "last_event_at"],
    )

    # 11. inventory_balances
    op.create_table(
        "inventory_balances",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("on_hand_qty", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_qty", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("on_hand_qty >= 0", name="chk_inventory_on_hand_non_negative"),
        sa.CheckConstraint("reserved_qty >= 0", name="chk_inventory_reserved_non_negative"),
        sa.CheckConstraint("on_hand_qty >= reserved_qty", name="chk_inventory_non_negative"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"]),
        sa.PrimaryKeyConstraint("tenant_id", "warehouse_id", "variant_id"),
    )

    # 12. inventory_ledger (append-only)
    op.create_table(
        "inventory_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("qty_delta_on_hand", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("qty_delta_reserved", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reference_type", sa.Text(), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('inbound_po', 'reserve_order', 'release_cancel', "
            "'deduct_shipped', 'stocktake_adjust')",
            name="chk_inventory_ledger_event_type",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"]),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_inventory_ledger_idempotency"),
    )
    op.create_index(
        "idx_inventory_ledger_tenant_variant",
        "inventory_ledger",
        ["tenant_id", "warehouse_id", "variant_id"],
    )

    # 13. receivables_payables
    op.create_table(
        "receivables_payables",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("milestone_label", sa.Text(), nullable=False),
        sa.Column("due_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("paid_amount", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("direction IN ('receivable', 'payable')", name="chk_rp_direction"),
        sa.CheckConstraint(
            "status IN ('open', 'partial', 'settled', 'overdue', 'void')", name="chk_rp_status"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_rp_tenant_order", "receivables_payables", ["tenant_id", "order_id"])

    # 14. payment_records
    op.create_table(
        "payment_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rp_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_method", sa.Text(), nullable=False),
        sa.Column("external_tx_ref", sa.Text(), nullable=True),
        sa.Column("amount_received", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("actual_fx_rate", sa.Numeric(12, 6), nullable=False),
        sa.Column("gateway_fee", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("receipt_r2_key", sa.Text(), nullable=True),
        sa.Column("verified_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["rp_id"], ["receivables_payables.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_payment_records_tenant_rp", "payment_records", ["tenant_id", "rp_id"])

    # 15. order_profit_snapshots (with GENERATED ALWAYS AS STORED column)
    op.create_table(
        "order_profit_snapshots",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_currency", sa.CHAR(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column(
            "gross_revenue_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("cogs_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "platform_commission_base",
            sa.Numeric(14, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "payment_gateway_fee_base",
            sa.Numeric(14, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "shipping_and_duty_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "channel_msg_cost_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "ad_attribution_cost_base",
            sa.Numeric(14, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "fx_gain_loss_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "net_contribution_margin_base",
            sa.Numeric(14, 2),
            sa.Computed(
                "gross_revenue_base - cogs_base - platform_commission_base "
                "- payment_gateway_fee_base - shipping_and_duty_base "
                "- channel_msg_cost_base - ad_attribution_cost_base + fx_gain_loss_base",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "recalculated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("order_id"),
    )
    op.create_index(
        "idx_order_profit_snapshots_tenant",
        "order_profit_snapshots",
        ["tenant_id", "recalculated_at"],
    )

    for table in TENANT_ERP_TABLES:
        _apply_tenant_rls(table, append_only=(table == "inventory_ledger"))


def downgrade() -> None:
    for table in reversed(TENANT_ERP_TABLES):
        op.drop_table(table)
