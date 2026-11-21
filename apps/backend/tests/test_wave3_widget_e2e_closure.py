"""Wave-3 widget closure: real webhook HMAC, clean outbound ack, guarded AI auto-reply."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels import ingress_worker
from erp_core.channels import widget_router as widget_router_module
from erp_core.channels.adapters.base import OutboundMessageRequest
from erp_core.channels.adapters.web_livechat import WebLiveChatAdapter
from erp_core.channels.widget_router import widget_router
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, MessageContentType

WEBHOOK_SECRET = "widget_webhook_shared_secret_2026"
ALLOWED_ORIGIN = "https://ledstore.example.com"
RAW_BODY = json.dumps(
    {
        "event_id": "evt_widget_1",
        "site_key": "site_demo",
        "visitor_id": "8f1b0b6e-1f6a-4f1c-9d1e-2a0b3c4d5e6f",
        "body_text": "Do you ship IP67 strip to Germany?",
    }
).encode()


# --------------------------------------------------------------------------- helpers


def _hmac_hex(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@dataclass
class _RecordingRedis:
    """Minimal async Redis stand-in recording published payloads in order."""

    published: list[tuple[str, str]] = field(default_factory=list)

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


@dataclass
class _FailingRedis:
    async def publish(self, channel: str, payload: str) -> int:
        raise ConnectionError("redis down")


@dataclass
class WidgetEnv:
    app: FastAPI
    client: AsyncClient
    session_factory: async_sessionmaker[AsyncSession]
    redis: _RecordingRedis
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    site_key: str


@pytest.fixture
async def widget_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WidgetEnv]:
    # Never let a real LLM/DEEPSEEK key leak into this suite's auto-reply path.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    site_key = f"site_{uuid.uuid4().hex[:10]}"
    redis = _RecordingRedis()

    app = FastAPI()
    app.state.session_factory = session_factory
    app.state.redis = cast(Redis, redis)
    app.include_router(widget_router)

    async with tenant_session(session_factory, tenant_id) as sess:
        await sess.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Widget Store')"),
            {"id": tenant_id, "slug": f"w3d-{tenant_id.hex[:8]}"},
        )
        await sess.execute(
            text(
                "INSERT INTO channel_accounts "
                "(id, tenant_id, channel_type, external_account_id, display_name, status, "
                "rate_limit_profile, config) VALUES ("
                ":id, :tid, 'web_livechat', :skey, 'LED Store Chat', 'active', "
                "'{\"mps\": 100}'::jsonb, CAST(:cfg AS jsonb))"
            ),
            {
                "id": account_id,
                "tid": tenant_id,
                "skey": site_key,
                "cfg": json.dumps(
                    {
                        "allowed_origins": [ALLOWED_ORIGIN],
                        "business_name": "Quanzhou LED Co.",
                    }
                ),
            },
        )
        await sess.execute(
            text(
                "SELECT register_channel_routing("
                "'web_livechat', :skey, CAST(:tid AS uuid), CAST(:aid AS uuid), true)"
            ),
            {"skey": site_key, "tid": tenant_id, "aid": account_id},
        )

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    env = WidgetEnv(
        app=app,
        client=client,
        session_factory=session_factory,
        redis=redis,
        tenant_id=tenant_id,
        account_id=account_id,
        site_key=site_key,
    )
    try:
        yield env
    finally:
        await client.aclose()
        async with tenant_session(session_factory, tenant_id) as conn:
            await conn.execute(
                text(
                    "SELECT register_channel_routing("
                    "'web_livechat', :skey, CAST(:tid AS uuid), CAST(:aid AS uuid), false)"
                ),
                {"skey": site_key, "tid": tenant_id, "aid": account_id},
            )
        await runtime_engine.dispose()


async def _open_widget_session(env: WidgetEnv) -> dict[str, Any]:
    resp = await env.client.post(
        "/api/v1/widget/session",
        json={"site_key": env.site_key, "page_url": "https://ledstore.example.com/products/cob"},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    return body


# ---------------------------------------------------------- webhook HMAC verification


def test_verify_webhook_signature_accepts_raw_and_sha256_prefixed_digest() -> None:
    adapter = WebLiveChatAdapter()
    digest = _hmac_hex(RAW_BODY, WEBHOOK_SECRET)

    assert adapter.verify_webhook_signature(
        RAW_BODY, {"x-widget-signature": digest}, WEBHOOK_SECRET
    )
    assert adapter.verify_webhook_signature(
        RAW_BODY, {"X-Widget-Signature": f"sha256={digest}"}, WEBHOOK_SECRET
    )
    assert adapter.verify_webhook_signature(
        RAW_BODY, {"x-webhook-signature": digest}, WEBHOOK_SECRET
    )
    assert adapter.verify_webhook_signature(
        RAW_BODY, {"X-Webhook-Signature": f"SHA256={digest.upper()}"}, WEBHOOK_SECRET
    )


def test_verify_webhook_signature_fails_closed_on_missing_or_wrong_material() -> None:
    adapter = WebLiveChatAdapter()
    digest = _hmac_hex(RAW_BODY, WEBHOOK_SECRET)

    # 1. Missing header entirely.
    assert not adapter.verify_webhook_signature(RAW_BODY, {}, WEBHOOK_SECRET)
    # 2. Empty / whitespace-only signature header.
    assert not adapter.verify_webhook_signature(
        RAW_BODY, {"x-widget-signature": "   "}, WEBHOOK_SECRET
    )
    assert not adapter.verify_webhook_signature(
        RAW_BODY, {"x-widget-signature": "sha256="}, WEBHOOK_SECRET
    )
    # 3. Empty secret must never authenticate an unsigned/forgable webhook.
    assert not adapter.verify_webhook_signature(RAW_BODY, {"x-widget-signature": digest}, "")
    assert not adapter.verify_webhook_signature(
        RAW_BODY, {"x-widget-signature": digest}, "   "
    )
    # 4. Digest computed with a different secret.
    assert not adapter.verify_webhook_signature(
        RAW_BODY, {"x-widget-signature": _hmac_hex(RAW_BODY, "attacker-secret")}, WEBHOOK_SECRET
    )
    # 5. Valid digest for a different body (replay with tampered payload).
    other_body = RAW_BODY.replace(b"Germany", b"Brazil")
    assert not adapter.verify_webhook_signature(
        other_body, {"x-widget-signature": digest}, WEBHOOK_SECRET
    )
    # 6. Garbage signature.
    assert not adapter.verify_webhook_signature(
        RAW_BODY, {"x-widget-signature": "not-a-signature"}, WEBHOOK_SECRET
    )


# --------------------------------------------------------------- outbound ack delivery


def _outbound_request() -> OutboundMessageRequest:
    return OutboundMessageRequest(
        tenant_id=uuid.uuid4(),
        channel_account_id=uuid.uuid4(),
        channel_type=ChannelType.WEB_LIVECHAT,
        external_account_id="site_demo",
        recipient_id=str(uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        content_type=MessageContentType.TEXT,
        body_text="IP67 strip ships tomorrow.",
    )


async def test_send_message_returns_deterministic_external_message_id() -> None:
    redis = _RecordingRedis()
    adapter = WebLiveChatAdapter(redis=cast(Redis, redis))
    request = _outbound_request()

    result = await adapter.send_message(
        session=cast(AsyncSession, None),
        tenant_id=request.tenant_id,
        credentials={},
        request=request,
    )

    assert result.success is True
    assert result.external_message_id == f"web:{request.message_id}"
    assert result.error_code is None
    assert result.retryable is False
    # Widget visitors read the reply from the messages table; the SSE nudge is optional.
    channel, payload = redis.published[0]
    assert channel == f"widget:conv:{request.conversation_id}"
    published = json.loads(payload)
    assert published["external_message_id"] == f"web:{request.message_id}"
    assert published["conversation_id"] == str(request.conversation_id)


async def test_send_message_succeeds_even_when_redis_push_fails() -> None:
    adapter = WebLiveChatAdapter(redis=cast(Redis, _FailingRedis()))
    request = _outbound_request()

    result = await adapter.send_message(
        session=cast(AsyncSession, None),
        tenant_id=request.tenant_id,
        credentials={},
        request=request,
    )

    assert result.success is True
    assert result.external_message_id == f"web:{request.message_id}"


# ------------------------------------------------------- guarded AI auto-reply trigger


@dataclass
class _TriggerObservation:
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    inbound_messages_at_call: int
    sse_events_at_call: int


async def test_visitor_message_triggers_auto_reply_after_commit(
    widget_env: WidgetEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _open_widget_session(widget_env)
    conversation_id = uuid.UUID(session["conversation_id"])
    client_id = str(session["customer_id"])
    observations: list[_TriggerObservation] = []

    async def spy(
        session_factory: async_sessionmaker[AsyncSession],
        redis: Any,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> None:
        # The visitor message must already be committed and visible to a fresh session.
        async with tenant_session(session_factory, tenant_id) as sess:
            committed = await sess.scalar(
                text(
                    "SELECT count(*) FROM messages WHERE tenant_id = :tid "
                    "AND conversation_id = :cid AND direction = 'inbound'"
                ),
                {"tid": tenant_id, "cid": conversation_id},
            )
        observations.append(
            _TriggerObservation(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                inbound_messages_at_call=int(cast(Any, committed) or 0),
                sse_events_at_call=len(cast(_RecordingRedis, redis).published),
            )
        )

    monkeypatch.setattr(widget_router_module, "_trigger_widget_auto_reply", spy)

    resp = await widget_env.client.post(
        "/api/v1/widget/messages",
        json={"body_text": "Can you quote 500m of COB strip?"},
        headers={
            "Authorization": f"Bearer {session['session_token']}",
            "Origin": ALLOWED_ORIGIN,
        },
    )
    assert resp.status_code == 201
    message_id = uuid.UUID(resp.json()["id"])

    assert len(observations) == 1, "auto-reply must fire exactly once per visitor turn"
    observed = observations[0]
    assert observed.tenant_id == widget_env.tenant_id
    assert observed.conversation_id == conversation_id
    # 1. Triggered only after the inbound message committed.
    assert observed.inbound_messages_at_call == 1
    # 2. Triggered only after the agent inbox `message.created` SSE event was published.
    assert observed.sse_events_at_call >= 1
    event = json.loads(widget_env.redis.published[0][1])
    assert event["event"] == "message.created"
    assert event["message_id"] == str(message_id)

    # 3. The visitor payload is what the AI layer will read (tenant-scoped).
    async with tenant_session(widget_env.session_factory, widget_env.tenant_id) as sess:
        stored = (
            (
                await sess.execute(
                    text(
                        "SELECT body_text, sender_type, customer_id FROM messages "
                        "WHERE tenant_id = :tid AND id = :mid"
                    ),
                    {"tid": widget_env.tenant_id, "mid": message_id},
                )
            )
            .mappings()
            .one()
        )
    assert stored["body_text"] == "Can you quote 500m of COB strip?"
    assert stored["sender_type"] == "customer"
    assert str(stored["customer_id"]) == client_id


async def test_auto_reply_helper_swallows_ai_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def boom(
        session_factory: Any, redis: Any, tenant_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> bool:
        called.append((tenant_id, conversation_id))
        raise RuntimeError("LLM provider exploded")

    monkeypatch.setattr(ingress_worker, "_maybe_run_auto_reply", boom)
    tenant_id = uuid.uuid4()
    conversation_id = uuid.uuid4()

    # Must never raise: visitor delivery outranks a broken AI provider.
    await widget_router_module._trigger_widget_auto_reply(
        cast(async_sessionmaker[AsyncSession], None),
        cast(Redis, _RecordingRedis()),
        tenant_id,
        conversation_id,
    )

    assert called == [(tenant_id, conversation_id)]


async def test_auto_reply_helper_actually_invokes_ingress_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake(
        session_factory: Any, redis: Any, tenant_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> bool:
        called.append((tenant_id, conversation_id))
        return True

    monkeypatch.setattr(ingress_worker, "_maybe_run_auto_reply", fake)
    session_factory = cast(async_sessionmaker[AsyncSession], object())
    redis = cast(Redis, _RecordingRedis())
    tenant_id = uuid.uuid4()
    conversation_id = uuid.uuid4()

    await widget_router_module._trigger_widget_auto_reply(
        session_factory, redis, tenant_id, conversation_id
    )

    assert called == [(tenant_id, conversation_id)]
