"""Wave 3 P1/P2 regressions: worker topology, bounded streams, nginx webhook routing.

Covers:
* ``drain_commerce_and_tracking_once`` drains all six dedicated provider streams and
  survives a single failing channel.
* ``_handle_storefront_webhook_delivery`` acknowledges storefront receipts through the
  broker (migrator) session instead of leaving them queued forever.
* ``_run_commerce_and_tracking_loop`` never dies on a drain exception and honours the
  ``WORKER_ENABLE_PERIODIC_CRONS`` gate.
* ``serve`` runs the ingress loop and the commerce/tracking loop concurrently.
* ``accept_delivery`` bounds the provider stream with ``MAXLEN ~ 50_000``.
* ``infra/docker/nginx.edge.conf`` rate-limits the real ``/api/v1/webhooks/`` routes.
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest

import erp_core.channels.webhook_delivery as webhook_delivery
import erp_core.worker_main as worker_main
from erp_core.channels.ecommerce.shopify import process_shopify_webhook
from erp_core.orders.tracking_worker import process_17track_webhook

STOREFRONT_CHANNELS = ("woocommerce", "shopline", "shoplazza", "tiktok_shop")
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Fakes: the topology must be testable without PostgreSQL or Redis.
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def one_or_none(self) -> tuple[Any, ...] | None:
        return self._row


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self.row = row
        self.statements: list[tuple[Any, dict[str, Any] | None]] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        self.statements.append((statement, params))
        return _FakeResult(self.row)


class _FakeSessionFactory:
    """Callable standing in for ``async_sessionmaker``; records every session."""

    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self.row = row
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession(self.row)
        self.sessions.append(session)
        return session


class _RecordingRedis:
    def __init__(self) -> None:
        self.xadd_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def xadd(self, key: str, fields: dict[str, Any], **kwargs: Any) -> str:
        self.xadd_calls.append((key, fields, kwargs))
        return "1-0"


class _StubState:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _StubApp:
    def __init__(self, state: _StubState) -> None:
        self.state = state


class _StubRequest:
    def __init__(self, state: _StubState) -> None:
        self.app = _StubApp(state)


class _DisposableRedis:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


# ---------------------------------------------------------------------------
# 1. Commerce/tracking topology
# ---------------------------------------------------------------------------
@pytest.fixture
def recorded_drains(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_drain(
        broker_factory: Any,
        redis: Any,
        channel: str,
        process: Callable[[str], Awaitable[int]],
    ) -> int:
        calls.append(
            {"factory": broker_factory, "redis": redis, "channel": channel, "process": process}
        )
        return 2

    monkeypatch.setattr(worker_main, "drain_deliveries", fake_drain)
    return calls


@pytest.mark.asyncio
async def test_drain_commerce_and_tracking_once_covers_every_provider(
    recorded_drains: list[dict[str, Any]],
) -> None:
    factory = _FakeSessionFactory()
    redis = _RecordingRedis()

    counts = await worker_main.drain_commerce_and_tracking_once(
        cast(Any, factory), cast(Any, redis)
    )

    assert sorted(counts) == sorted(worker_main.COMMERCE_TRACKING_CHANNELS)
    assert counts == {channel: 2 for channel in worker_main.COMMERCE_TRACKING_CHANNELS}
    assert [call["channel"] for call in recorded_drains] == list(
        worker_main.COMMERCE_TRACKING_CHANNELS
    )
    handlers = {call["channel"]: call["process"] for call in recorded_drains}
    assert handlers["17track"] is process_17track_webhook
    assert handlers["shopify"] is process_shopify_webhook
    for channel in STOREFRONT_CHANNELS:
        handler = handlers[channel]
        assert isinstance(handler, functools.partial)
        assert handler.func is worker_main._handle_storefront_webhook_delivery
        assert handler.keywords["broker_session_factory"] is factory


@pytest.mark.asyncio
async def test_drain_commerce_and_tracking_once_isolates_a_failing_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def flaky_drain(
        broker_factory: Any,
        redis: Any,
        channel: str,
        process: Callable[[str], Awaitable[int]],
    ) -> int:
        if channel == "shopify":
            raise RuntimeError("redis stream unavailable")
        return 1

    monkeypatch.setattr(worker_main, "drain_deliveries", flaky_drain)

    counts = await worker_main.drain_commerce_and_tracking_once(
        cast(Any, _FakeSessionFactory()), cast(Any, _RecordingRedis())
    )

    assert counts["shopify"] == 0
    assert counts["17track"] == 1
    assert all(counts[channel] == 1 for channel in STOREFRONT_CHANNELS)


@pytest.mark.asyncio
async def test_storefront_handler_marks_receipt_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_set_state(session: Any, **kwargs: Any) -> None:
        recorded.append({"session": session, **kwargs})

    monkeypatch.setattr(worker_main, "set_raw_webhook_processing_state", fake_set_state)
    factory = _FakeSessionFactory(row=("woocommerce",))
    receipt_id = uuid.uuid4()

    handled = await worker_main._handle_storefront_webhook_delivery(
        str(receipt_id), broker_session_factory=cast(Any, factory)
    )

    assert handled == 1
    assert recorded == [
        {
            "session": factory.sessions[0],
            "raw_event_id": receipt_id,
            "status": "processed",
        }
    ]


@pytest.mark.asyncio
async def test_storefront_handler_ignores_unknown_or_malformed_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_set_state(session: Any, **kwargs: Any) -> None:
        recorded.append({"session": session, **kwargs})

    monkeypatch.setattr(worker_main, "set_raw_webhook_processing_state", fake_set_state)

    missing = await worker_main._handle_storefront_webhook_delivery(
        str(uuid.uuid4()), broker_session_factory=cast(Any, _FakeSessionFactory(row=None))
    )
    malformed = await worker_main._handle_storefront_webhook_delivery(
        "not-a-uuid", broker_session_factory=cast(Any, _FakeSessionFactory(row=("woo",)))
    )

    assert (missing, malformed) == (0, 0)
    assert recorded == []


# ---------------------------------------------------------------------------
# 2. Commerce/tracking loop resilience + cron gate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_loop_survives_drain_failures_and_runs_periodic_crons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    calls = {"drain": 0, "reconcile": 0, "stalled": 0}

    async def boom(factory: Any, redis: Any) -> dict[str, int]:
        calls["drain"] += 1
        if calls["drain"] >= 2:
            stop.set()
        raise RuntimeError("postgres unreachable")

    async def reconcile() -> dict[str, int]:
        calls["reconcile"] += 1
        return {"orders": 0}

    async def stalled() -> int:
        calls["stalled"] += 1
        return 0

    monkeypatch.setattr(worker_main, "drain_commerce_and_tracking_once", boom)
    monkeypatch.setattr(worker_main, "reconcile_ecommerce_orders_cron", reconcile)
    monkeypatch.setattr(worker_main, "monitor_stalled_shipments_cron", stalled)

    await asyncio.wait_for(
        worker_main._run_commerce_and_tracking_loop(
            stop,
            cast(Any, _FakeSessionFactory()),
            cast(Any, _RecordingRedis()),
            drain_interval=0.0,
            reconcile_interval=0.0,
            stalled_interval=0.0,
            periodic_crons=True,
        ),
        timeout=5.0,
    )

    assert calls["drain"] >= 2
    assert calls["reconcile"] >= 1
    assert calls["stalled"] >= 1


@pytest.mark.asyncio
async def test_loop_skips_periodic_crons_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    crons = {"reconcile": 0, "stalled": 0}

    async def drain(factory: Any, redis: Any) -> dict[str, int]:
        stop.set()
        return {channel: 0 for channel in worker_main.COMMERCE_TRACKING_CHANNELS}

    async def reconcile() -> dict[str, int]:
        crons["reconcile"] += 1
        return {}

    async def stalled() -> int:
        crons["stalled"] += 1
        return 0

    monkeypatch.setattr(worker_main, "drain_commerce_and_tracking_once", drain)
    monkeypatch.setattr(worker_main, "reconcile_ecommerce_orders_cron", reconcile)
    monkeypatch.setattr(worker_main, "monitor_stalled_shipments_cron", stalled)

    await asyncio.wait_for(
        worker_main._run_commerce_and_tracking_loop(
            stop,
            cast(Any, _FakeSessionFactory()),
            cast(Any, _RecordingRedis()),
            drain_interval=0.0,
            reconcile_interval=0.0,
            stalled_interval=0.0,
            periodic_crons=False,
        ),
        timeout=5.0,
    )

    assert crons == {"reconcile": 0, "stalled": 0}


def test_periodic_crons_env_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_ENABLE_PERIODIC_CRONS", raising=False)
    assert worker_main._periodic_crons_enabled() is True

    monkeypatch.setenv("WORKER_ENABLE_PERIODIC_CRONS", "false")
    assert worker_main._periodic_crons_enabled() is False

    monkeypatch.setenv("WORKER_ENABLE_PERIODIC_CRONS", "TRUE")
    assert worker_main._periodic_crons_enabled() is True


# ---------------------------------------------------------------------------
# 3. serve() runs ingress and commerce/tracking concurrently
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_serve_runs_both_worker_loops_until_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []
    redis = _DisposableRedis()
    runtime_engine = _DisposableEngine()
    broker_engine = _DisposableEngine()

    async def fake_ingress(
        session_factory: Any, redis_client: Any, stop: asyncio.Event, **kwargs: Any
    ) -> None:
        started.append("ingress")
        if len(started) == 2:
            stop.set()
        await stop.wait()

    async def fake_commerce(
        stop: asyncio.Event, broker_factory: Any, redis_client: Any, **kwargs: Any
    ) -> None:
        started.append("commerce")
        if len(started) == 2:
            stop.set()
        await stop.wait()

    monkeypatch.setattr(worker_main, "_redis", redis)
    monkeypatch.setattr(worker_main, "_runtime_engine", runtime_engine)
    monkeypatch.setattr(worker_main, "_broker_engine", broker_engine)
    monkeypatch.setattr(worker_main, "run_webhook_worker_loop", fake_ingress)
    monkeypatch.setattr(worker_main, "_run_commerce_and_tracking_loop", fake_commerce)

    await asyncio.wait_for(worker_main.serve(), timeout=5.0)

    assert sorted(started) == ["commerce", "ingress"]
    assert redis.closed is True
    assert runtime_engine.disposed is True
    assert broker_engine.disposed is True


# ---------------------------------------------------------------------------
# 4. Bounded Redis streams
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_accept_delivery_bounds_provider_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_id = uuid.uuid4()

    async def fake_accept_raw_webhook_event(session: Any, **kwargs: Any) -> uuid.UUID:
        return receipt_id

    monkeypatch.setattr(webhook_delivery, "accept_raw_webhook_event", fake_accept_raw_webhook_event)
    factory = _FakeSessionFactory()
    redis = _RecordingRedis()
    request = _StubRequest(_StubState(broker_session_factory=factory, redis=redis))

    accepted = await webhook_delivery.accept_delivery(
        cast(Any, request),
        channel="shopify",
        external_event_id="evt-1",
        payload={"id": 1},
    )

    assert accepted is True
    assert len(redis.xadd_calls) == 1
    key, fields, kwargs = redis.xadd_calls[0]
    assert key == "stream:webhooks:shopify"
    assert fields["raw_event_id"] == str(receipt_id)
    assert kwargs["maxlen"] == 50_000
    assert kwargs["approximate"] is True


# ---------------------------------------------------------------------------
# 5. Nginx webhook routing
# ---------------------------------------------------------------------------
def test_nginx_rate_limits_real_webhook_routes() -> None:
    conf = (REPO_ROOT / "infra/docker/nginx.edge.conf").read_text(encoding="utf-8")

    marker = "location ~ ^/api/v1/webhooks/ {"
    assert marker in conf
    assert "location ~ ^/webhooks/" not in conf

    block = conf.split(marker, 1)[1].split("location ", 1)[0]
    assert "limit_req zone=erp_webhook" in block
    assert "proxy_read_timeout 30s" in block
