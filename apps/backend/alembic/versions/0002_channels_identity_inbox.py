"""Create omnichannel inbox, webhook pipeline, and identity graph tables.

Revision ID: 0002_channels_identity_inbox
Revises: 0001_tenancy_rls_and_audit
Create Date: 2026-11-19 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_channels_identity_inbox"
down_revision: str | None = "0001_tenancy_rls_and_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHANNEL_TYPES: tuple[str, ...] = (
    "whatsapp_cloud",
    "whatsapp_coexistence",
    "email",
    "web_livechat",
    "shopify",
    "woocommerce",
    "shopline",
    "shoplazza",
    "tiktok_shop",
    "made_in_china_email",
    "alibaba_email",
    "messenger",
    "instagram_dm",
    "telegram",
    "amazon_sp",
    "ebay",
    "mercado_libre",
    "temu",
    "walmart",
    "shopee",
    "lazada",
)
CHANNEL_TYPE_CHECK = (
    "channel_type IN (" + ", ".join(f"'{item}'" for item in _CHANNEL_TYPES) + ")"
)
_CHANNEL_SQL_LIST = ", ".join(f"'{item}'" for item in _CHANNEL_TYPES)

TENANT_RLS_TABLES: tuple[str, ...] = (
    "channel_accounts",
    "dead_letter_events",
    "b2b_companies",
    "customers",
    "customer_identities",
    "identity_merge_events",
    "conversations",
    "messages",
    "consent_records",
    "suppression_list",
)


def _enable_force_rls(table_name: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table_name} TO app_runtime")
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY app_runtime_permissive ON {table_name} "
        "AS PERMISSIVE FOR ALL TO app_runtime USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"CREATE POLICY tenant_isolation_policy ON {table_name} "
        "AS RESTRICTIVE FOR ALL TO app_runtime "
        "USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 1. channel_routing_directory (Non-PII global routing table, accessed via SECURITY DEFINER fn)
    op.create_table(
        "channel_routing_directory",
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_account_id", sa.Text(), nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(CHANNEL_TYPE_CHECK, name="ck_channel_routing_directory_channel_type"),
        sa.PrimaryKeyConstraint("channel_type", "external_account_id"),
    )
    op.execute("REVOKE ALL ON TABLE channel_routing_directory FROM PUBLIC, app_runtime")
    op.execute("GRANT SELECT ON TABLE channel_routing_directory TO app_runtime")
    op.execute("ALTER TABLE channel_routing_directory ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE channel_routing_directory FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY app_runtime_permissive ON channel_routing_directory "
        "AS PERMISSIVE FOR ALL TO app_runtime USING (true) WITH CHECK (false)"
    )
    op.execute(
        "CREATE POLICY tenant_isolation_policy ON channel_routing_directory "
        "AS RESTRICTIVE FOR ALL TO app_runtime "
        "USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE POLICY migrator_lookup_permissive ON channel_routing_directory "
        "AS PERMISSIVE FOR SELECT TO app_migrator USING (true)"
    )
    op.execute(
        "CREATE POLICY migrator_lookup_restrictive ON channel_routing_directory "
        "AS RESTRICTIVE FOR SELECT TO app_migrator USING ("
        "channel_type = current_setting('app.routing_lookup_channel', true) "
        "AND external_account_id = current_setting('app.routing_lookup_external', true) "
        "AND current_setting('app.routing_lookup_channel', true) <> '' "
        "AND current_setting('app.routing_lookup_external', true) <> ''"
        ")"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION resolve_tenant_by_channel_external_id(
            p_channel_type TEXT,
            p_external_id TEXT
        )
        RETURNS TABLE (tenant_id UUID, channel_account_id UUID)
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        BEGIN
            IF p_channel_type IS NULL OR p_external_id IS NULL THEN
                RETURN;
            END IF;
            IF p_channel_type NOT IN ({_CHANNEL_SQL_LIST}) THEN
                RETURN;
            END IF;
            IF length(p_external_id) = 0 OR length(p_external_id) > 256 THEN
                RETURN;
            END IF;
            IF p_external_id ~ '[^A-Za-z0-9_.:@+\\-]' THEN
                RETURN;
            END IF;
            PERFORM set_config('app.routing_lookup_channel', p_channel_type, true);
            PERFORM set_config('app.routing_lookup_external', p_external_id, true);
            RETURN QUERY
                SELECT crd.tenant_id, crd.channel_account_id
                FROM public.channel_routing_directory crd
                WHERE crd.channel_type = p_channel_type
                  AND crd.external_account_id = p_external_id
                  AND crd.is_active = true
                LIMIT 1;
        END;
        $fn$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION resolve_tenant_by_channel_external_id(TEXT, TEXT) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION resolve_tenant_by_channel_external_id(TEXT, TEXT) TO app_runtime"
    )

    # 2. channel_accounts
    op.create_table(
        "channel_accounts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_account_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "rate_limit_profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{\"mps\": 80, \"coexistence\": false}'::jsonb"),
        ),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(CHANNEL_TYPE_CHECK, name="ck_channel_accounts_channel_type"),
        sa.CheckConstraint(
            "status IN ('active', 'token_expired', 'rate_limited', 'disconnected')",
            name="ck_channel_accounts_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_type",
            "external_account_id",
            name="uq_channel_accounts_tenant_type_ext",
        ),
    )
    op.create_index(
        "idx_channel_accounts_tenant_type",
        "channel_accounts",
        ["tenant_id", "channel_type"],
    )
    _enable_force_rls("channel_accounts")

    # 3. raw_webhook_events (No RLS: tenant_id nullable until resolved at ingress)
    op.create_table(
        "raw_webhook_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_event_id", sa.Text(), nullable=False),
        sa.Column("external_account_id", sa.Text(), nullable=True),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "signature_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "processing_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'received'"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(CHANNEL_TYPE_CHECK, name="ck_raw_webhook_events_channel_type"),
        sa.CheckConstraint(
            "processing_status IN ("
            "'received', 'queued', 'processing', 'processed', 'deferred', 'dead_letter')",
            name="ck_raw_webhook_events_status",
        ),
        sa.UniqueConstraint(
            "channel_type",
            "external_event_id",
            name="uq_raw_webhook_events_channel_ext_event",
        ),
    )
    op.create_index(
        "idx_raw_webhook_events_status",
        "raw_webhook_events",
        ["processing_status", "received_at"],
    )
    op.execute("REVOKE ALL ON TABLE raw_webhook_events FROM PUBLIC")
    op.execute(
        "GRANT SELECT, UPDATE ON TABLE raw_webhook_events TO app_runtime"
    )
    op.execute("ALTER TABLE raw_webhook_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE raw_webhook_events FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY app_runtime_permissive ON raw_webhook_events "
        "AS PERMISSIVE FOR ALL TO app_runtime USING (true) WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY tenant_isolation_policy ON raw_webhook_events "
        "AS RESTRICTIVE FOR ALL TO app_runtime "
        "USING ("
        "tenant_id IS NOT NULL AND "
        "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
        ") WITH CHECK ("
        "tenant_id IS NOT NULL AND "
        "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
        ")"
    )
    op.execute(
        "CREATE POLICY migrator_broker_permissive ON raw_webhook_events "
        "AS PERMISSIVE FOR ALL TO app_migrator USING (true) WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY migrator_broker_restrictive ON raw_webhook_events "
        "AS RESTRICTIVE FOR ALL TO app_migrator "
        "USING (current_setting('app.webhook_broker', true) = '1') "
        "WITH CHECK (current_setting('app.webhook_broker', true) = '1')"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION accept_raw_webhook_event(
            p_id UUID,
            p_tenant_id UUID,
            p_channel_type TEXT,
            p_external_event_id TEXT,
            p_external_account_id TEXT,
            p_headers JSONB,
            p_payload JSONB
        ) RETURNS UUID
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        DECLARE
            inserted_id UUID;
            existing_id UUID;
            existing_status TEXT;
        BEGIN
            PERFORM set_config('app.webhook_broker', '1', true);
            IF p_channel_type IS NULL OR p_external_event_id IS NULL THEN
                RAISE EXCEPTION 'webhook identity required' USING ERRCODE = '22023';
            END IF;
            IF p_channel_type NOT IN ({_CHANNEL_SQL_LIST}) THEN
                RAISE EXCEPTION 'unsupported channel' USING ERRCODE = '22023';
            END IF;
            IF length(p_external_event_id) = 0 OR length(p_external_event_id) > 512 THEN
                RAISE EXCEPTION 'event id length' USING ERRCODE = '22023';
            END IF;
            IF p_external_account_id IS NOT NULL
               AND length(p_external_account_id) > 256 THEN
                RAISE EXCEPTION 'account id length' USING ERRCODE = '22023';
            END IF;
            IF octet_length(coalesce(p_payload, '{{}}'::jsonb)::text) > 1048576
               OR octet_length(coalesce(p_headers, '{{}}'::jsonb)::text) > 65536 THEN
                RAISE EXCEPTION 'webhook payload too large' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.raw_webhook_events (
                id, tenant_id, channel_type, external_event_id, external_account_id,
                headers, raw_payload, signature_verified, processing_status
            ) VALUES (
                p_id, p_tenant_id, p_channel_type, p_external_event_id,
                p_external_account_id, coalesce(p_headers, '{{}}'::jsonb),
                coalesce(p_payload, '{{}}'::jsonb), true, 'received'
            )
            ON CONFLICT (channel_type, external_event_id) DO NOTHING
            RETURNING id INTO inserted_id;
            IF inserted_id IS NOT NULL THEN
                RETURN inserted_id;
            END IF;
            SELECT id, processing_status INTO existing_id, existing_status
            FROM public.raw_webhook_events
            WHERE channel_type = p_channel_type
              AND external_event_id = p_external_event_id;
            IF existing_status = 'received' THEN
                RETURN existing_id;
            END IF;
            RETURN NULL;
        END;
        $fn$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION claim_raw_webhook_event(p_id UUID)
        RETURNS TABLE (
            id UUID,
            tenant_id UUID,
            channel_type TEXT,
            external_event_id TEXT,
            external_account_id TEXT,
            headers JSONB,
            raw_payload JSONB,
            processing_status TEXT
        )
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        BEGIN
            IF p_id IS NULL THEN
                RETURN;
            END IF;
            PERFORM set_config('app.webhook_broker', '1', true);
            RETURN QUERY
                SELECT e.id, e.tenant_id, e.channel_type, e.external_event_id,
                       e.external_account_id, e.headers, e.raw_payload, e.processing_status
                FROM public.raw_webhook_events e
                WHERE e.id = p_id;
        END;
        $fn$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_raw_webhook_processing_state(
            p_id UUID,
            p_status TEXT,
            p_tenant_id UUID,
            p_attempt INTEGER,
            p_error TEXT
        ) RETURNS VOID
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        BEGIN
            IF p_status NOT IN (
                'received', 'queued', 'processing', 'processed', 'deferred', 'dead_letter'
            ) THEN
                RAISE EXCEPTION 'invalid webhook status' USING ERRCODE = '22023';
            END IF;
            PERFORM set_config('app.webhook_broker', '1', true);
            UPDATE public.raw_webhook_events
            SET processing_status = p_status,
                tenant_id = COALESCE(p_tenant_id, tenant_id),
                attempt_count = COALESCE(p_attempt, attempt_count),
                error_message = p_error,
                processed_at = CASE
                    WHEN p_status = 'processed' THEN now() ELSE processed_at
                END
            WHERE id = p_id;
        END;
        $fn$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION list_unenqueued_raw_webhook_ids(p_limit INTEGER)
        RETURNS TABLE (id UUID, channel_type TEXT)
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        DECLARE
            bounded INTEGER;
        BEGIN
            bounded := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 50);
            PERFORM set_config('app.webhook_broker', '1', true);
            RETURN QUERY
                SELECT e.id, e.channel_type
                FROM public.raw_webhook_events e
                WHERE e.processing_status = 'received'
                ORDER BY e.received_at
                LIMIT bounded;
        END;
        $fn$;
        """
    )
    for fn_sig in (
        "accept_raw_webhook_event(UUID, UUID, TEXT, TEXT, TEXT, JSONB, JSONB)",
        "claim_raw_webhook_event(UUID)",
        "set_raw_webhook_processing_state(UUID, TEXT, UUID, INTEGER, TEXT)",
        "list_unenqueued_raw_webhook_ids(INTEGER)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {fn_sig} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn_sig} TO app_runtime")

    # 4. dead_letter_events
    op.create_table(
        "dead_letter_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "raw_webhook_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_webhook_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column(
            "direction",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'inbound'"),
        ),
        sa.Column("external_event_id", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("replayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("direction IN ('inbound', 'outbound')", name="ck_dlq_direction"),
        sa.CheckConstraint(
            "status IN ('open', 'replayed', 'resolved', 'discarded')",
            name="ck_dlq_status",
        ),
    )
    op.create_index(
        "idx_dead_letter_events_tenant_status",
        "dead_letter_events",
        ["tenant_id", "status", sa.text("created_at DESC")],
    )
    _enable_force_rls("dead_letter_events")

    # 5. b2b_companies
    op.create_table(
        "b2b_companies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("legal_name", sa.Text(), nullable=False),
        sa.Column("country_code", sa.CHAR(2), nullable=False, server_default=sa.text("'US'")),
        sa.Column("tax_vat_number", sa.Text(), nullable=True),
        sa.Column("website_domain", sa.Text(), nullable=True),
        sa.Column(
            "credit_tier",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'prepaid'"),
        ),
        sa.Column(
            "default_incoterm",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'FOB'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "credit_tier IN ('prepaid', 'net_30', 'vip_credit')",
            name="ck_b2b_companies_credit_tier",
        ),
        sa.CheckConstraint(
            "default_incoterm IN ('EXW', 'FOB', 'CIF', 'DDP', 'DAP')",
            name="ck_b2b_companies_incoterm",
        ),
    )
    op.create_index(
        "idx_b2b_companies_tenant_domain",
        "b2b_companies",
        ["tenant_id", "website_domain"],
    )
    op.execute(
        "CREATE INDEX idx_b2b_companies_legal_name_trgm "
        "ON b2b_companies USING gin (legal_name gin_trgm_ops)"
    )
    _enable_force_rls("b2b_companies")

    # 6. customers
    op.create_table(
        "customers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'b2c_consumer'"),
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("b2b_companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("primary_email", sa.Text(), nullable=True),
        sa.Column("primary_phone_e164", sa.Text(), nullable=True),
        sa.Column("country_code", sa.CHAR(2), nullable=True),
        sa.Column("postal_code", sa.Text(), nullable=True),
        sa.Column(
            "preferred_language",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'en'"),
        ),
        sa.Column("iana_timezone", sa.Text(), nullable=True),
        sa.Column("timezone_source", sa.Text(), nullable=True),
        sa.Column(
            "lifecycle_stage",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'lead'"),
        ),
        sa.Column(
            "ltv_usd",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "merged_into_customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "customer_type IN ('b2c_consumer', 'b2b_contact', 'b2b_company_rep')",
            name="ck_customers_customer_type",
        ),
        sa.CheckConstraint(
            "timezone_source IS NULL OR timezone_source IN ("
            "'phone_prefix', 'shipping_country', 'manual')",
            name="ck_customers_timezone_source",
        ),
    )
    op.create_index(
        "idx_customers_tenant_created",
        "customers",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_customers_tenant_email",
        "customers",
        ["tenant_id", "primary_email"],
        postgresql_where=sa.text("primary_email IS NOT NULL"),
    )
    op.create_index(
        "idx_customers_tenant_phone",
        "customers",
        ["tenant_id", "primary_phone_e164"],
        postgresql_where=sa.text("primary_phone_e164 IS NOT NULL"),
    )
    op.execute(
        "CREATE INDEX idx_customers_display_name_trgm "
        "ON customers USING gin (display_name gin_trgm_ops)"
    )
    _enable_force_rls("customers")

    # 7. customer_identities
    op.create_table(
        "customer_identities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("identity_type", sa.Text(), nullable=False),
        sa.Column("identity_value", sa.Text(), nullable=False),
        sa.Column("confidence_level", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "identity_type IN ("
            "'email', 'phone_e164', 'whatsapp_waid', 'whatsapp_bsuid', "
            "'ig_psid', 'fb_psid', 'shopify_customer_id', 'tiktok_buyer_id', "
            "'amazon_buyer_email', 'web_visitor_id'"
            ")",
            name="ck_customer_identities_type",
        ),
        sa.CheckConstraint(
            "confidence_level IN ('deterministic', 'human_verified', 'probabilistic')",
            name="ck_customer_identities_confidence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "identity_type",
            "identity_value",
            name="uq_customer_identities_tenant_type_val",
        ),
    )
    op.create_index(
        "idx_customer_identities_tenant_customer",
        "customer_identities",
        ["tenant_id", "customer_id"],
    )
    _enable_force_rls("customer_identities")

    # 8. identity_merge_events
    op.create_table(
        "identity_merge_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "moved_identity_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column(
            "moved_conversation_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column(
            "reverted_merge_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("identity_merge_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("performed_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("action IN ('merge', 'split')", name="ck_identity_merge_events_action"),
    )
    op.create_index(
        "idx_identity_merge_events_tenant_created",
        "identity_merge_events",
        ["tenant_id", sa.text("created_at DESC")],
    )
    _enable_force_rls("identity_merge_events")

    # 9. conversations
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "channel_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_thread_id", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column(
            "conversation_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'b2c_support'"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("assigned_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ai_autonomy_override", sa.Text(), nullable=True),
        sa.Column("csw_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fep_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ctwa_clid", sa.Text(), nullable=True),
        sa.Column("human_agent_window_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_preview", sa.Text(), nullable=True),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "channel_policy_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(CHANNEL_TYPE_CHECK, name="ck_conversations_channel_type"),
        sa.CheckConstraint(
            "conversation_mode IN ('b2c_support', 'b2b_inquiry', 'mixed')",
            name="ck_conversations_mode",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'pending_human', 'snoozed', 'resolved')",
            name="ck_conversations_status",
        ),
        sa.CheckConstraint(
            "ai_autonomy_override IS NULL OR ai_autonomy_override IN ("
            "'L0_copilot', 'L1_proposal', 'L2_auto')",
            name="ck_conversations_autonomy",
        ),
    )
    op.create_index(
        "idx_conversations_tenant_updated",
        "conversations",
        ["tenant_id", sa.text("updated_at DESC")],
    )
    op.create_index(
        "idx_conversations_tenant_status_sla",
        "conversations",
        ["tenant_id", "status", sa.text("sla_due_at ASC NULLS LAST")],
    )
    op.create_index(
        "idx_conversations_tenant_thread",
        "conversations",
        ["tenant_id", "channel_account_id", "external_thread_id"],
        postgresql_where=sa.text("external_thread_id IS NOT NULL"),
    )
    op.create_index(
        "idx_conversations_tenant_customer",
        "conversations",
        ["tenant_id", "customer_id"],
    )
    _enable_force_rls("conversations")

    # 10. messages
    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "customer_identity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer_identities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("external_message_id", sa.Text(), nullable=True),
        sa.Column("external_sender_id", sa.Text(), nullable=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("sender_type", sa.Text(), nullable=False),
        sa.Column("sender_id", sa.Text(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_sanitized_html", sa.Text(), nullable=True),
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "channel_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "delivery_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "channel_msg_cost",
            sa.Numeric(12, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(CHANNEL_TYPE_CHECK, name="ck_messages_channel_type"),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound', 'internal_note')",
            name="ck_messages_direction",
        ),
        sa.CheckConstraint(
            "sender_type IN ('customer', 'human_agent', 'ai_agent', 'system')",
            name="ck_messages_sender_type",
        ),
        sa.CheckConstraint(
            "content_type IN ("
            "'text', 'rich_html', 'template', 'media', 'quote_card', 'order_card')",
            name="ck_messages_content_type",
        ),
        sa.CheckConstraint(
            "delivery_status IN ('queued', 'sent', 'delivered', 'read', 'failed')",
            name="ck_messages_delivery_status",
        ),
    )
    op.create_index(
        "uq_messages_idempotency",
        "messages",
        ["tenant_id", "channel_type", "external_message_id"],
        unique=True,
        postgresql_where=sa.text("external_message_id IS NOT NULL"),
    )
    op.create_index(
        "idx_messages_tenant_conv_ts",
        "messages",
        ["tenant_id", "conversation_id", "source_timestamp", "created_at"],
    )
    _enable_force_rls("messages")

    # 11. consent_records
    op.create_table(
        "consent_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("consent_status", sa.Text(), nullable=False),
        sa.Column(
            "opt_in_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[\"order_updates\"]'::jsonb"),
        ),
        sa.Column("disclosed_business_name", sa.Text(), nullable=True),
        sa.Column("proof_source", sa.Text(), nullable=False),
        sa.Column("proof_ip", postgresql.INET(), nullable=True),
        sa.Column("evidence_ref", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "consent_status IN ('opted_in', 'opted_out')",
            name="ck_consent_records_status",
        ),
    )
    op.create_index(
        "idx_consent_records_tenant_customer_channel",
        "consent_records",
        ["tenant_id", "customer_id", "channel_type", sa.text("recorded_at DESC")],
    )
    _enable_force_rls("consent_records")

    # 12. suppression_list
    op.create_table(
        "suppression_list",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("normalized_address", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_type",
            "normalized_address",
            name="uq_suppression_list_tenant_channel_addr",
        ),
    )
    _enable_force_rls("suppression_list")


def downgrade() -> None:
    for table_name in reversed(TENANT_RLS_TABLES):
        op.drop_table(table_name)
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "accept_raw_webhook_event(UUID, UUID, TEXT, TEXT, TEXT, JSONB, JSONB)"
    )
    op.execute("DROP FUNCTION IF EXISTS claim_raw_webhook_event(UUID)")
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "set_raw_webhook_processing_state(UUID, TEXT, UUID, INTEGER, TEXT)"
    )
    op.execute("DROP FUNCTION IF EXISTS list_unenqueued_raw_webhook_ids(INTEGER)")
    op.drop_table("raw_webhook_events")
    op.execute("DROP FUNCTION IF EXISTS resolve_tenant_by_channel_external_id(TEXT, TEXT)")
    op.drop_table("channel_routing_directory")
