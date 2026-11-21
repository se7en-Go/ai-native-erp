"""Wave 2 security & finance hardening: AP payment ledger, payment idempotency index,
non-negative order amounts, audit partition RLS hardening, and users RLS scoping.

Revision ID: 0013_wave2_security_finance
Revises: 0012_ai_autoreply_sourcing_ap
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0013_wave2_security_finance"
down_revision: str | None = "0012_ai_autoreply_sourcing_ap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"

# Tenant-scoped reads on the global identity table: only rows that belong to the tenant in
# the transaction-scoped GUC are visible. `app.current_tenant_id` unset (auth bootstrap:
# login / refresh / registration) keeps the historical full visibility, and unaffiliated
# identities (brand-new accounts created by registration/invite before their membership row
# lands) stay readable so INSERT ... RETURNING and account provisioning keep working.
_USERS_SCOPED_READ = f"""
    NULLIF(current_setting('app.current_tenant_id', true), '') IS NULL
    OR NOT EXISTS (
        SELECT 1 FROM tenant_memberships any_m WHERE any_m.user_id = users.id
    )
    OR EXISTS (
        SELECT 1 FROM tenant_memberships m
        WHERE m.user_id = users.id AND m.{_SCOPE}
    )
"""


def _apply_rls(table: str, *, append_only: bool = False) -> None:
    """Fail-closed tenant isolation, identical policy shape to 0012_ai_autoreply_sourcing_ap."""
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
    if append_only:
        op.execute(f"REVOKE ALL ON {table} FROM app_runtime")
        op.execute(f"GRANT SELECT, INSERT ON {table} TO app_runtime")
    else:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_runtime")


def _upgrade_payable_payments() -> None:
    op.execute("""
        CREATE TABLE payable_payments (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            payable_id UUID NOT NULL REFERENCES payables(id) ON DELETE CASCADE,
            amount NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
            currency VARCHAR(3) NOT NULL DEFAULT 'USD',
            payment_ref VARCHAR(128),
            idempotency_key VARCHAR(128) NOT NULL,
            paid_by_user_id UUID,
            paid_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_payable_payments_tenant_idempotency
                UNIQUE (tenant_id, idempotency_key)
        )
    """)
    op.create_index(
        "ix_payable_payments_tenant_payable",
        "payable_payments",
        ["tenant_id", "payable_id"],
    )
    _apply_rls("payable_payments", append_only=True)


def _downgrade_payable_payments() -> None:
    op.drop_table("payable_payments")


def _upgrade_payment_records_tx_ref_idempotency() -> None:
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_records_tenant_tx_ref
            ON payment_records (tenant_id, external_tx_ref)
            WHERE external_tx_ref IS NOT NULL AND external_tx_ref <> ''
    """)


def _downgrade_payment_records_tx_ref_idempotency() -> None:
    op.execute("DROP INDEX IF EXISTS uq_payment_records_tenant_tx_ref")


