"""Tenant-scoped MCP API tokens with a narrow pre-tenant hash lookup helper.

An external MCP client (Claude Desktop / Cursor / OpenClaw) presents a bearer
token before any tenant context exists, so the token row must be resolvable
without `app.current_tenant_id`. The table therefore follows the exact
`0017_collector_api_tokens` pattern:

* `mcp_api_tokens` carries PG17 `ENABLE` + `FORCE ROW LEVEL SECURITY` with a
  permissive `app_runtime` policy and a RESTRICTIVE `tenant_id = ...` policy.
* `app_migrator` gets two SELECT-only policies, the RESTRICTIVE one pinned to
  `app.mcp_lookup_hash`, so a lookup can only ever return the single row whose
  SHA-256 hash was just set inside the transaction.
* `resolve_mcp_token(TEXT)` is `SECURITY DEFINER` with
  `SET search_path = public, pg_temp`, validates the hex digest shape, and is
  granted to `app_runtime` only.

Only the SHA-256 hex digest (`token_hash`) and the display prefix
(`token_prefix`, e.g. `erp_mcp_ab12`) are persisted; the plaintext token is
never stored.

Revision ID: 0023_tenant_mcp_tokens
Revises: 0022_finance_dual_settlement
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_tenant_mcp_tokens"
down_revision: str | None = "0022_finance_dual_settlement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
_LOOKUP_SETTING = "app.mcp_lookup_hash"


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
        CREATE TABLE mcp_api_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            token_prefix TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            scopes TEXT[] NOT NULL DEFAULT ARRAY['mcp:read']::TEXT[],
            allowed_tools TEXT[],
            last_used_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_mcp_api_tokens_hash UNIQUE (token_hash)
        )
    """)
    op.execute("""
        CREATE INDEX ix_mcp_api_tokens_tenant_created
        ON mcp_api_tokens (tenant_id, created_at DESC)
    """)
    _apply_rls("mcp_api_tokens")

    # Narrow exact-hash policies for app_migrator so the SECURITY DEFINER helper can
    # resolve a single MCP token before app.current_tenant_id is set.
    op.execute(
        "CREATE POLICY mcp_api_tokens_migrator_select ON mcp_api_tokens "
        "AS PERMISSIVE FOR SELECT TO app_migrator USING (true)"
    )
    op.execute(
        "CREATE POLICY mcp_api_tokens_migrator_lookup ON mcp_api_tokens "
        "AS RESTRICTIVE FOR SELECT TO app_migrator USING ("
        f"token_hash = current_setting('{_LOOKUP_SETTING}', true) "
        f"AND current_setting('{_LOOKUP_SETTING}', true) <> ''"
        ")"
    )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION resolve_mcp_token(p_token_hash TEXT)
        RETURNS TABLE (
            token_id UUID,
            tenant_id UUID,
            created_by_user_id UUID,
            scopes TEXT[],
            allowed_tools TEXT[],
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        )
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        BEGIN
            IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{{64}}$' THEN
                RETURN;
            END IF;
            PERFORM set_config('{_LOOKUP_SETTING}', p_token_hash, true);
            RETURN QUERY
                SELECT
                    t.id,
                    t.tenant_id,
                    t.created_by_user_id,
                    t.scopes,
                    t.allowed_tools,
                    t.expires_at,
                    t.revoked_at
                FROM public.mcp_api_tokens t
                WHERE t.token_hash = p_token_hash
                LIMIT 1;
        END;
        $fn$;
    """)
    op.execute("REVOKE ALL ON FUNCTION resolve_mcp_token(TEXT) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_mcp_token(TEXT) TO app_runtime")


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION resolve_mcp_token(TEXT) FROM app_runtime")
    op.execute("DROP FUNCTION IF EXISTS resolve_mcp_token(TEXT)")
    op.execute("DROP POLICY IF EXISTS mcp_api_tokens_migrator_lookup ON mcp_api_tokens")
    op.execute("DROP POLICY IF EXISTS mcp_api_tokens_migrator_select ON mcp_api_tokens")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_policy ON mcp_api_tokens")
    op.execute("DROP POLICY IF EXISTS tenant_allow_runtime ON mcp_api_tokens")
    op.execute("DROP INDEX IF EXISTS ix_mcp_api_tokens_tenant_created")
    op.drop_table("mcp_api_tokens")
