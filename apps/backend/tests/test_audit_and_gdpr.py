from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx as raw_httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.db import system_session, tenant_session
from erp_core.shared.storage import ObjectStorageService
from erp_core.tenancy.audit import ensure_audit_child_rls, record_audit_event

_TEMP_DDL = (
    """
    CREATE TABLE IF NOT EXISTS customers (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        display_name TEXT NOT NULL,
        primary_email TEXT,
        primary_phone TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS customer_identities (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        customer_id UUID NOT NULL,
        identity_type TEXT NOT NULL,
        identity_value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        customer_id UUID NOT NULL,
        order_no TEXT NOT NULL,
        total_amount NUMERIC(12, 2) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS order_profit_snapshots (
        order_id UUID PRIMARY KEY,
        tenant_id UUID NOT NULL,
        net_profit_base NUMERIC(12, 2) NOT NULL
    )
    """,
    """
    GRANT SELECT, INSERT, UPDATE, DELETE
        ON customers, customer_identities, orders, order_profit_snapshots
        TO app_runtime
    """,
)


async def _register_owner(async_client: AsyncClient) -> tuple[str, UUID, UUID]:
    suffix = uuid4().hex[:8]
    resp = await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"gdpr-{suffix}@factory.example.com",
            "password": "StrongPassword2026!",
            "full_name": "GDPR Owner",
            "tenant_name": f"GDPR Co {suffix}",
            "tenant_slug": f"gdpr-{suffix}",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return (
        data["access_token"],
        UUID(data["principal"]["tenant_id"]),
        UUID(data["principal"]["user_id"]),
    )


async def test_audit_events_partitioned_and_immutable_for_app_runtime(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, tenant_id, user_id = await _register_owner(async_client)

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        part_count = await session.scalar(
            text(
                "SELECT count(*) FROM pg_inherits "
                "WHERE inhparent = 'audit_events'::regclass "
                "  AND inhrelid::regclass::text LIKE 'audit_events_20%'"
            )
        )
        assert int(part_count or 0) >= 4

        event_id = await record_audit_event(
            session,
            tenant_id,
            "human_user",
            str(user_id),
            "quote.sent",
            "quote",
            uuid4(),
            before_state={"status": "draft"},
            after_state={"status": "sent"},
            ip_address="203.0.113.10",
            user_agent="pytest-agent/1.0",
        )
        assert isinstance(event_id, int)
        assert event_id > 0

    with pytest.raises(DBAPIError) as update_err:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text("UPDATE audit_events SET action = 'tampered' WHERE id = :id"),
                {"id": event_id},
            )
    assert "permission denied" in str(update_err.value).lower()

    with pytest.raises(DBAPIError) as delete_err:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text("DELETE FROM audit_events WHERE id = :id"),
                {"id": event_id},
            )
    assert "permission denied" in str(delete_err.value).lower()

    list_resp = await async_client.get(
        "/api/v1/tenancy/audit-events",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["total"] >= 1
    assert any(item["action"] == "quote.sent" for item in body["items"])


async def test_audit_child_partition_direct_grant_cannot_cross_tenants(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _token_a, tenant_a, user_a = await _register_owner(async_client)
    _token_b, tenant_b, user_b = await _register_owner(async_client)

    async with migrator_session_factory() as session, session.begin():
        children = await ensure_audit_child_rls(session)
    assert "audit_events_default" in children
    monthly = [name for name in children if name.startswith("audit_events_20")]
    assert len(monthly) >= 4

    async with migrator_session_factory() as session:
        flags = (
            await session.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid "
                    "WHERE i.inhparent = 'audit_events'::regclass"
                )
            )
        ).all()
    assert flags
    assert all(row.relrowsecurity and row.relforcerowsecurity for row in flags)

    async with tenant_session(runtime_session_factory, tenant_a) as session:
        event_a = await record_audit_event(
            session,
            tenant_a,
            "human_user",
            str(user_a),
            "partition.probe.a",
            "tenant",
            tenant_a,
        )
        child_name = await session.scalar(
            text("SELECT tableoid::regclass::text FROM audit_events WHERE id = :id"),
            {"id": event_a},
        )
    async with tenant_session(runtime_session_factory, tenant_b) as session:
        await record_audit_event(
            session,
            tenant_b,
            "human_user",
            str(user_b),
            "partition.probe.b",
            "tenant",
            tenant_b,
        )
    assert isinstance(child_name, str)
    child = child_name.split(".")[-1]

    async with migrator_session_factory() as session, session.begin():
        await session.execute(text(f'GRANT SELECT ON TABLE "{child}" TO app_runtime'))
    try:
        async with tenant_session(runtime_session_factory, tenant_a) as session:
            own_rows = (
                await session.execute(
                    text(f'SELECT action FROM "{child}" WHERE tenant_id = :tid'),
                    {"tid": str(tenant_a)},
                )
            ).all()
            leaked = (
                await session.execute(
                    text(f'SELECT action FROM "{child}" WHERE tenant_id = :tid'),
                    {"tid": str(tenant_b)},
                )
            ).all()
        assert any(row.action == "partition.probe.a" for row in own_rows)
        assert leaked == []

        async with system_session(runtime_session_factory) as session:
            residual = (await session.execute(text(f'SELECT action FROM "{child}"'))).all()
        assert residual == []
    finally:
        async with migrator_session_factory() as session, session.begin():
            await session.execute(text(f'REVOKE SELECT ON TABLE "{child}" FROM app_runtime'))


