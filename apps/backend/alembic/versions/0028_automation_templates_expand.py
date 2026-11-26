"""Track 10 polish: two new automation templates + scan-to-ship dispatch ledger.

Two independent changes, both strictly additive:

1. `automation_rules.ck_automation_rules_template_code` grows from five to seven
   templates. `negative_review_ticket` turns a 1-star/complaint/defect inbound
   message into an L1 service ticket proposal, `high_adoption_faq_extract` turns an
   approved high-confidence AI reply into a structured FAQ knowledge-base
   extraction. The constraint is dropped and re-created (never widened in place) so
   the vocabulary stays explicit and auditable.
2. `scan_dispatch_events` - the warehouse barcode scan ledger. One row per scan,
   including the blocked scans (unpaid B2B balance / active negative-margin hold),
   so an operator can prove why a parcel was *not* released. Tenant-scoped through
   PG17 `ENABLE`+`FORCE ROW LEVEL SECURITY`, exactly like every other tenant table.

No hard foreign key points at `kb_documents` or any other table owned by revision
`0004`, so that migration keeps its standalone downgrade round-trip.

Revision ID: 0028_automation_templates_expand
Revises: 0027_visual_jobs_and_ads_sync
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_automation_templates_expand"
down_revision: str | None = "0027_visual_jobs_and_ads_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"

#: The five templates shipped by `0020_notifications_automation`.
_LEGACY_TEMPLATE_CODES: tuple[str, ...] = (
    "low_doc_replenishment",
    "negative_margin_hold",
    "stalled_shipment_soothe",
    "cold_b2b_quote_followup",
    "daily_patrol_brief",
)

#: Full vocabulary after this revision.
_TEMPLATE_CODES: tuple[str, ...] = (
    *_LEGACY_TEMPLATE_CODES,
    "negative_review_ticket",
    "high_adoption_faq_extract",
)


def _template_code_check(codes: tuple[str, ...]) -> str:
    values = ",\n                    ".join(f"'{code}'" for code in codes)
    return (
        "ALTER TABLE automation_rules ADD CONSTRAINT ck_automation_rules_template_code "
        f"CHECK (template_code IN (\n                    {values}\n                ))"
    )


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
    op.execute(
        "ALTER TABLE automation_rules "
        "DROP CONSTRAINT IF EXISTS ck_automation_rules_template_code"
    )
    op.execute(_template_code_check(_TEMPLATE_CODES))

    op.execute("""
        CREATE TABLE scan_dispatch_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            carrier_shipping_label_id UUID
                REFERENCES carrier_shipping_labels(id) ON DELETE SET NULL,
            scanned_code TEXT NOT NULL,
            tracking_number TEXT,
            outcome TEXT NOT NULL DEFAULT 'dispatched'
                CONSTRAINT ck_scan_dispatch_events_outcome
                CHECK (outcome IN ('dispatched', 'blocked_balance', 'blocked_margin_hold')),
            dispatched_by UUID REFERENCES users(id) ON DELETE SET NULL,
            dispatched_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.create_index(
        "ix_scan_dispatch_events_tenant_order",
        "scan_dispatch_events",
        ["tenant_id", "order_id", "dispatched_at"],
    )
    op.create_index(
        "ix_scan_dispatch_events_tenant_code",
        "scan_dispatch_events",
        ["tenant_id", "scanned_code"],
    )
    _apply_rls("scan_dispatch_events")


def downgrade() -> None:
    op.drop_index("ix_scan_dispatch_events_tenant_code", table_name="scan_dispatch_events")
    op.drop_index("ix_scan_dispatch_events_tenant_order", table_name="scan_dispatch_events")
    op.drop_table("scan_dispatch_events")

    op.execute(
        "ALTER TABLE automation_rules "
        "DROP CONSTRAINT IF EXISTS ck_automation_rules_template_code"
    )
    # The restored vocabulary rejects the two Track 10 templates, so their rule rows must
    # go first (a rule is derived configuration — thresholds only — never business data).
    # `automation_rules` is FORCE RLS and this DELETE runs as the table owner, which FORCE
    # subjects to the policies; only `app_runtime` holds a policy, so the owner would
    # silently match zero rows and the narrower CHECK would then fail validation. Toggling
    # FORCE off for exactly this statement inside the migration transaction is the smallest
    # correct escape hatch: the table stays ENABLEd and FORCE is restored immediately.
    op.execute("ALTER TABLE automation_rules NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "DELETE FROM automation_rules WHERE template_code IN "
        "('negative_review_ticket', 'high_adoption_faq_extract')"
    )
    op.execute("ALTER TABLE automation_rules ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE automation_rules FORCE ROW LEVEL SECURITY")
    op.execute(_template_code_check(_LEGACY_TEMPLATE_CODES))
