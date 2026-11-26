"""Dual-basis profit reconciliation: platform settlement batches, lines, payout statements.

Three tenant tables close the loop between what an order *estimated* it would earn
(`order_profit_snapshots`) and what a marketplace *actually* paid out:

1. `settlement_batches` - one imported marketplace payout statement header per
   `(tenant, channel_type, batch_ref)`, in the statement currency, carrying the
   period, the locked FX rate to the tenant base currency, and every fee bucket
   (commission, fulfillment, storage, advertising, refund) plus the net payout.
2. `settlement_lines` - the per-order / per-SKU rows of a batch. `order_id` is
   auto-linked from `external_order_id` / `order_no` against `orders` in the same
   tenant; `net_settlement_base` is the base-currency net settlement.
3. `payout_statements` - cross-border collection statements (PingPong, LianLian,
   Payoneer, bank T/T, Stripe) with the *actual* FX rate, the *expected* FX rate
   locked on the receivable, and the resulting `fx_variance_base` gain/loss.

All money is PostgreSQL `NUMERIC` and every amount reaching Python stays a
`decimal.Decimal`; no `float` column exists in this revision.

Fail-closed tenancy: each table gets PG17 `ENABLE ROW LEVEL SECURITY` +
`FORCE ROW LEVEL SECURITY` with the `tenant_allow_runtime` PERMISSIVE and
`tenant_isolation_policy` RESTRICTIVE pair from `0003_dual_mode_erp_and_cvp`,
plus `GRANT SELECT, INSERT, UPDATE, DELETE ... TO app_runtime`.

Revision ID: 0022_finance_dual_settlement
Revises: 0021_b2b_buyer_portal
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_finance_dual_settlement"
down_revision: str | None = "0021_b2b_buyer_portal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTLEMENT_BATCHES = "settlement_batches"
SETTLEMENT_LINES = "settlement_lines"
PAYOUT_STATEMENTS = "payout_statements"
TENANT_TABLES: tuple[str, ...] = (SETTLEMENT_BATCHES, SETTLEMENT_LINES, PAYOUT_STATEMENTS)

#: Providers admitted by `ck_payout_statements_provider`.
PAYOUT_PROVIDERS: tuple[str, ...] = (
    "pingpong",
    "lianlian",
    "payoneer",
    "bank_tt",
    "stripe",
    "other",
)


def _apply_tenant_rls(table_name: str) -> None:
    """Fail-closed tenant isolation: `ENABLE` + `FORCE` RLS with a RESTRICTIVE policy."""
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")

    if op.get_context().as_sql:
        # `alembic upgrade head --sql` renders against a mock connection, so the
        # `pg_roles` catalog cannot be queried; assume the runtime role exists.
        runtime_role_present = True
    else:
        runtime_role_present = bool(
            op.get_bind()
            .execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime'"))
            .scalar()
        )
    if not runtime_role_present:
        return

    tenant_predicate = (
        "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
    )
    op.execute(
        f"CREATE POLICY tenant_allow_runtime ON {table_name} AS PERMISSIVE "
        "FOR ALL TO app_runtime USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"CREATE POLICY tenant_isolation_policy ON {table_name} AS RESTRICTIVE "
        f"FOR ALL TO app_runtime USING ({tenant_predicate}) "
        f"WITH CHECK ({tenant_predicate})"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table_name} TO app_runtime")


def upgrade() -> None:
    op.create_table(
        SETTLEMENT_BATCHES,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_type", sa.Text(), nullable=False),
        sa.Column("channel_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("batch_ref", sa.Text(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "gross_sales_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("commission_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "fulfillment_fee_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "storage_fee_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("ad_fee_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("refund_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("net_payout_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("fx_rate_to_base", sa.Numeric(12, 6), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'imported'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('imported', 'reconciled')", name="ck_settlement_batches_status"
        ),
        sa.CheckConstraint("period_end >= period_start", name="ck_settlement_batches_period"),
        sa.CheckConstraint("fx_rate_to_base > 0", name="ck_settlement_batches_fx_rate"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["channel_account_id"], ["channel_accounts.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_type",
            "batch_ref",
            name="uq_settlement_batches_tenant_ref",
        ),
    )
    op.create_index(
        "idx_settlement_batches_tenant_period",
        SETTLEMENT_BATCHES,
        ["tenant_id", sa.text("period_start DESC")],
    )
    op.create_index(
        "idx_settlement_batches_tenant_status",
        SETTLEMENT_BATCHES,
        ["tenant_id", "status"],
    )
    op.create_index(
        "idx_settlement_batches_tenant_channel",
        SETTLEMENT_BATCHES,
        ["tenant_id", "channel_type", "currency"],
    )

    op.create_table(
        SETTLEMENT_LINES,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_order_id", sa.Text(), nullable=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sku", sa.Text(), nullable=True),
        sa.Column(
            "gross_sales_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("commission_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "fulfillment_fee_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "storage_fee_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("ad_fee_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("refund_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "net_settlement_ccy", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("fx_rate_to_base", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "net_settlement_base", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("fx_rate_to_base > 0", name="ck_settlement_lines_fx_rate"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], [f"{SETTLEMENT_BATCHES}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_settlement_lines_tenant_batch", SETTLEMENT_LINES, ["tenant_id", "batch_id"]
    )
    op.create_index(
        "idx_settlement_lines_tenant_order", SETTLEMENT_LINES, ["tenant_id", "order_id"]
    )
    op.create_index(
        "idx_settlement_lines_tenant_ext_order",
        SETTLEMENT_LINES,
        ["tenant_id", "external_order_id"],
    )

    op.create_table(
        PAYOUT_STATEMENTS,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("external_tx_ref", sa.Text(), nullable=False),
        sa.Column("payout_currency", sa.CHAR(3), nullable=False),
        sa.Column("payout_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("settled_base_currency", sa.CHAR(3), nullable=False),
        sa.Column("settled_base_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("actual_fx_rate", sa.Numeric(12, 6), nullable=False),
        sa.Column("expected_fx_rate", sa.Numeric(12, 6), nullable=False),
        sa.Column("fx_variance_base", sa.Numeric(14, 2), nullable=False),
        sa.Column("matched_settlement_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("matched_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("match_status", sa.Text(), server_default=sa.text("'unmatched'"), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('pingpong', 'lianlian', 'payoneer', 'bank_tt', 'stripe', 'other')",
            name="ck_payout_statements_provider",
        ),
        sa.CheckConstraint(
            "match_status IN ('unmatched', 'matched')", name="ck_payout_statements_match_status"
        ),
        sa.CheckConstraint("payout_amount > 0", name="ck_payout_statements_payout_amount"),
        sa.CheckConstraint(
            "actual_fx_rate > 0 AND expected_fx_rate > 0", name="ck_payout_statements_fx_rates"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["matched_settlement_batch_id"], [f"{SETTLEMENT_BATCHES}.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["matched_order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "external_tx_ref",
            name="uq_payout_statements_tenant_ref",
        ),
    )
    op.create_index(
        "idx_payout_statements_tenant_status",
        PAYOUT_STATEMENTS,
        ["tenant_id", "match_status"],
    )
    op.create_index(
        "idx_payout_statements_tenant_paid_at",
        PAYOUT_STATEMENTS,
        ["tenant_id", sa.text("paid_at DESC")],
    )
    op.create_index(
        "idx_payout_statements_tenant_currency",
        PAYOUT_STATEMENTS,
        ["tenant_id", "payout_currency"],
    )

    for table_name in TENANT_TABLES:
        _apply_tenant_rls(table_name)


def downgrade() -> None:
    op.drop_index("idx_payout_statements_tenant_currency", table_name=PAYOUT_STATEMENTS)
    op.drop_index("idx_payout_statements_tenant_paid_at", table_name=PAYOUT_STATEMENTS)
    op.drop_index("idx_payout_statements_tenant_status", table_name=PAYOUT_STATEMENTS)
    op.drop_table(PAYOUT_STATEMENTS)

    op.drop_index("idx_settlement_lines_tenant_ext_order", table_name=SETTLEMENT_LINES)
    op.drop_index("idx_settlement_lines_tenant_order", table_name=SETTLEMENT_LINES)
    op.drop_index("idx_settlement_lines_tenant_batch", table_name=SETTLEMENT_LINES)
    op.drop_table(SETTLEMENT_LINES)

    op.drop_index("idx_settlement_batches_tenant_channel", table_name=SETTLEMENT_BATCHES)
    op.drop_index("idx_settlement_batches_tenant_status", table_name=SETTLEMENT_BATCHES)
    op.drop_index("idx_settlement_batches_tenant_period", table_name=SETTLEMENT_BATCHES)
    op.drop_table(SETTLEMENT_BATCHES)
