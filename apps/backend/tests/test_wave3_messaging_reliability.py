"""Wave 3 P1 messaging reliability: outbound idempotency, DLQ and monotonic receipts.

Covers four defects that each ended in silent, customer-visible message loss:

* a redelivered outbound stream entry double-sending after a crash between send and XACK;
* a missing channel adapter raising ``RuntimeError``, which left the message ``queued``
  forever with no DLQ row and no SSE announcement;
* out-of-order delivery receipts regressing ``read -> delivered`` / ``delivered -> sent``;
* widget replies dead-lettering because the gateway stub, not ``WebLiveChatAdapter``,
  was registered for ``web_livechat``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels import ingress_worker
from erp_core.channels.adapters.base import (
    AdapterRegistry,
    MessagingChannelAdapter,
    NormalizedWebhookBatch,
    OutboundMessageRequest,
    OutboundSendResult,
)
from erp_core.channels.adapters.gateway import GatewayIngressAdapter
from erp_core.channels.adapters.web_livechat import WebLiveChatAdapter
from erp_core.channels.ingress_worker import (
    can_transition_delivery_status,
    send_outbound_message,
)
from erp_core.channels.webhook_router import (
    OUTBOUND_STREAM_KEY,
    build_default_adapter_registry,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import (
    ChannelType,
    DeliveryStatus,
    MessageContentType,
    UserRole,
)
from erp_core.tenancy.dependencies import CurrentPrincipal


class _RecordingAdapter(MessagingChannelAdapter):
    """Adapter that records every send so a duplicate dispatch is observable."""

    channel_types: ClassVar[tuple[ChannelType, ...]] = ()

    def __init__(self, *, mark_delivered: bool = False) -> None:
        self.sent: list[uuid.UUID] = []
        self._mark_delivered = mark_delivered

    def verify_webhook_signature(
        self, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> bool:
        return False

    def extract_external_event_ids(
        self, payload: dict[str, Any], raw_body: bytes, headers: Mapping[str, str]
    ) -> list[tuple[str, str | None]]:
        return []

    async def parse_webhook(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        channel_account_id: uuid.UUID,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> NormalizedWebhookBatch:
        return NormalizedWebhookBatch()

    async def send_message(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        credentials: dict[str, Any],
        request: OutboundMessageRequest,
    ) -> OutboundSendResult:
        self.sent.append(request.message_id)
        status = "delivered" if self._mark_delivered else "sent"
        await session.execute(
            text("UPDATE messages SET delivery_status = :dstat WHERE id = :mid"),
            {"dstat": status, "mid": request.message_id},
        )
        return OutboundSendResult(success=True, external_message_id=f"ext-{request.message_id}")


@dataclass
class _Fixture:
    """One tenant with a channel account, conversation and outbound message."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    channel_type: ChannelType

    def request(self, channel_type: ChannelType | None = None) -> OutboundMessageRequest:
        effective = channel_type or self.channel_type
        return OutboundMessageRequest(
            tenant_id=self.tenant_id,
            channel_account_id=self.account_id,
            channel_type=effective,
            external_account_id=f"pn_{self.account_id.hex[:10]}",
            recipient_id="+491701234567",
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            content_type=MessageContentType.TEXT,
            body_text="Your COB strip ships tomorrow.",
        )


@dataclass
class _Runtime:
    factory: async_sessionmaker[AsyncSession]
    redis: aioredis.Redis
    tenants: list[uuid.UUID] = field(default_factory=list)

    async def seed(
        self, *, channel_type: ChannelType, delivery_status: DeliveryStatus
    ) -> _Fixture:
        fixture = await _seed_message(
            self.factory, channel_type=channel_type, delivery_status=delivery_status
        )
        self.tenants.append(fixture.tenant_id)
        return fixture


