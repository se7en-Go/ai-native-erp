"""Startup must harden audit partitions without a test calling the helper."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.api_main import create_app
from erp_core.shared.db import system_session, tenant_session
from erp_core.tenancy.audit import record_audit_event


async def _child_names(session: AsyncSession) -> list[str]:
    rows = (
        await session.execute(
            text(
                "SELECT c.relname "
                "FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = 'public.audit_events'::regclass "
                "ORDER BY c.relname"
            )
        )
    ).all()
    return [str(row.relname) for row in rows]


async def _simulate_fresh_migration_children(session: AsyncSession) -> str:
    """Return a monthly child dropped so only real startup management can recreate it."""
    names = await _child_names(session)
    monthly = [name for name in names if name.startswith("audit_events_20")]
    assert monthly, "0001 should have created monthly audit partitions"
    target = monthly[-1]
    for name in names:
        policies = (
            await session.execute(
                text(
                    "SELECT polname FROM pg_policy "
                    "WHERE polrelid = to_regclass(:rel)"
                ),
                {"rel": f"public.{name}"},
            )
        ).all()
        for policy in policies:
            await session.execute(
                text(f'DROP POLICY IF EXISTS "{policy.polname}" ON "{name}"')
            )
        await session.execute(text(f'ALTER TABLE "{name}" NO FORCE ROW LEVEL SECURITY'))
        await session.execute(text(f'ALTER TABLE "{name}" DISABLE ROW LEVEL SECURITY'))
    await session.execute(text(f'DROP TABLE IF EXISTS "{target}"'))
    return target


async def test_app_lifespan_hardens_audit_partitions_without_test_helper(
    migrator_session_factory: async_sessionmaker[AsyncSession],
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with migrator_session_factory() as session, session.begin():
        dropped = await _simulate_fresh_migration_children(session)

    app = create_app()
    async with app.router.lifespan_context(app):
        async with migrator_session_factory() as session:
            names = await _child_names(session)
            assert dropped in names
            flags = (
                await session.execute(
                    text(
                        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                        "       count(p.polname) FILTER ("
                        "           WHERE NOT p.polpermissive"
                        "       ) AS restrictive_policies "
                        "FROM pg_inherits i "
                        "JOIN pg_class c ON c.oid = i.inhrelid "
                        "LEFT JOIN pg_policy p ON p.polrelid = c.oid "
                        "WHERE i.inhparent = 'public.audit_events'::regclass "
                        "GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity"
                    )
                )
            ).all()
        assert flags
        assert all(row.relrowsecurity and row.relforcerowsecurity for row in flags)
        assert all(int(row.restrictive_policies) >= 1 for row in flags)

        tenant_a = uuid4()
        tenant_b = uuid4()
        user_a = uuid4()
        user_b = uuid4()
        for tenant_id, user_id, label in (
            (tenant_a, user_a, "a"),
            (tenant_b, user_b, "b"),
        ):
            async with tenant_session(runtime_session_factory, tenant_id) as session:
                await session.execute(
                    text(
                        "INSERT INTO users (id, email, full_name, password_hash) "
                        "VALUES (:id, :email, :name, 'x')"
                    ),
                    {
                        "id": str(user_id),
                        "email": f"part-{label}-{user_id.hex[:8]}@example.com",
                        "name": label,
                    },
                )
                await session.execute(
                    text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, :name)"),
                    {
                        "id": str(tenant_id),
                        "slug": f"part-{label}-{tenant_id.hex[:8]}",
                        "name": label,
                    },
                )
                event_id = await record_audit_event(
                    session,
                    tenant_id,
                    "human_user",
                    str(user_id),
                    f"startup.probe.{label}",
                    "tenant",
                    tenant_id,
                )
                if label == "a":
                    child = await session.scalar(
                        text(
                            "SELECT tableoid::regclass::text FROM audit_events WHERE id = :id"
                        ),
                        {"id": event_id},
                    )
        assert isinstance(child, str)
        child_name = child.split(".")[-1]
        async with migrator_session_factory() as session, session.begin():
            await session.execute(text(f'GRANT SELECT ON TABLE "{child_name}" TO app_runtime'))
        try:
            async with tenant_session(runtime_session_factory, tenant_a) as session:
                own_rows = (
                    await session.execute(text(f'SELECT action FROM "{child_name}"'))
                ).all()
                leaked = (
                    await session.execute(
                        text(f'SELECT action FROM "{child_name}" WHERE tenant_id = :tid'),
                        {"tid": str(tenant_b)},
                    )
                ).all()
            assert any(row.action == "startup.probe.a" for row in own_rows)
            assert all(row.action != "startup.probe.b" for row in own_rows)
            assert leaked == []
            async with system_session(runtime_session_factory) as session:
                residual = (
                    await session.execute(text(f'SELECT action FROM "{child_name}"'))
                ).all()
            assert residual == []
            with pytest.raises(Exception) as alter_err:
                async with runtime_session_factory() as session, session.begin():
                    await session.execute(
                        text(f'ALTER TABLE "{child_name}" DISABLE ROW LEVEL SECURITY')
                    )
            assert "must be owner" in str(alter_err.value).lower()
        finally:
            async with migrator_session_factory() as session, session.begin():
                await session.execute(
                    text(f'REVOKE SELECT ON TABLE "{child_name}" FROM app_runtime')
                )
