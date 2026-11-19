"""Tenancy core tables, fail-closed RLS, and monthly partitioned audit_events.

Revision ID: 0001_tenancy_rls_and_audit
Revises:
Create Date: 2026-11-19
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_tenancy_rls_and_audit"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _split_sql(script: str) -> list[str]:
    """Split a SQL script into statements asyncpg can execute one at a time.

    Dollar-quoted bodies (DO $$ ... $$) stay intact. The statement text is unchanged.
    """
    statements: list[str] = []
    current: list[str] = []
    in_dollar = False
    for line in script.splitlines():
        stripped = line.strip()
        if not in_dollar and stripped.startswith("--"):
            continue
        current.append(line)
        if "$$" in line:
            count = line.count("$$")
            if count % 2 == 1:
                in_dollar = not in_dollar
        if not in_dollar and stripped.endswith(";"):
            statement = "\n".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _execute_script(script: str) -> None:
    for statement in _split_sql(script):
        op.execute(statement)


def upgrade() -> None:
    _execute_script(
        """
        CREATE TABLE tenants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            business_preset TEXT NOT NULL DEFAULT 'hybrid_factory_b2b_b2c',
            base_currency TEXT NOT NULL DEFAULT 'USD',
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            is_active BOOLEAN NOT NULL DEFAULT true,
            shredded_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE tenant_memberships (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'manager', 'agent', 'finance')),
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, user_id)
        );
        CREATE INDEX idx_tenant_memberships_tenant_user ON tenant_memberships (tenant_id, user_id);
        CREATE INDEX idx_tenant_memberships_user ON tenant_memberships (user_id);

        CREATE TABLE tenant_keys (
            tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            kek_key_arn TEXT NOT NULL,
            encrypted_dek BYTEA NOT NULL,
            key_version INT NOT NULL DEFAULT 1,
            rotated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE channel_credentials (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            channel_account_id UUID NOT NULL,
            credential_type TEXT NOT NULL,
            ciphertext BYTEA NOT NULL,
            nonce BYTEA NOT NULL,
            auth_tag BYTEA NOT NULL,
            key_version INT NOT NULL,
            expires_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, channel_account_id, credential_type)
        );
        CREATE INDEX idx_channel_credentials_tenant
            ON channel_credentials (tenant_id, channel_account_id);

        CREATE TABLE onboarding_states (
            tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            current_stage TEXT NOT NULL DEFAULT 'WorkspaceCreated'
                CHECK (current_stage IN (
                    'WorkspaceCreated',
                    'ChannelConnecting',
                    'ChannelVerified',
                    'CatalogSyncing',
                    'AISandboxReady',
                    'LiveProduction'
                )),
            preset_type TEXT NOT NULL DEFAULT 'hybrid_factory_b2b_b2c',
            completed_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE audit_events (
            tenant_id UUID NOT NULL,
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            actor_type TEXT NOT NULL CHECK (
                actor_type IN ('human_user', 'ai_agent', 'system_cron', 'webhook')
            ),
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id UUID NOT NULL,
            proposal_id UUID,
            before_state JSONB,
            after_state JSONB,
            ip_address INET,
            user_agent TEXT,
            PRIMARY KEY (tenant_id, id, created_at)
        ) PARTITION BY RANGE (created_at);

        CREATE INDEX idx_audit_events_tenant_created
            ON audit_events (tenant_id, created_at DESC);
        CREATE INDEX idx_audit_events_tenant_action
            ON audit_events (tenant_id, action, created_at DESC);

        -- Create monthly partitions for current month + next 3 months
        DO $$
        DECLARE
            base_month TIMESTAMPTZ := date_trunc(
                'month', now() AT TIME ZONE 'UTC'
            ) AT TIME ZONE 'UTC';
            m_start TIMESTAMPTZ;
            m_end TIMESTAMPTZ;
            part_name TEXT;
            i INT;
        BEGIN
            FOR i IN 0..3 LOOP
                m_start := base_month + (i || ' month')::interval;
                m_end := base_month + ((i + 1) || ' month')::interval;
                part_name := 'audit_events_' || to_char(m_start AT TIME ZONE 'UTC', 'YYYY_MM');
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF audit_events '
                    'FOR VALUES FROM (%L) TO (%L)',
                    part_name, m_start, m_end
                );
            END LOOP;
        END
        $$;
        CREATE TABLE audit_events_default PARTITION OF audit_events DEFAULT;
        """
    )

    _execute_script(
        """
        ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenants_isolation_permissive ON tenants
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        CREATE POLICY tenants_isolation_restrictive ON tenants
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        CREATE POLICY tenants_migrator_auth_select ON tenants
            AS PERMISSIVE FOR SELECT TO app_migrator
            USING (true);

        ALTER TABLE tenant_memberships ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_memberships FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_memberships_isolation_permissive ON tenant_memberships
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            );
        CREATE POLICY tenant_memberships_isolation_restrictive ON tenant_memberships
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            );
        CREATE POLICY tenant_memberships_migrator_auth_select ON tenant_memberships
            AS PERMISSIVE FOR SELECT TO app_migrator
            USING (true);

        ALTER TABLE tenant_keys ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_keys FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_keys_isolation_permissive ON tenant_keys
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            );
        CREATE POLICY tenant_keys_isolation_restrictive ON tenant_keys
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            );

        ALTER TABLE channel_credentials ENABLE ROW LEVEL SECURITY;
        ALTER TABLE channel_credentials FORCE ROW LEVEL SECURITY;
        CREATE POLICY channel_credentials_isolation_permissive ON channel_credentials
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            );
        CREATE POLICY channel_credentials_isolation_restrictive ON channel_credentials
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            );

        ALTER TABLE onboarding_states ENABLE ROW LEVEL SECURITY;
        ALTER TABLE onboarding_states FORCE ROW LEVEL SECURITY;
        CREATE POLICY onboarding_states_isolation_permissive ON onboarding_states
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            );
        CREATE POLICY onboarding_states_isolation_restrictive ON onboarding_states
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            );

        ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY audit_events_isolation_permissive ON audit_events
            AS PERMISSIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            );
        CREATE POLICY audit_events_isolation_restrictive ON audit_events
            AS RESTRICTIVE FOR ALL TO app_runtime
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
            );

        -- Narrow SECURITY DEFINER function for pre-tenant auth login membership discovery
        CREATE OR REPLACE FUNCTION resolve_user_memberships(p_user_id UUID)
        RETURNS TABLE (
            tenant_id UUID,
            tenant_slug TEXT,
            tenant_name TEXT,
            role TEXT,
            is_active BOOLEAN
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT m.tenant_id, t.slug, t.name, m.role,
                   (m.is_active AND t.is_active AND t.shredded_at IS NULL)
            FROM tenant_memberships m
            JOIN tenants t ON t.id = m.tenant_id
            WHERE m.user_id = p_user_id
              AND m.is_active = true
              AND t.is_active = true
              AND t.shredded_at IS NULL
            ORDER BY m.created_at ASC;
        $$;

        REVOKE ALL ON FUNCTION resolve_user_memberships(UUID) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION resolve_user_memberships(UUID) TO app_runtime;

        -- Explicit table grants for app_runtime
        GRANT SELECT, INSERT, UPDATE, DELETE ON tenants, users, tenant_memberships,
            tenant_keys, channel_credentials, onboarding_states TO app_runtime;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_runtime;

        -- Immutable audit_events: ONLY SELECT & INSERT for app_runtime
        REVOKE ALL ON TABLE audit_events FROM PUBLIC, app_runtime;
        GRANT SELECT, INSERT ON TABLE audit_events TO app_runtime;
        """
    )


def downgrade() -> None:
    _execute_script(
        """
        DROP FUNCTION IF EXISTS resolve_user_memberships(UUID);
        DROP TABLE IF EXISTS audit_events CASCADE;
        DROP TABLE IF EXISTS onboarding_states CASCADE;
        DROP TABLE IF EXISTS channel_credentials CASCADE;
        DROP TABLE IF EXISTS tenant_keys CASCADE;
        DROP TABLE IF EXISTS tenant_memberships CASCADE;
        DROP TABLE IF EXISTS users CASCADE;
        DROP TABLE IF EXISTS tenants CASCADE;
        """
    )