async def _seed_message(
    factory: async_sessionmaker[AsyncSession],
    *,
    channel_type: ChannelType,
    delivery_status: DeliveryStatus,
) -> _Fixture:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name) "
                "VALUES (:id, :slug, 'Reliability Tenant')"
            ),
            {"id": tenant_id, "slug": f"w3r-{tenant_id.hex[:10]}"},
        )
        await session.execute(
            text(
                "INSERT INTO channel_accounts "
                "(id, tenant_id, channel_type, external_account_id, display_name, status, "
                "rate_limit_profile) VALUES ("
                ":id, :tid, :ctype, :ext, 'Reliability Channel', 'active', "
                "'{\"mps\": 50}'::jsonb)"
            ),
            {
                "id": account_id,
                "tid": tenant_id,
                "ctype": channel_type.value,
                "ext": f"pn_{account_id.hex[:10]}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO customers "
                "(id, tenant_id, customer_type, display_name, primary_phone_e164) "
                "VALUES (:id, :tid, 'b2c_consumer', 'Hans Mueller', '+491701234567')"
            ),
            {"id": customer_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO conversations "
                "(id, tenant_id, customer_id, channel_account_id, channel_type, "
                "external_thread_id, conversation_mode, status, unread_count, updated_at) "
                "VALUES (:id, :tid, :cid, :aid, :ctype, '+491701234567', 'b2c_support', "
                "'open', 0, now())"
            ),
            {
                "id": conversation_id,
                "tid": tenant_id,
                "cid": customer_id,
                "aid": account_id,
                "ctype": channel_type.value,
            },
        )
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, tenant_id, conversation_id, customer_id, channel_type, direction, "
                "sender_type, content_type, body_text, delivery_status, source_timestamp) "
                "VALUES (:id, :tid, :conv, :cid, :ctype, 'outbound', 'human_agent', 'text', "
                "'Your COB strip ships tomorrow.', :dstat, now())"
            ),
            {
                "id": message_id,
                "tid": tenant_id,
                "conv": conversation_id,
                "cid": customer_id,
                "ctype": channel_type.value,
                "dstat": delivery_status.value,
            },
        )
    return _Fixture(
        tenant_id=tenant_id,
        account_id=account_id,
        conversation_id=conversation_id,
        message_id=message_id,
        channel_type=channel_type,
    )


@pytest.fixture
async def runtime() -> Any:
    engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    handle = _Runtime(
        factory=factory,
        redis=aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0")),
    )
    try:
        yield handle
    finally:
        for tenant_id in handle.tenants:
            async with tenant_session(factory, tenant_id) as session:
                await session.execute(
                    text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
                )
        await handle.redis.aclose()
        await engine.dispose()


