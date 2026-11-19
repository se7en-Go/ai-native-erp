"""Verify the Phase 3D revision and its fail-closed RLS against real PostgreSQL."""

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

REVISION = "0008_phase3d_social_matrix"
PARENT = "0007_phase3c_growth_leads"

TABLES = ("social_accounts", "social_posts", "social_mentions")

ACCOUNT_COLUMNS = "platform, region, handle, display_name, followers_count, created_by"
ACCOUNT_VALUES = "'instagram', 'overseas', '@lumenlight', 'Lumen Light', 12345, :actor"


async def _seed_account(session: AsyncSession, params: dict[str, object]) -> str:
    account_id = await session.scalar(
        text(
            f"INSERT INTO social_accounts (tenant_id, {ACCOUNT_COLUMNS}) "
            f"VALUES (:tid, {ACCOUNT_VALUES}) RETURNING id"
        ),
        params,
    )
    assert account_id is not None
    return str(account_id)


async def test_phase3d_alembic_upgrade_downgrade_upgrade(
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    script = ScriptDirectory(str(Path(__file__).parents[1] / "alembic"))
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
            # Roll back the whole DDL round trip, including version changes and any data.
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


async def test_social_tables_force_rls_and_reject_unscoped_or_foreign_tenant(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = runtime_session_factory
    tenant_a, tenant_b, actor = (uuid4() for _ in range(3))
    params: dict[str, object] = {
        "tid": tenant_a,
        "actor": actor,
        "slug": f"rls3d-{tenant_a.hex}",
        "email": f"{actor.hex}@example.com",
    }
    for tid in (tenant_a, tenant_b):
        async with tenant_session(factory, tid) as session:
            await session.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, '3D RLS')"),
                {"id": tid, "slug": f"rls3d-{tid.hex}"},
            )
    async with tenant_session(factory, tenant_a) as session:
        assert await session.scalar(text("SELECT current_user")) == "app_runtime"
        assert 170000 <= int(await session.scalar(text("SHOW server_version_num"))) < 180000
        for stmt in (
            "INSERT INTO users (id, email, full_name, password_hash) "
            "VALUES (:actor, :email, '3D', 'test')",
            "INSERT INTO tenant_memberships (tenant_id,user_id,role) VALUES (:tid,:actor,'owner')",
        ):
            await session.execute(text(stmt), params)
        for name in TABLES:
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
            grants = {
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT privilege_type FROM information_schema.role_table_grants "
                            "WHERE table_name = :name AND grantee = 'app_runtime'"
                        ),
                        {"name": name},
                    )
                ).all()
            }
            assert {"SELECT", "INSERT", "UPDATE", "DELETE"} <= grants, name

        account_id = await _seed_account(session, params)
        post_id = await session.scalar(
            text(
                "INSERT INTO social_posts (tenant_id, social_account_id, platform, title, "
                "caption, hashtags, created_by) VALUES (:tid, :account, 'instagram', "
                "'Work light reel', 'IP65 work light for the workshop', "
                "CAST('[\"#worklight\"]' AS jsonb), :actor) "
                "RETURNING id"
            ),
            {**params, "account": account_id},
        )
        mention_id = await session.scalar(
            text(
                "INSERT INTO social_mentions (tenant_id, social_account_id, platform, "
                "author_handle, content_text, sentiment, occurred_at) VALUES "
                "(:tid, :account, 'instagram', '@bulk_buyer', 'Where can I buy in bulk?', "
                "'lead_intent', now()) RETURNING id"
            ),
            {**params, "account": account_id},
        )
        assert post_id is not None and mention_id is not None
        for name in TABLES:
            assert await session.scalar(text(f"SELECT count(*) FROM {name}")) == 1, name

    async def check_hidden(session: AsyncSession) -> None:
        for name in TABLES:
            assert await session.scalar(text(f"SELECT count(*) FROM {name}")) == 0, name
            assert (
                await session.execute(text(f"UPDATE {name} SET id=id RETURNING id"))
            ).all() == []
            assert (await session.execute(text(f"DELETE FROM {name} RETURNING id"))).all() == []
        with pytest.raises(DBAPIError, match="row-level security"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        f"INSERT INTO social_accounts (tenant_id, {ACCOUNT_COLUMNS}) "
                        f"VALUES (:tid, {ACCOUNT_VALUES})"
                    ),
                    params,
                )

    async with factory() as session, session.begin():
        await check_hidden(session)
    async with tenant_session(factory, tenant_b) as session:
        await check_hidden(session)


async def test_social_constraints_reject_invalid_enums_and_duplicate_handles(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tid, actor = uuid4(), uuid4()
    params: dict[str, object] = {"tid": tid, "actor": actor}
    async with tenant_session(runtime_session_factory, tid) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, '3D checks')"),
            {"id": tid, "slug": f"checks3d-{tid.hex}"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, 'Checks', 'test')"
            ),
            {"uid": actor, "email": f"{actor.hex}@example.com"},
        )
        account_id = await _seed_account(session, params)

        for statement, values in (
            (
                "INSERT INTO social_accounts (tenant_id, platform, region, handle, "
                "display_name, created_by) VALUES (:tid, 'myspace', 'overseas', '@x', 'X', :actor)",
                params,
            ),
            (
                "INSERT INTO social_accounts (tenant_id, platform, region, handle, "
                "display_name, created_by) VALUES (:tid, 'x', 'mars', '@x', 'X', :actor)",
                params,
            ),
            (
                "INSERT INTO social_accounts (tenant_id, platform, region, handle, "
                "display_name, created_by) VALUES (:tid, 'instagram', 'overseas', '@lumenlight', "
                "'Duplicate', :actor)",
                params,
            ),
            (
                "INSERT INTO social_posts (tenant_id, platform, caption, status) "
                "VALUES (:tid, 'instagram', 'late post', 'scheduled')",
                params,
            ),
            (
                "INSERT INTO social_posts (tenant_id, platform, caption, status) "
                "VALUES (:tid, 'myspace', 'caption', 'draft')",
                params,
            ),
            (
                "INSERT INTO social_mentions (tenant_id, platform, author_handle, "
                "content_text, sentiment, occurred_at) VALUES (:tid, 'tiktok', '@a', 'text', "
                "'furious', now())",
                params,
            ),
        ):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(text(statement), values)

        # The all-or-nothing token contract: a half-written envelope is rejected.
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "UPDATE social_accounts SET token_ciphertext = '\\x00'::bytea "
                        "WHERE id = :account"
                    ),
                    {"account": account_id},
                )
