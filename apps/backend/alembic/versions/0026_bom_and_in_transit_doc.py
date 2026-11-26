"""1-level light BOM, in-transit headhaul stock transfers, and DOC replenishment.

Three tenant-scoped tables, each forced through PG17 row level security so the
`app_runtime` role can never read across tenants even if a query forgets a
`WHERE tenant_id = ...` predicate:

1. `bom_components` - a strictly one-level light bill of materials: a parent
   (kit) SKU consumes a component SKU at `qty_per_unit` plus a `waste_rate_pct`
   allowance. The `parent_variant_id <> component_variant_id` CHECK and the
   service-level multi-level guard keep the graph exactly one hop deep, which is
   what a factory kit actually is: a bundle of finished goods, never a nested
   assembly tree.
2. `stock_transfers` + `stock_transfer_lines` - the first-leg ("headhaul")
   container/air transfer between the domestic factory warehouse and the
   overseas / FBA warehouse. The stock leaves the source warehouse on dispatch
   and only lands in the destination warehouse on receipt, so the units are
   *in transit* (not sellable, not yet in the destination) for the whole
   ocean/air leg.
3. Days-of-cover (DOC) replenishment is computed from existing tables only
   (`inventory_balances`, `warehouses`, `orders`, `order_items`,
   `stock_transfer_lines`); it adds no table of its own.

Revision ID: 0026_bom_and_in_transit_doc
Revises: 0025_team_ops_kb_gaps
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026_bom_and_in_transit_doc"
down_revision: str | None = "0025_team_ops_kb_gaps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"


def _apply_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_allow_runtime ON {table} AS PERMISSIVE "
        "FOR ALL TO app_runtime USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"CREATE POLICY tenant_isolation_policy ON {table} AS RESTRICTIVE "
        f"FOR ALL TO app_runtime USING ({_SCOPE}) WITH CHECK ({_SCOPE})"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_runtime")


_MOVEMENT_TYPES_SQL = "'inbound_po', 'outbound_order', 'transfer', 'stocktake_adjust'"
_MOVEMENT_TYPES_WITH_TRANSFERS_SQL = (
    "'inbound_po', 'outbound_order', 'transfer', 'transfer_out', 'transfer_in', "
    "'stocktake_adjust'"
)


def upgrade() -> None:
    # A headhaul dispatch/receipt is a directional WMS movement, so the 0005
    # movement-type CHECK has to admit the two new transfer directions.
    op.execute("ALTER TABLE wms_movements DROP CONSTRAINT wms_movements_movement_type_check")
    op.execute(
        "ALTER TABLE wms_movements ADD CONSTRAINT wms_movements_movement_type_check "
        f"CHECK (movement_type IN ({_MOVEMENT_TYPES_WITH_TRANSFERS_SQL}))"
    )

    op.execute("""
        CREATE TABLE bom_components (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            parent_variant_id UUID NOT NULL REFERENCES product_variants(id) ON DELETE CASCADE,
            component_variant_id UUID NOT NULL REFERENCES product_variants(id) ON DELETE CASCADE,
            qty_per_unit NUMERIC(14, 4) NOT NULL,
            waste_rate_pct NUMERIC(6, 2) NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_bom_components_qty_per_unit CHECK (qty_per_unit > 0),
            CONSTRAINT ck_bom_components_waste_rate_pct
                CHECK (waste_rate_pct >= 0 AND waste_rate_pct <= 100),
            CONSTRAINT ck_bom_components_no_self_reference
                CHECK (parent_variant_id <> component_variant_id),
            CONSTRAINT uq_bom_components_tenant_parent_component
                UNIQUE (tenant_id, parent_variant_id, component_variant_id)
        )
    """)
    op.create_index(
        "ix_bom_components_tenant_parent",
        "bom_components",
        ["tenant_id", "parent_variant_id"],
    )
    op.create_index(
        "ix_bom_components_tenant_component",
        "bom_components",
        ["tenant_id", "component_variant_id"],
    )
    _apply_rls("bom_components")

    op.execute("""
        CREATE TABLE stock_transfers (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            transfer_no TEXT NOT NULL,
            source_warehouse_id UUID NOT NULL REFERENCES warehouses(id),
            destination_warehouse_id UUID NOT NULL REFERENCES warehouses(id),
            status TEXT NOT NULL DEFAULT 'draft',
            shipping_method TEXT NOT NULL DEFAULT 'sea_freight',
            tracking_no TEXT,
            eta_days INT NOT NULL DEFAULT 25,
            dispatched_at TIMESTAMPTZ,
            received_at TIMESTAMPTZ,
            notes TEXT,
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_stock_transfers_status
                CHECK (status IN ('draft', 'in_transit', 'received', 'cancelled')),
            CONSTRAINT ck_stock_transfers_shipping_method
                CHECK (shipping_method IN (
                    'sea_freight', 'air_freight', 'express', 'rail', 'truck'
                )),
            CONSTRAINT ck_stock_transfers_eta_days CHECK (eta_days >= 0),
            CONSTRAINT ck_stock_transfers_distinct_warehouses
                CHECK (source_warehouse_id <> destination_warehouse_id),
            CONSTRAINT uq_stock_transfers_tenant_transfer_no UNIQUE (tenant_id, transfer_no)
        )
    """)
    op.create_index(
        "ix_stock_transfers_tenant_status",
        "stock_transfers",
        ["tenant_id", "status", "created_at"],
    )
    _apply_rls("stock_transfers")

    op.execute("""
        CREATE TABLE stock_transfer_lines (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            transfer_id UUID NOT NULL REFERENCES stock_transfers(id) ON DELETE CASCADE,
            variant_id UUID NOT NULL REFERENCES product_variants(id) ON DELETE CASCADE,
            qty INT NOT NULL,
            received_qty INT NOT NULL DEFAULT 0,
            unit_freight_cost_base NUMERIC(12, 4) NOT NULL DEFAULT 0,
            CONSTRAINT ck_stock_transfer_lines_qty CHECK (qty > 0),
            CONSTRAINT ck_stock_transfer_lines_received_qty
                CHECK (received_qty >= 0 AND received_qty <= qty),
            CONSTRAINT ck_stock_transfer_lines_unit_freight_cost_base
                CHECK (unit_freight_cost_base >= 0),
            CONSTRAINT uq_stock_transfer_lines_tenant_transfer_variant
                UNIQUE (tenant_id, transfer_id, variant_id)
        )
    """)
    op.create_index(
        "ix_stock_transfer_lines_tenant_variant",
        "stock_transfer_lines",
        ["tenant_id", "variant_id"],
    )
    op.create_index(
        "ix_stock_transfer_lines_tenant_transfer",
        "stock_transfer_lines",
        ["tenant_id", "transfer_id"],
    )
    _apply_rls("stock_transfer_lines")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS stock_transfer_lines")
    op.execute("DROP TABLE IF EXISTS stock_transfers")
    op.execute("DROP TABLE IF EXISTS bom_components")
    # Restore the pre-0026 movement-type CHECK. `wms_movements` is FORCE RLS
    # protected, so the deleting role (the table owner, `app_migrator`) only sees
    # rows once FORCE is lifted - otherwise the DELETE would silently match
    # nothing and the narrower CHECK below would fail on the leftover rows.
    op.execute("ALTER TABLE wms_movements NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "DELETE FROM wms_movements WHERE movement_type IN ('transfer_out', 'transfer_in')"
    )
    op.execute("ALTER TABLE wms_movements FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE wms_movements DROP CONSTRAINT wms_movements_movement_type_check")
    op.execute(
        "ALTER TABLE wms_movements ADD CONSTRAINT wms_movements_movement_type_check "
        f"CHECK (movement_type IN ({_MOVEMENT_TYPES_SQL}))"
    )
