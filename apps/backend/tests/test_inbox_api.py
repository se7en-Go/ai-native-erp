from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.inbox_router import _require_inbox_access, inbox_router
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, ConversationStatus, UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal


@pytest.mark.asyncio
async def test_inbox_filters_sorting_notes_outbound_sse_and_tenant_404() -> None:
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    acct_wa = uuid.uuid4()
    acct_email = uuid.uuid4()
    cust_a = uuid.uuid4()
    conv_wa = uuid.uuid4()
    conv_email = uuid.uuid4()

    active_principal = CurrentPrincipal(
        user_id=user_a,
        tenant_id=tenant_a,
        role=UserRole.AGENT,
        email="agent_a@factory.test",
    )

    enqueued_tasks: list[dict] = []

    async def _fake_enqueue(req_dict: dict) -> None:
        enqueued_tasks.append(req_dict)

    app = FastAPI()
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.outbound_enqueue_fn = _fake_enqueue
    app.include_router(inbox_router)
    app.dependency_overrides[_require_inbox_access] = lambda: active_principal

    try:
        async with tenant_session(session_factory, tenant_b) as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:tb, :sb, 'Inbox B')"),
                {"tb": tenant_b, "sb": f"inb-b-{tenant_b.hex[:8]}"},
            )

        async with tenant_session(session_factory, tenant_a) as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:ta, :sa, 'Inbox A')"),
                {"ta": tenant_a, "sa": f"inb-a-{tenant_a.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status, "
                    "rate_limit_profile) VALUES "
                    "(:awa, :ta, 'whatsapp_cloud', :ext_wa, 'WA', 'active', "
                    "'{\"mps\": 80}'::jsonb), "
                    "(:aem, :ta, 'email', :ext_em, 'Email', 'active', '{\"mps\": 50}'::jsonb)"
                ),
                {
                    "awa": acct_wa,
                    "aem": acct_email,
                    "ta": tenant_a,
                    "ext_wa": f"pn_{acct_wa.hex[:8]}",
                    "ext_em": f"sales-{tenant_a.hex[:6]}@inbound.example.com",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO customers "
                    "(id, tenant_id, customer_type, display_name, primary_phone_e164, "
                    "iana_timezone) VALUES "
                    "(:cid, :ta, 'b2b_contact', 'Hans Mueller', '+491701234567', 'Europe/Berlin')"
                ),
                {"cid": cust_a, "ta": tenant_a},
            )
            now = datetime.now(UTC)
            await conn.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, tenant_id, customer_id, channel_account_id, channel_type, "
                    "external_thread_id, "
                    "conversation_mode, status, unread_count, csw_expires_at, sla_due_at, "
                    "updated_at) VALUES "
                    "(:cwa, :ta, :cid, :awa, 'whatsapp_cloud', '+491701234567', 'b2c_support', "
                    "'open', 2, :csw, :sla1, :u1), "
                    "(:cem, :ta, :cid, :aem, 'email', 'hans@example.de', 'b2b_inquiry', "
                    "'pending_human', 0, NULL, :sla2, :u2)"
                ),
                {
                    "cwa": conv_wa,
                    "cem": conv_email,
                    "ta": tenant_a,
                    "cid": cust_a,
                    "awa": acct_wa,
                    "aem": acct_email,
                    "csw": now + timedelta(hours=12),
                    "sla1": now + timedelta(hours=6),
                    "sla2": now + timedelta(hours=1),
                    "u1": now,
                    "u2": now - timedelta(minutes=10),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO messages "
                    "(id, tenant_id, conversation_id, customer_id, channel_type, "
                    "external_message_id, "
                    "direction, sender_type, content_type, body_text, delivery_status, "
                    "source_timestamp) VALUES "
                    "(:m1, :ta, :cwa, :cid, 'whatsapp_cloud', 'wamid.in.1', 'inbound', "
                    "'customer', 'text', 'Need LED price', 'delivered', :ts)"
                ),
                {"m1": uuid.uuid4(), "ta": tenant_a, "cwa": conv_wa, "cid": cust_a, "ts": now},
            )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Default sorting (updated_at DESC) + customer local timezone enrichment
            all_resp = await client.get("/api/v1/inbox/conversations?tab=all")
            assert all_resp.status_code == 200
            all_items = all_resp.json()["items"]
            assert [i["id"] for i in all_items] == [str(conv_wa), str(conv_email)]
            assert all_items[0]["customer"]["buyer_local_time"]["iana_timezone"] == "Europe/Berlin"

            # 2. Tab filters: unread, pending_human, sla_urgent, channel_type
            unread_resp = await client.get("/api/v1/inbox/conversations?tab=unread")
            assert [i["id"] for i in unread_resp.json()["items"]] == [str(conv_wa)]

            pending_resp = await client.get("/api/v1/inbox/conversations?tab=pending_human")
            assert [i["id"] for i in pending_resp.json()["items"]] == [str(conv_email)]

            sla_resp = await client.get("/api/v1/inbox/conversations?tab=sla_urgent")
            assert [i["id"] for i in sla_resp.json()["items"]] == [str(conv_email), str(conv_wa)]

            ch_resp = await client.get(
                f"/api/v1/inbox/conversations?channel_type={ChannelType.EMAIL.value}"
            )
            assert [i["id"] for i in ch_resp.json()["items"]] == [str(conv_email)]

            # 3. Reading messages resets unread_count to 0
            msgs_resp = await client.get(f"/api/v1/inbox/conversations/{conv_wa}/messages")
            assert msgs_resp.status_code == 200
            assert len(msgs_resp.json()["items"]) == 1
            unread_after = await client.get("/api/v1/inbox/conversations?tab=unread")
            assert unread_after.json()["total"] == 0

            # 4. Internal note persists WITHOUT enqueueing outbound delivery
            note_resp = await client.post(
                f"/api/v1/inbox/conversations/{conv_wa}/messages",
                json={
                    "direction": "internal_note",
                    "body_text": "Verify warehouse stock before quoting",
                },
            )
            assert note_resp.status_code == 201
            assert note_resp.json()["queued_for_delivery"] is False
            assert len(enqueued_tasks) == 0

            # 5. Outbound message with translate_to hook enqueues delivery
            out_resp = await client.post(
                f"/api/v1/inbox/conversations/{conv_wa}/messages",
                json={
                    "direction": "outbound",
                    "body_text": "We can ship 5000m within 7 days.",
                    "translate_to": "de",
                },
            )
            assert out_resp.status_code == 201
            assert out_resp.json()["queued_for_delivery"] is True
            assert out_resp.json()["translated_preview"] is not None
            assert len(enqueued_tasks) == 1
            assert enqueued_tasks[0]["recipient_id"] == "+491701234567"

            # 6. PATCH conversation assigns user & resolves status
            patch_resp = await client.patch(
                f"/api/v1/inbox/conversations/{conv_email}",
                json={
                    "status": ConversationStatus.OPEN.value,
                    "assigned_user_id": str(user_a),
                },
            )
            assert patch_resp.status_code == 200
            assert patch_resp.json()["status"] == "open"
            assert patch_resp.json()["assigned_user_id"] == str(user_a)

            # 7. SSE stream receives tenant-scoped Redis Pub/Sub event after handshake
            received_chunks: list[str] = []
            published = False
            async with client.stream("GET", "/api/v1/inbox/stream?max_events=1") as sse_resp:
                assert sse_resp.status_code == 200
                async for line in sse_resp.aiter_lines():
                    if line:
                        received_chunks.append(line)
                        if not published and line == "event: connected":
                            published = True
                            await redis.publish(
                                f"inbox:events:{tenant_a}",
                                json.dumps(
                                    {
                                        "event": "conversation.updated",
                                        "conversation_id": str(conv_wa),
                                    }
                                ),
                            )
            assert any("conversation.updated" in line for line in received_chunks)

            # 8. Cross-tenant isolation: Tenant B receives 404 on Tenant A conversation
            active_principal = CurrentPrincipal(
                user_id=user_b,
                tenant_id=tenant_b,
                role=UserRole.ADMIN,
                email="admin_b@other.test",
            )
            cross_resp = await client.get(f"/api/v1/inbox/conversations/{conv_wa}/messages")
            assert cross_resp.status_code == 404
    finally:
        async with tenant_session(session_factory, tenant_a) as conn:
            for tbl in (
                "audit_events",
                "messages",
                "conversations",
                "customers",
                "channel_accounts",
            ):
                await conn.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id = :ta"),
                    {"ta": tenant_a},
                )
            await conn.execute(text("DELETE FROM tenants WHERE id = :ta"), {"ta": tenant_a})
        async with tenant_session(session_factory, tenant_b) as conn:
            await conn.execute(text("DELETE FROM tenants WHERE id = :tb"), {"tb": tenant_b})
        await redis.aclose()
        await runtime_engine.dispose()
