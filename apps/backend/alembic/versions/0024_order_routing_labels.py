"""Order routing / anti-loss rules, routing evaluations, and carrier waybill labels.

Three tenant-scoped tables, all fail-closed under PG17 ``FORCE ROW LEVEL SECURITY``:

1. ``order_routing_rules`` - ordered (``priority``) condition/action pairs that decide
   which warehouse/carrier an order is routed to and when an order must be held for
   review (for example when the estimated contribution margin goes negative).
2. ``order_routing_evaluations`` - the append-only audit trail of one routing decision
   per order, including the matched rules, the recommendation, and the hold state.
3. ``carrier_shipping_labels`` - booked 4x6 thermal waybills (YunExpress / 4PX / DHL /
   UPS / FedEx / sandbox) with the deterministic tracking number, waybill number,
   shipping cost estimate, and the object-storage key of the rendered PDF.

Revision ID: 0024_order_routing_labels
Revises: 0023_tenant_mcp_tokens
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_order_routing_labels"
down_revision: str | None = "0023_tenant_mcp_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"

_RULES = "order_routing_rules"
_EVALUATIONS = "order_routing_evaluations"
_LABELS = "carrier_shipping_labels"


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


def upgrade() -> None:
    op.execute("""
        CREATE TABLE order_routing_rules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            priority INT NOT NULL DEFAULT 100,
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            conditions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            actions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id)
        )
    """)
    op.create_index(
        "ix_order_routing_rules_tenant_priority",
        _RULES,
        ["tenant_id", "priority"],
    )

    op.execute("""
        CREATE TABLE order_routing_evaluations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            matched_rule_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            recommended_warehouse_id UUID REFERENCES warehouses(id) ON DELETE SET NULL,
            recommended_carrier TEXT,
            recommended_service TEXT,
            estimated_margin_pct NUMERIC(8,4),
            is_held BOOLEAN NOT NULL DEFAULT false,
            hold_reason TEXT,
            hold_released_by UUID REFERENCES users(id) ON DELETE SET NULL,
            hold_released_at TIMESTAMPTZ,
            applied_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
            evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id)
        )
    """)
    op.create_index(
        "ix_order_routing_evaluations_tenant_order",
        _EVALUATIONS,
        ["tenant_id", "order_id", "evaluated_at"],
    )
    op.create_index(
        "ix_order_routing_evaluations_tenant_held",
        _EVALUATIONS,
        ["tenant_id", "is_held"],
    )

    op.execute("""
        CREATE TABLE carrier_shipping_labels (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            shipment_id UUID REFERENCES shipments(id) ON DELETE SET NULL,
            carrier_code TEXT NOT NULL
                CHECK (carrier_code IN
                       ('yunexpress', '4px', 'dhl', 'ups', 'fedex', 'sandbox_carrier')),
            service_code TEXT NOT NULL,
            tracking_number TEXT NOT NULL,
            waybill_number TEXT NOT NULL,
            label_format TEXT NOT NULL DEFAULT 'pdf_4x6',
            label_r2_key TEXT,
            mode TEXT NOT NULL DEFAULT 'sandbox'
                CHECK (mode IN ('live', 'sandbox')),
            shipping_cost_estimate NUMERIC(12,2),
            currency CHAR(3) NOT NULL DEFAULT 'USD',
            status TEXT NOT NULL DEFAULT 'created'
                CHECK (status IN ('created', 'cancelled')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id)
        )
    """)
    op.create_index(
        "ix_carrier_shipping_labels_tenant_order",
        _LABELS,
        ["tenant_id", "order_id", "created_at"],
    )
    op.create_index(
        "ix_carrier_shipping_labels_tenant_tracking",
        _LABELS,
        ["tenant_id", "tracking_number"],
    )

    for table in (_RULES, _EVALUATIONS, _LABELS):
        _apply_rls(table)


def downgrade() -> None:
    op.drop_table(_LABELS)
    op.drop_table(_EVALUATIONS)
    op.drop_table(_RULES)
