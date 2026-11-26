"""External notification channels (Feishu / DingTalk / WeCom) and event automation rules.

Three tenant-scoped tables close the "AI findings reach a human" loop:

1. `notification_channels` stores where an operator wants alerts delivered. The
   provider webhook signing secret is never stored in plaintext: only the AES-256-GCM
   envelope tuple (`secret_ciphertext`, `secret_nonce`, `secret_auth_tag`,
   `secret_key_version`) written by `EnvelopeCryptoService` with
   `AAD = f"{tenant_id}:{channel_id}:webhook_signing_secret"`.
2. `notification_deliveries` is the honest delivery ledger: a row is queued as
   `pending`, becomes `sent` with a real `http_status`, or moves to `retrying`
   (bounded backoff) and finally `failed`.
3. `automation_rules` holds the five event-driven templates and their thresholds.

Every table carries PG17 `ENABLE` + `FORCE ROW LEVEL SECURITY`, the permissive
`tenant_allow_runtime` policy, the restrictive `tenant_isolation_policy` and the
`app_runtime` grants, so an `app_runtime` connection can never read across tenants.

Revision ID: 0020_notifications_automation
Revises: 0019_analytics_im_sourcing
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_notifications_automation"
down_revision: str | None = "0019_analytics_im_sourcing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
# Partial-index predicate for the bounded retry scan.
_RETRYING_PREDICATE = "status = 'retrying'"


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
    # 1. Destination channels: Feishu / DingTalk / WeCom bots plus a generic webhook.
    op.execute("""
        CREATE TABLE notification_channels (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            provider TEXT NOT NULL
                CONSTRAINT ck_notification_channels_provider
                CHECK (provider IN ('feishu', 'dingtalk', 'wecom', 'generic_webhook')),
            webhook_url TEXT NOT NULL,
            secret_ciphertext BYTEA,
            secret_nonce BYTEA,
            secret_auth_tag BYTEA,
            secret_key_version INT,
            min_priority TEXT NOT NULL DEFAULT 'P1'
                CONSTRAINT ck_notification_channels_min_priority
                CHECK (min_priority IN ('P0', 'P1', 'P2')),
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            last_tested_at TIMESTAMPTZ,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.create_index(
        "idx_notification_channels_tenant_enabled",
        "notification_channels",
        ["tenant_id", "is_enabled"],
    )
    op.create_index(
        "idx_notification_channels_tenant_provider",
        "notification_channels",
        ["tenant_id", "provider"],
    )
    _apply_rls("notification_channels")

    # 2. Delivery ledger with bounded retries and the real HTTP outcome.
    op.execute("""
        CREATE TABLE notification_deliveries (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            channel_id UUID NOT NULL
                REFERENCES notification_channels(id) ON DELETE CASCADE,
            priority TEXT NOT NULL
                CONSTRAINT ck_notification_deliveries_priority
                CHECK (priority IN ('P0', 'P1', 'P2')),
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            deep_link TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CONSTRAINT ck_notification_deliveries_status
                CHECK (status IN ('pending', 'sent', 'failed', 'retrying')),
            attempts INT NOT NULL DEFAULT 0,
            next_retry_at TIMESTAMPTZ,
            http_status INT,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            sent_at TIMESTAMPTZ
        )
    """)
    op.create_index(
        "idx_notification_deliveries_tenant_created",
        "notification_deliveries",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "idx_notification_deliveries_channel_created",
        "notification_deliveries",
        ["channel_id", "created_at"],
    )
    op.create_index(
        "idx_notification_deliveries_retry_due",
        "notification_deliveries",
        ["tenant_id", "status", "next_retry_at"],
        postgresql_where=_RETRYING_PREDICATE,
    )
    _apply_rls("notification_deliveries")

    # 3. Event automation rules: one row per template per tenant.
    op.execute("""
        CREATE TABLE automation_rules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            template_code TEXT NOT NULL
                CONSTRAINT ck_automation_rules_template_code
                CHECK (template_code IN (
                    'low_doc_replenishment',
                    'negative_margin_hold',
                    'stalled_shipment_soothe',
                    'cold_b2b_quote_followup',
                    'daily_patrol_brief'
                )),
            name TEXT NOT NULL,
            is_enabled BOOLEAN NOT NULL DEFAULT true,
            autonomy_level TEXT NOT NULL DEFAULT 'L1_proposal'
                CONSTRAINT ck_automation_rules_autonomy_level
                CHECK (autonomy_level IN ('L0_copilot', 'L1_proposal', 'L2_auto')),
            priority TEXT NOT NULL DEFAULT 'P1'
                CONSTRAINT ck_automation_rules_priority
                CHECK (priority IN ('P0', 'P1', 'P2')),
            config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_run_at TIMESTAMPTZ,
            last_triggered_count INT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_automation_rules_tenant_tpl UNIQUE (tenant_id, template_code)
        )
    """)
    op.create_index(
        "idx_automation_rules_tenant_enabled",
        "automation_rules",
        ["tenant_id", "is_enabled"],
    )
    _apply_rls("automation_rules")


def downgrade() -> None:
    op.drop_table("automation_rules")
    op.drop_table("notification_deliveries")
    op.drop_table("notification_channels")
