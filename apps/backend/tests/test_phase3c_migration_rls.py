"""Verify the Phase 3C revision and its fail-closed RLS against real PostgreSQL."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.db import tenant_session

REVISION = "0007_phase3c_growth_leads"
PARENT = "0006_phase3b_ai_employees"

JOB_COLUMNS = (
    "source_provider, keywords, target_region, max_results, min_rating, require_phone, "
    "created_by"
)
JOB_VALUES = (
    "'overpass_osm', CAST('[\"lighting distributor\"]' AS jsonb), 'Berlin, DE', 50, "
    "CAST('4.00' AS numeric(3,2)), true, :actor"
)
CAMPAIGN_COLUMNS = "name, channel_type, subject, body_template, created_by"
CAMPAIGN_VALUES = (
    "'Dubai LED wholesale outreach', 'email', 'COB strip catalogue', "
    "'Hello {{company_name}},', :actor"
)


async def _seed_parent_rows(session: AsyncSession, params: dict[str, object]) -> tuple[str, str]:
    """Create the job and campaign a prospect/recipient row has to reference."""
    job_id = await session.scalar(
        text(
            f"INSERT INTO lead_collection_jobs (tenant_id, {JOB_COLUMNS}) "
            f"VALUES (:tid, {JOB_VALUES}) RETURNING id"
        ),
        params,
    )
    campaign_id = await session.scalar(
        text(
            f"INSERT INTO marketing_campaigns (tenant_id, {CAMPAIGN_COLUMNS}) "
            f"VALUES (:tid, {CAMPAIGN_VALUES}) RETURNING id"
        ),
        params,
    )
    return str(job_id), str(campaign_id)


async def test_phase3c_alembic_upgrade_downgrade_upgrade(
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    script = ScriptDirectory(str(Path(__file__).parents[1] / "alembic"))
    assert script.get_current_head() == REVISION
    assert len(REVISION) <= 32
    revision = script.get_revision(REVISION)
    assert revision is not None
    assert revision.down_revision == PARENT

    tables = (
        "lead_collection_jobs",
        "collected_prospects",
        "marketing_campaigns",
        "campaign_recipients",
    )

    async with migrator_session_factory() as session:
        conn = await session.connection()

        def roundtrip(sync_conn):
            # Roll back the whole DDL round trip, including version changes and any existing data.
            ctx = MigrationContext.configure(sync_conn)
            assert ctx.get_current_revision() == REVISION
            for target, planner, present in (
                (PARENT, script._downgrade_revs, False),
                (REVISION, script._upgrade_revs, True),
            ):
                migration = MigrationContext.configure(
                    sync_conn,
                    opts={"fn": lambda current, _, p=planner, t=target: p(t, current)},
                )
                with Operations.context(migration), migration.begin_transaction():
                    migration.run_migrations()
                assert migration.get_current_revision() == target
                for name in tables:
                    found = sync_conn.scalar(text("SELECT to_regclass(:name)"), {"name": name})
                    assert (found is not None) == present, name

        try:
            await conn.run_sync(roundtrip)
        finally:
            await session.rollback()


async def test_growth_tables_force_rls_and_reject_unscoped_or_foreign_tenant(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = runtime_session_factory
    tenant_a, tenant_b, actor = (uuid4() for _ in range(3))
    params: dict[str, object] = {
        "tid": tenant_a,
        "actor": actor,
        "slug": f"rls3c-{tenant_a.hex}",
        "email": f"{actor.hex}@example.com",
    }
    for tid in (tenant_a, tenant_b):
        async with tenant_session(factory, tid) as session:
            await session.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, '3C RLS')"),
                {"id": tid, "slug": f"rls3c-{tid.hex}"},
            )
    async with tenant_session(factory, tenant_a) as session:
        assert await session.scalar(text("SELECT current_user")) == "app_runtime"
        assert 170000 <= int(await session.scalar(text("SHOW server_version_num"))) < 180000
        for stmt in (
            "INSERT INTO users (id, email, full_name, password_hash) "
            "VALUES (:actor, :email, '3C', 'test')",
            "INSERT INTO tenant_memberships (tenant_id,user_id,role) VALUES (:tid,:actor,'owner')",
        ):
            await session.execute(text(stmt), params)
        for name in (
            "lead_collection_jobs",
            "collected_prospects",
            "marketing_campaigns",
            "campaign_recipients",
        ):
            rls, forced, restrictive = (
                await session.execute(
                    text(
                        "SELECT c.relrowsecurity, c.relforcerowsecurity, EXISTS ("
                        "SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid "
                        "AND NOT p.polpermissive AND p.polqual IS NOT NULL "
                        "AND p.polwithcheck IS NOT NULL) FROM pg_class c "
                        "WHERE c.oid = to_regclass(:name)"
                    ),
                    {"name": name},
                )
            ).one()
            assert rls and forced and restrictive, name
        job_id, campaign_id = await _seed_parent_rows(session, params)
        prospect_id = await session.scalar(
            text(
                "INSERT INTO collected_prospects (tenant_id, job_id, company_name, "
                "dedupe_fingerprint, grade, latitude, longitude, rating) VALUES "
                "(:tid, :job, 'Gulf Lighting Trading LLC', :fp, 'A', 25.2048, 55.2708, "
                "CAST('4.60' AS numeric(3,2))) RETURNING id"
            ),
            {**params, "job": job_id, "fp": f"fp-{tenant_a.hex}"},
        )
        recipient_id = await session.scalar(
            text(
                "INSERT INTO campaign_recipients (tenant_id, campaign_id, prospect_id, address) "
                "VALUES (:tid, :campaign, :prospect, 'buyer@gulf-lighting.example.com') "
                "RETURNING id"
            ),
            {**params, "campaign": campaign_id, "prospect": str(prospect_id)},
        )
        assert prospect_id is not None and recipient_id is not None
        assert await session.scalar(text("SELECT count(*) FROM lead_collection_jobs")) == 1
        assert await session.scalar(text("SELECT count(*) FROM collected_prospects")) == 1
        assert await session.scalar(text("SELECT count(*) FROM marketing_campaigns")) == 1
        assert await session.scalar(text("SELECT count(*) FROM campaign_recipients")) == 1

    async def check_hidden(session: AsyncSession) -> None:
        for name in (
            "lead_collection_jobs",
            "collected_prospects",
            "marketing_campaigns",
            "campaign_recipients",
        ):
            assert await session.scalar(text(f"SELECT count(*) FROM {name}")) == 0, name
            assert (
                await session.execute(text(f"UPDATE {name} SET id=id RETURNING id"))
            ).all() == []
            assert (await session.execute(text(f"DELETE FROM {name} RETURNING id"))).all() == []
        with pytest.raises(DBAPIError, match="row-level security"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        f"INSERT INTO lead_collection_jobs (tenant_id, {JOB_COLUMNS}) "
                        f"VALUES (:tid, {JOB_VALUES})"
                    ),
                    params,
                )

    async with factory() as session, session.begin():
        await check_hidden(session)
    async with tenant_session(factory, tenant_b) as session:
        await check_hidden(session)


async def test_phase3c_constraints_reject_invalid_enums_and_duplicate_fingerprints(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tid, actor = uuid4(), uuid4()
    params: dict[str, object] = {"tid": tid, "actor": actor}
    async with tenant_session(runtime_session_factory, tid) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, '3C checks')"),
            {"id": tid, "slug": f"checks3c-{tid.hex}"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, 'Checks', 'test')"
            ),
            {"uid": actor, "email": f"{actor.hex}@example.com"},
        )
        job_id, campaign_id = await _seed_parent_rows(session, params)

        prospect = text(
            "INSERT INTO collected_prospects (tenant_id, job_id, company_name, "
            "dedupe_fingerprint) VALUES (:tid, :job, 'Acme Lighting', :fp)"
        )
        await session.execute(prospect, {**params, "job": job_id, "fp": "dup-fp"})
        duplicate = "collected_prospects_tenant_id_dedupe_fingerprint_key"
        with pytest.raises(DBAPIError, match=duplicate):
            async with session.begin_nested():
                await session.execute(prospect, {**params, "job": job_id, "fp": "dup-fp"})
        with pytest.raises(DBAPIError, match="ck_collected_prospects_grade"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO collected_prospects (tenant_id, company_name, "
                        "dedupe_fingerprint, grade) VALUES (:tid, 'Acme 2', 'bad-grade', 'S')"
                    ),
                    params,
                )
        with pytest.raises(DBAPIError, match="ck_collected_prospects_sync_status"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO collected_prospects (tenant_id, company_name, "
                        "dedupe_fingerprint, sync_status) VALUES (:tid, 'Acme 3', 'bad-sync', "
                        "'mailed')"
                    ),
                    params,
                )
        with pytest.raises(DBAPIError, match="ck_collected_prospects_latitude"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO collected_prospects (tenant_id, company_name, "
                        "dedupe_fingerprint, latitude) VALUES (:tid, 'Acme 4', 'bad-lat', 120)"
                    ),
                    params,
                )
        with pytest.raises(DBAPIError, match="ck_lead_collection_jobs_source_provider"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO lead_collection_jobs (tenant_id, source_provider, "
                        "created_by) VALUES (:tid, 'linkedin_scraper', :actor)"
                    ),
                    params,
                )
        with pytest.raises(DBAPIError, match="ck_lead_collection_jobs_status"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO lead_collection_jobs (tenant_id, source_provider, status, "
                        "created_by) VALUES (:tid, 'csv_import', 'archived', :actor)"
                    ),
                    params,
                )
        with pytest.raises(DBAPIError, match="ck_marketing_campaigns_channel_type"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO marketing_campaigns (tenant_id, name, channel_type, "
                        "body_template, created_by) VALUES (:tid, 'Bad', 'sms', 'Body', :actor)"
                    ),
                    params,
                )
        with pytest.raises(DBAPIError, match="ck_marketing_campaigns_status"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO marketing_campaigns (tenant_id, name, channel_type, "
                        "body_template, status, created_by) VALUES (:tid, 'Bad 2', 'email', "
                        "'Body', 'sending', :actor)"
                    ),
                    params,
                )
        recipient = text(
            "INSERT INTO campaign_recipients (tenant_id, campaign_id, address) "
            "VALUES (:tid, :campaign, :address)"
        )
        await session.execute(
            recipient,
            {**params, "campaign": campaign_id, "address": "dup@example.com"},
        )
        with pytest.raises(DBAPIError, match="uq_campaign_recipients_tenant_address"):
            async with session.begin_nested():
                await session.execute(
                    recipient,
                    {**params, "campaign": campaign_id, "address": "dup@example.com"},
                )
        with pytest.raises(DBAPIError, match="ck_campaign_recipients_status"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO campaign_recipients (tenant_id, campaign_id, address, "
                        "status) VALUES (:tid, :campaign, 'x@example.com', 'bounced')"
                    ),
                    {**params, "campaign": campaign_id},
                )
