"""Browser collector API tokens table and narrow hash lookup helper.

Revision ID: 0017_collector_api_tokens
Revises: 0016_channel_analytics_metrics
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_collector_api_tokens"
down_revision: str | None = "0016_channel_analytics_metrics"
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
        CREATE TABLE collector_api_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            token_prefix TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            scopes TEXT[] NOT NULL DEFAULT ARRAY['sourcing:collect']::TEXT[],
            last_used_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_collector_api_tokens_hash UNIQUE (token_hash)
        )
    """)
    op.execute("""
        CREATE INDEX ix_collector_api_tokens_tenant_created
        ON collector_api_tokens (tenant_id, created_at DESC)
    """)
    _apply_rls("collector_api_tokens")

    # Narrow exact-hash lookup policies for app_migrator so the SECURITY DEFINER helper
    # can resolve a single collector token before app.current_tenant_id is set.
    op.execute(
        "CREATE POLICY collector_api_tokens_migrator_select ON collector_api_tokens "
        "AS PERMISSIVE FOR SELECT TO app_migrator USING (true)"
    )
    op.execute(
        "CREATE POLICY collector_api_tokens_migrator_lookup ON collector_api_tokens "
        "AS RESTRICTIVE FOR SELECT TO app_migrator USING ("
        "token_hash = current_setting('app.collector_lookup_hash', true) "
        "AND current_setting('app.collector_lookup_hash', true) <> ''"
        ")"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION resolve_collector_token(p_token_hash TEXT)
        RETURNS TABLE (
            token_id UUID,
            tenant_id UUID,
            created_by_user_id UUID,
            scopes TEXT[],
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        )
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        BEGIN
            IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
                RETURN;
            END IF;
            PERFORM set_config('app.collector_lookup_hash', p_token_hash, true);
            RETURN QUERY
                SELECT
                    t.id,
                    t.tenant_id,
                    t.created_by_user_id,
                    t.scopes,
                    t.expires_at,
                    t.revoked_at
                FROM public.collector_api_tokens t
                WHERE t.token_hash = p_token_hash
                LIMIT 1;
        END;
        $fn$;
    """)
    op.execute("REVOKE ALL ON FUNCTION resolve_collector_token(TEXT) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_collector_token(TEXT) TO app_runtime")


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION resolve_collector_token(TEXT) FROM app_runtime")
    op.execute("DROP FUNCTION IF EXISTS resolve_collector_token(TEXT)")
    op.execute("DROP POLICY IF EXISTS collector_api_tokens_migrator_lookup ON collector_api_tokens")
    op.execute("DROP POLICY IF EXISTS collector_api_tokens_migrator_select ON collector_api_tokens")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_policy ON collector_api_tokens")
    op.execute("DROP POLICY IF EXISTS tenant_allow_runtime ON collector_api_tokens")
    op.execute("DROP INDEX IF EXISTS ix_collector_api_tokens_tenant_created")
    op.drop_table("collector_api_tokens")
