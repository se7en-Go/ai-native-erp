"""Add AI auto-reply/profiling columns, sourcing_drafts, and AP payables tables."""

from alembic import op

revision = "0012_ai_autoreply_sourcing_ap"
down_revision = "0011_pack_a_wms_warehouses"
branch_labels = None
depends_on = None

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


def upgrade() -> None:
    # 1. Per-conversation AI auto-reply toggle + AI CRM customer profile storage
    op.execute(
        "ALTER TABLE conversations "
        "ADD COLUMN IF NOT EXISTS ai_auto_reply_enabled BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE customers "
        "ADD COLUMN IF NOT EXISTS ai_profile JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE customers "
        "ADD COLUMN IF NOT EXISTS ai_profile_updated_at TIMESTAMPTZ"
    )

    # 2. Miaoshou-style product sourcing & multi-platform listing drafts (FORCE RLS)
    op.execute("""
        CREATE TABLE sourcing_drafts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            source_platform TEXT NOT NULL DEFAULT '1688',
            source_url TEXT,
            raw_title TEXT NOT NULL,
            raw_price_cny NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
            fx_rate_usd_cny NUMERIC(12, 6) NOT NULL DEFAULT 7.200000,
            target_margin_pct NUMERIC(8, 2) NOT NULL DEFAULT 35.00,
            optimized_title_en TEXT NOT NULL,
            bullet_points JSONB NOT NULL DEFAULT '[]'::jsonb,
            suggested_sku TEXT NOT NULL,
            suggested_category TEXT NOT NULL DEFAULT 'General Export',
            suggested_hs_code TEXT,
            target_price_usd NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
            moq_tiers JSONB NOT NULL DEFAULT '[]'::jsonb,
            images JSONB NOT NULL DEFAULT '[]'::jsonb,
            attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'draft',
            imported_product_id UUID,
            imported_variant_id UUID,
            publish_history JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_by_user_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_sourcing_drafts_status
                CHECK (status IN ('draft', 'imported', 'published'))
        )
    """)
    op.create_index(
        "ix_sourcing_drafts_tenant_created",
        "sourcing_drafts",
        ["tenant_id", "created_at"],
    )
    _apply_rls("sourcing_drafts")

    # 3. Supplier Accounts Payable (AP) ledger closing Finance AR + AP loop (FORCE RLS)
    op.execute("""
        CREATE TABLE payables (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            po_id UUID REFERENCES purchase_orders(id) ON DELETE SET NULL,
            supplier_id UUID REFERENCES suppliers(id) ON DELETE SET NULL,
            po_no TEXT NOT NULL DEFAULT '',
            supplier_name TEXT NOT NULL DEFAULT '',
            due_amount NUMERIC(14, 2) NOT NULL,
            paid_amount NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
            currency CHAR(3) NOT NULL DEFAULT 'CNY',
            due_date DATE,
            status TEXT NOT NULL DEFAULT 'open',
            payment_ref TEXT,
            paid_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_payables_status
                CHECK (status IN ('open', 'partial', 'paid')),
            CONSTRAINT ck_payables_non_negative
                CHECK (due_amount >= 0 AND paid_amount >= 0)
        )
    """)
    op.create_index("ix_payables_tenant_status", "payables", ["tenant_id", "status"])
    op.create_index("ix_payables_tenant_po", "payables", ["tenant_id", "po_id"])
    _apply_rls("payables")


def downgrade() -> None:
    op.drop_table("payables")
    op.drop_table("sourcing_drafts")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS ai_profile_updated_at")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS ai_profile")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS ai_auto_reply_enabled")
