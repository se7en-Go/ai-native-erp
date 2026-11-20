"""Add deterministic ERP tool provenance to AI proposals.

Migration 0010: grounded auto-proposal provenance.

A customer reply draft may only be dispatched straight from the approval queue when its
numeric claims are traceable to a verified `ErpToolRegistry` SQL call. `source_tool_call_id`
records that provenance on the proposal row itself so the executor can enforce the
zero-hallucination invariant (`source_tool_call_id IS NOT NULL`) instead of guessing from
free-form text.
"""

from __future__ import annotations

from alembic import op

revision = "0010_ai_proposal_grounding"
down_revision = "0009_channel_account_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai_proposals ADD COLUMN source_tool_call_id UUID")
    op.execute(
        "CREATE INDEX idx_ai_proposals_tenant_source_tool "
        "ON ai_proposals (tenant_id, source_tool_call_id) "
        "WHERE source_tool_call_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_ai_proposals_tenant_source_tool")
    op.execute("ALTER TABLE ai_proposals DROP COLUMN IF EXISTS source_tool_call_id")