async def _message_state(runtime: _Runtime, fixture: _Fixture) -> dict[str, Any]:
    async with tenant_session(runtime.factory, fixture.tenant_id) as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT delivery_status, external_message_id, channel_metadata "
                        "FROM messages WHERE tenant_id = :tid AND id = :mid"
                    ),
                    {"tid": fixture.tenant_id, "mid": fixture.message_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _dead_letters(runtime: _Runtime, fixture: _Fixture) -> list[dict[str, Any]]:
    async with tenant_session(runtime.factory, fixture.tenant_id) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT direction, channel_type, error_code, error_detail, "
                        "external_event_id, attempt_count FROM dead_letter_events "
                        "WHERE tenant_id = :tid ORDER BY created_at"
                    ),
                    {"tid": fixture.tenant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# --------------------------------------------------------- monotonic delivery receipts


@pytest.mark.parametrize(
    ("current", "incoming"),
    [
        ("queued", "sent"),
        ("queued", "delivered"),
        ("queued", "read"),
        ("sent", "delivered"),
        ("sent", "read"),
        ("delivered", "read"),
        ("queued", "failed"),
        ("sent", "failed"),
    ],
)
def test_can_transition_delivery_status_allows_forward_progress(
    current: str, incoming: str
) -> None:
    assert can_transition_delivery_status(current, incoming) is True


@pytest.mark.parametrize(
    ("current", "incoming"),
    [
        ("read", "delivered"),
        ("read", "sent"),
        ("read", "failed"),
        ("delivered", "sent"),
        ("delivered", "failed"),
        ("sent", "queued"),
        ("delivered", "queued"),
        ("queued", "queued"),
        ("sent", "sent"),
        ("failed", "sent"),
        ("failed", "delivered"),
        ("failed", "read"),
        ("queued", "unknown_status"),
    ],
)
def test_can_transition_delivery_status_rejects_regression_or_unknown(
    current: str, incoming: str
) -> None:
    assert can_transition_delivery_status(current, incoming) is False


# ------------------------------------------------------------ outbound idempotency guard


@pytest.mark.parametrize("terminal", [DeliveryStatus.SENT, DeliveryStatus.DELIVERED])
async def test_send_outbound_message_is_idempotent_for_terminal_statuses(
    runtime: _Runtime, terminal: DeliveryStatus
) -> None:
    fixture = await runtime.seed(channel_type=ChannelType.WHATSAPP_CLOUD, delivery_status=terminal)
    adapter = _RecordingAdapter()
    registry = AdapterRegistry()
    registry.register(ChannelType.WHATSAPP_CLOUD.value, adapter)

    outcome = await send_outbound_message(
        session_factory=runtime.factory,
        redis=runtime.redis,
        crypto_service=EnvelopeCryptoService(get_settings()),
        request_payload=fixture.request(),
        adapter_registry=registry,
        attempt=2,
    )

    assert outcome == "already_sent"
    # The whole point: a redelivered entry must never reach the channel twice.
    assert adapter.sent == []
    state = await _message_state(runtime, fixture)
    assert state["delivery_status"] == terminal.value


async def test_send_outbound_message_without_adapter_dead_letters_instead_of_sticking(
    runtime: _Runtime,
) -> None:
    fixture = await runtime.seed(
        channel_type=ChannelType.AMAZON_SP, delivery_status=DeliveryStatus.QUEUED
    )

    outcome = await send_outbound_message(
        session_factory=runtime.factory,
        redis=runtime.redis,
        crypto_service=EnvelopeCryptoService(get_settings()),
        request_payload=fixture.request(ChannelType.AMAZON_SP),
        adapter_registry=AdapterRegistry(),
        attempt=1,
    )

    assert outcome == "dead_letter"
    state = await _message_state(runtime, fixture)
    assert state["delivery_status"] == DeliveryStatus.FAILED.value
    assert state["channel_metadata"]["error_code"] == "ERR_ADAPTER_MISSING"

    dead_letters = await _dead_letters(runtime, fixture)
    assert len(dead_letters) == 1
    assert dead_letters[0]["direction"] == "outbound"
    assert dead_letters[0]["channel_type"] == ChannelType.AMAZON_SP.value
    assert dead_letters[0]["error_code"] == "ERR_ADAPTER_MISSING"
    assert dead_letters[0]["external_event_id"] == str(fixture.message_id)


async def test_send_outbound_message_never_downgrades_a_delivered_receipt(
    runtime: _Runtime,
) -> None:
    fixture = await runtime.seed(
        channel_type=ChannelType.WHATSAPP_CLOUD, delivery_status=DeliveryStatus.QUEUED
    )
    adapter = _RecordingAdapter(mark_delivered=True)
    registry = AdapterRegistry()
    registry.register(ChannelType.WHATSAPP_CLOUD.value, adapter)

    outcome = await send_outbound_message(
        session_factory=runtime.factory,
        redis=runtime.redis,
        crypto_service=EnvelopeCryptoService(get_settings()),
        request_payload=fixture.request(),
        adapter_registry=registry,
        attempt=1,
    )

    assert outcome == "sent"
    assert adapter.sent == [fixture.message_id]
    state = await _message_state(runtime, fixture)
    # The adapter already confirmed delivery; the worker must not rewrite it to 'sent'.
    assert state["delivery_status"] == DeliveryStatus.DELIVERED.value
    assert state["external_message_id"] == f"ext-{fixture.message_id}"


# ------------------------------------------------- web_livechat outbound is registered


def test_default_registry_registers_web_livechat_outbound_adapter() -> None:
    registry = build_default_adapter_registry()
    adapter = registry.get(ChannelType.WEB_LIVECHAT.value)

    assert adapter is not None
    # Previously the gateway stub owned this key and refused every outbound send with
    # UnsupportedOutbound, dead-lettering agent replies to widget visitors.
    assert isinstance(adapter, WebLiveChatAdapter)
    assert not isinstance(adapter, GatewayIngressAdapter)
    assert registry.get("web_livechat") is adapter


class _RecordingRedis:
    """Minimal Redis surface used by the widget adapter's SSE nudge."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


async def test_web_livechat_adapter_sends_outbound_without_gateway_failure(
    runtime: _Runtime,
) -> None:
    fixture = await runtime.seed(
        channel_type=ChannelType.WEB_LIVECHAT, delivery_status=DeliveryStatus.QUEUED
    )
    recording_redis = _RecordingRedis()
    registry = build_default_adapter_registry(cast(Any, recording_redis))
    adapter = registry.get(ChannelType.WEB_LIVECHAT.value)
    assert isinstance(adapter, WebLiveChatAdapter)

    async with tenant_session(runtime.factory, fixture.tenant_id) as session:
        result = await adapter.send_message(
            session=session,
            tenant_id=fixture.tenant_id,
            credentials={},
            request=fixture.request(ChannelType.WEB_LIVECHAT),
        )

    assert result.success is True
    assert result.error_code is None
    assert result.external_message_id
    # The visitor's widget gets the live SSE nudge on its own conversation channel.
    channel, payload = recording_redis.published[0]
    assert channel == f"widget:conv:{fixture.conversation_id}"
    assert json.loads(payload)["external_message_id"] == result.external_message_id


async def test_web_livechat_reply_dispatches_through_the_worker_without_dlq(
    runtime: _Runtime,
) -> None:
    fixture = await runtime.seed(
        channel_type=ChannelType.WEB_LIVECHAT, delivery_status=DeliveryStatus.QUEUED
    )

    outcome = await send_outbound_message(
        session_factory=runtime.factory,
        redis=runtime.redis,
        crypto_service=EnvelopeCryptoService(get_settings()),
        request_payload=fixture.request(ChannelType.WEB_LIVECHAT),
        attempt=1,
    )

    assert outcome == "sent"
    state = await _message_state(runtime, fixture)
    assert state["delivery_status"] in (
        DeliveryStatus.SENT.value,
        DeliveryStatus.DELIVERED.value,
    )
    assert state["external_message_id"]
    # The gateway stub used to fail every widget reply into the DLQ.
    assert await _dead_letters(runtime, fixture) == []


# ------------------------------------------------ stream entries stay pending on failure


async def test_outbound_dispatch_failure_keeps_entry_pending_for_xautoclaim() -> None:
    stream_key = OUTBOUND_STREAM_KEY
    group = f"w3r-pending-{uuid.uuid4().hex[:8]}"
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    request = OutboundMessageRequest(
        tenant_id=uuid.uuid4(),
        channel_account_id=uuid.uuid4(),
        channel_type=ChannelType.WHATSAPP_CLOUD,
        external_account_id="pn_test",
        recipient_id="+491701234567",
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        body_text="boom",
    )
    entry_id = await redis.xadd(
        stream_key, {"payload": json.dumps(request.model_dump(mode="json"))}
    )

    async def _boom(*args: Any, **kwargs: Any) -> str:
        raise ConnectionError("adapter transport exploded")

    original = ingress_worker.send_outbound_message
    ingress_worker.send_outbound_message = _boom  # type: ignore[assignment]
    try:
        await redis.xgroup_create(stream_key, group, id="0", mkstream=True)
        consumed = await ingress_worker.consume_outbound_stream_batch(
            session_factory=None,  # type: ignore[arg-type]
            redis=redis,
            group_name=group,
            consumer_name="worker-test",
            count=50,
            block_ms=10,
            min_idle_ms=0,
        )
    finally:
        ingress_worker.send_outbound_message = original  # type: ignore[assignment]

    try:
        assert consumed == 0
        pending = await redis.xpending(stream_key, group)
        # NOT acked: XAUTOCLAIM retries it, so a crash cannot silently drop the message.
        assert int(pending["pending"]) == 1
        claimed = await redis.xautoclaim(
            stream_key, group, "worker-test-2", 0, start_id="0-0", count=10
        )
        reclaimed_ids = [msg_id for msg_id, _fields in claimed[1]]
        assert entry_id in reclaimed_ids
    finally:
        await redis.delete(stream_key)
        await redis.aclose()


async def test_dlq_outbound_replay_pushes_onto_the_consumed_stream(
    runtime: _Runtime,
) -> None:
    fixture = await runtime.seed(
        channel_type=ChannelType.WHATSAPP_CLOUD, delivery_status=DeliveryStatus.FAILED
    )
    principal = CurrentPrincipal(
        user_id=uuid.uuid4(),
        tenant_id=fixture.tenant_id,
        role=UserRole.OWNER,
        email="owner@reliability.test",
    )
    dlq_id = uuid.uuid4()
    async with tenant_session(runtime.factory, fixture.tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO dead_letter_events "
                "(id, tenant_id, channel_type, direction, external_event_id, payload, "
                "error_code, error_detail, attempt_count, status) VALUES ("
                ":id, :tid, 'whatsapp_cloud', 'outbound', :ext, CAST(:payload AS jsonb), "
                "'ERR_OUTBOUND_FAILED', 'exhausted', 6, 'open')"
            ),
            {
                "id": dlq_id,
                "tid": fixture.tenant_id,
                "ext": str(fixture.message_id),
                "payload": json.dumps(fixture.request().model_dump(mode="json")),
            },
        )

    app = FastAPI()
    app.state.session_factory = runtime.factory
    app.state.redis = runtime.redis
    app.include_router(ingress_worker.dlq_router)
    app.dependency_overrides[ingress_worker._require_dlq_operator] = lambda: principal

    replayed_ids: list[str] = []
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/v1/channels/dlq/{dlq_id}/replay")
        assert resp.status_code == 200, resp.text
        assert resp.json()["outcome"] == "outbound_requeued"

        entries = await runtime.redis.xrevrange(OUTBOUND_STREAM_KEY, count=200)
        for entry_id, fields in entries:
            body = json.loads(fields[b"payload"])
            if body.get("message_id") == str(fixture.message_id):
                replayed_ids.append(entry_id)
        assert replayed_ids, "replayed outbound request never reached the consumed stream"
        assert await runtime.redis.xlen("stream:outbound:replay") == 0
    finally:
        if replayed_ids:
            await runtime.redis.xdel(OUTBOUND_STREAM_KEY, *replayed_ids)
