"""Add per-user channel ownership, visibility scope, and private customer isolation.

Migration 0009: multi-account / per-salesperson channel ownership.

Every channel account is either a company asset (`visibility_scope = 'shared'`,
`owner_user_id IS NULL`) or a private asset bound to one workspace member
(`visibility_scope = 'personal'`, `owner_user_id` NOT NULL). The CHECK constraints make the
half-bound state unrepresentable in PostgreSQL, not just in application code.
"""

from __future__ import annotations

from alembic import op

revision = "0009_channel_account_ownership"
down_revision = "0008_phase3d_social_matrix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE channel_accounts "
        "ADD COLUMN owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE channel_accounts "
        "ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'shared'"
    )
    op.execute(
        "ALTER TABLE channel_accounts "
        "ADD CONSTRAINT ck_channel_accounts_visibility_scope "
        "CHECK (visibility_scope IN ('shared', 'personal'))"
    )
    op.execute(
        "ALTER TABLE channel_accounts "
        "ADD CONSTRAINT ck_channel_accounts_personal_owner "
        "CHECK (visibility_scope <> 'personal' OR owner_user_id IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX idx_channel_accounts_tenant_owner "
        "ON channel_accounts (tenant_id, owner_user_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_channel_accounts_tenant_owner")
    op.execute(
        "ALTER TABLE channel_accounts "
        "DROP CONSTRAINT IF EXISTS ck_channel_accounts_personal_owner"
    )
    op.execute(
        "ALTER TABLE channel_accounts "
        "DROP CONSTRAINT IF EXISTS ck_channel_accounts_visibility_scope"
    )
    op.execute("ALTER TABLE channel_accounts DROP COLUMN IF EXISTS visibility_scope")
    op.execute("ALTER TABLE channel_accounts DROP COLUMN IF EXISTS owner_user_id")
