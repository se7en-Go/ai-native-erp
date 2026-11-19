from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import (
    AdapterRegistry,
    MessagingChannelAdapter,
    NormalizedInboundEvent,
    NormalizedStatusEvent,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.ingress_worker import (
    consume_webhook_stream_batch,
    dlq_router,
    process_raw_webhook_event,
)
from erp_core.channels.webhook_router import webhook_router
from erp_core.shared.enums import (
    ChannelType,
    ConversationMode,
    DeliveryStatus,
    IdentityType,
    MessageContentType,
    MessageDirection,
    MessageSenderType,
    UserRole,
)
from erp_core.tenancy.dependencies import CurrentPrincipal


class _TestWhatsAppEchoAdapter(MessagingChannelAdapter):
    channel_types: ClassVar[tuple[ChannelType, ...]] = (ChannelType.WHATSAPP_CLOUD,)

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        sig = headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        events: list[tuple[str, str | None]] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                val = change.get("value", {})
                phone_id = val.get("metadata", {}).get("phone_number_id")
                for msg in val.get("messages", []):
                    events.append((msg["id"], phone_id))
                for status in val.get("statuses", []):
                    events.append(
                        (f"status:{status['id']}:{status['status']}", phone_id)
                    )
        if not events:
            events.append((hashlib.sha256(raw_body).hexdigest(), None))
        return events

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        inbound: list[NormalizedInboundEvent] = []
        statuses: list[NormalizedStatusEvent] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                val = change.get("value", {})
                for msg in val.get("messages", []):
                    inbound.append(
                        NormalizedInboundEvent(
                            external_event_id=msg["id"],
                            external_account_id=val["metadata"]["phone_number_id"],
                            external_message_id=msg["id"],
                            external_thread_id=msg["from"],
                            sender_identity_type=IdentityType.WHATSAPP_WAID,
                            sender_identity_value=msg["from"],
                            sender_display_name="Storm Tester",
                            direction=MessageDirection.INBOUND,
                            sender_type=MessageSenderType.CUSTOMER,
                            content_type=MessageContentType.TEXT,
                            body_text=msg["text"]["body"],
                            source_timestamp=datetime.fromtimestamp(
                                int(msg["timestamp"]), tz=UTC
                            ),
                            conversation_mode=ConversationMode.B2C_SUPPORT,
                            parent_external_id=msg.get("parent_external_id"),
                        )
                    )
                for st in val.get("statuses", []):
                    statuses.append(
                        NormalizedStatusEvent(
                            external_message_id=st["id"],
                            external_account_id=val["metadata"]["phone_number_id"],
                            delivery_status=DeliveryStatus(st["status"]),
                            timestamp=datetime.fromtimestamp(
                                int(st["timestamp"]), tz=UTC
                            ),
                            require_existing_message=True,
                        )
                    )
        return NormalizedWebhookBatch(inbound_events=inbound, status_events=statuses)

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        return OutboundSendResult(success=True, external_message_id="wamid.out.1")


@pytest.mark.asyncio
async def test_webhook_hmac_latency_100x_storm_and_dlq_replay(monkeypatch) -> None:
    secret = "test_whatsapp_app_secret"
    monkeypatch.setenv("WHATSAPP_APP_SECRET", secret)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify_tok_123")

    migrator_engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
        future=True,
    )
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    phone_number_id = f"pn_{uuid.uuid4().hex[:10]}"

    registry = AdapterRegistry()
    registry.register("whatsapp", _TestWhatsAppEchoAdapter())
    registry.register(ChannelType.WHATSAPP_CLOUD.value, _TestWhatsAppEchoAdapter())

    app = FastAPI()
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.adapter_registry = registry
    app.include_router(webhook_router)
    app.include_router(dlq_router)

    # Override RBAC dependency on dlq_router to act as tenant admin
    from erp_core.channels.ingress_worker import _require_dlq_operator

    app.dependency_overrides[_require_dlq_operator] = lambda: CurrentPrincipal(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=UserRole.ADMIN,
        email="admin@factory.test",
    )

    try:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Storm Factory')"),
                {"id": tenant_id, "slug": f"storm-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, "
                    "status, rate_limit_profile) "
                    "VALUES (:id, :tid, 'whatsapp_cloud', :ext, 'Storm WA', 'active', "
                    "'{\"mps\": 80}'::jsonb)"
                ),
                {"id": account_id, "tid": tenant_id, "ext": phone_number_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_routing_directory "
                    "(channel_type, external_account_id, tenant_id, channel_account_id) "
                    "VALUES ('whatsapp_cloud', :ext, :tid, :aid)"
                ),
                {"ext": phone_number_id, "tid": tenant_id, "aid": account_id},
            )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Invalid HMAC must return 401
            bad_resp = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=b'{"entry":[]}',
                headers={"X-Hub-Signature-256": "sha256=deadbeef"},
            )
            assert bad_resp.status_code == 401

            # 2. Warmup + <100ms 200 OK persistence check
            warmup_payload = {
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {"phone_number_id": phone_number_id},
                                    "messages": [
                                        {
                                            "id": f"wamid.warm.{uuid.uuid4().hex[:8]}",
                                            "from": "14155550199",
                                            "timestamp": "1763500000",
                                            "type": "text",
                                            "text": {"body": "Warmup"},
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ]
            }
            warm_raw = json.dumps(warmup_payload).encode()
            warm_sig = "sha256=" + hmac.new(secret.encode(), warm_raw, hashlib.sha256).hexdigest()
            t0 = time.perf_counter()
            warm_resp = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=warm_raw,
                headers={"X-Hub-Signature-256": warm_sig, "Content-Type": "application/json"},
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert warm_resp.status_code == 200
            assert elapsed_ms < 100.0

            # 3. 100 duplicate webhook storm -> 1 persisted message + 0 duplicates
            storm_wamid = f"wamid.storm.{uuid.uuid4().hex[:8]}"
            storm_payload = {
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {"phone_number_id": phone_number_id},
                                    "messages": [
                                        {
                                            "id": storm_wamid,
                                            "from": "14155550188",
                                            "timestamp": "1763500100",
                                            "type": "text",
                                            "text": {"body": "Need 2000m LED strip quote"},
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ]
            }
            storm_raw = json.dumps(storm_payload).encode()
            storm_sig = "sha256=" + hmac.new(secret.encode(), storm_raw, hashlib.sha256).hexdigest()

            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/v1/webhooks/whatsapp",
                        content=storm_raw,
                        headers={
                            "X-Hub-Signature-256": storm_sig,
                            "Content-Type": "application/json",
                        },
                    )
                    for _ in range(100)
                ]
            )
            assert all(r.status_code == 200 for r in responses)
            new_accepts = [r.json() for r in responses if not r.json()["deduplicated"]]
            assert len(new_accepts) == 1

            raw_event_id = uuid.UUID(new_accepts[0]["raw_event_ids"][0])
            assert raw_event_id.version == 4
            consumed = 0
            for _ in range(4):
                consumed += await consume_webhook_stream_batch(
                    session_factory,
                    redis,
                    ChannelType.WHATSAPP_CLOUD,
                    adapter_registry=registry,
                    count=50,
                )
            assert consumed >= 1
            again = await consume_webhook_stream_batch(
                session_factory,
                redis,
                ChannelType.WHATSAPP_CLOUD,
                adapter_registry=registry,
                count=50,
            )
            assert again == 0

            async with migrator_engine.connect() as conn:
                raw_cnt = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM raw_webhook_events WHERE external_event_id = :eid"
                        ),
                        {"eid": storm_wamid},
                    )
                ).scalar_one()
                msg_cnt = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM messages "
                            "WHERE tenant_id = :tid AND external_message_id = :eid"
                        ),
                        {"tid": tenant_id, "eid": storm_wamid},
                    )
                ).scalar_one()
                assert raw_cnt == 1
                assert msg_cnt == 1

            # 4. Out-of-order parent -> DLQ after max attempts -> Replay API
            orphan_status_payload = {
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "metadata": {"phone_number_id": phone_number_id},
                                    "statuses": [
                                        {
                                            "id": f"wamid.missing.{uuid.uuid4().hex[:8]}",
                                            "status": "delivered",
                                            "timestamp": "1763500200",
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ]
            }
            orphan_raw = json.dumps(orphan_status_payload).encode()
            orphan_sig = (
                "sha256=" + hmac.new(secret.encode(), orphan_raw, hashlib.sha256).hexdigest()
            )
            orphan_resp = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=orphan_raw,
                headers={"X-Hub-Signature-256": orphan_sig, "Content-Type": "application/json"},
            )
            assert orphan_resp.status_code == 200
            orphan_raw_id = uuid.UUID(orphan_resp.json()["raw_event_ids"][0])

            # Attempt 6 exceeds max 5 retries -> writes to dead_letter_events
            await process_raw_webhook_event(
                session_factory, redis, orphan_raw_id, registry, attempt=6
            )
            async with migrator_engine.connect() as conn:
                dlq_id = (
                    await conn.execute(
                        text(
                            "SELECT id FROM dead_letter_events WHERE raw_webhook_event_id = :rid"
                        ),
                        {"rid": orphan_raw_id},
                    )
                ).scalar_one()

            replay_resp = await client.post(f"/api/v1/channels/dlq/{dlq_id}/replay")
            assert replay_resp.status_code == 200
            assert replay_resp.json()["status"] == "replayed"
    finally:
        async with migrator_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM audit_events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM dead_letter_events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM raw_webhook_events WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM channel_routing_directory WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM messages WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM conversations WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM customer_identities WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(
                text("DELETE FROM channel_accounts WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await redis.aclose()
        await runtime_engine.dispose()
        await migrator_engine.dispose()
