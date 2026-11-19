from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.shared.db import tenant_session
from erp_core.shared.enums import (
    ChannelStatus,
    ChannelType,
    ConfidenceLevel,
    ConversationMode,
    ConversationStatus,
    CustomerType,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
)


def _runtime_url() -> str:
    return os.environ["DATABASE_RUNTIME_URL"]


def _migrator_url() -> str:
    return os.environ["DATABASE_MIGRATOR_URL"]


def _admin_url() -> str:
    """Plan 01 FORCE RLS blocks app_migrator DML on tenants."""
    return (
        "postgresql+asyncpg://postgres:postgres_dev_pass"
        "@localhost:5432/erp_saas"
    )


@pytest.mark.asyncio
async def test_migration_0002_objects_exist() -> None:
    engine = create_async_engine(_migrator_url(), future=True)
    try:
        async with engine.connect() as conn:
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert rev == "0002_channels_identity_inbox"

            tables = set(
                (
                    await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                        )
                    )
                ).scalars()
            )
            expected = {
                "channel_routing_directory",
                "channel_accounts",
                "raw_webhook_events",
                "dead_letter_events",
                "b2b_companies",
                "customers",
                "customer_identities",
                "identity_merge_events",
                "conversations",
                "messages",
                "consent_records",
                "suppression_list",
            }
            assert expected.issubset(tables)

            fn_exists = (
                await conn.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_proc "
                        "WHERE proname = 'resolve_tenant_by_channel_external_id'"
                        ")"
                    )
                )
            ).scalar_one()
            assert fn_exists is True

            trgm_exists = (
                await conn.execute(
                    text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')")
                )
            ).scalar_one()
            assert trgm_exists is True

            role = (
                await conn.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname = 'app_runtime'"
                    )
                )
            ).one()
            assert role.rolsuper is False
            assert role.rolbypassrls is False
            fn = (
                await conn.execute(
                    text(
                        "SELECT prosecdef, proconfig FROM pg_proc "
                        "WHERE proname = "
                        "'resolve_tenant_by_channel_external_id'"
                    )
                )
            ).one()
            assert fn.prosecdef is True
            assert any(
                "search_path=public, pg_temp" in item for item in (fn.proconfig or [])
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_messages_idempotency_and_rls_isolation() -> None:
    admin_engine = create_async_engine(_admin_url(), future=True)
    runtime_engine = create_async_engine(_runtime_url(), future=True)
    runtime_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    account_a = uuid.uuid4()
    customer_a = uuid.uuid4()
    conv_a = uuid.uuid4()

    try:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) VALUES "
                    "(:ta, :sa, 'Tenant A'), (:tb, :sb, 'Tenant B')"
                ),
                {
                    "ta": tenant_a,
                    "sa": f"tenant-a-{tenant_a.hex[:8]}",
                    "tb": tenant_b,
                    "sb": f"tenant-b-{tenant_b.hex[:8]}",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_routing_directory "
                    "(channel_type, external_account_id, tenant_id, channel_account_id) "
                    "VALUES (:ctype, :ext_id, :tid, :aid)"
                ),
                {
                    "ctype": ChannelType.WHATSAPP_CLOUD.value,
                    "ext_id": f"pn_{account_a.hex[:12]}",
                    "tid": tenant_a,
                    "aid": account_a,
                },
            )

        # 1. No tenant context: reads return nothing and writes fail closed.
        async with runtime_factory() as bare_session:
            async with bare_session.begin():
                rows = (await bare_session.execute(text("SELECT id FROM customers"))).all()
                assert rows == []
                resolved = (
                    await bare_session.execute(
                        text(
                            "SELECT tenant_id, channel_account_id "
                            "FROM resolve_tenant_by_channel_external_id(:ctype, :ext_id)"
                        ),
                        {
                            "ctype": ChannelType.WHATSAPP_CLOUD.value,
                            "ext_id": f"pn_{account_a.hex[:12]}",
                        },
                    )
                ).first()
                assert resolved is not None
                assert resolved[0] == tenant_a
                assert resolved[1] == account_a

            with pytest.raises(DBAPIError):
                async with bare_session.begin():
                    await bare_session.execute(
                        text(
                            "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                            "VALUES (:id, :tid, :ctype, 'Unauthorized')"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "tid": tenant_a,
                            "ctype": CustomerType.B2C_CONSUMER.value,
                        },
                    )

        # 2. Tenant A writes channel_account, customer, identity, conversation, and message
        async with tenant_session(runtime_factory, tenant_a) as sess_a:
            await sess_a.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name,"
                    "status, rate_limit_profile) "
                    "VALUES (:id, :tid, :ctype, :ext_id, 'WA Main', :status, "
                    "CAST(:profile AS jsonb))"
                ),
                {
                    "id": account_a,
                    "tid": tenant_a,
                    "ctype": ChannelType.WHATSAPP_CLOUD.value,
                    "ext_id": f"pn_{account_a.hex[:12]}",
                    "status": ChannelStatus.ACTIVE.value,
                    "profile": '{"mps": 80, "coexistence": false}',
                },
            )
            await sess_a.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, customer_type, display_name,"
                    "primary_email) "
                    "VALUES (:id, :tid, :ctype, 'Alice Buyer', 'alice@example.com')"
                ),
                {
                    "id": customer_a,
                    "tid": tenant_a,
                    "ctype": CustomerType.B2C_CONSUMER.value,
                },
            )
            await sess_a.execute(
                text(
                    "INSERT INTO customer_identities "
                    "(id, tenant_id, customer_id, identity_type, identity_value, confidence_level) "
                    "VALUES (:id, :tid, :cid, :itype, 'alice@example.com', :conf)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tenant_a,
                    "cid": customer_a,
                    "itype": IdentityType.EMAIL.value,
                    "conf": ConfidenceLevel.DETERMINISTIC.value,
                },
            )
            await sess_a.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, tenant_id, customer_id, channel_account_id, channel_type,"
                    "conversation_mode, status) "
                    "VALUES (:id, :tid, :cid, :aid, :ctype, :mode, :status)"
                ),
                {
                    "id": conv_a,
                    "tid": tenant_a,
                    "cid": customer_a,
                    "aid": account_a,
                    "ctype": ChannelType.WHATSAPP_CLOUD.value,
                    "mode": ConversationMode.B2C_SUPPORT.value,
                    "status": ConversationStatus.OPEN.value,
                },
            )
            now = datetime.now(UTC)
            await sess_a.execute(
                text(
                    "INSERT INTO messages "
                    "(id, tenant_id, conversation_id, customer_id, channel_type,"
                    "external_message_id, "
                    "direction, sender_type, content_type, body_text, delivery_status,"
                    "source_timestamp) "
                    "VALUES (:id, :tid, :conv_id, :cid, :ctype, 'wamid.dedup.001', "
                    ":dir, :stype, :cont, 'Hello', :dstat, :ts)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tenant_a,
                    "conv_id": conv_a,
                    "cid": customer_a,
                    "ctype": ChannelType.WHATSAPP_CLOUD.value,
                    "dir": MessageDirection.INBOUND.value,
                    "stype": MessageSenderType.CUSTOMER.value,
                    "cont": MessageContentType.TEXT.value,
                    "dstat": DeliveryStatus.DELIVERED.value,
                    "ts": now,
                },
            )
            # NULL external_message_id internal notes must NOT collide
            for note_idx in range(2):
                await sess_a.execute(
                    text(
                        "INSERT INTO messages "
                        "(id, tenant_id, conversation_id, customer_id, channel_type,"
                        "external_message_id, "
                        "direction, sender_type, content_type, body_text, delivery_status,"
                        "source_timestamp) "
                        "VALUES (:id, :tid, :conv_id, :cid, :ctype, NULL, "
                        ":dir, :stype, :cont, :body, :dstat, :ts)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": tenant_a,
                        "conv_id": conv_a,
                        "cid": customer_a,
                        "ctype": ChannelType.WHATSAPP_CLOUD.value,
                        "dir": MessageDirection.INTERNAL_NOTE.value,
                        "stype": MessageSenderType.HUMAN_AGENT.value,
                        "cont": MessageContentType.TEXT.value,
                        "body": f"Internal note {note_idx}",
                        "dstat": DeliveryStatus.DELIVERED.value,
                        "ts": now,
                    },
                )

        # 3. Duplicate external_message_id violates uq_messages_idempotency.
        with pytest.raises(IntegrityError):
            async with tenant_session(runtime_factory, tenant_a) as sess_dup:
                await sess_dup.execute(
                    text(
                        "INSERT INTO messages "
                        "(id, tenant_id, conversation_id, customer_id, channel_type,"
                        "external_message_id, "
                        "direction, sender_type, content_type, body_text, delivery_status,"
                        "source_timestamp) "
                        "VALUES (:id, :tid, :conv_id, :cid, :ctype, 'wamid.dedup.001', "
                        ":dir, :stype, :cont, 'Dup', :dstat, :ts)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": tenant_a,
                        "conv_id": conv_a,
                        "cid": customer_a,
                        "ctype": ChannelType.WHATSAPP_CLOUD.value,
                        "dir": MessageDirection.INBOUND.value,
                        "stype": MessageSenderType.CUSTOMER.value,
                        "cont": MessageContentType.TEXT.value,
                        "dstat": DeliveryStatus.DELIVERED.value,
                        "ts": datetime.now(UTC),
                    },
                )

        # 4. Tenant B sees zero customers and zero messages from Tenant A
        async with tenant_session(runtime_factory, tenant_b) as sess_b:
            cust_rows = (await sess_b.execute(text("SELECT id FROM customers"))).all()
            msg_rows = (await sess_b.execute(text("SELECT id FROM messages"))).all()
            assert cust_rows == []
            assert msg_rows == []
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM channel_routing_directory WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM messages WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM conversations WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM customer_identities WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM customers WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM channel_accounts WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM tenants WHERE id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
        await runtime_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_twelve_tables_force_rls_and_hide_foreign_raw_payloads() -> None:
    admin_engine = create_async_engine(_admin_url(), future=True)
    runtime_engine = create_async_engine(_runtime_url(), future=True)
    runtime_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    raw_a = uuid.uuid4()
    raw_unresolved = uuid.uuid4()
    account_a = uuid.uuid4()
    external_id = f"pn_{account_a.hex[:12]}"
    try:
        async with admin_engine.connect() as conn:
            flags = (
                await conn.execute(
                    text(
                        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                        "AND c.relname = ANY(:names)"
                    ),
                    {
                        "names": [
                            "channel_routing_directory",
                            "channel_accounts",
                            "raw_webhook_events",
                            "dead_letter_events",
                            "b2b_companies",
                            "customers",
                            "customer_identities",
                            "identity_merge_events",
                            "conversations",
                            "messages",
                            "consent_records",
                            "suppression_list",
                        ]
                    },
                )
            ).all()
            assert len(flags) == 12
            assert all(row.relrowsecurity and row.relforcerowsecurity for row in flags)
            policy_rows = (
                await conn.execute(
                    text(
                        "SELECT tablename, policyname FROM pg_policies "
                        "WHERE schemaname = 'public' AND policyname IN "
                        "('app_runtime_permissive', 'tenant_isolation_policy')"
                    )
                )
            ).all()
            policies: dict[str, set[str]] = {}
            for table_name, policy_name in policy_rows:
                policies.setdefault(str(table_name), set()).add(str(policy_name))
            required = {"app_runtime_permissive", "tenant_isolation_policy"}
            for row in flags:
                assert required <= policies.get(str(row.relname), set())

        async with admin_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) VALUES "
                    "(:ta, :sa, 'RLS A'), (:tb, :sb, 'RLS B')"
                ),
                {
                    "ta": tenant_a,
                    "sa": f"rls-a-{tenant_a.hex[:8]}",
                    "tb": tenant_b,
                    "sb": f"rls-b-{tenant_b.hex[:8]}",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_routing_directory "
                    "(channel_type, external_account_id, tenant_id, channel_account_id) "
                    "VALUES ('whatsapp_cloud', :ext, :tid, :aid)"
                ),
                {"ext": external_id, "tid": tenant_a, "aid": account_a},
            )
            await conn.execute(
                text(
                    "INSERT INTO raw_webhook_events ("
                    "id, tenant_id, channel_type, external_event_id, raw_payload"
                    ") VALUES ("
                    ":id, :tid, 'whatsapp_cloud', :eid, CAST(:payload AS jsonb)"
                    ")"
                ),
                {
                    "id": raw_a,
                    "tid": tenant_a,
                    "eid": f"evt-{raw_a.hex}",
                    "payload": '{"secret":"pii-probe"}',
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO raw_webhook_events ("
                    "id, tenant_id, channel_type, external_event_id, raw_payload"
                    ") VALUES ("
                    ":id, NULL, 'whatsapp_cloud', :eid, CAST(:payload AS jsonb)"
                    ")"
                ),
                {
                    "id": raw_unresolved,
                    "eid": f"evt-{raw_unresolved.hex}",
                    "payload": '{"secret":"unresolved-pii"}',
                },
            )

        async with tenant_session(runtime_factory, tenant_b) as sess_b:
            leaked = (
                await sess_b.execute(
                    text(
                        "SELECT raw_payload->>'secret' FROM raw_webhook_events "
                        "WHERE id = :id"
                    ),
                    {"id": raw_a},
                )
            ).scalar_one_or_none()
            assert leaked is None
            unresolved = (
                await sess_b.execute(
                    text("SELECT count(*) FROM raw_webhook_events WHERE tenant_id IS NULL")
                )
            ).scalar_one()
            assert unresolved == 0
            foreign_routes = (
                await sess_b.execute(text("SELECT count(*) FROM channel_routing_directory"))
            ).scalar_one()
            assert foreign_routes == 0

        async with tenant_session(runtime_factory, tenant_a) as sess_a:
            own = (
                await sess_a.execute(
                    text("SELECT raw_payload->>'secret' FROM raw_webhook_events WHERE id = :id"),
                    {"id": raw_a},
                )
            ).scalar_one()
            assert own == "pii-probe"
            hidden = (
                await sess_a.execute(
                    text("SELECT count(*) FROM raw_webhook_events WHERE tenant_id IS NULL")
                )
            ).scalar_one()
            assert hidden == 0
            resolved = (
                await sess_a.execute(
                    text(
                        "SELECT tenant_id FROM resolve_tenant_by_channel_external_id("
                        "'whatsapp_cloud', :ext)"
                    ),
                    {"ext": external_id},
                )
            ).scalar_one()
            assert resolved == tenant_a

        async with runtime_factory() as bare:
            with pytest.raises(DBAPIError):
                async with bare.begin():
                    await bare.execute(
                        text(
                            "INSERT INTO channel_routing_directory "
                            "(channel_type, external_account_id, tenant_id, channel_account_id) "
                            "VALUES ('whatsapp_cloud', :ext, :tid, :aid)"
                        ),
                        {
                            "ext": f"forged-{uuid.uuid4().hex[:8]}",
                            "tid": tenant_b,
                            "aid": uuid.uuid4(),
                        },
                    )
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM raw_webhook_events WHERE id IN (:a, :u)"),
                {"a": raw_a, "u": raw_unresolved},
            )
            await conn.execute(
                text("DELETE FROM channel_routing_directory WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM tenants WHERE id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
        await runtime_engine.dispose()
        await admin_engine.dispose()
