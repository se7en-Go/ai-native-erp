"""Track 8: production observability + admin ops health + Stripe closed loop.

Three layers are covered:

1. `erp_core.shared.observability` is exercised as a pure unit: the Prometheus
   text exposition (counter, histogram, gauge), thread safety, request
   correlation, route-template cardinality control, and the "never buffer an
   SSE body" guarantee of the pure-ASGI middleware.
2. `GET /api/v1/ops/health` runs against the live transactional Pooler/PG pair
   exactly like production: tenant RLS session for the SQL counters, Redis
   `SCAN`/`XLEN` for stream backlog, and threshold-driven `ok|warn|critical`.
3. The Stripe closed loop is driven through the signed webhook adapter with
   replay, so idempotency (no double credit) and downgrade/overage behaviour are
   verified end to end instead of asserted on a mock.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.responses import PlainTextResponse
from test_auth_rbac import _register_owner

from erp_core.billing import wallet_service
from erp_core.billing.stripe_sync import (
    StripeMeterSyncService,
    interpret_stripe_event,
    verify_stripe_webhook_signature,
)
from erp_core.billing.wallet_service import CreditWalletService
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UsageMetricType
from erp_core.shared.errors import AppError
from erp_core.shared.observability import (
    PROMETHEUS_CONTENT_TYPE,
    ObservabilityMiddleware,
    ObservabilityRegistry,
    TelemetryConfig,
    dispatch_error_event,
    dispatch_trace_span,
    get_observability_registry,
    route_path_template,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_url() -> str:
    """Plan 01 FORCE RLS blocks `app_migrator` DML on tenant tables."""
    return os.environ.get(
        "DATABASE_ADMIN_URL",
        "postgresql+asyncpg://postgres:postgres_dev_pass@127.0.0.1:55432/erp_saas",
    )


def _sign(body: bytes, secret: str, *, stamp: int | None = None) -> dict[str, str]:
    signed_at = int(time.time()) if stamp is None else stamp
    digest = hmac.new(
        secret.encode(), str(signed_at).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {"Stripe-Signature": f"t={signed_at},v1={digest}"}


def _event(event_type: str, obj: dict[str, Any], event_id: str | None = None) -> bytes:
    return json.dumps(
        {
            "id": event_id or f"evt_{uuid4().hex}",
            "type": event_type,
            "data": {"object": obj},
        },
        sort_keys=True,
    ).encode()


async def _call_webhook(client: AsyncClient, body: bytes, secret: str) -> Any:
    return await client.post(
        "/api/v1/billing/stripe/webhook",
        content=body,
        headers=_sign(body, secret),
    )


# ---------------------------------------------------------------------------
# 1. Observability registry (pure unit)
# ---------------------------------------------------------------------------


def test_registry_renders_counter_histogram_and_gauge_in_prometheus_text() -> None:
    registry = ObservabilityRegistry()
    for duration in (0.004, 0.03, 0.2, 3.0):
        registry.observe_request(
            method="GET",
            path_template="/api/v1/orders/{order_id}",
            status=200,
            duration_seconds=duration,
        )
    registry.observe_request(
        method="POST", path_template="/api/v1/orders", status=500, duration_seconds=0.5
    )
    registry.set_stream_backlog("stream:webhooks:whatsapp_cloud", 42)

    rendered = registry.render()

    assert "# TYPE http_requests_total counter" in rendered
    assert (
        'http_requests_total{method="GET",path_template="/api/v1/orders/{order_id}",'
        'status="200"} 4' in rendered
    )
    assert (
        'http_requests_total{method="POST",path_template="/api/v1/orders",status="500"} 1'
        in rendered
    )
    assert "# TYPE http_request_duration_seconds histogram" in rendered
    # Buckets are cumulative: 0.004 -> le 0.01; +0.03 -> le 0.05; +0.2 -> le 0.25; +3 -> le 5.
    for le, expected in (
        ("0.01", 1),
        ("0.05", 2),
        ("0.1", 2),
        ("0.25", 3),
        ("0.5", 3),
        ("1", 3),
        ("2.5", 3),
        ("5", 4),
        ("+Inf", 4),
    ):
        assert (
            "http_request_duration_seconds_bucket"
            f'{{method="GET",path_template="/api/v1/orders/{{order_id}}",le="{le}"}} '
            f"{expected}" in rendered
        )
    assert (
        'http_request_duration_seconds_count{method="GET",'
        'path_template="/api/v1/orders/{order_id}"} 4' in rendered
    )
    assert (
        'http_request_duration_seconds_sum{method="GET",'
        'path_template="/api/v1/orders/{order_id}"} 3.234' in rendered
    )
    assert "# TYPE erp_stream_backlog_messages gauge" in rendered
    assert 'erp_stream_backlog_messages{stream="stream:webhooks:whatsapp_cloud"} 42' in rendered
    # The POST series is tracked separately from the GET template series.
    assert (
        'http_request_duration_seconds_bucket{method="POST",'
        'path_template="/api/v1/orders",le="0.5"} 1' in rendered
    )
    assert (
        'http_request_duration_seconds_bucket{method="POST",'
        'path_template="/api/v1/orders",le="0.25"} 0' in rendered
    )
    assert (
        'http_request_duration_seconds_bucket{method="POST",'
        'path_template="/api/v1/orders",le="+Inf"} 1' in rendered
    )


def test_registry_escapes_label_values_and_is_thread_safe() -> None:
    registry = ObservabilityRegistry()

    def worker() -> None:
        for _ in range(50):
            registry.observe_request(
                method="GET",
                path_template='/api/v1/weird"{path}\\tail',
                status=200,
                duration_seconds=0.02,
            )

    threads = [__import__("threading").Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rendered = registry.render()
    assert registry.request_count(
        method="GET", path_template='/api/v1/weird"{path}\\tail', status="200"
    ) == 400
    assert 'path_template="/api/v1/weird\\"{path}\\\\tail"' in rendered


def test_registry_reset_clears_every_series() -> None:
    registry = ObservabilityRegistry()
    registry.observe_request(
        method="GET", path_template="/healthz", status=200, duration_seconds=0.001
    )
    registry.set_stream_backlog("stream:erp:events", 7)
    registry.reset()
    rendered = registry.render()
    assert "http_requests_total{" not in rendered
    assert "erp_stream_backlog_messages{" not in rendered
    assert registry.stream_backlog("stream:erp:events") is None


def test_route_path_template_prefers_the_matched_route_and_collapses_identifiers() -> None:
    class _Route:
        path = "/api/v1/orders/{order_id}/items"

    matched: dict[str, Any] = {"path": "/api/v1/orders/8f14e45f/items", "route": _Route()}
    unmatched: dict[str, Any] = {
        "path": (
            "/api/v1/ghost/3f2504e0-4f89-11d3-9a0c-0305e82c3301/"
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9deadbeef/42"
        )
    }

    assert route_path_template(matched) == "/api/v1/orders/{order_id}/items"
    collapsed = route_path_template(unmatched)
    assert collapsed == "/api/v1/ghost/{id}/{id}/{id}"
    assert len(route_path_template({"path": "/" + "a/" * 400 + "b"})) <= 200


async def _drive_asgi(
    app: Callable[..., Awaitable[None]],
    *,
    path: str = "/metrics",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return sent


async def test_middleware_correlates_request_id_and_bounds_metric_cardinality() -> None:
    registry = ObservabilityRegistry()
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, registry=registry)

    @app.get("/api/v1/items/{item_id}")
    async def read_item(item_id: str) -> PlainTextResponse:
        return PlainTextResponse(item_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        traced = await client.get(
            "/api/v1/items/8f14e45f-ceea-467f-a8b5-bb6f2b0d2b1a",
            headers={"X-Request-ID": "edge.7f3a-ABC_1"},
        )
        assert traced.status_code == 200
        assert traced.headers["x-request-id"] == "edge.7f3a-ABC_1"
        assert registry.request_count(
            method="GET", path_template="/api/v1/items/{item_id}", status="200"
        ) == 1

        generated = await client.get(
            "/api/v1/items/8f14e45f-ceea-467f-a8b5-bb6f2b0d2b1a",
            headers={"X-Request-ID": "not a valid id!"},
        )
        request_id = generated.headers["x-request-id"]
        assert request_id != "not a valid id!"
        UUID(request_id)  # a fresh uuid4
        # Same route template, so the metric series stays at two observations.
        assert registry.request_count(
            method="GET", path_template="/api/v1/items/{item_id}", status="200"
        ) == 2
        missing_route = await client.get("/api/v1/ghost/8f14e45f-ceea-467f-a8b5-bb6f2b0d2b1a")
        assert missing_route.status_code == 404
        assert registry.request_count(
            method="GET", path_template="/api/v1/ghost/{id}", status="404"
        ) == 1


async def test_middleware_forwards_streaming_body_messages_without_buffering() -> None:
    registry = ObservabilityRegistry()
    delivered: list[dict[str, Any]] = []
    forwarded_before_return: list[bool] = []

    async def streaming_app(scope: Any, receive: Any, send: Any) -> None:
        assert scope["state"]["request_id"]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send({"type": "http.response.body", "body": b"data: one\n\n", "more_body": True})
        # The first chunk must already be on the wire while the app is still running.
        forwarded_before_return.append(len(delivered) >= 2)
        await send({"type": "http.response.body", "body": b"data: two\n\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    wrapped = ObservabilityMiddleware(streaming_app, registry=registry)

    async def send(message: dict[str, Any]) -> None:
        delivered.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/inbox/stream",
        "raw_path": b"/api/v1/inbox/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"x-request-id", b"sse-1")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    await wrapped(scope, receive, send)

    assert forwarded_before_return == [True]
    assert [m["type"] for m in delivered] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
        "http.response.body",
    ]
    start = delivered[0]
    assert (b"x-request-id", b"sse-1") in [tuple(pair) for pair in start["headers"]]
    assert registry.request_count(
        method="GET", path_template="/api/v1/inbox/stream", status="200"
    ) == 1


async def test_metrics_router_exposes_prometheus_text_on_the_module_registry() -> None:
    from erp_core.api_main import create_app

    registry = get_observability_registry()
    registry.reset()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/healthz")).status_code == 200
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == PROMETHEUS_CONTENT_TYPE
    body = response.text
    assert "# TYPE http_requests_total counter" in body
    assert 'path_template="/healthz"' in body


async def test_telemetry_hooks_are_pure_noops_when_unconfigured() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def deliverer(target: str, payload: dict[str, Any], headers: dict[str, str]) -> bool:
        calls.append((target, payload))
        return True

    unconfigured = TelemetryConfig()
    assert unconfigured.enabled is False
    assert (
        dispatch_error_event(
            config=unconfigured,
            message="boom",
            request_id="r-1",
            path="/api/v1/orders",
            method="GET",
            status_code=500,
            deliverer=deliverer,
        )
        is False
    )
    assert (
        dispatch_trace_span(
            config=unconfigured, name="ops.probe", attributes={"k": "v"}, deliverer=deliverer
        )
        is False
    )
    assert calls == []

    # A malformed DSN must be inert instead of raising inside request handling.
    broken = TelemetryConfig(sentry_dsn="not-a-dsn")
    assert dispatch_error_event(config=broken, message="boom", deliverer=deliverer) is False
    assert calls == []


async def test_telemetry_hooks_deliver_a_single_best_effort_event_when_configured() -> None:
    calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    received = asyncio.Event()

    async def deliverer(target: str, payload: dict[str, Any], headers: dict[str, str]) -> bool:
        calls.append((target, payload, headers))
        received.set()
        return True

    config = TelemetryConfig(sentry_dsn="https://public@sentry.example.com/42")
    scheduled = dispatch_error_event(
        config=config,
        message="RuntimeError: pool exhausted",
        request_id="req-42",
        path="/api/v1/orders",
        method="POST",
        status_code=500,
        deliverer=deliverer,
    )
    assert scheduled is True
    await asyncio.wait_for(received.wait(), timeout=2)

    target, payload, headers = calls[0]
    assert target == "https://sentry.example.com/api/42/store/"
    assert headers["X-Sentry-Auth"].startswith("Sentry sentry_version=7")
    assert payload["tags"]["request_id"] == "req-42"
    assert payload["extra"]["path"] == "/api/v1/orders"

    # Trace spans post to the OTLP HTTP exporter path.
    span_calls: list[str] = []

    span_delivered = asyncio.Event()

    async def span_deliverer(
        target: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> bool:
        span_calls.append(target)
        span_delivered.set()
        return True

    assert dispatch_trace_span(
        config=TelemetryConfig(otlp_endpoint="http://collector:4318/"),
        name="ops.health",
        attributes={"status": "ok"},
        deliverer=span_deliverer,
    )
    await asyncio.wait_for(span_delivered.wait(), timeout=2)
    assert span_calls == ["http://collector:4318/v1/traces"]


async def test_telemetry_delivery_failure_never_breaks_the_request() -> None:
    async def exploding(
        target: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> bool:
        raise RuntimeError("collector unreachable")

    config = TelemetryConfig(sentry_dsn="https://public@sentry.example.com/42")
    # Configured => queued (True), the exploding collector is swallowed in the
    # background task and never surfaces to the caller.
    assert dispatch_error_event(config=config, message="boom", deliverer=exploding) is True
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# 2. Admin ops health endpoint (live PostgreSQL + Redis)
# ---------------------------------------------------------------------------


async def test_ops_health_requires_owner_or_admin(
    async_client: AsyncClient,
) -> None:
    owner_token, _, _ = await _register_owner(async_client)
    agent_email = f"agent-{uuid4().hex[:6]}@factory.example.com"
    invited = await async_client.post(
        "/api/v1/tenants/memberships",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "email": agent_email,
            "full_name": "Agent Ops",
            "password": "RolePassword2026!",
            "role": "agent",
        },
    )
    assert invited.status_code == 201, invited.text
    login = await async_client.post(
        "/api/v1/auth/login",
        json={"email": agent_email, "password": "RolePassword2026!"},
    )
    assert login.status_code == 200, login.text
    agent_token = login.json()["access_token"]

    denied = await async_client.get(
        "/api/v1/ops/health", headers={"Authorization": f"Bearer {agent_token}"}
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "ERR_INSUFFICIENT_ROLE"

    anonymous = await async_client.get("/api/v1/ops/health")
    assert anonymous.status_code == 401

    allowed = await async_client.get(
        "/api/v1/ops/health", headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert allowed.status_code == 200, allowed.text


async def test_ops_health_reports_clean_tenant_as_ok(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _, _ = await _register_owner(async_client)
    settings = get_settings()
    monkeypatch.setattr(settings, "OPS_QUEUE_WARN_DEPTH", 500)
    monkeypatch.setattr(settings, "OPS_QUEUE_CRIT_DEPTH", 5000)

    response = await async_client.get(
        "/api/v1/ops/health", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["alerts"] == []
    assert body["queue_warn_depth"] == 500 and body["queue_critical_depth"] == 5000
    assert body["postgres"]["status"] == "ok"
    assert body["postgres"]["latency_ms"] >= 0
    assert body["postgres"]["alembic_version"]
    assert body["postgres"]["raw_webhook_status_counts"] == {}
    assert body["postgres"]["notification_deliveries_pending"] == 0
    assert body["postgres"]["inventory_sync_jobs_pending"] == 0
    assert body["redis"]["status"] == "ok"
    assert body["redis"]["used_memory_bytes"] is None or body["redis"]["used_memory_bytes"] >= 0
    assert isinstance(body["streams"], list)
    assert body["max_backlog"] >= 0


async def test_ops_health_counts_webhook_and_job_backlog_from_sql(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _, raw_tid = await _register_owner(async_client)
    tid = UUID(raw_tid)
    monkeypatch.setattr(get_settings(), "OPS_QUEUE_WARN_DEPTH", 100)
    monkeypatch.setattr(get_settings(), "OPS_QUEUE_CRIT_DEPTH", 1000)

    admin_engine = create_async_engine(_admin_url(), future=True)
    try:
        async with admin_engine.begin() as conn:
            for status in ("received", "queued", "processed", "dead_letter"):
                await conn.execute(
                    text(
                        "INSERT INTO raw_webhook_events "
                        "(tenant_id, channel_type, external_event_id, raw_payload, "
                        "processing_status) VALUES "
                        "(:tid, 'telegram', :eid, '{}'::jsonb, :status)"
                    ),
                    {"tid": tid, "eid": f"ops-{status}-{uuid4().hex}", "status": status},
                )
    finally:
        await admin_engine.dispose()

    response = await async_client.get(
        "/api/v1/ops/health", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    counts = body["postgres"]["raw_webhook_status_counts"]
    assert counts["received"] == 1
    assert counts["queued"] == 1
    assert counts["processed"] == 1
    assert counts["dead_letter"] == 1
    assert body["status"] == "warn"
    assert any("dead" in alert.lower() for alert in body["alerts"])

    # Tenant isolation: another tenant must never see this tenant's counters.
    other_token, _, _ = await _register_owner(async_client)
    other = await async_client.get(
        "/api/v1/ops/health", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert other.status_code == 200, other.text
    assert other.json()["postgres"]["raw_webhook_status_counts"] == {}

    async with tenant_session(runtime_session_factory, tid) as session:
        assert await session.scalar(
            text("SELECT count(*) FROM raw_webhook_events WHERE tenant_id = :tid"),
            {"tid": tid},
        ) == 4


async def test_ops_health_escalates_stream_backlog_and_updates_the_gauge(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _, _ = await _register_owner(async_client)
    monkeypatch.setattr(get_settings(), "OPS_QUEUE_WARN_DEPTH", 2)
    monkeypatch.setattr(get_settings(), "OPS_QUEUE_CRIT_DEPTH", 4)

    stream = f"stream:webhooks:ops_probe_{uuid4().hex[:8]}"

    from erp_core.shared.redis_client import create_redis_client

    client = create_redis_client(get_settings())
    try:
        # The ops probe reports the *global* stream backlog, so leftovers from
        # earlier suites would mask the two thresholds under test. This is the
        # dedicated test Redis; drop only stream keys, never the whole DB.
        async for key in client.scan_iter(match="*stream*"):
            await client.delete(key)
        for _ in range(3):
            await client.xadd(stream, {"probe": "1"})
        warn = await async_client.get(
            "/api/v1/ops/health", headers={"Authorization": f"Bearer {token}"}
        )
        assert warn.status_code == 200, warn.text
        warn_body = warn.json()
        assert warn_body["status"] == "warn"
        assert any(stream in alert for alert in warn_body["alerts"])
        probe = next(item for item in warn_body["streams"] if item["stream"] == stream)
        assert probe["length"] == 3
        assert get_observability_registry().stream_backlog(stream) == 3

        for _ in range(3):
            await client.xadd(stream, {"probe": "1"})
        critical = await async_client.get(
            "/api/v1/ops/health", headers={"Authorization": f"Bearer {token}"}
        )
        assert critical.status_code == 200, critical.text
        critical_body = critical.json()
        assert critical_body["status"] == "critical"
        assert critical_body["max_backlog"] >= 6
        assert get_observability_registry().stream_backlog(stream) == 6
    finally:
        await client.delete(stream)
        await client.aclose()


def test_compute_ops_status_is_threshold_driven() -> None:
    from erp_core.tenancy.ops_router import compute_ops_status

    status, alerts = compute_ops_status(
        postgres_ok=True,
        redis_ok=True,
        max_backlog=0,
        failed_events=0,
        warn_depth=10,
        critical_depth=100,
    )
    assert (status, alerts) == ("ok", [])

    status, alerts = compute_ops_status(
        postgres_ok=True,
        redis_ok=True,
        max_backlog=10,
        failed_events=0,
        warn_depth=10,
        critical_depth=100,
    )
    assert status == "warn" and alerts

    status, _ = compute_ops_status(
        postgres_ok=True,
        redis_ok=True,
        max_backlog=100,
        failed_events=0,
        warn_depth=10,
        critical_depth=100,
    )
    assert status == "critical"

    status, alerts = compute_ops_status(
        postgres_ok=False,
        redis_ok=True,
        max_backlog=0,
        failed_events=0,
        warn_depth=10,
        critical_depth=100,
    )
    assert status == "critical"
    assert any("postgres" in alert.lower() for alert in alerts)


async def test_ops_probe_degrades_instead_of_raising_when_postgres_is_down() -> None:
    from types import SimpleNamespace

    from sqlalchemy.exc import SQLAlchemyError

    from erp_core.tenancy.ops_router import _probe_postgres

    class _FailingSavepoint:
        async def __aenter__(self) -> None:
            raise SQLAlchemyError("connection refused")

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _BrokenSession:
        def begin_nested(self) -> _FailingSavepoint:
            return _FailingSavepoint()

        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("unreachable: the probe must stop after SELECT 1 fails")

        async def scalar(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("unreachable")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    probe = await _probe_postgres(request, _BrokenSession(), uuid4())

    assert probe.status == "critical"
    assert probe.latency_ms >= 0
    assert probe.raw_webhook_status_counts == {}
    assert probe.alembic_version is None


async def test_ops_probe_keeps_partial_results_when_a_secondary_query_fails() -> None:
    from types import SimpleNamespace

    from sqlalchemy.exc import SQLAlchemyError

    from erp_core.tenancy.ops_router import _probe_postgres

    class _Savepoint:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _HalfBrokenSession:
        """`SELECT 1` works; every aggregate/version query is denied by the pooler."""

        def begin_nested(self) -> _Savepoint:
            return _Savepoint()

        async def execute(self, statement: Any, params: Any = None) -> Any:
            sql = str(statement)
            if sql.strip() == "SELECT 1":
                return None
            raise SQLAlchemyError("permission denied for table")

        async def scalar(self, statement: Any, params: Any = None) -> Any:
            raise SQLAlchemyError("permission denied for table")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    probe = await _probe_postgres(request, _HalfBrokenSession(), uuid4())

    assert probe.status == "ok"
    assert probe.latency_ms >= 0
    assert probe.raw_webhook_status_counts == {}
    assert probe.notification_deliveries_pending == 0
    assert probe.inventory_sync_jobs_pending == 0
    assert probe.dead_letter_events == 0
    assert probe.alembic_version is None


# ---------------------------------------------------------------------------
# 3. Stripe closed loop
# ---------------------------------------------------------------------------


def test_signature_verification_rejects_expired_and_forged_headers() -> None:
    secret = "whsec_ops_test"
    body = b'{"type":"invoice.paid"}'
    assert verify_stripe_webhook_signature(body, _sign(body, secret)["Stripe-Signature"], secret)
    expired = _sign(body, secret, stamp=int(time.time()) - 301)["Stripe-Signature"]
    assert not verify_stripe_webhook_signature(body, expired, secret)
    forged = _sign(body, "whsec_other")["Stripe-Signature"]
    assert not verify_stripe_webhook_signature(body, forged, secret)
    assert not verify_stripe_webhook_signature(body, "", secret)


def test_interpret_stripe_event_maps_the_closed_loop_events() -> None:
    tenant_id = str(uuid4())
    paid = interpret_stripe_event(
        {
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_ops_paid_1",
                    "status": "paid",
                    "currency": "usd",
                    "amount_paid": 250,
                    "metadata": {
                        "tenant_id": tenant_id,
                        "erp_credit_topup": "1",
                        "credits": "250",
                    },
                }
            },
        }
    )
    assert paid is not None
    assert (paid.kind, str(paid.tenant_id), paid.reference, paid.credits) == (
        "topup",
        tenant_id,
        "in_ops_paid_1",
        250,
    )

    failed = interpret_stripe_event(
        {
            "type": "invoice.payment_failed",
            "data": {"object": {"id": "in_ops_failed_1", "metadata": {"tenant_id": tenant_id}}},
        }
    )
    assert failed is not None
    assert (failed.kind, failed.reference, failed.reason) == (
        "downgrade",
        "in_ops_failed_1",
        "invoice_payment_failed",
    )

    deleted = interpret_stripe_event(
        {
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_ops_1", "metadata": {"tenant_id": tenant_id}}},
        }
    )
    assert deleted is not None
    assert (deleted.kind, deleted.reference, deleted.reason) == (
        "downgrade",
        "sub_ops_1",
        "subscription_deleted",
    )

    # Events we cannot map to a tenant are ignored (200, no side effect).
    assert (
        interpret_stripe_event(
            {"type": "invoice.paid", "data": {"object": {"id": "in_x", "metadata": {}}}}
        )
        is None
    )
    assert interpret_stripe_event({"type": "charge.refunded", "data": {"object": {}}}) is None

    with pytest.raises(ValueError):
        interpret_stripe_event(
            {
                "type": "invoice.paid",
                "data": {
                    "object": {
                        "id": "in_bad",
                        "status": "paid",
                        "currency": "usd",
                        "amount_paid": 999,
                        "metadata": {
                            "tenant_id": tenant_id,
                            "erp_credit_topup": "1",
                            "credits": "250",
                        },
                    }
                },
            }
        )


async def test_invoice_paid_credits_the_wallet_once_across_replays(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _, raw_tid = await _register_owner(async_client)
    tid = UUID(raw_tid)
    secret = "whsec_ops_topup"
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", secret)
    wallet = CreditWalletService()
    async with tenant_session(runtime_session_factory, tid) as session:
        await wallet.ensure_wallet(session, tenant_id=tid, included_credits=0, plan_tier="starter")

    body = _event(
        "invoice.paid",
        {
            "id": "in_ops_renewal_1",
            "status": "paid",
            "currency": "usd",
            "amount_paid": 120,
            "metadata": {"tenant_id": raw_tid, "erp_credit_topup": "1", "credits": "120"},
        },
    )

    statuses = []
    for _ in range(3):
        response = await _call_webhook(async_client, body, secret)
        assert response.status_code == 200, response.text
        statuses.append(response.json()["status"])
    assert statuses == ["credited", "already_credited", "already_credited"]

    headers = {"Authorization": f"Bearer {token}"}
    wallet_body = (await async_client.get("/api/v1/billing/wallet", headers=headers)).json()
    assert wallet_body["topup_credits_balance"] == 120
    usage = (await async_client.get("/api/v1/billing/usage", headers=headers)).json()
    # Funding rows are ledger entries but never reported as AI consumption.
    assert usage["total"] == 0

    async with tenant_session(runtime_session_factory, tid) as session:
        funding_rows = await session.scalar(
            text(
                "SELECT count(*) FROM usage_ledger "
                "WHERE tenant_id = :tid AND idempotency_key = 'funding:stripe:in_ops_renewal_1'"
            ),
            {"tid": tid},
        )
        assert funding_rows == 1


async def test_invoice_payment_failed_downgrades_and_trips_l0_once(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _, raw_tid = await _register_owner(async_client)
    tid = UUID(raw_tid)
    secret = "whsec_ops_downgrade"
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", secret)
    wallet = CreditWalletService()
    async with tenant_session(runtime_session_factory, tid) as session:
        await wallet.ensure_wallet(session, tenant_id=tid, plan_tier="growth")
        await session.execute(
            text(
                "UPDATE credit_wallets SET overage_spend_cap_cents = 50000, "
                "auto_downgrade_to_l0 = false WHERE tenant_id = :tid"
            ),
            {"tid": tid},
        )

    body = _event(
        "invoice.payment_failed",
        {"id": "in_ops_failed_1", "metadata": {"tenant_id": raw_tid}},
    )
    first = await _call_webhook(async_client, body, secret)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "downgraded"
    second = await _call_webhook(async_client, body, secret)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "already_downgraded"

    wallet_body = (
        await async_client.get(
            "/api/v1/billing/wallet", headers={"Authorization": f"Bearer {token}"}
        )
    ).json()
    assert wallet_body["plan_tier"] == "starter"
    assert wallet_body["overage_spend_cap_cents"] == 0
    assert wallet_body["auto_downgrade_to_l0"] is True

    async with tenant_session(runtime_session_factory, tid) as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM usage_ledger WHERE tenant_id = :tid "
                    "AND idempotency_key = 'billing:event:in_ops_failed_1'"
                ),
                {"tid": tid},
            )
            == 1
        )
        assert (
            await session.scalar(
                text(
                    "SELECT model_or_provider FROM usage_ledger WHERE tenant_id = :tid "
                    "AND idempotency_key = 'billing:event:in_ops_failed_1'"
                ),
                {"tid": tid},
            )
            == "stripe_downgrade"
        )


async def test_subscription_deleted_downgrades_and_unmapped_events_are_ignored(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _, raw_tid = await _register_owner(async_client)
    tid = UUID(raw_tid)
    secret = "whsec_ops_sub_deleted"
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", secret)
    async with tenant_session(runtime_session_factory, tid) as session:
        await CreditWalletService().ensure_wallet(session, tenant_id=tid, plan_tier="scale")

    deleted = _event(
        "customer.subscription.deleted",
        {"id": "sub_ops_deleted_1", "metadata": {"tenant_id": raw_tid}},
    )
    response = await _call_webhook(async_client, deleted, secret)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "downgraded"
    wallet_body = (
        await async_client.get(
            "/api/v1/billing/wallet", headers={"Authorization": f"Bearer {token}"}
        )
    ).json()
    assert wallet_body["plan_tier"] == "starter"

    unmapped = _event("customer.subscription.deleted", {"id": "sub_ops_other", "metadata": {}})
    ignored = await _call_webhook(async_client, unmapped, secret)
    assert ignored.status_code == 200
    assert ignored.json()["status"] == "ignored"

    unsigned = await async_client.post(
        "/api/v1/billing/stripe/webhook",
        content=deleted,
        headers={"Stripe-Signature": "t=1,v1=" + "0" * 64},
    )
    assert unsigned.status_code == 401
    assert unsigned.json()["code"] == "ERR_INVALID_STRIPE_SIGNATURE"


async def test_exhausted_wallet_blocks_ai_spend_and_a_topup_reenables_it(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _, raw_tid = await _register_owner(async_client)
    tid = UUID(raw_tid)
    headers = {"Authorization": f"Bearer {token}"}
    secret = "whsec_ops_exhausted"
    monkeypatch.setattr(get_settings(), "STRIPE_WEBHOOK_SECRET", secret)
    service = wallet_service.CreditWalletService()
    async with tenant_session(runtime_session_factory, tid) as session:
        await service.ensure_wallet(session, tenant_id=tid, included_credits=0)
        allowed, level = await service.check_ai_execution_allowed(session, tid)
        assert allowed is False and level is not None
        with pytest.raises(AppError) as depleted:
            await service.record_usage(
                session,
                tid,
                f"exhausted-{uuid4().hex}",
                UsageMetricType.AI_CREDIT,
                Decimal("1"),
                Decimal("0.01"),
                "test-meter",
            )
        assert depleted.value.status_code == 402
        assert depleted.value.code == "ERR_CREDITS_DEPLETED"

    chat = await async_client.post(
        "/api/v1/ai/assistant/chat",
        headers=headers,
        json={"capability": "content_studio", "message": "Draft a follow-up"},
    )
    assert chat.status_code == 200, chat.text
    assert chat.json()["degraded_to_l0"] is True
    assert chat.json()["auto_executed"] is False

    funded = _event(
        "payment_intent.succeeded",
        {
            "id": f"pi_{uuid4().hex}",
            "status": "succeeded",
            "currency": "usd",
            "amount_received": 300,
            "metadata": {"tenant_id": raw_tid, "erp_credit_topup": "1", "credits": "300"},
        },
    )
    assert (await _call_webhook(async_client, funded, secret)).status_code == 200

    async with tenant_session(runtime_session_factory, tid) as session:
        await service.record_usage(
            session,
            tid,
            f"funded-{uuid4().hex}",
            UsageMetricType.AI_CREDIT,
            Decimal("2"),
            Decimal("0.02"),
            "test-meter",
        )
        wallet = await service.get_wallet(session, tid)
        assert wallet.topup_credits_balance == 298


async def test_downgrade_writes_are_rejected_without_a_valid_reference(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, raw_tid = await _register_owner(async_client)
    tid = UUID(raw_tid)
    async with tenant_session(runtime_session_factory, tid) as session:
        with pytest.raises(AppError) as invalid:
            await CreditWalletService().apply_billing_downgrade(
                session, tid, "", reason="invoice_payment_failed"
            )
        assert invalid.value.status_code == 422
        with pytest.raises(AppError):
            await CreditWalletService().apply_stripe_topup(session, tid, "pi_zero", 0)
    assert StripeMeterSyncService().handle_billing_event is not None
