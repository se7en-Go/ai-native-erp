from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from erp_core.shared.config import get_settings
from erp_core.shared.db import (
    create_migrator_engine,
    create_runtime_engine,
    create_session_factory,
    tenant_session,
)


@pytest.fixture
async def migrator_engine() -> AsyncEngine:
    engine = create_migrator_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def runtime_engine() -> AsyncEngine:
    engine = create_runtime_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ai_billing_tables_indexes_and_views_exist(migrator_engine: AsyncEngine) -> None:
    expected_tables = {
        "kb_documents",
        "kb_chunks",
        "ai_proposals",
        "ai_eval_feedback",
        "credit_wallets",
        "usage_ledger",
    }
    expected_indexes = {
        "idx_kb_chunks_tsv_gin",
        "idx_kb_chunks_embedding_hnsw",
        "idx_ai_proposals_inbox",
        "uq_usage_ledger_tenant_idempotency",
    }

    async with migrator_engine.connect() as conn:
        table_rows = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = ANY(:names)"
            ),
            {"names": list(expected_tables)},
        )
        found_tables = {row[0] for row in table_rows.fetchall()}
        assert found_tables == expected_tables
        forced = await conn.scalars(
            text(
                "SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity "
                "AND relnamespace = 'public'::regnamespace AND relname = ANY(:tables)"
            ),
            {"tables": list(expected_tables)},
        )
        assert set(forced) == expected_tables

        col_row = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'tenants' "
                "AND column_name = 'ai_policy_settings'"
            )
        )
        assert col_row.scalar_one_or_none() == "ai_policy_settings"

        idx_rows = await conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = ANY(:names)"
            ),
            {"names": list(expected_indexes)},
        )
        idx_map = {row[0]: row[1] for row in idx_rows.fetchall()}
        assert set(idx_map.keys()) == expected_indexes
        assert "gin" in idx_map["idx_kb_chunks_tsv_gin"].lower()
        assert "hnsw" in idx_map["idx_kb_chunks_embedding_hnsw"].lower()
        assert "vector_cosine_ops" in idx_map["idx_kb_chunks_embedding_hnsw"].lower()
        assert "WHERE (status = 'pending'::text)" in idx_map["idx_ai_proposals_inbox"] or (
            "WHERE (status = 'pending')" in idx_map["idx_ai_proposals_inbox"]
        )

        view_rows = await conn.execute(
            text(
                "SELECT c.relname, c.reloptions FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'v' "
                "AND c.relname = ANY(:views)"
            ),
            {"views": ["vw_tenant_order_profit_summary", "vw_tenant_inventory_risk_summary"]},
        )
        views = {row[0]: row[1] or [] for row in view_rows.fetchall()}
        assert set(views.keys()) == {
            "vw_tenant_order_profit_summary",
            "vw_tenant_inventory_risk_summary",
        }
        assert "security_invoker=true" in views["vw_tenant_order_profit_summary"]
        assert "security_invoker=true" in views["vw_tenant_inventory_risk_summary"]


