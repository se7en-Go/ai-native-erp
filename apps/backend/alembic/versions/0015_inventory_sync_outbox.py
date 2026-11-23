"""Transactional multi-platform inventory sync outbox table and RLS policies.

Revision ID: 0015_inventory_sync_outbox
Revises: 0014_multiplatform_media
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_inventory_sync_outbox"
down_revision: str | None = "0014_multiplatform_media"
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
        CREATE TABLE inventory_sync_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            variant_id UUID NOT NULL REFERENCES product_variants(id) ON DELETE CASCADE,
            mapping_id UUID NOT NULL REFERENCES platform_sku_mappings(id) ON DELETE CASCADE,
            channel_account_id UUID NOT NULL REFERENCES channel_accounts(id) ON DELETE CASCADE,
            channel_type TEXT NOT NULL,
            external_sku TEXT NOT NULL,
            requested_qty INTEGER NOT NULL CHECK (requested_qty >= 0),
            synced_qty INTEGER,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'succeeded', 'failed', 'dead_letter')),
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            error_code TEXT,
            error_detail TEXT,
            trigger_source TEXT NOT NULL DEFAULT 'manual',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.create_index(
        "ix_inv_sync_jobs_poll",
        "inventory_sync_jobs",
        ["tenant_id", "status", "next_retry_at"],
    )
    op.create_index(
        "ix_inv_sync_jobs_variant",
        "inventory_sync_jobs",
        ["tenant_id", "variant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "uq_inv_sync_jobs_pending_mapping",
        "inventory_sync_jobs",
        ["tenant_id", "mapping_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    _apply_rls("inventory_sync_jobs")


def downgrade() -> None:
    op.drop_index("uq_inv_sync_jobs_pending_mapping", table_name="inventory_sync_jobs")
    op.drop_index("ix_inv_sync_jobs_variant", table_name="inventory_sync_jobs")
    op.drop_index("ix_inv_sync_jobs_poll", table_name="inventory_sync_jobs")
    op.drop_table("inventory_sync_jobs")
