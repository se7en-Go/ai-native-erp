"""Add Phase 3A CRM, procurement and warehouse tables."""

from alembic import op

revision = "0005_phase3a_crm_supply_wms"
down_revision = "0004"
branch_labels = None
depends_on = None

_TABLES = (
    "customer_followups",
    "suppliers",
    "purchase_orders",
    "purchase_order_items",
    "wms_movements",
    "task_reminders",
)
_REFERENCED = ("customers", "opportunities", "product_variants")
_SCOPE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"


def upgrade() -> None:
    # Existing row IDs remain unchanged. Composite keys prevent cross-tenant references even
    # when PostgreSQL bypasses RLS internally to validate a foreign key.
    for table in _REFERENCED:
        op.create_unique_constraint(f"uq_{table}_tenant_id", table, ["tenant_id", "id"])
    op.execute("""
        CREATE TABLE customer_followups (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            customer_id UUID NOT NULL,
            opportunity_id UUID,
            author_user_id UUID NOT NULL REFERENCES users(id),
            followup_type TEXT NOT NULL CHECK (followup_type IN ('email','call','meeting','note')),
            summary TEXT NOT NULL,
            next_action_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, customer_id) REFERENCES customers(tenant_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, opportunity_id) REFERENCES opportunities(tenant_id, id)
                ON DELETE SET NULL (opportunity_id)
        )
    """)
    op.execute("""
        CREATE TABLE suppliers (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            contact_name TEXT,
            contact_email TEXT,
            contact_phone TEXT,
            country CHAR(2),
            lead_time_days INT NOT NULL DEFAULT 0 CHECK (lead_time_days >= 0),
            payment_terms TEXT,
            rating NUMERIC(3,2) CHECK (rating BETWEEN 0 AND 5),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, code)
        )
    """)
    op.execute("""
        CREATE TABLE purchase_orders (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            po_no TEXT NOT NULL,
            supplier_id UUID NOT NULL,
            warehouse_id UUID NOT NULL,
            currency CHAR(3) NOT NULL,
            total_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (total_amount >= 0),
            status TEXT NOT NULL DEFAULT 'draft' CHECK (
                status IN ('draft','ordered','partially_received','received','cancelled')
            ),
            expected_arrival_date DATE,
            notes TEXT,
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, po_no),
            FOREIGN KEY (tenant_id, supplier_id) REFERENCES suppliers(tenant_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE purchase_order_items (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            purchase_order_id UUID NOT NULL,
            variant_id UUID NOT NULL,
            sku TEXT NOT NULL,
            qty_ordered INT NOT NULL CHECK (qty_ordered > 0),
            qty_received INT NOT NULL DEFAULT 0 CHECK (qty_received BETWEEN 0 AND qty_ordered),
            unit_cost NUMERIC(14,4) NOT NULL CHECK (unit_cost >= 0),
            UNIQUE (tenant_id, purchase_order_id, variant_id),
            FOREIGN KEY (tenant_id, purchase_order_id) REFERENCES purchase_orders(tenant_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, variant_id) REFERENCES product_variants(tenant_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE wms_movements (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            movement_no TEXT NOT NULL,
            warehouse_id UUID NOT NULL,
            variant_id UUID NOT NULL,
            movement_type TEXT NOT NULL CHECK (
                movement_type IN ('inbound_po','outbound_order','transfer','stocktake_adjust')
            ),
            qty_delta INT NOT NULL CHECK (qty_delta <> 0),
            reference_type TEXT,
            reference_id UUID,
            operator_user_id UUID REFERENCES users(id),
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, movement_no),
            FOREIGN KEY (tenant_id, variant_id) REFERENCES product_variants(tenant_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE task_reminders (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            due_at TIMESTAMPTZ,
            priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high')),
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
            linked_entity_type TEXT,
            linked_entity_id UUID,
            assignee_user_id UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK ((linked_entity_type IS NULL) = (linked_entity_id IS NULL))
        )
    """)
    for table, columns in (
        ("customer_followups", ["tenant_id", "customer_id", "created_at"]),
        ("customer_followups", ["tenant_id", "opportunity_id"]),
        ("purchase_orders", ["tenant_id", "supplier_id"]),
        ("purchase_order_items", ["tenant_id", "variant_id"]),
        ("wms_movements", ["tenant_id", "variant_id", "created_at"]),
        ("wms_movements", ["tenant_id", "created_at"]),
        ("task_reminders", ["tenant_id", "status", "due_at"]),
    ):
        op.create_index(f"ix_{table}_{columns[1]}", table, columns)
    for table in _TABLES:
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


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
    for table in reversed(_REFERENCED):
        op.drop_constraint(f"uq_{table}_tenant_id", table, type_="unique")
