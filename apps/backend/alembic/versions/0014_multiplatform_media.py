"""Unified product media assets, multi-variant sourcing specs, and platform SKU sync columns.

Revision ID: 0014_multiplatform_media
Revises: 0013_wave2_security_finance
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_multiplatform_media"
down_revision: str | None = "0013_wave2_security_finance"
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


def upgrade() -> None:
    op.execute("""
        CREATE TABLE product_media_assets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            product_id UUID REFERENCES products(id) ON DELETE CASCADE,
            variant_id UUID REFERENCES product_variants(id) ON DELETE CASCADE,
            sourcing_draft_id UUID REFERENCES sourcing_drafts(id) ON DELETE CASCADE,
            storage_key TEXT NOT NULL,
            source_url TEXT,
            sha256 CHAR(64) NOT NULL,
            mime_type TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK (byte_size > 0),
            sort_order INTEGER NOT NULL DEFAULT 0,
            alt_text TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_product_media_tenant_key UNIQUE (tenant_id, storage_key)
        )
    """)
    op.create_index(
        "ix_product_media_tenant_product",
        "product_media_assets",
        ["tenant_id", "product_id"],
    )
    op.create_index(
        "ix_product_media_tenant_draft",
        "product_media_assets",
        ["tenant_id", "sourcing_draft_id"],
    )
    _apply_rls("product_media_assets")

    op.execute("""
        ALTER TABLE sourcing_drafts
        ADD COLUMN IF NOT EXISTS options_spec JSONB NOT NULL DEFAULT '[]'::jsonb,
        ADD COLUMN IF NOT EXISTS variants_spec JSONB NOT NULL DEFAULT '[]'::jsonb
    """)

    op.execute("""
        ALTER TABLE platform_sku_mappings
        ADD COLUMN IF NOT EXISTS external_variant_id TEXT,
        ADD COLUMN IF NOT EXISTS external_inventory_item_id TEXT,
        ADD COLUMN IF NOT EXISTS safety_stock INTEGER NOT NULL DEFAULT 0
            CHECK (safety_stock >= 0),
        ADD COLUMN IF NOT EXISTS last_inventory_synced_qty INTEGER,
        ADD COLUMN IF NOT EXISTS last_inventory_synced_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS last_sync_status TEXT NOT NULL DEFAULT 'idle',
        ADD COLUMN IF NOT EXISTS last_sync_error TEXT
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE platform_sku_mappings
        DROP COLUMN IF EXISTS last_sync_error,
        DROP COLUMN IF EXISTS last_sync_status,
        DROP COLUMN IF EXISTS last_inventory_synced_at,
        DROP COLUMN IF EXISTS last_inventory_synced_qty,
        DROP COLUMN IF EXISTS safety_stock,
        DROP COLUMN IF EXISTS external_inventory_item_id,
        DROP COLUMN IF EXISTS external_variant_id
    """)
    op.execute("""
        ALTER TABLE sourcing_drafts
        DROP COLUMN IF EXISTS variants_spec,
        DROP COLUMN IF EXISTS options_spec
    """)
    op.drop_index("ix_product_media_tenant_draft", table_name="product_media_assets")
    op.drop_index("ix_product_media_tenant_product", table_name="product_media_assets")
    op.drop_table("product_media_assets")
