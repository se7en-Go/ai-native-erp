"""Add Phase 3B AI digital employees, quick replies and creative studio assets."""

from alembic import op

revision = "0006_phase3b_ai_employees"
down_revision = "0005_phase3a_crm_supply_wms"
branch_labels = None
depends_on = None

_TABLES = ("ai_employees", "quick_reply_templates", "ai_creative_assets")
_SCOPE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ai_employees (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            code TEXT NOT NULL CHECK (length(btrim(code)) > 0),
            display_name TEXT NOT NULL CHECK (length(btrim(display_name)) > 0),
            role_title TEXT NOT NULL,
            department TEXT NOT NULL,
            avatar_emoji TEXT,
            description TEXT NOT NULL,
            capability_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
            autonomy_level TEXT NOT NULL DEFAULT 'L1_proposal'
                CONSTRAINT ck_ai_employees_autonomy_level CHECK (
                    autonomy_level IN ('L0_copilot','L1_proposal','L2_auto')
                ),
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            tool_count INT NOT NULL DEFAULT 0 CHECK (tool_count >= 0),
            system_persona TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, code),
            CHECK (jsonb_typeof(capability_codes) = 'array')
        )
    """)
    op.execute("""
        CREATE TABLE quick_reply_templates (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            title TEXT NOT NULL CHECK (length(btrim(title)) > 0),
            category TEXT NOT NULL,
            language TEXT NOT NULL CHECK (language IN ('zh','en')),
            body_text TEXT NOT NULL CHECK (length(btrim(body_text)) > 0),
            shortcut_code TEXT NOT NULL CHECK (length(btrim(shortcut_code)) > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, shortcut_code)
        )
    """)
    op.execute("""
        CREATE TABLE ai_creative_assets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            asset_type TEXT NOT NULL CONSTRAINT ck_ai_creative_assets_asset_type CHECK (
                asset_type IN (
                    'product_copy','listing','email_template','social_caption','ad_hook'
                )
            ),
            title TEXT NOT NULL CHECK (length(btrim(title)) > 0),
            language TEXT NOT NULL,
            sku_ref TEXT,
            prompt TEXT,
            content_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
                CONSTRAINT ck_ai_creative_assets_status CHECK (
                    status IN ('draft','approved','archived')
                ),
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id)
        )
    """)
    for table, columns in (
        ("ai_employees", ["tenant_id", "is_enabled", "autonomy_level"]),
        ("quick_reply_templates", ["tenant_id", "category", "language"]),
        ("ai_creative_assets", ["tenant_id", "asset_type", "status", "created_at"]),
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
