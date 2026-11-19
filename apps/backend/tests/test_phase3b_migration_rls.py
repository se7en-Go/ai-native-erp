"""Verify the Phase 3B revision and RLS against real PostgreSQL via app_runtime."""

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

REVISION = "0006_phase3b_ai_employees"
PARENT = "0005_phase3a_crm_supply_wms"
TABLES = {
    "ai_employees": (
        "code, display_name, role_title, department, description, capability_codes, "
        "autonomy_level"
    ),
    "quick_reply_templates": "title, category, language, body_text, shortcut_code",
    "ai_creative_assets": "asset_type, title, language, content_text, created_by",
}
VALUES = {
    "ai_employees": (
        "'EMP-1', 'Quanzhou Sales', 'B2B Sales', 'Sales', 'Qualifies inbound RFQs', "
        "CAST('[\"sales_quoting\"]' AS jsonb), 'L1_proposal'"
    ),
    "quick_reply_templates": (
        "'MOQ reply', 'sales', 'en', 'Our MOQ is 100 units.', 'moq-reply'"
    ),
    "ai_creative_assets": (
        "'product_copy', 'COB 24V copy', 'en', 'Bright and safe.', :actor"
    ),
}


async def test_phase3b_alembic_upgrade_downgrade_upgrade(
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    script = ScriptDirectory(str(Path(__file__).parents[1] / "alembic"))
    # Later verticals extend the chain; this test owns one revision and round-trips
    # the whole chain from its parent back up to the current head.
    head = script.get_current_head()
    assert head is not None and head != PARENT
    assert len(REVISION) <= 32
    revision = script.get_revision(REVISION)
    assert revision is not None
    assert revision.down_revision == PARENT
    assert head == REVISION or script.get_revision(head) is not None

    async with migrator_session_factory() as session:
        conn = await session.connection()

        def roundtrip(sync_conn):
            # Roll back the whole DDL round trip, including version changes and any existing data.
            ctx = MigrationContext.configure(sync_conn)
            assert ctx.get_current_revision() == head
            for target, planner, present in (
                (PARENT, script._downgrade_revs, False),
                (head, script._upgrade_revs, True),
            ):
                migration = MigrationContext.configure(
                    sync_conn,
                    opts={"fn": lambda current, _, p=planner, t=target: p(t, current)},
                )
                with Operations.context(migration), migration.begin_transaction():
                    migration.run_migrations()
                assert migration.get_current_revision() == target
                for name in TABLES:
                    found = sync_conn.scalar(text("SELECT to_regclass(:name)"), {"name": name})
                    assert (found is not None) == present, name

        try:
            await conn.run_sync(roundtrip)
        finally:
            await session.rollback()


async def test_three_tables_force_rls_and_reject_unscoped_or_foreign_tenant(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = runtime_session_factory
    tenant_a, tenant_b, actor = (uuid4() for _ in range(3))
    params = {
        "tid": tenant_a,
        "actor": actor,
        "slug": f"rls3b-{tenant_a.hex}",
        "email": f"{actor.hex}@example.com",
    }
    for tid in (tenant_a, tenant_b):
        async with tenant_session(factory, tid) as session:
            await session.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, '3B RLS')"),
                {"id": tid, "slug": f"rls3b-{tid.hex}"},
            )
    async with tenant_session(factory, tenant_a) as session:
        assert await session.scalar(text("SELECT current_user")) == "app_runtime"
        assert 170000 <= int(await session.scalar(text("SHOW server_version_num"))) < 180000
        for stmt in (
            "INSERT INTO users (id, email, full_name, password_hash) "
            "VALUES (:actor, :email, '3B', 'test')",
            "INSERT INTO tenant_memberships (tenant_id,user_id,role) VALUES (:tid,:actor,'owner')",
        ):
            await session.execute(text(stmt), params)
        for name, columns in TABLES.items():
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
            row_id = await session.scalar(
                text(
                    f"INSERT INTO {name} (tenant_id, {columns}) "
                    f"VALUES (:tid, {VALUES[name]}) RETURNING id"
                ),
                params,
            )
            assert row_id is not None, name
            assert (await session.execute(text(f"SELECT id FROM {name}"))).scalar_one() == row_id

    async def check_hidden(session: AsyncSession) -> None:
        for name, columns in TABLES.items():
            assert await session.scalar(text(f"SELECT count(*) FROM {name}")) == 0, name
            assert (
                await session.execute(text(f"UPDATE {name} SET id=id RETURNING id"))
            ).all() == []
            assert (await session.execute(text(f"DELETE FROM {name} RETURNING id"))).all() == []
            with pytest.raises(DBAPIError, match="row-level security"):
                async with session.begin_nested():
                    await session.execute(
                        text(
                            f"INSERT INTO {name} (tenant_id, {columns}) "
                            f"VALUES (:tid, {VALUES[name]})"
                        ),
                        params,
                    )

    async with factory() as session, session.begin():
        await check_hidden(session)
    async with tenant_session(factory, tenant_b) as session:
        await check_hidden(session)


async def test_phase3b_constraints_reject_invalid_enums_and_duplicate_employee_code(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tid, actor = uuid4(), uuid4()
    async with tenant_session(runtime_session_factory, tid) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, '3B checks')"),
            {"id": tid, "slug": f"checks-{tid.hex}"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, 'Checks', 'test')"
            ),
            {"uid": actor, "email": f"{actor.hex}@example.com"},
        )
        employee = text(
            "INSERT INTO ai_employees (tenant_id, code, display_name, role_title, department, "
            "description, autonomy_level) VALUES (:tid, :code, 'Bot', 'Role', 'Dept', 'Desc', "
            ":level)"
        )
        await session.execute(employee, {"tid": tid, "code": "EMP-1", "level": "L1_proposal"})
        with pytest.raises(DBAPIError, match="ai_employees_tenant_id_code_key"):
            async with session.begin_nested():
                await session.execute(
                    employee, {"tid": tid, "code": "EMP-1", "level": "L1_proposal"}
                )
        with pytest.raises(DBAPIError, match="ck_ai_employees_autonomy_level"):
            async with session.begin_nested():
                await session.execute(
                    employee, {"tid": tid, "code": "EMP-2", "level": "L9_skynet"}
                )
        with pytest.raises(DBAPIError, match="ck_ai_creative_assets_asset_type"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO ai_creative_assets (tenant_id, asset_type, title, language, "
                        "content_text, created_by) "
                        "VALUES (:tid, 'tv_script', 'T', 'en', 'C', :uid)"
                    ),
                    {"tid": tid, "uid": actor},
                )
        with pytest.raises(DBAPIError, match="ck_ai_creative_assets_status"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO ai_creative_assets (tenant_id, asset_type, title, language, "
                        "content_text, status, created_by) "
                        "VALUES (:tid, 'listing', 'T', 'en', 'C', 'live', :uid)"
                    ),
                    {"tid": tid, "uid": actor},
                )
        with pytest.raises(DBAPIError, match="quick_reply_templates_tenant_id_shortcut_code_key"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO quick_reply_templates (tenant_id, title, category, language, "
                        "body_text, shortcut_code) VALUES (:tid, 'T', 'c', 'en', 'B', 'dup')"
                    ),
                    {"tid": tid},
                )
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO quick_reply_templates (tenant_id, title, category, language, "
                        "body_text, shortcut_code) VALUES (:tid, 'T2', 'c', 'en', 'B', 'dup')"
                    ),
                    {"tid": tid},
                )
