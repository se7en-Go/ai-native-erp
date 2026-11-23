"""Channel daily traffic & ad-spend metrics table for multi-platform analytics.

Revision ID: 0016_channel_analytics_metrics
Revises: 0015_inventory_sync_outbox
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_channel_analytics_metrics"
down_revision: str | None = "0015_inventory_sync_outbox"
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
        CREATE TABLE channel_daily_metrics (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            channel_account_id UUID NOT NULL REFERENCES channel_accounts(id) ON DELETE CASCADE,
            variant_id UUID REFERENCES product_variants(id) ON DELETE SET NULL,
            metric_date DATE NOT NULL,
            impressions INTEGER NOT NULL DEFAULT 0 CHECK (impressions >= 0),
            clicks INTEGER NOT NULL DEFAULT 0 CHECK (clicks >= 0),
            ad_spend_base NUMERIC(14, 2) NOT NULL DEFAULT 0.00 CHECK (ad_spend_base >= 0),
            external_sessions INTEGER NOT NULL DEFAULT 0 CHECK (external_sessions >= 0),
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_channel_daily_metrics_scope
        ON channel_daily_metrics (
            tenant_id,
            channel_account_id,
            COALESCE(variant_id, '00000000-0000-0000-0000-000000000000'::uuid),
            metric_date
        )
    """)
    op.create_index(
        "ix_channel_daily_metrics_lookup",
        "channel_daily_metrics",
        ["tenant_id", "metric_date", "channel_account_id"],
    )
    _apply_rls("channel_daily_metrics")


def downgrade() -> None:
    op.drop_index("ix_channel_daily_metrics_lookup", table_name="channel_daily_metrics")
    op.execute("DROP INDEX IF EXISTS uq_channel_daily_metrics_scope")
    op.drop_table("channel_daily_metrics")
