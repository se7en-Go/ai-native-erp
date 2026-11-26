"""Team store-scope governance, outbound red-line guard, and KB gap backlog.

Three tenant-scoped tables, each forced through PG17 row level security so the
`app_runtime` role can never read across tenants even if a query forgets a
`WHERE tenant_id = ...` predicate:

1. `member_channel_scopes` - which team member may operate which connected store
   / channel account, and at what access level (`read` / `operate` / `manage`).
   The same table carries the reassignment payload of the offboarding handover,
   so a departing member's store grants move to the successor.
2. `outbound_redline_rules` - operator-authored outbound content red lines
   (`keyword` or `regex`) evaluated before any AI-drafted or human reply leaves
   the building.
3. `kb_gap_items` - the customer-question backlog the knowledge base cannot
   answer yet, with the promote-to-KB link back to the generated
   `kb_documents` row.

Revision ID: 0025_team_ops_kb_gaps
Revises: 0024_order_routing_labels
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025_team_ops_kb_gaps"
down_revision: str | None = "0024_order_routing_labels"
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
        CREATE TABLE member_channel_scopes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            channel_account_id UUID NOT NULL REFERENCES channel_accounts(id) ON DELETE CASCADE,
            access_level TEXT NOT NULL DEFAULT 'operate',
            CONSTRAINT ck_member_channel_scopes_access_level
                CHECK (access_level IN ('read', 'operate', 'manage')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_member_channel_scopes_unique
                UNIQUE (tenant_id, user_id, channel_account_id)
        )
    """)
    op.create_index(
        "ix_member_channel_scopes_user",
        "member_channel_scopes",
        ["tenant_id", "user_id"],
    )
    op.create_index(
        "ix_member_channel_scopes_account",
        "member_channel_scopes",
        ["tenant_id", "channel_account_id"],
    )
    _apply_rls("member_channel_scopes")

    op.execute("""
        CREATE TABLE outbound_redline_rules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            pattern TEXT NOT NULL,
            match_type TEXT NOT NULL DEFAULT 'keyword',
            CONSTRAINT ck_outbound_redline_rules_match_type
                CHECK (match_type IN ('keyword', 'regex')),
            action TEXT NOT NULL DEFAULT 'block',
            CONSTRAINT ck_outbound_redline_rules_action
                CHECK (action IN ('block', 'warn')),
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.create_index(
        "ix_outbound_redline_rules_tenant_enabled",
        "outbound_redline_rules",
        ["tenant_id", "is_enabled"],
    )
    _apply_rls("outbound_redline_rules")

    op.execute("""
        CREATE TABLE kb_gap_items (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
            customer_question TEXT NOT NULL,
            detected_language TEXT NOT NULL DEFAULT 'en',
            frequency_count INT NOT NULL DEFAULT 1,
            CONSTRAINT ck_kb_gap_items_frequency_count CHECK (frequency_count >= 1),
            status TEXT NOT NULL DEFAULT 'open',
            CONSTRAINT ck_kb_gap_items_status
                CHECK (status IN ('open', 'resolved', 'dismissed')),
            resolved_answer TEXT,
            -- Logical link to kb_documents(id) rather than a hard FK so 0004's
            -- standalone downgrade round-trip test can drop kb_documents cleanly.
            promoted_kb_document_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.create_index(
        "ix_kb_gap_items_tenant_status",
        "kb_gap_items",
        ["tenant_id", "status", "frequency_count"],
    )
    op.create_index(
        "uq_kb_gap_items_open_question",
        "kb_gap_items",
        ["tenant_id", "customer_question"],
        unique=True,
        postgresql_where="status = 'open'",
    )
    _apply_rls("kb_gap_items")


def downgrade() -> None:
    op.drop_index("uq_kb_gap_items_open_question", table_name="kb_gap_items")
    op.drop_index("ix_kb_gap_items_tenant_status", table_name="kb_gap_items")
    op.drop_table("kb_gap_items")

    op.drop_index("ix_outbound_redline_rules_tenant_enabled", table_name="outbound_redline_rules")
    op.drop_table("outbound_redline_rules")

    op.drop_index("ix_member_channel_scopes_account", table_name="member_channel_scopes")
    op.drop_index("ix_member_channel_scopes_user", table_name="member_channel_scopes")
    op.drop_table("member_channel_scopes")