@pytest.mark.asyncio
async def test_rls_isolation_and_usage_ledger_idempotency(
    migrator_engine: AsyncEngine,
    runtime_engine: AsyncEngine,
) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    doc_a = uuid4()
    chunk_a = uuid4()
    proposal_a = uuid4()
    runtime_factory = create_session_factory(runtime_engine)
    zero_vec = "[" + ",".join(["0.01"] * 1536) + "]"

    for tid, slug in [(tenant_a, f"t-a-{tenant_a.hex[:8]}"), (tenant_b, f"t-b-{tenant_b.hex[:8]}")]:
        async with tenant_session(runtime_factory, tid) as setup_sess:
            await setup_sess.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": tid, "name": slug, "slug": slug},
            )

    try:
        # 1. Fail-closed check: runtime session WITHOUT app.current_tenant_id cannot read or write
        async with runtime_factory() as bare_session:
            with pytest.raises(DBAPIError):
                async with bare_session.begin():
                    await bare_session.execute(
                        text(
                            "INSERT INTO kb_documents (id, tenant_id, title, source_type, "
                            "doc_hash) "
                            "VALUES (:id, :tid, 'Fail', 'markdown', 'hash-fail')"
                        ),
                        {"id": uuid4(), "tid": tenant_a},
                    )

        # 2. Tenant A writes kb_documents, kb_chunks, ai_proposals, credit_wallets, usage_ledger
        async with tenant_session(runtime_factory, tenant_a) as session_a:
            await session_a.execute(
                text(
                    "INSERT INTO kb_documents (id, tenant_id, title, source_type, doc_hash) "
                    "VALUES (:id, :tid, 'XJ-400Pro Spec', 'markdown', 'hash-a1')"
                ),
                {"id": doc_a, "tid": tenant_a},
            )
            await session_a.execute(
                text(
                    "INSERT INTO kb_chunks (id, tenant_id, document_id, chunk_index, content, "
                    "embedding, has_dynamic_price_disclaimer) "
                    "VALUES (:id, :tid, :doc_id, 0, 'XJ-400Pro-EU IP66 waterproof 5-year "
                    "warranty', CAST(CAST(:emb AS text) AS vector), false)"
                ),
                {"id": chunk_a, "tid": tenant_a, "doc_id": doc_a, "emb": zero_vec},
            )
            await session_a.execute(
                text(
                    "INSERT INTO ai_proposals (id, tenant_id, capability, proposal_type, "
                    "proposed_payload, confidence_score, status) "
                    "VALUES (:id, :tid, 'cs_support', 'reply_draft', '{\"body_text\": "
                    "\"Hello\"}'::jsonb, 0.910, 'pending')"
                ),
                {"id": proposal_a, "tid": tenant_a},
            )
            await session_a.execute(
                text(
                    "INSERT INTO credit_wallets (tenant_id, plan_tier, "
                    "included_credits_remaining, billing_cycle_end) "
                    "VALUES (:tid, 'growth', 8000, :cycle_end)"
                ),
                {"tid": tenant_a, "cycle_end": datetime.now(UTC) + timedelta(days=30)},
            )
            await session_a.execute(
                text(
                    "INSERT INTO usage_ledger (tenant_id, idempotency_key, metric_type, "
                    "units_consumed, raw_provider_cost_usd, model_or_provider) "
                    "VALUES (:tid, 'idem-001', 'ai_credit', 2.0000, 0.005000, "
                    "'gemini-3.1-flash-lite')"
                ),
                {"tid": tenant_a},
            )

            await session_a.execute(
                text("""
                INSERT INTO ai_eval_feedback (tenant_id, proposal_id, capability, proposal_type,
                    original_payload, edited_payload, diff_patch, created_by_user_id)
                VALUES (:tid, :pid, 'cs_support', 'reply_draft', '{}', '{}', '{}', :uid)
            """),
                {"tid": tenant_a, "pid": proposal_a, "uid": uuid4()},
            )

        # 3. Tenant B cannot see ANY of Tenant A's rows
        async with tenant_session(runtime_factory, tenant_b) as session_b:
            for tbl in (
                "kb_documents",
                "kb_chunks",
                "ai_proposals",
                "ai_eval_feedback",
                "credit_wallets",
                "usage_ledger",
            ):
                cnt = (await session_b.execute(text(f"SELECT COUNT(*) FROM {tbl}"))).scalar_one()
                assert cnt == 0, f"Tenant B leaked rows in {tbl}"

        # 4. Duplicate idempotency_key in usage_ledger for Tenant A raises IntegrityError
        with pytest.raises(IntegrityError):
            async with tenant_session(runtime_factory, tenant_a) as session_a_dup:
                await session_a_dup.execute(
                    text(
                        "INSERT INTO usage_ledger (tenant_id, idempotency_key, metric_type, "
                        "units_consumed, raw_provider_cost_usd, model_or_provider) "
                        "VALUES (:tid, 'idem-001', 'ai_credit', 1.0000, 0.002500, "
                        "'gemini-3.1-flash-lite')"
                    ),
                    {"tid": tenant_a},
                )

        # 5. Tenant A can read its own tsvector generated column
        async with tenant_session(runtime_factory, tenant_a) as session_a_read:
            matched = (
                await session_a_read.execute(
                    text(
                        "SELECT id FROM kb_chunks "
                        "WHERE tsv @@ websearch_to_tsquery('simple', 'XJ-400Pro-EU')"
                    )
                )
            ).scalar_one()
            assert matched == chunk_a
    finally:
        admin = create_async_engine(
            os.environ.get(
                "DATABASE_ADMIN_URL",
                "postgresql+asyncpg://postgres:postgres_dev_pass@127.0.0.1:5432/erp_saas",
            )
        )
        try:
            async with admin.begin() as conn:
                await conn.execute(
                    text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [tenant_a, tenant_b]}
                )
        finally:
            await admin.dispose()


@pytest.mark.asyncio
async def test_0004_round_trip_in_rolled_back_transaction(migrator_engine: AsyncEngine) -> None:
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0004_ai_rag_proposals_billing.py"
    spec = spec_from_file_location("revision_0004", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    def round_trip(conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            assert migration.revision == "0004"
            assert migration.down_revision == "0003c_channel_routing"
            migration.downgrade()
            assert conn.scalar(text("SELECT to_regclass('kb_documents')")) is None
            assert conn.scalar(text("SELECT to_regclass('usage_ledger')")) is None
            assert conn.scalar(text("SELECT to_regclass('vw_tenant_order_profit_summary')")) is None
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE "
                        "table_name = 'tenants' AND column_name = 'ai_policy_settings'"
                    )
                )
                == 0
            )
            migration.upgrade()
            assert conn.scalar(text("SELECT to_regclass('kb_chunks')::text")) == "kb_chunks"
            assert conn.scalar(text("SELECT to_regclass('usage_ledger')::text")) == "usage_ledger"

    async with migrator_engine.connect() as conn:
        try:
            await conn.run_sync(round_trip)
        finally:
            await conn.rollback()