def _upgrade_orders_non_negative_amounts() -> None:
    """Reject negative order amounts without a long exclusive lock on existing rows."""
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_orders_non_negative_amounts'
            ) THEN
                ALTER TABLE orders
                    ADD CONSTRAINT ck_orders_non_negative_amounts
                    CHECK (subtotal_amount >= 0 AND total_amount >= 0) NOT VALID;
            END IF;
        END
        $$;
    """)
    op.execute("ALTER TABLE orders VALIDATE CONSTRAINT ck_orders_non_negative_amounts")


def _downgrade_orders_non_negative_amounts() -> None:
    op.execute("ALTER TABLE orders DROP CONSTRAINT IF EXISTS ck_orders_non_negative_amounts")


def _audit_partition_names() -> list[str]:
    if context.is_offline_mode():
        # `alembic upgrade head --sql` renders against a mock connection whose
        # `execute()` yields no rows, and partition discovery is inherently
        # data-dependent, so the offline script cannot enumerate live children.
        # Nothing is lost at runtime: `ensure_audit_child_rls` FORCEs RLS on every
        # partition (including newly created ones) during application startup.
        return []
    rows = op.get_bind().execute(
        sa.text(
            "SELECT c.relname "
            "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'audit_events'::regclass "
            "ORDER BY c.relname"
        )
    )
    return [str(name) for name in rows.scalars()]


def _upgrade_audit_partition_rls() -> None:
    """Harden every existing audit_events child partition (direct-partition access guard).

    Partitions created after this migration are handled by the runtime partition manager; this
    helper is idempotent so a re-run never fails on already hardened partitions.
    """
    for partition in _audit_partition_names():
        op.execute(f"ALTER TABLE {partition} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {partition} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_allow_runtime ON {partition}")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_policy ON {partition}")
        op.execute(
            f"CREATE POLICY tenant_allow_runtime ON {partition} AS PERMISSIVE "
            "FOR ALL TO app_runtime USING (true) WITH CHECK (true)"
        )
        op.execute(
            f"CREATE POLICY tenant_isolation_policy ON {partition} AS RESTRICTIVE "
            f"FOR ALL TO app_runtime USING ({_SCOPE}) WITH CHECK ({_SCOPE})"
        )
        op.execute(f"REVOKE ALL ON {partition} FROM app_runtime")
        op.execute(f"GRANT SELECT, INSERT ON {partition} TO app_runtime")


def _downgrade_audit_partition_rls() -> None:
    for partition in _audit_partition_names():
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_policy ON {partition}")
        op.execute(f"DROP POLICY IF EXISTS tenant_allow_runtime ON {partition}")
        op.execute(f"ALTER TABLE {partition} DISABLE ROW LEVEL SECURITY")


def _upgrade_users_rls() -> None:
    """Harden the global identity table.

    `users` is a cross-tenant identity table (no tenant_id column), so isolation is expressed
    as "only identities that belong to the tenant in the transaction GUC are readable". Login
    and registration run with `app.current_tenant_id` unset and therefore keep full access;
    cross-tenant identity lookups by exact email go through the narrow SECURITY DEFINER
    helper `resolve_user_identity(TEXT)` instead of a table scan.
    """
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY users_allow_runtime ON users AS PERMISSIVE "
        "FOR ALL TO app_runtime USING (true) WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY users_tenant_scoped_read ON users AS RESTRICTIVE "
        f"FOR SELECT TO app_runtime USING ({_USERS_SCOPED_READ})"
    )
    op.execute(
        "CREATE POLICY users_migrator_auth_select ON users AS PERMISSIVE "
        "FOR SELECT TO app_migrator USING (true)"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION resolve_user_identity(p_email TEXT)
        RETURNS TABLE (id UUID, full_name TEXT)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        STABLE
        AS $fn$
            SELECT u.id, u.full_name
            FROM users u
            WHERE u.email = lower(btrim(p_email))
            LIMIT 1;
        $fn$;
    """)
    op.execute("REVOKE ALL ON FUNCTION resolve_user_identity(TEXT) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_user_identity(TEXT) TO app_runtime")


def _downgrade_users_rls() -> None:
    op.execute("REVOKE ALL ON FUNCTION resolve_user_identity(TEXT) FROM app_runtime")
    op.execute("DROP FUNCTION IF EXISTS resolve_user_identity(TEXT)")
    op.execute("DROP POLICY IF EXISTS users_migrator_auth_select ON users")
    op.execute("DROP POLICY IF EXISTS users_tenant_scoped_read ON users")
    op.execute("DROP POLICY IF EXISTS users_allow_runtime ON users")
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    # 1. AP payment idempotency ledger (W2-C finance hardening)
    _upgrade_payable_payments()

    # 2. Channel-payment idempotency at the DB layer
    _upgrade_payment_records_tx_ref_idempotency()

    # 3. Non-negative order money invariants
    _upgrade_orders_non_negative_amounts()

    # 4. Fail-closed RLS on every existing audit partition (GPT #21)
    _upgrade_audit_partition_rls()

    # 5. Identity table hardening (GPT #09)
    _upgrade_users_rls()


def downgrade() -> None:
    _downgrade_users_rls()
    _downgrade_audit_partition_rls()
    _downgrade_orders_non_negative_amounts()
    _downgrade_payment_records_tx_ref_idempotency()
    _downgrade_payable_payments()
