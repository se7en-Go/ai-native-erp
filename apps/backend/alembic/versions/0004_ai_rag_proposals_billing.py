"""Add AI engine Hybrid RAG, L0-L2 governance proposals, credit wallets, and usage ledger.

Revision ID: 0004
Revises: 0003c_channel_routing
Create Date: 2026-11-19 09:45:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003c_channel_routing"
branch_labels = None
depends_on = None

_TENANT_RLS_EXPR = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"


def _ddl(script: str) -> None:
    # asyncpg prepares each statement individually. All statements here are static DDL.
    for statement in script.split(";"):
        if statement.strip():
            op.execute(statement.strip())


def _enable_force_rls(table_name: str) -> None:
    _ddl(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;")
    _ddl(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;")
    _ddl(
        f"""
        CREATE POLICY tenant_allow_runtime ON {table_name}
            AS PERMISSIVE
            FOR ALL
            TO app_runtime
            USING (true)
            WITH CHECK (true);
        CREATE POLICY tenant_isolation_policy ON {table_name}
            AS RESTRICTIVE
            FOR ALL
            TO app_runtime
            USING ({_TENANT_RLS_EXPR})
            WITH CHECK ({_TENANT_RLS_EXPR});
        """
    )


def upgrade() -> None:
    # Installed by the cluster bootstrap; the migrator is not a superuser.
    _ddl("CREATE EXTENSION IF NOT EXISTS vector")

    _ddl(
        """
        ALTER TABLE tenants
            ADD COLUMN IF NOT EXISTS ai_policy_settings JSONB NOT NULL DEFAULT '{}'::jsonb;
        """
    )

    _ddl(
        """
        CREATE TABLE kb_documents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK (
                source_type IN ('markdown', 'pdf', 'csv', 'faq_json', 'policy_doc')
            ),
            source_uri TEXT,
            doc_hash TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'en',
            is_active BOOLEAN NOT NULL DEFAULT true,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id)
        );
        CREATE INDEX idx_kb_documents_tenant_active
            ON kb_documents (tenant_id, is_active, created_at DESC);
        CREATE INDEX idx_kb_documents_tenant_hash
            ON kb_documents (tenant_id, doc_hash);
        """
    )
    _enable_force_rls("kb_documents")
    _ddl("GRANT SELECT, INSERT, UPDATE, DELETE ON kb_documents TO app_runtime;")

    _ddl(
        """
        CREATE TABLE kb_chunks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            document_id UUID NOT NULL,
            chunk_index INT NOT NULL CHECK (chunk_index >= 0),
            content TEXT NOT NULL,
            tsv TSVECTOR GENERATED ALWAYS AS (
                to_tsvector('simple', regexp_replace(content, '([一-龯])', ' \\1 ', 'g'))
            ) STORED NOT NULL,
            embedding vector(1536) NOT NULL,
            has_dynamic_price_disclaimer BOOLEAN NOT NULL DEFAULT false,
            token_count INT NOT NULL DEFAULT 0,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, document_id)
                REFERENCES kb_documents(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_kb_chunks_doc_idx UNIQUE (tenant_id, document_id, chunk_index)
        );
        CREATE INDEX idx_kb_chunks_tenant_doc
            ON kb_chunks (tenant_id, document_id, chunk_index);
        CREATE INDEX idx_kb_chunks_tsv_gin
            ON kb_chunks USING GIN (tsv);
        CREATE INDEX idx_kb_chunks_embedding_hnsw
            ON kb_chunks USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """
    )
    _enable_force_rls("kb_chunks")
    _ddl("GRANT SELECT, INSERT, UPDATE, DELETE ON kb_chunks TO app_runtime;")

    _ddl(
        """
        CREATE TABLE ai_proposals (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            capability TEXT NOT NULL CHECK (
                capability IN (
                    'cs_support', 'sales_quoting', 'order_logistics',
                    'content_studio', 'biz_analytics', 'finance_recon'
                )
            ),
            proposal_type TEXT NOT NULL CHECK (
                proposal_type IN (
                    'reply_draft', 'crm_entity_extract', 'quote_draft',
                    'follow_up_suggestion', 'identity_merge',
                    'refund_or_reship', 'payment_match'
                )
            ),
            conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
            target_entity_type TEXT,
            target_entity_id UUID,
            proposed_payload JSONB NOT NULL,
            citations JSONB NOT NULL DEFAULT '[]'::jsonb,
            confidence_score NUMERIC(4, 3) NOT NULL,
            estimated_margin_pct NUMERIC(5, 2),
            status TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN (
                    'pending', 'approved', 'edited_and_approved',
                    'rejected', 'expired', 'failed'
                )
            ),
            reviewed_by_user_id UUID,
            edited_payload JSONB,
            rejection_reason TEXT,
            execution_result JSONB,
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reviewed_at TIMESTAMPTZ,
            UNIQUE (tenant_id, id)
        );
        CREATE INDEX idx_ai_proposals_inbox
            ON ai_proposals (tenant_id, status, created_at DESC)
            WHERE status = 'pending';
        CREATE INDEX idx_ai_proposals_tenant_capability
            ON ai_proposals (tenant_id, capability, created_at DESC);
        """
    )
    _enable_force_rls("ai_proposals")
    _ddl("GRANT SELECT, INSERT, UPDATE, DELETE ON ai_proposals TO app_runtime;")

    _ddl(
        """
        CREATE TABLE ai_eval_feedback (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            proposal_id UUID NOT NULL,
            capability TEXT NOT NULL CHECK (
                capability IN (
                    'cs_support', 'sales_quoting', 'order_logistics',
                    'content_studio', 'biz_analytics', 'finance_recon'
                )
            ),
            proposal_type TEXT NOT NULL,
            original_payload JSONB NOT NULL,
            edited_payload JSONB NOT NULL,
            diff_patch JSONB NOT NULL,
            feedback_note TEXT,
            embedding vector(1536),
            created_by_user_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, proposal_id)
                REFERENCES ai_proposals(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_ai_eval_feedback_proposal UNIQUE (tenant_id, proposal_id)
        );
        CREATE INDEX idx_ai_eval_feedback_tenant_cap
            ON ai_eval_feedback (tenant_id, capability, created_at DESC);
        """
    )
    _enable_force_rls("ai_eval_feedback")
    _ddl("GRANT SELECT, INSERT, UPDATE, DELETE ON ai_eval_feedback TO app_runtime;")

    _ddl(
        """
        CREATE TABLE credit_wallets (
            tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            plan_tier TEXT NOT NULL DEFAULT 'starter' CHECK (
                plan_tier IN ('starter', 'growth', 'scale', 'enterprise')
            ),
            included_credits_remaining BIGINT NOT NULL DEFAULT 0
                CHECK (included_credits_remaining >= 0),
            topup_credits_balance BIGINT NOT NULL DEFAULT 0
                CHECK (topup_credits_balance >= 0),
            fractional_credit_carry NUMERIC(10, 4) NOT NULL DEFAULT 0,
            overage_spend_cap_cents BIGINT NOT NULL DEFAULT 5000
                CHECK (overage_spend_cap_cents >= 0),
            overage_spent_cents BIGINT NOT NULL DEFAULT 0 CHECK (overage_spent_cents >= 0),
            auto_downgrade_to_l0 BOOLEAN NOT NULL DEFAULT true,
            billing_cycle_end TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    _enable_force_rls("credit_wallets")
    _ddl("GRANT SELECT, INSERT, UPDATE, DELETE ON credit_wallets TO app_runtime;")

    _ddl(
        """
        CREATE TABLE usage_ledger (
            id BIGINT GENERATED ALWAYS AS IDENTITY,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            idempotency_key TEXT NOT NULL,
            metric_type TEXT NOT NULL CHECK (
                metric_type IN (
                    'ai_credit', 'whatsapp_template', 'whatsapp_service', 'tracking_quota'
                )
            ),
            units_consumed NUMERIC(12, 4) NOT NULL,
            raw_provider_cost_usd NUMERIC(10, 6) NOT NULL,
            model_or_provider TEXT NOT NULL,
            stripe_meter_synced BOOLEAN NOT NULL DEFAULT false,
            stripe_meter_event_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT uq_usage_ledger_tenant_idempotency UNIQUE (tenant_id, idempotency_key)
        );
        CREATE INDEX idx_usage_ledger_unsynced
            ON usage_ledger (tenant_id, created_at)
            WHERE stripe_meter_synced = false;
        CREATE INDEX idx_usage_ledger_tenant_created
            ON usage_ledger (tenant_id, created_at DESC);
        """
    )
    _enable_force_rls("usage_ledger")
    _ddl(
        """
        GRANT SELECT, INSERT ON usage_ledger TO app_runtime;
        GRANT UPDATE (stripe_meter_synced, stripe_meter_event_id) ON usage_ledger TO app_runtime;
        """
    )

    _ddl(
        """
        CREATE OR REPLACE VIEW vw_tenant_order_profit_summary
        WITH (security_invoker = true) AS
        SELECT
            o.tenant_id,
            o.order_mode,
            o.currency,
            o.fulfillment_status,
            o.payment_status,
            DATE_TRUNC('day', o.created_at) AS order_day,
            COUNT(o.id) AS order_count,
            COALESCE(SUM(o.total_amount), 0) AS total_order_amount,
            COALESCE(SUM(ops.gross_revenue_base), 0) AS gross_revenue_base,
            COALESCE(SUM(ops.cogs_base), 0) AS cogs_base,
            COALESCE(SUM(ops.net_contribution_margin_base), 0) AS net_contribution_margin_base
        FROM orders o
        LEFT JOIN order_profit_snapshots ops
            ON ops.order_id = o.id AND ops.tenant_id = o.tenant_id
        GROUP BY
            o.tenant_id,
            o.order_mode,
            o.currency,
            o.fulfillment_status,
            o.payment_status,
            DATE_TRUNC('day', o.created_at);

        CREATE OR REPLACE VIEW vw_tenant_inventory_risk_summary
        WITH (security_invoker = true) AS
        SELECT
            ib.tenant_id,
            ib.warehouse_id,
            pv.id AS variant_id,
            pv.sku,
            p.spu_code,
            p.category,
            ib.on_hand_qty,
            ib.reserved_qty,
            (ib.on_hand_qty - ib.reserved_qty) AS available_qty,
            pv.lead_time_days
        FROM inventory_balances ib
        JOIN product_variants pv
            ON pv.id = ib.variant_id AND pv.tenant_id = ib.tenant_id
        JOIN products p
            ON p.id = pv.product_id AND p.tenant_id = pv.tenant_id;

        GRANT SELECT ON vw_tenant_order_profit_summary TO app_runtime;
        GRANT SELECT ON vw_tenant_inventory_risk_summary TO app_runtime;
        """
    )


def downgrade() -> None:
    _ddl("DROP VIEW IF EXISTS vw_tenant_inventory_risk_summary;")
    _ddl("DROP VIEW IF EXISTS vw_tenant_order_profit_summary;")
    _ddl("DROP TABLE IF EXISTS usage_ledger;")
    _ddl("DROP TABLE IF EXISTS credit_wallets;")
    _ddl("DROP TABLE IF EXISTS ai_eval_feedback;")
    _ddl("DROP TABLE IF EXISTS ai_proposals;")
    _ddl("DROP TABLE IF EXISTS kb_chunks;")
    _ddl("DROP TABLE IF EXISTS kb_documents;")
    _ddl("ALTER TABLE tenants DROP COLUMN IF EXISTS ai_policy_settings;")
