"""Add Phase 3C compliant lead collection, prospect pool and outbound campaigns."""

from alembic import op

revision = "0007_phase3c_growth_leads"
down_revision = "0006_phase3b_ai_employees"
branch_labels = None
depends_on = None

_TABLES = (
    "lead_collection_jobs",
    "collected_prospects",
    "marketing_campaigns",
    "campaign_recipients",
)
_SCOPE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE lead_collection_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            source_provider TEXT NOT NULL
                CONSTRAINT ck_lead_collection_jobs_source_provider CHECK (
                    source_provider IN ('google_places','overpass_osm','overture_poi','csv_import')
                ),
            keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
            target_region TEXT,
            max_results INT NOT NULL DEFAULT 100
                CONSTRAINT ck_lead_collection_jobs_max_results CHECK (
                    max_results > 0 AND max_results <= 10000
                ),
            min_rating NUMERIC(3, 2)
                CONSTRAINT ck_lead_collection_jobs_min_rating CHECK (
                    min_rating >= 0 AND min_rating <= 5
                ),
            require_phone BOOLEAN NOT NULL DEFAULT false,
            status TEXT NOT NULL DEFAULT 'pending'
                CONSTRAINT ck_lead_collection_jobs_status CHECK (
                    status IN ('pending','running','completed','failed')
                ),
            collected_count INT NOT NULL DEFAULT 0
                CONSTRAINT ck_lead_collection_jobs_collected_count CHECK (collected_count >= 0),
            cleaned_count INT NOT NULL DEFAULT 0
                CONSTRAINT ck_lead_collection_jobs_cleaned_count CHECK (cleaned_count >= 0),
            synced_count INT NOT NULL DEFAULT 0
                CONSTRAINT ck_lead_collection_jobs_synced_count CHECK (synced_count >= 0),
            error_message TEXT,
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            UNIQUE (tenant_id, id),
            CHECK (jsonb_typeof(keywords) = 'array')
        )
    """)
    op.execute("""
        CREATE TABLE collected_prospects (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            job_id UUID REFERENCES lead_collection_jobs(id) ON DELETE CASCADE,
            company_name TEXT NOT NULL CHECK (length(btrim(company_name)) > 0),
            contact_name TEXT,
            phone_e164 TEXT,
            email TEXT,
            website TEXT,
            country TEXT,
            city TEXT,
            latitude DOUBLE PRECISION
                CONSTRAINT ck_collected_prospects_latitude CHECK (
                    latitude >= -90 AND latitude <= 90
                ),
            longitude DOUBLE PRECISION
                CONSTRAINT ck_collected_prospects_longitude CHECK (
                    longitude >= -180 AND longitude <= 180
                ),
            rating NUMERIC(3, 2)
                CONSTRAINT ck_collected_prospects_rating CHECK (rating >= 0 AND rating <= 5),
            grade TEXT NOT NULL DEFAULT 'C'
                CONSTRAINT ck_collected_prospects_grade CHECK (grade IN ('A','B','C')),
            whatsapp_reachable BOOLEAN,
            dedupe_fingerprint TEXT NOT NULL CHECK (length(btrim(dedupe_fingerprint)) > 0),
            sync_status TEXT NOT NULL DEFAULT 'raw'
                CONSTRAINT ck_collected_prospects_sync_status CHECK (
                    sync_status IN ('raw','cleaned','synced_to_crm','discarded')
                ),
            crm_lead_id UUID,
            raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, dedupe_fingerprint),
            CHECK (jsonb_typeof(raw_metadata) = 'object')
        )
    """)
    op.execute("""
        CREATE TABLE marketing_campaigns (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name TEXT NOT NULL CHECK (length(btrim(name)) > 0),
            channel_type TEXT NOT NULL
                CONSTRAINT ck_marketing_campaigns_channel_type CHECK (
                    channel_type IN ('email','whatsapp_cloud')
                ),
            subject TEXT,
            body_template TEXT NOT NULL CHECK (length(btrim(body_template)) > 0),
            status TEXT NOT NULL DEFAULT 'draft'
                CONSTRAINT ck_marketing_campaigns_status CHECK (
                    status IN ('draft','scheduled','running','paused','completed')
                ),
            total_targets INT NOT NULL DEFAULT 0
                CONSTRAINT ck_marketing_campaigns_total_targets CHECK (total_targets >= 0),
            sent_count INT NOT NULL DEFAULT 0
                CONSTRAINT ck_marketing_campaigns_sent_count CHECK (sent_count >= 0),
            suppressed_count INT NOT NULL DEFAULT 0
                CONSTRAINT ck_marketing_campaigns_suppressed_count CHECK (suppressed_count >= 0),
            replied_count INT NOT NULL DEFAULT 0
                CONSTRAINT ck_marketing_campaigns_replied_count CHECK (replied_count >= 0),
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE campaign_recipients (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            campaign_id UUID NOT NULL REFERENCES marketing_campaigns(id) ON DELETE CASCADE,
            customer_id UUID,
            prospect_id UUID,
            address TEXT NOT NULL CHECK (length(btrim(address)) > 0),
            status TEXT NOT NULL DEFAULT 'queued'
                CONSTRAINT ck_campaign_recipients_status CHECK (
                    status IN ('queued','sent','suppressed','failed','replied')
                ),
            error_reason TEXT,
            sent_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            CONSTRAINT uq_campaign_recipients_tenant_address
                UNIQUE (tenant_id, campaign_id, address)
        )
    """)
    for table, index_columns in (
        ("lead_collection_jobs", "tenant_id, status, created_at"),
        ("collected_prospects", "tenant_id, sync_status, grade"),
        ("collected_prospects", "tenant_id, job_id"),
        ("marketing_campaigns", "tenant_id, status, created_at"),
        ("campaign_recipients", "tenant_id, campaign_id, status"),
    ):
        columns = [part.strip() for part in index_columns.split(",")]
        op.create_index(f"ix_{table}_{'_'.join(columns[1:])}", table, columns)
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
