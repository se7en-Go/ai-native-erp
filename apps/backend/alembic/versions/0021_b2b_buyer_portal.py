"""B2B buyer magic-link self-service portal tokens and narrow hash lookup helper.

Revision ID: 0021_b2b_buyer_portal
Revises: 0020_notifications_automation
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_b2b_buyer_portal"
down_revision: str | None = "0020_notifications_automation"
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
        CREATE TABLE b2b_portal_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            quote_id UUID NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
            created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_prefix TEXT NOT NULL,
            token_hash TEXT NOT NULL CONSTRAINT uq_b2b_portal_tokens_hash UNIQUE,
            buyer_email TEXT,
            allow_accept BOOLEAN NOT NULL DEFAULT true,
            allow_receipt_upload BOOLEAN NOT NULL DEFAULT true,
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            last_accessed_at TIMESTAMPTZ,
            accepted_at TIMESTAMPTZ,
            accepted_by_name TEXT,
            accepted_ip INET,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX ix_b2b_portal_tokens_tenant_quote
        ON b2b_portal_tokens (tenant_id, quote_id, created_at DESC)
    """)
    _apply_rls("b2b_portal_tokens")

    # Narrow exact-hash lookup policies for app_migrator so the SECURITY DEFINER helper
    # can resolve a single portal token before app.current_tenant_id is set.
    op.execute(
        "CREATE POLICY b2b_portal_tokens_migrator_select ON b2b_portal_tokens "
        "AS PERMISSIVE FOR SELECT TO app_migrator USING (true)"
    )
    op.execute(
        "CREATE POLICY b2b_portal_tokens_migrator_lookup ON b2b_portal_tokens "
        "AS RESTRICTIVE FOR SELECT TO app_migrator USING ("
        "token_hash = current_setting('app.portal_lookup_hash', true) "
        "AND current_setting('app.portal_lookup_hash', true) <> ''"
        ")"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION resolve_b2b_portal_token(p_token_hash TEXT)
        RETURNS TABLE (
            token_id UUID,
            tenant_id UUID,
            quote_id UUID,
            buyer_email TEXT,
            allow_accept BOOLEAN,
            allow_receipt_upload BOOLEAN,
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            accepted_at TIMESTAMPTZ,
            accepted_by_name TEXT
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
            PERFORM set_config('app.portal_lookup_hash', p_token_hash, true);
            RETURN QUERY
                SELECT
                    t.id,
                    t.tenant_id,
                    t.quote_id,
                    t.buyer_email,
                    t.allow_accept,
                    t.allow_receipt_upload,
                    t.expires_at,
                    t.revoked_at,
                    t.accepted_at,
                    t.accepted_by_name
                FROM public.b2b_portal_tokens t
                WHERE t.token_hash = p_token_hash
                LIMIT 1;
        END;
        $fn$;
    """)
    op.execute("REVOKE ALL ON FUNCTION resolve_b2b_portal_token(TEXT) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_b2b_portal_token(TEXT) TO app_runtime")


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION resolve_b2b_portal_token(TEXT) FROM app_runtime")
    op.execute("DROP FUNCTION IF EXISTS resolve_b2b_portal_token(TEXT)")
    op.execute("DROP POLICY IF EXISTS b2b_portal_tokens_migrator_lookup ON b2b_portal_tokens")
    op.execute("DROP POLICY IF EXISTS b2b_portal_tokens_migrator_select ON b2b_portal_tokens")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_policy ON b2b_portal_tokens")
    op.execute("DROP POLICY IF EXISTS tenant_allow_runtime ON b2b_portal_tokens")
    op.execute("DROP INDEX IF EXISTS ix_b2b_portal_tokens_tenant_quote")
    op.drop_table("b2b_portal_tokens")
