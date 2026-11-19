from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from erp_core.api_main import create_app
from erp_core.channels.ingress_worker import run_webhook_worker_loop
from erp_core.shared.db import tenant_session


def _admin_url() -> str:
    return "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas"


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _message_payload(wamid: str, phone_number_id: str, body: str) -> bytes:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": phone_number_id},
                                "messages": [
                                    {
                                        "from": "14155552671",
                                        "id": wamid,
                                        "timestamp": "1763500000",
                                        "text": {"body": body},
                                        "type": "text",
                                    }
                                ],
                            },
                        }
                    ]
                }
            ]
        }
    ).encode()


class _BoomRedis:
    """Real Redis client whose XADD fails once the ingress has committed."""

    def __init__(self, inner: aioredis.Redis) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def xadd(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        raise ConnectionError("redis unavailable after commit")


async def _wait_until(predicate: Callable[[], Awaitable[bool]], timeout: float = 8.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not met before timeout")


@pytest.mark.asyncio
async def test_production_app_rejects_invalid_signature_with_401(monkeypatch) -> None:
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test_whatsapp_app_secret")
    app = create_app()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        missing = await client.post(
            "/api/v1/webhooks/whatsapp",
            content=b'{"entry":[]}',
            headers={"Content-Type": "application/json"},
        )
        invalid = await client.post(
            "/api/v1/webhooks/whatsapp",
            content=b'{"entry":[]}',
            headers={"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"},
        )
    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert app.dependency_overrides == {}


@pytest.mark.asyncio
async def test_production_worker_storm_reclaim_compensation_and_dlq(monkeypatch) -> None:
    secret = "test_whatsapp_app_secret"
    monkeypatch.setenv("WHATSAPP_APP_SECRET", secret)
    monkeypatch.setenv("WEBHOOK_DEFER_DELAY_SECONDS", "0")
    app = create_app()
    admin = create_async_engine(_admin_url(), future=True)
    phone_number_id = f"pn_{uuid.uuid4().hex[:10]}"
    account_id = uuid.uuid4()
    tenant_id: uuid.UUID | None = None
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        registered = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"wh-{uuid.uuid4().hex[:8]}@factory.example.com",
                "password": "StrongPassword2026!",
                "full_name": "Webhook Owner",
                "tenant_name": "Webhook Factory",
                "tenant_slug": f"wh-{uuid.uuid4().hex[:8]}",
            },
        )
        assert registered.status_code == 201, registered.text
        token = registered.json()["access_token"]
        tenant_id = uuid.UUID(registered.json()["principal"]["tenant_id"])
        async with admin.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status) "
                    "VALUES (:id, :tid, 'whatsapp_cloud', :ext, 'WA', 'active')"
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

        session_factory = app.state.session_factory
        real_redis = app.state.redis

        async def raw_count(event_id: str) -> int:
            async with admin.connect() as conn:
                return int(
                    (
                        await conn.execute(
                            text(
                                "SELECT count(*) FROM raw_webhook_events "
                                "WHERE external_event_id = :eid"
                            ),
                            {"eid": event_id},
                        )
                    ).scalar_one()
                )

        async def message_count(event_id: str) -> int:
            assert tenant_id is not None
            async with tenant_session(session_factory, tenant_id) as sess:
                return int(
                    (
                        await sess.execute(
                            text(
                                "SELECT count(*) FROM messages "
                                "WHERE tenant_id = :tid AND external_message_id = :eid"
                            ),
                            {"tid": tenant_id, "eid": event_id},
                        )
                    ).scalar_one()
                )

        comp_id = f"wamid.comp.{uuid.uuid4().hex[:8]}"
        comp_body = _message_payload(comp_id, phone_number_id, "compensate")
        app.state.redis = _BoomRedis(real_redis)
        failed = await client.post(
            "/api/v1/webhooks/whatsapp",
            content=comp_body,
            headers={
                "X-Hub-Signature-256": _sign(secret, comp_body),
                "Content-Type": "application/json",
            },
        )
        assert failed.status_code == 503
        assert await raw_count(comp_id) == 1
        assert await message_count(comp_id) == 0
        app.state.redis = real_redis

        retry_id = f"wamid.retry.{uuid.uuid4().hex[:8]}"
        retry_body = _message_payload(retry_id, phone_number_id, "retry")
        app.state.redis = _BoomRedis(real_redis)
        first = await client.post(
            "/api/v1/webhooks/whatsapp",
            content=retry_body,
            headers={
                "X-Hub-Signature-256": _sign(secret, retry_body),
                "Content-Type": "application/json",
            },
        )
        assert first.status_code == 503
        app.state.redis = real_redis
        second = await client.post(
            "/api/v1/webhooks/whatsapp",
            content=retry_body,
            headers={
                "X-Hub-Signature-256": _sign(secret, retry_body),
                "Content-Type": "application/json",
            },
        )
        assert second.status_code == 200
        assert second.json()["deduplicated"] is False
        assert await raw_count(retry_id) == 1

        reclaim_id = f"wamid.reclaim.{uuid.uuid4().hex[:8]}"
        reclaim_body = _message_payload(reclaim_id, phone_number_id, "reclaim")
        queued = await client.post(
            "/api/v1/webhooks/whatsapp",
            content=reclaim_body,
            headers={
                "X-Hub-Signature-256": _sign(secret, reclaim_body),
                "Content-Type": "application/json",
            },
        )
        assert queued.status_code == 200
        try:
            await real_redis.xgroup_create(
                "stream:webhooks:whatsapp_cloud",
                "erp_webhook_workers",
                id="0",
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        pending = await real_redis.xreadgroup(
            groupname="erp_webhook_workers",
            consumername="crasher",
            streams={"stream:webhooks:whatsapp_cloud": ">"},
            count=20,
        )
        assert pending
        crasher_ids = [item[0] for _stream, items in pending for item in items]
        assert crasher_ids

        stop = asyncio.Event()
        worker = asyncio.create_task(
            run_webhook_worker_loop(
                session_factory,
                real_redis,
                stop,
                min_idle_ms=0,
                poll_interval=0.02,
                broker_session_factory=app.state.broker_session_factory,
            )
        )
        try:
            await _wait_until(lambda: _eq(message_count(comp_id), 1))
            await _wait_until(lambda: _eq(message_count(retry_id), 1))
            await _wait_until(lambda: _eq(message_count(reclaim_id), 1))
            assert await raw_count(comp_id) == 1
            assert await raw_count(retry_id) == 1
            assert await raw_count(reclaim_id) == 1

            latency_id = f"wamid.latency.{uuid.uuid4().hex[:8]}"
            latency_body = _message_payload(latency_id, phone_number_id, "latency")
            started = time.perf_counter()
            latency = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=latency_body,
                headers={
                    "X-Hub-Signature-256": _sign(secret, latency_body),
                    "Content-Type": "application/json",
                },
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            assert latency.status_code == 200
            assert elapsed_ms < 100.0

            storm_id = f"wamid.storm.{uuid.uuid4().hex[:8]}"
            storm_body = _message_payload(storm_id, phone_number_id, "storm")
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/v1/webhooks/whatsapp",
                        content=storm_body,
                        headers={
                            "X-Hub-Signature-256": _sign(secret, storm_body),
                            "Content-Type": "application/json",
                        },
                    )
                    for _ in range(100)
                ]
            )
            assert all(item.status_code == 200 for item in responses)
            await _wait_until(lambda: _eq(message_count(storm_id), 1))
            assert await raw_count(storm_id) == 1
            assert await message_count(storm_id) == 1
            assert await message_count(latency_id) == 1

            orphan_id = f"wamid.missing.{uuid.uuid4().hex[:8]}"
            orphan_body = json.dumps(
                {
                    "entry": [
                        {
                            "changes": [
                                {
                                    "field": "messages",
                                    "value": {
                                        "metadata": {"phone_number_id": phone_number_id},
                                        "statuses": [
                                            {
                                                "id": orphan_id,
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
            ).encode()
            orphan = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=orphan_body,
                headers={
                    "X-Hub-Signature-256": _sign(secret, orphan_body),
                    "Content-Type": "application/json",
                },
            )
            assert orphan.status_code == 200

            async def dlq_id() -> uuid.UUID | None:
                assert tenant_id is not None
                async with tenant_session(session_factory, tenant_id) as sess:
                    found = (
                        await sess.execute(
                            text(
                                "SELECT id FROM dead_letter_events "
                                "WHERE tenant_id = :tid AND external_event_id LIKE :pat "
                                "ORDER BY created_at DESC LIMIT 1"
                            ),
                            {"tid": tenant_id, "pat": f"%{orphan_id}%"},
                        )
                    ).scalar_one_or_none()
                return None if found is None else uuid.UUID(str(found))

            await _wait_until(lambda: _present(dlq_id()), timeout=12.0)
            event_id = await dlq_id()
            assert event_id is not None
            denied = await client.post(f"/api/v1/channels/dlq/{event_id}/replay")
            assert denied.status_code == 401

            customer_id = uuid.uuid4()
            conv_id = uuid.uuid4()
            async with admin.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO customers (id, tenant_id, display_name) "
                        "VALUES (:id, :tid, 'Replay Buyer')"
                    ),
                    {"id": customer_id, "tid": tenant_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO conversations ("
                        "id, tenant_id, customer_id, channel_account_id, channel_type, status"
                        ") VALUES (:id, :tid, :cid, :aid, 'whatsapp_cloud', 'open')"
                    ),
                    {"id": conv_id, "tid": tenant_id, "cid": customer_id, "aid": account_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO messages ("
                        "id, tenant_id, conversation_id, customer_id, channel_type, "
                        "external_message_id, direction, sender_type, content_type, "
                        "body_text, delivery_status, source_timestamp"
                        ") VALUES ("
                        ":id, :tid, :conv, :cid, 'whatsapp_cloud', :eid, "
                        "'inbound', 'customer', 'text', 'parent', 'queued', now()"
                        ")"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": tenant_id,
                        "conv": conv_id,
                        "cid": customer_id,
                        "eid": orphan_id,
                    },
                )
            replayed = await client.post(
                f"/api/v1/channels/dlq/{event_id}/replay",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert replayed.status_code == 200
            assert replayed.json()["status"] == "replayed"
            assert replayed.json()["outcome"] == "queued"

            async def delivered() -> bool:
                assert tenant_id is not None
                async with tenant_session(session_factory, tenant_id) as sess:
                    status = (
                        await sess.execute(
                            text(
                                "SELECT delivery_status FROM messages "
                                "WHERE tenant_id = :tid AND external_message_id = :eid"
                            ),
                            {"tid": tenant_id, "eid": orphan_id},
                        )
                    ).scalar_one()
                return str(status) == "delivered"

            await _wait_until(delivered)
        finally:
            stop.set()
            await worker
            async with admin.begin() as conn:
                assert tenant_id is not None
                await conn.execute(
                    text("DELETE FROM dead_letter_events WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM raw_webhook_events WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM messages WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM conversations WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM customer_identities WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM customers WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM channel_routing_directory WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM channel_accounts WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
            await admin.dispose()


def _runtime_url() -> str:
    return os.environ["DATABASE_RUNTIME_URL"]


@pytest.mark.asyncio
async def test_runtime_cannot_use_broker_functions_across_tenants() -> None:
    admin = create_async_engine(_admin_url(), future=True)
    runtime = create_async_engine(_runtime_url(), future=True)
    runtime_factory = async_sessionmaker(runtime, expire_on_commit=False)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    raw_id = uuid.uuid4()
    account_id = uuid.uuid4()
    external_id = f"pn_{account_a_hex(account_id)}"
    try:
        async with admin.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) VALUES "
                    "(:ta, :sa, 'Victim'), (:tb, :sb, 'Attacker')"
                ),
                {
                    "ta": tenant_a,
                    "sa": f"vic-{tenant_a.hex[:8]}",
                    "tb": tenant_b,
                    "sb": f"atk-{tenant_b.hex[:8]}",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_routing_directory "
                    "(channel_type, external_account_id, tenant_id, channel_account_id) "
                    "VALUES ('whatsapp_cloud', :ext, :tid, :aid)"
                ),
                {"ext": external_id, "tid": tenant_a, "aid": account_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO raw_webhook_events ("
                    "id, tenant_id, channel_type, external_event_id, raw_payload, "
                    "processing_status"
                    ") VALUES ("
                    ":id, :tid, 'whatsapp_cloud', :eid, CAST(:payload AS jsonb), 'received'"
                    ")"
                ),
                {
                    "id": raw_id,
                    "tid": tenant_a,
                    "eid": f"evt-{raw_id.hex}",
                    "payload": '{"secret":"victim-pii"}',
                },
            )

        async with tenant_session(runtime_factory, tenant_b) as attack:
            await attack.execute(text("SELECT set_config('app.webhook_broker', '1', true)"))
            hidden = (
                await attack.execute(
                    text("SELECT raw_payload->>'secret' FROM raw_webhook_events WHERE id = :id"),
                    {"id": raw_id},
                )
            ).scalar_one_or_none()
            assert hidden is None

        async def denied(statement: str, params: dict[str, object]) -> None:
            with pytest.raises(DBAPIError):
                async with tenant_session(runtime_factory, tenant_b) as attack:
                    await attack.execute(
                        text("SELECT set_config('app.webhook_broker', '1', true)")
                    )
                    await attack.execute(text(statement), params)

        await denied(
            "SELECT raw_payload->>'secret' FROM claim_raw_webhook_event(CAST(:id AS uuid))",
            {"id": raw_id},
        )
        await denied(
            "SELECT set_raw_webhook_processing_state("
            "CAST(:id AS uuid), 'processed', CAST(:tid AS uuid), 3, 'hijack')",
            {"id": raw_id, "tid": tenant_b},
        )
        await denied(
            "SELECT accept_raw_webhook_event("
            "CAST(:id AS uuid), CAST(:tid AS uuid), 'whatsapp_cloud', :eid, "
            "'forged-account', CAST('{}' AS jsonb), CAST(:payload AS jsonb))",
            {
                "id": uuid.uuid4(),
                "tid": tenant_b,
                "eid": f"forged-{uuid.uuid4().hex}",
                "payload": '{"forged":true}',
            },
        )
        await denied("SELECT id FROM list_unenqueued_raw_webhook_ids(50)", {})
        async with tenant_session(runtime_factory, tenant_b) as attack:
            resolved = (
                await attack.execute(
                    text(
                        "SELECT tenant_id FROM resolve_tenant_by_channel_external_id("
                        "'whatsapp_cloud', :ext)"
                    ),
                    {"ext": external_id},
                )
            ).scalar_one()
            assert resolved == tenant_a

        async with admin.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT tenant_id, processing_status, raw_payload->>'secret' AS secret "
                        "FROM raw_webhook_events WHERE id = :id"
                    ),
                    {"id": raw_id},
                )
            ).one()
            assert row.tenant_id == tenant_a
            assert row.processing_status == "received"
            assert row.secret == "victim-pii"
            forged = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM raw_webhook_events "
                        "WHERE external_event_id LIKE 'forged-%'"
                    )
                )
            ).scalar_one()
            assert forged == 0
    finally:
        async with admin.begin() as conn:
            await conn.execute(
                text("DELETE FROM raw_webhook_events WHERE id = :id"),
                {"id": raw_id},
            )
            await conn.execute(
                text("DELETE FROM channel_routing_directory WHERE tenant_id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
            await conn.execute(
                text("DELETE FROM tenants WHERE id IN (:ta, :tb)"),
                {"ta": tenant_a, "tb": tenant_b},
            )
        await runtime.dispose()
        await admin.dispose()


def account_a_hex(value: uuid.UUID) -> str:
    return value.hex[:12]


def _start_worker(env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "erp_core.worker_main"],
        cwd=str(Path(".").resolve()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _assert_worker_alive(proc: subprocess.Popen[bytes]) -> None:
    code = proc.poll()
    if code is None:
        return
    err = proc.stderr.read().decode() if proc.stderr is not None else ""
    raise AssertionError(f"worker exited {code}: {err}")


def _stop_worker(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    assert proc.returncode == 0


@pytest.mark.asyncio
async def test_worker_module_entrypoint_consumes_http_webhook(monkeypatch) -> None:
    secret = "test_whatsapp_app_secret"
    monkeypatch.setenv("WHATSAPP_APP_SECRET", secret)
    monkeypatch.setenv("WEBHOOK_DEFER_DELAY_SECONDS", "0")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path("apps/backend/src").resolve())
    env["WHATSAPP_APP_SECRET"] = secret
    env["WEBHOOK_DEFER_DELAY_SECONDS"] = "0"
    proc = _start_worker(env)
    app = create_app()
    admin = create_async_engine(_admin_url(), future=True)
    phone_number_id = f"pn_{uuid.uuid4().hex[:10]}"
    account_id = uuid.uuid4()
    tenant_id: uuid.UUID | None = None
    try:
        await asyncio.sleep(1.0)
        _assert_worker_alive(proc)
        transport = ASGITransport(app=app)
        async with app.router.lifespan_context(app), AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"wk-{uuid.uuid4().hex[:8]}@factory.example.com",
                    "password": "StrongPassword2026!",
                    "full_name": "Worker Owner",
                    "tenant_name": "Worker Factory",
                    "tenant_slug": f"wk-{uuid.uuid4().hex[:8]}",
                },
            )
            assert registered.status_code == 201, registered.text
            token = registered.json()["access_token"]
            tenant_id = uuid.UUID(registered.json()["principal"]["tenant_id"])
            async with admin.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO channel_accounts "
                        "(id, tenant_id, channel_type, external_account_id, display_name, status) "
                        "VALUES (:id, :tid, 'whatsapp_cloud', :ext, 'WA', 'active')"
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
            session_factory = app.state.session_factory

            async def message_count(event_id: str) -> int:
                assert tenant_id is not None
                async with tenant_session(session_factory, tenant_id) as sess:
                    return int(
                        (
                            await sess.execute(
                                text(
                                    "SELECT count(*) FROM messages "
                                    "WHERE tenant_id = :tid AND external_message_id = :eid"
                                ),
                                {"tid": tenant_id, "eid": event_id},
                            )
                        ).scalar_one()
                    )

            live_id = f"wamid.live.{uuid.uuid4().hex[:8]}"
            live_body = _message_payload(live_id, phone_number_id, "from subprocess worker")
            accepted = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=live_body,
                headers={
                    "X-Hub-Signature-256": _sign(secret, live_body),
                    "Content-Type": "application/json",
                },
            )
            assert accepted.status_code == 200
            await _wait_until(lambda: _eq(message_count(live_id), 1), timeout=8.0)
            _assert_worker_alive(proc)

            orphan_id = f"wamid.replay.{uuid.uuid4().hex[:8]}"
            orphan_body = json.dumps(
                {
                    "entry": [
                        {
                            "changes": [
                                {
                                    "field": "messages",
                                    "value": {
                                        "metadata": {"phone_number_id": phone_number_id},
                                        "statuses": [
                                            {
                                                "id": orphan_id,
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
            ).encode()
            orphan = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=orphan_body,
                headers={
                    "X-Hub-Signature-256": _sign(secret, orphan_body),
                    "Content-Type": "application/json",
                },
            )
            assert orphan.status_code == 200

            async def open_dlq() -> uuid.UUID | None:
                assert tenant_id is not None
                async with tenant_session(session_factory, tenant_id) as sess:
                    found = (
                        await sess.execute(
                            text(
                                "SELECT id FROM dead_letter_events "
                                "WHERE tenant_id = :tid AND status = 'open' "
                                "AND external_event_id LIKE :pat LIMIT 1"
                            ),
                            {"tid": tenant_id, "pat": f"%{orphan_id}%"},
                        )
                    ).scalar_one_or_none()
                return None if found is None else uuid.UUID(str(found))

            await _wait_until(lambda: _present(open_dlq()), timeout=12.0)
            event_id = await open_dlq()
            assert event_id is not None
            customer_id = uuid.uuid4()
            conv_id = uuid.uuid4()
            async with admin.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO customers (id, tenant_id, display_name) "
                        "VALUES (:id, :tid, 'Replay Buyer')"
                    ),
                    {"id": customer_id, "tid": tenant_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO conversations ("
                        "id, tenant_id, customer_id, channel_account_id, channel_type, status"
                        ") VALUES (:id, :tid, :cid, :aid, 'whatsapp_cloud', 'open')"
                    ),
                    {"id": conv_id, "tid": tenant_id, "cid": customer_id, "aid": account_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO messages ("
                        "id, tenant_id, conversation_id, customer_id, channel_type, "
                        "external_message_id, direction, sender_type, content_type, "
                        "body_text, delivery_status, source_timestamp"
                        ") VALUES ("
                        ":id, :tid, :conv, :cid, 'whatsapp_cloud', :eid, "
                        "'inbound', 'customer', 'text', 'parent', 'queued', now()"
                        ")"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tid": tenant_id,
                        "conv": conv_id,
                        "cid": customer_id,
                        "eid": orphan_id,
                    },
                )
            replayed = await client.post(
                f"/api/v1/channels/dlq/{event_id}/replay",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert replayed.status_code == 200

            async def delivered() -> bool:
                assert tenant_id is not None
                async with tenant_session(session_factory, tenant_id) as sess:
                    status = (
                        await sess.execute(
                            text(
                                "SELECT delivery_status FROM messages "
                                "WHERE tenant_id = :tid AND external_message_id = :eid"
                            ),
                            {"tid": tenant_id, "eid": orphan_id},
                        )
                    ).scalar_one()
                return str(status) == "delivered"

            await _wait_until(delivered, timeout=8.0)
            _stop_worker(proc)
            parked_id = f"wamid.parked.{uuid.uuid4().hex[:8]}"
            parked_body = _message_payload(parked_id, phone_number_id, "pending reclaim")
            parked = await client.post(
                "/api/v1/webhooks/whatsapp",
                content=parked_body,
                headers={
                    "X-Hub-Signature-256": _sign(secret, parked_body),
                    "Content-Type": "application/json",
                },
            )
            assert parked.status_code == 200
            redis = app.state.redis
            try:
                await redis.xgroup_create(
                    "stream:webhooks:whatsapp_cloud",
                    "erp_webhook_workers",
                    id="0",
                    mkstream=True,
                )
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            stolen = await redis.xreadgroup(
                groupname="erp_webhook_workers",
                consumername="crasher",
                streams={"stream:webhooks:whatsapp_cloud": ">"},
                count=50,
            )
            stolen_ids = [
                fields.get(b"raw_event_id", fields.get("raw_event_id"))
                for _name, items in stolen
                for _msg_id, fields in items
            ]
            assert stolen_ids
            proc = _start_worker(env)
            await asyncio.sleep(0.5)
            _assert_worker_alive(proc)
            await _wait_until(lambda: _eq(message_count(parked_id), 1), timeout=20.0)
            _stop_worker(proc)
            proc = None
    finally:
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5)
        if tenant_id is not None:
            async with admin.begin() as conn:
                await conn.execute(
                    text("DELETE FROM dead_letter_events WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM messages WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM conversations WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM customer_identities WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM customers WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM raw_webhook_events WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM channel_routing_directory WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
                await conn.execute(
                    text("DELETE FROM channel_accounts WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
        await admin.dispose()


async def _eq(awaitable: Awaitable[int], expected: int) -> bool:
    return await awaitable == expected


async def _present(awaitable: Awaitable[uuid.UUID | None]) -> bool:
    return await awaitable is not None
