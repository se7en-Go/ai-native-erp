"""Verify the Phase 3A revision and RLS against real PostgreSQL via app_runtime."""

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

REVISION = "0005_phase3a_crm_supply_wms"
TABLES = {
    "customer_followups": "customer_id, author_user_id, followup_type, summary",
    "suppliers": "code, name",
    "purchase_orders": "po_no, supplier_id, warehouse_id, currency, created_by",
    "purchase_order_items": "purchase_order_id, variant_id, sku, qty_ordered, unit_cost",
    "wms_movements": "movement_no, warehouse_id, variant_id, movement_type, qty_delta",
    "task_reminders": "title",
}
VALUES = {
    "customer_followups": ":customer, :actor, 'email', 'Meeting recap'",
    "suppliers": "'S-1', 'Supplier'",
    "purchase_orders": "'PO-1', :supplier, :warehouse, 'USD', :actor",
    "purchase_order_items": ":po, :variant, 'SKU-1', 3, 1.2345",
    "wms_movements": "'WMS-1', :warehouse, :variant, 'stocktake_adjust', 2",
    "task_reminders": "'Review sample'",
}


async def test_phase3a_alembic_upgrade_downgrade_upgrade(
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    script = ScriptDirectory(str(Path(__file__).parents[1] / "alembic"))
    # Later verticals (3B+) extend the chain; this test only owns the 3A revision itself
    # and now round-trips the whole chain from base-of-3A back to the current head.
    head = script.get_current_head()
    assert head is not None and head != "0004"
    assert script.get_revision(REVISION).down_revision == "0004"

    async with migrator_session_factory() as session:
        conn = await session.connection()

        def roundtrip(sync_conn):
            # Roll back the whole DDL round trip, including version changes and any existing data.
            ctx = MigrationContext.configure(sync_conn)
            assert ctx.get_current_revision() == head
            for target, planner, present in (
                ("0004", script._downgrade_revs, False),
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


async def test_all_six_tables_force_rls_and_reject_unscoped_or_foreign_tenant(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = runtime_session_factory
    tenant_a, tenant_b, actor, customer, product, variant, warehouse = [uuid4() for _ in range(7)]
    params = {
        "tid": tenant_a,
        "actor": actor,
        "customer": customer,
        "product": product,
        "variant": variant,
        "warehouse": warehouse,
        "slug": f"rls-{tenant_a.hex}",
        "email": f"{actor.hex}@example.com",
    }
    for tid in (tenant_a, tenant_b):
        async with tenant_session(factory, tid) as session:
            await session.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, '3A RLS')"),
                {"id": tid, "slug": f"rls-{tid.hex}"},
            )
    async with tenant_session(factory, tenant_a) as session:
        assert await session.scalar(text("SELECT current_user")) == "app_runtime"
        assert 170000 <= int(await session.scalar(text("SHOW server_version_num"))) < 180000
        for stmt in (
            "INSERT INTO users (id, email, full_name, password_hash) "
            "VALUES (:actor, :email, '3A', 'test')",
            "INSERT INTO tenant_memberships (tenant_id,user_id,role) VALUES (:tid,:actor,'owner')",
            "INSERT INTO customers (id,tenant_id,customer_type,display_name) "
            "VALUES (:customer,:tid,'b2b_contact','RLS Customer')",
            "INSERT INTO products (id,tenant_id,spu_code,category) "
            "VALUES (:product,:tid,'SPU-1','industrial')",
            "INSERT INTO product_variants (id,tenant_id,product_id,sku,retail_price_usd,"
            "cost_price_base,weight_grams) VALUES (:variant,:tid,:product,'SKU-1',10,1,100)",
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
            if name == "suppliers":
                params["supplier"] = row_id
            if name == "purchase_orders":
                params["po"] = row_id
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
