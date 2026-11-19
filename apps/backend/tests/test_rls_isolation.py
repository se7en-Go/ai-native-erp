from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.db import system_session, tenant_session


async def test_rls_fails_closed_without_tenant_context(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Even when tenant rows exist in DB, querying without app.current_tenant_id returns 0 rows.
    async with system_session(runtime_session_factory) as session:
        rows = (await session.execute(text("SELECT id, tenant_id FROM tenant_memberships"))).all()
        assert rows == []

    # Writing without app.current_tenant_id MUST fail closed at PostgreSQL RLS layer.
    dummy_tenant_id = uuid4()
    dummy_user_id = uuid4()
    with pytest.raises(DBAPIError) as exc_info:
        async with system_session(runtime_session_factory) as session:
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                    "VALUES (:id, :tid, :uid, 'owner')"
                ),
                {"id": str(uuid4()), "tid": str(dummy_tenant_id), "uid": str(dummy_user_id)},
            )
    assert "row-level security" in str(exc_info.value).lower()


async def test_pgbouncer_20_concurrent_tasks_zero_cross_tenant_leakage(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    user_a = uuid4()
    user_b = uuid4()

    async with tenant_session(runtime_session_factory, tenant_a) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :em, :fn, 'h')"
            ),
            {"id": str(user_a), "em": f"rls-a-{user_a}@example.com", "fn": "RLS A"},
        )
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Tenant A')"),
            {"id": str(tenant_a), "slug": f"tenant-a-{tenant_a.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:id, :tid, :uid, 'owner')"
            ),
            {"id": str(uuid4()), "tid": str(tenant_a), "uid": str(user_a)},
        )

    async with tenant_session(runtime_session_factory, tenant_b) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :em, :fn, 'h')"
            ),
            {"id": str(user_b), "em": f"rls-b-{user_b}@example.com", "fn": "RLS B"},
        )
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Tenant B')"),
            {"id": str(tenant_b), "slug": f"tenant-b-{tenant_b.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:id, :tid, :uid, 'admin')"
            ),
            {"id": str(uuid4()), "tid": str(tenant_b), "uid": str(user_b)},
        )

    async def worker_task(idx: int) -> None:
        target_tenant: UUID = tenant_a if idx % 2 == 0 else tenant_b
        other_tenant: UUID = tenant_b if idx % 2 == 0 else tenant_a
        expected_role = "owner" if idx % 2 == 0 else "admin"

        for _ in range(5):
            async with tenant_session(runtime_session_factory, target_tenant) as session:
                current_tid = await session.scalar(
                    text("SELECT NULLIF(current_setting('app.current_tenant_id', true), '')")
                )
                assert current_tid == str(target_tenant)
                await asyncio.sleep(0.01)
                rows = (
                    await session.execute(text("SELECT tenant_id, role FROM tenant_memberships"))
                ).all()
                assert len(rows) == 1
                assert str(rows[0].tenant_id) == str(target_tenant)
                assert rows[0].role == expected_role

                # Attempt explicit cross-tenant read with WHERE clause must still return 0 rows
                leaked = (
                    await session.execute(
                        text("SELECT id FROM tenant_memberships WHERE tenant_id = :other"),
                        {"other": str(other_tenant)},
                    )
                ).all()
                assert leaked == []

    await asyncio.gather(*(worker_task(i) for i in range(20)))

    # Confirm PgBouncer connection returns with zero residual session GUC leakage
    async with system_session(runtime_session_factory) as session:
        residual = await session.scalar(
            text("SELECT NULLIF(current_setting('app.current_tenant_id', true), '')")
        )
        assert residual is None