async def test_gdpr_export_anonymize_preserving_ledger_and_tenant_shred(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, tenant_id, _user_id = await _register_owner(async_client)
    storage = ObjectStorageService(get_settings())
    await storage.ensure_bucket_exists()

    customer_id = uuid4()
    order_id = uuid4()
    created_temp_tables = False
    async with migrator_session_factory() as m_sess, m_sess.begin():
        customers_exists = await m_sess.scalar(text("SELECT to_regclass('public.customers')"))
        if customers_exists is None:
            created_temp_tables = True
            for statement in _TEMP_DDL:
                await m_sess.execute(text(statement))

    try:
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO customers "
                    "(id, tenant_id, display_name, primary_email, primary_phone) "
                    "VALUES (:cid, :tid, 'Hans Mueller', "
                    "'hans@berlin-lighting.de', '+491701234567')"
                ),
                {"cid": str(customer_id), "tid": str(tenant_id)},
            )
            await session.execute(
                text(
                    "INSERT INTO customer_identities "
                    "(id, tenant_id, customer_id, identity_type, identity_value) "
                    "VALUES (:id, :tid, :cid, 'email', 'hans@berlin-lighting.de')"
                ),
                {"id": str(uuid4()), "tid": str(tenant_id), "cid": str(customer_id)},
            )
            await session.execute(
                text(
                    "INSERT INTO orders (id, tenant_id, customer_id, order_no, total_amount) "
                    "VALUES (:oid, :tid, :cid, 'ORD-2026-0001', 12450.00)"
                ),
                {"oid": str(order_id), "tid": str(tenant_id), "cid": str(customer_id)},
            )
            await session.execute(
                text(
                    "INSERT INTO order_profit_snapshots (order_id, tenant_id, net_profit_base) "
                    "VALUES (:oid, :tid, 3890.50)"
                ),
                {"oid": str(order_id), "tid": str(tenant_id)},
            )

        export_resp = await async_client.post(
            f"/api/v1/tenancy/gdpr/customers/{customer_id}/export",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert export_resp.status_code == 200, export_resp.text
        export_body = export_resp.json()
        assert export_body["download_url"]

        async with raw_httpx.AsyncClient() as dl_client:
            downloaded = await dl_client.get(export_body["download_url"])
        assert downloaded.status_code == 200
        exported_json = json.loads(downloaded.content)
        assert exported_json["customer_id"] == str(customer_id)
        assert exported_json["tenant_id"] == str(tenant_id)
        assert len(exported_json["tables"]["customers"]) == 1

        anon_resp = await async_client.post(
            f"/api/v1/tenancy/gdpr/customers/{customer_id}/anonymize",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert anon_resp.status_code == 200, anon_resp.text
        anon_body = anon_resp.json()
        assert anon_body["redacted_identifier"].startswith("REDACTED_")

        async with tenant_session(runtime_session_factory, tenant_id) as session:
            cust_row = (
                await session.execute(
                    text(
                        "SELECT display_name, primary_email, primary_phone "
                        "FROM customers WHERE id = :cid"
                    ),
                    {"cid": str(customer_id)},
                )
            ).one()
            assert cust_row.display_name == anon_body["redacted_identifier"]
            assert cust_row.primary_phone is None

            ident_count = await session.scalar(
                text("SELECT count(*) FROM customer_identities WHERE customer_id = :cid"),
                {"cid": str(customer_id)},
            )
            assert ident_count == 0

            order_total = await session.scalar(
                text("SELECT total_amount FROM orders WHERE id = :oid"),
                {"oid": str(order_id)},
            )
            profit_val = await session.scalar(
                text("SELECT net_profit_base FROM order_profit_snapshots WHERE order_id = :oid"),
                {"oid": str(order_id)},
            )
            assert str(order_total) == "12450.00"
            assert str(profit_val) == "3890.50"

        shred_resp = await async_client.post(
            "/api/v1/tenancy/gdpr/tenant/redact",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert shred_resp.status_code == 200, shred_resp.text
        assert shred_resp.json()["shredded"] is True

        me_after_shred = await async_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_after_shred.status_code == 401
    finally:
        if created_temp_tables:
            async with migrator_session_factory() as m_sess, m_sess.begin():
                await m_sess.execute(
                    text(
                        "DROP TABLE IF EXISTS order_profit_snapshots, orders, "
                        "customer_identities, customers CASCADE"
                    )
                )
