"""Wave 6 / W6-A: inbox SSE idle keepalive + proxy/buffering headers.

The inbox stream previously went silent whenever Redis Pub/Sub had nothing to
say, so idle proxies/ingress (60s timeouts) would kill the connection and the
frontend EventSource would reconnect-loop. The fix emits SSE comment frames
(`: keepalive\\n\\n`) on an idle interval and pins the response headers that
disable proxy buffering.

These tests drive the route handler's async generator directly with a fake
Redis pub/sub, so they are fast, deterministic and need no live Redis/Postgres.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from erp_core.channels import inbox_router as inbox_router_module
from erp_core.channels.inbox_router import SSE_HEARTBEAT_INTERVAL_SECONDS, stream_inbox_events
from erp_core.shared.enums import UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal

KEEPALIVE_FRAME = ": keepalive\n\n"


class FakePubSub:
    """Minimal `redis.asyncio.client.PubSub` stand-in."""

    def __init__(
        self, messages: list[dict[str, Any]] | None = None, idle_polls_before_message: int = 0
    ) -> None:
        self._messages: list[dict[str, Any]] = list(messages or [])
        self._idle_polls_before_message = idle_polls_before_message
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False
        self.poll_count = 0

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float = 1.0
    ) -> dict[str, Any] | None:
        self.poll_count += 1
        if self._idle_polls_before_message > 0:
            self._idle_polls_before_message -= 1
            return None
        if self._messages:
            return self._messages.pop(0)
        return None

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    def __init__(self, pubsub: FakePubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> FakePubSub:
        return self._pubsub


async def _never_disconnect_receive() -> dict[str, Any]:
    """`Request.is_disconnected()` probes this; report "still connected" forever."""
    return {"type": "http.request", "body": b"", "more_body": False}


def build_request(fake_redis: FakeRedis) -> Request:
    app = FastAPI()
    app.state.redis = fake_redis
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/inbox/stream",
        "raw_path": b"/api/v1/inbox/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 4321),
        "server": ("testserver", 80),
        "app": app,
    }
    return Request(scope, receive=_never_disconnect_receive)


def principal_for(tenant_id: Any) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=uuid4(),
        tenant_id=tenant_id,
        role=UserRole.OWNER,
        email="owner@wave6.test",
    )


async def read_chunk(iterator: AsyncIterator[str], timeout: float = 2.0) -> str:
    chunk = await asyncio.wait_for(anext(iterator), timeout=timeout)
    assert isinstance(chunk, str)
    return chunk


@pytest.mark.asyncio
async def test_sse_stream_emits_keepalive_while_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inbox_router_module, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    assert SSE_HEARTBEAT_INTERVAL_SECONDS > 0

    pubsub = FakePubSub()
    fake_redis = FakeRedis(pubsub)
    tenant_id = uuid4()

    response = await stream_inbox_events(
        request=build_request(fake_redis),
        principal=principal_for(tenant_id),
        max_events=None,
    )

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"

    iterator = response.body_iterator
    assert isinstance(iterator, AsyncIterator)

    chunks: list[str] = []
    try:
        chunks.append(await read_chunk(iterator))
        # First frame is the handshake, and the subscription is live by then.
        assert chunks[0].startswith("event: connected")
        assert pubsub.subscribed == [f"inbox:events:{tenant_id}"]

        # Idle stream: the very next frame must be the heartbeat comment.
        keepalive = await read_chunk(iterator)
        assert keepalive == KEEPALIVE_FRAME
        chunks.append(keepalive)

        # And it keeps beating while nothing is published.
        assert await read_chunk(iterator) == KEEPALIVE_FRAME
    finally:
        await iterator.aclose()  # type: ignore[attr-defined]

    # The generator's finally-block releases the subscription.
    assert pubsub.unsubscribed == [f"inbox:events:{tenant_id}"]
    assert pubsub.closed is True


@pytest.mark.asyncio
async def test_sse_keepalive_does_not_count_towards_max_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inbox_router_module, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    tenant_id = uuid4()
    event_payload = {"event": "conversation.updated", "conversation_id": str(uuid4())}
    pubsub = FakePubSub(
        messages=[{"type": "message", "data": json.dumps(event_payload).encode("utf-8")}],
        idle_polls_before_message=2,
    )

    response = await stream_inbox_events(
        request=build_request(FakeRedis(pubsub)),
        principal=principal_for(tenant_id),
        max_events=1,
    )

    iterator = response.body_iterator
    assert isinstance(iterator, AsyncIterator)
    frames: list[str] = []
    async with asyncio.timeout(5):
        async for frame in iterator:  # terminates on max_events=1, not on keepalives
            frames.append(frame)

    assert frames[0].startswith("event: connected")
    keepalive_index = frames.index(KEEPALIVE_FRAME)
    event_index = next(i for i, f in enumerate(frames) if "event: conversation.updated" in f)
    # Heartbeats landed before the real event and did not consume the budget.
    assert keepalive_index < event_index
    assert frames.count(KEEPALIVE_FRAME) >= 1
    assert event_index == len(frames) - 1
