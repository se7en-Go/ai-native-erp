"""Sourcing visual-AI jobs and read-only ads spend synchronisation.

Three tenant-scoped tables, each forced through PG17 row level security so the
`app_runtime` role can never read across tenants even if a query forgets a
`WHERE tenant_id = ...` predicate:

1. `sourcing_image_jobs` - one row per "去白底 / 换场景 / 图片文字翻译" request
   against a catalog variant, carrying the provider that rendered it and the
   resulting image URL (a presigned R2/MinIO URL, or a deterministic
   `data:image/png;base64,...` preview when object storage is not attached).
2. `ad_account_connections` - Meta Ads / Amazon Ads / sandbox ad accounts whose
   OAuth or API token is stored **only** as an AES-256-GCM envelope bound to
   `AAD = f"{tenant_id}:{id}:ads_oauth"`.
3. `ad_spend_daily_snapshots` - the read-only daily spend / attributed-sales /
   total-store-sales facts pulled from those accounts, with the exact
   `NUMERIC(14, 2)` money columns ROAS and TACoS are computed from.

No hard foreign key is created against `kb_documents` or any other `0004`
direct-sourced table, so `0004` keeps its standalone downgrade round-trip.
Safe FK targets only: `tenants(id)`, `users(id)`, `product_variants(id)`.

Revision ID: 0027_visual_jobs_and_ads_sync
Revises: 0026_bom_and_in_transit_doc
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027_visual_jobs_and_ads_sync"
down_revision: str | None = "0026_bom_and_in_transit_doc"
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
        CREATE TABLE sourcing_image_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            variant_id UUID REFERENCES product_variants(id) ON DELETE SET NULL,
            provider TEXT NOT NULL DEFAULT 'sandbox',
            CONSTRAINT ck_sourcing_image_jobs_provider
                CHECK (provider IN ('sandbox', 'remove_bg', 'openai_image')),
            operation TEXT NOT NULL,
            CONSTRAINT ck_sourcing_image_jobs_operation
                CHECK (operation IN ('remove_bg', 'scene_replace', 'translate_overlay')),
            source_image_url TEXT NOT NULL,
            target_locale TEXT,
            prompt TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            CONSTRAINT ck_sourcing_image_jobs_status
                CHECK (status IN ('queued', 'processing', 'completed', 'failed')),
            output_image_url TEXT,
            output_mime_type TEXT,
            output_r2_key TEXT,
            error_message TEXT,
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ
        )
    """)
    op.create_index(
        "ix_sourcing_image_jobs_tenant_created",
        "sourcing_image_jobs",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_sourcing_image_jobs_tenant_variant",
        "sourcing_image_jobs",
        ["tenant_id", "variant_id"],
    )
    _apply_rls("sourcing_image_jobs")

    op.execute("""
        CREATE TABLE ad_account_connections (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            platform TEXT NOT NULL,
            CONSTRAINT ck_ad_account_connections_platform
                CHECK (platform IN ('meta_ads', 'amazon_ads', 'sandbox_ads')),
            account_name TEXT NOT NULL,
            external_account_id TEXT NOT NULL,
            -- AES-256-GCM envelope bound to AAD tenant_id + account id + ads_oauth.
            -- The plaintext token never reaches this table and is never returned
            -- by any API response.
            encrypted_credentials_b64 TEXT NOT NULL,
            currency CHAR(3) NOT NULL DEFAULT 'USD',
            status TEXT NOT NULL DEFAULT 'active',
            CONSTRAINT ck_ad_account_connections_status
                CHECK (status IN ('active', 'paused', 'error')),
            last_synced_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_ad_account_connections_tenant_platform_ext
                UNIQUE (tenant_id, platform, external_account_id)
        )
    """)
    op.create_index(
        "ix_ad_account_connections_tenant_platform",
        "ad_account_connections",
        ["tenant_id", "platform"],
    )
    _apply_rls("ad_account_connections")

    op.execute("""
        CREATE TABLE ad_spend_daily_snapshots (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            ad_account_id UUID NOT NULL
                REFERENCES ad_account_connections(id) ON DELETE CASCADE,
            spend_date DATE NOT NULL,
            campaign_name TEXT NOT NULL,
            sku TEXT,
            impressions INT NOT NULL DEFAULT 0,
            CONSTRAINT ck_ad_spend_daily_snapshots_impressions CHECK (impressions >= 0),
            clicks INT NOT NULL DEFAULT 0,
            CONSTRAINT ck_ad_spend_daily_snapshots_clicks CHECK (clicks >= 0),
            ad_spend NUMERIC(14, 2) NOT NULL,
            CONSTRAINT ck_ad_spend_daily_snapshots_ad_spend CHECK (ad_spend >= 0),
            attributed_sales NUMERIC(14, 2) NOT NULL DEFAULT 0,
            CONSTRAINT ck_ad_spend_daily_snapshots_attributed
                CHECK (attributed_sales >= 0),
            total_store_sales NUMERIC(14, 2) NOT NULL DEFAULT 0,
            CONSTRAINT ck_ad_spend_daily_snapshots_total_sales
                CHECK (total_store_sales >= 0),
            currency CHAR(3) NOT NULL DEFAULT 'USD',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_ad_spend_daily_snapshots_grain
                UNIQUE (tenant_id, ad_account_id, spend_date, campaign_name)
        )
    """)
    op.create_index(
        "ix_ad_spend_daily_snapshots_tenant_date",
        "ad_spend_daily_snapshots",
        ["tenant_id", "spend_date"],
    )
    _apply_rls("ad_spend_daily_snapshots")


def downgrade() -> None:
    op.drop_index(
        "ix_ad_spend_daily_snapshots_tenant_date",
        table_name="ad_spend_daily_snapshots",
    )
    op.drop_table("ad_spend_daily_snapshots")

    op.drop_index(
        "ix_ad_account_connections_tenant_platform",
        table_name="ad_account_connections",
    )
    op.drop_table("ad_account_connections")

    op.drop_index(
        "ix_sourcing_image_jobs_tenant_variant",
        table_name="sourcing_image_jobs",
    )
    op.drop_index(
        "ix_sourcing_image_jobs_tenant_created",
        table_name="sourcing_image_jobs",
    )
    op.drop_table("sourcing_image_jobs")
