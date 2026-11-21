"""Live social publishing and the scheduled dispatch cron, verified on PostgreSQL 17.

Two things are proven here that the record-only suites cannot prove:

* `POST /publish {"execute_remote": true}` really decrypts the envelope-encrypted OAuth
  token (AES-256-GCM, `AAD = f"{tenant_id}:{account_id}:social_oauth"`) and really drives
  the vendor HTTP contract — every request is intercepted by `httpx.MockTransport`, so the
  assertions are about the exact outbound call and the persisted receipt;
* `dispatch_due_social_posts_cron` sweeps due `scheduled_at` rows per tenant inside a
  fail-closed RLS session, persists terminal failures (so a revoked token is not retried
  every minute), and never publishes the same post twice.

Failure honesty is asserted at the API boundary as well: a missing token, a manual-only
platform and a revoked authorization each answer with their own typed problem document
and never leave a post marked `published`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Configure ORM modules so string FKs (tenants/users) resolve, exactly like api_main.
import erp_core.crm.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.social.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
import erp_core.worker_main as worker_main
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.social.publishers import HttpSocialPublisherDispatcher
from erp_core.social.router import router as social_router
from erp_core.social.scheduler import dispatch_due_social_posts_cron
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
BASE = "/api/v1/social"
X_TOKEN = "x-live-oauth-token-0123456789-never-echoed"
IG_TOKEN = "IGQVJ-live-instagram-token-never-echoed"
REVOKED_TOKEN = "x-revoked-token-abcdef-never-echoed"
CRON_TASK_NAME = "erp_core.social.scheduler:dispatch_due_social_posts_cron"


# --------------------------------------------------------------------------- harness
def client_for(
    factory: Factory,
    principal: CurrentPrincipal,
    *,
    publisher: HttpSocialPublisherDispatcher | None = None,
) -> AsyncClient:
    """Isolated app whose composition root mirrors api_main (crypto + publisher)."""
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.state.crypto_service = EnvelopeCryptoService(get_settings())
    if publisher is not None:
        app.state.social_publisher = publisher
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(social_router, prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def dispatcher(handler: Any) -> HttpSocialPublisherDispatcher:
    return HttpSocialPublisherDispatcher(transport=httpx.MockTransport(handler))


@pytest.fixture
async def workspace(runtime_session_factory: Factory) -> dict[str, Any]:
    """Two fully isolated tenants, each with one owner (for cron + RLS assertions)."""
    tenants = [uuid4(), uuid4()]
    principals: list[CurrentPrincipal] = []
    for tenant_id in tenants:
        owner_id = uuid4()
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            await session.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Live Social')"),
                {"id": tenant_id, "slug": f"live-social-{tenant_id.hex[:12]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, 'Live Owner', 'test')"
                ),
                {"id": owner_id, "email": f"{owner_id.hex}@live-social.example.com"},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (:tid, :uid, 'owner')"
                ),
                {"tid": tenant_id, "uid": owner_id},
            )
        principals.append(
            CurrentPrincipal(
                user_id=owner_id,
                tenant_id=tenant_id,
                role=UserRole.OWNER,
                email=f"{owner_id.hex}@live-social.example.com",
            )
        )
    return {
        "factory": runtime_session_factory,
        "tenant_a": tenants[0],
        "tenant_b": tenants[1],
        "owner_a": principals[0],
        "owner_b": principals[1],
    }


async def _connect(
    client: AsyncClient,
    platform: str,
    handle: str,
    *,
    token: str | None,
    external_account_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "platform": platform,
        "handle": handle,
        "display_name": f"Live {platform}",
        "external_account_id": external_account_id,
    }
    if token is not None:
        payload["access_token"] = token
    response = await client.post(f"{BASE}/accounts/connect", json=payload)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def _create_post(
    client: AsyncClient,
    platform: str,
    *,
    caption: str,
    scheduled_at: datetime | None = None,
    media_urls: list[str] | None = None,
    hashtags: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "platform": platform,
        "caption": caption,
        "hashtags": hashtags or [],
        "media_urls": media_urls or [],
        "status": "scheduled" if scheduled_at is not None else "draft",
    }
    if scheduled_at is not None:
        payload["scheduled_at"] = scheduled_at.isoformat()
    response = await client.post(f"{BASE}/posts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- live X
async def test_live_publish_decrypts_the_token_and_persists_the_platform_receipt(
    workspace: dict[str, Any],
) -> None:
    factory, tenant, owner = workspace["factory"], workspace["tenant_a"], workspace["owner_a"]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"data": {"id": "1800000000000000123", "text": "ok"}})

    async with client_for(factory, owner, publisher=dispatcher(handler)) as client:
        account = await _connect(client, "x", "@lumenlight", token=X_TOKEN)
        post = await _create_post(
            client,
            "x",
            caption="IP65 work light, factory direct",
            hashtags=["#lighting"],
        )
        response = await client.post(
            f"{BASE}/posts/{post['id']}/publish", json={"execute_remote": True}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "published"
    assert body["social_account_id"] == account["id"]
    assert body["external_post_id"] == "1800000000000000123"
    assert body["delivery_mode"] == "external_api"
    assert body["external_publish_confirmed"] is True
    assert body["published_at"] is not None
    assert body["metrics"]["publish_channel"] == "api"
    assert body["metrics"]["delivery_mode"] == "external_api"
    assert body["metrics"]["external_publish_confirmed"] is True
    assert body["metrics"]["remote_status"] == "published"
    assert body["metrics"]["permalink"] == "https://x.com/i/web/status/1800000000000000123"
    assert X_TOKEN not in response.text

    # Exactly one outbound call, carrying the *plaintext* token decrypted from the envelope.
    assert len(calls) == 1
    outbound = calls[0]
    assert str(outbound.url) == "https://api.x.com/2/tweets"
    assert outbound.headers["authorization"] == f"Bearer {X_TOKEN}"
    assert outbound.read().decode() == ('{"text":"IP65 work light, factory direct #lighting"}')

    # The token is stored encrypted, never as plaintext, and the receipt is durable.
    async with tenant_session(factory, tenant) as session:
        ciphertext, stored_status, external_post_id = (
            await session.execute(
                text(
                    "SELECT a.token_ciphertext, p.status, p.external_post_id "
                    "FROM social_posts p JOIN social_accounts a ON a.id = p.social_account_id "
                    "WHERE p.id = :pid"
                ),
                {"pid": UUID(post["id"])},
            )
        ).one()
        assert ciphertext is not None
        assert X_TOKEN.encode() not in bytes(ciphertext)
        assert stored_status == "published"
        assert external_post_id == "1800000000000000123"


async def test_live_instagram_publish_runs_the_two_step_container_flow(
    workspace: dict[str, Any],
) -> None:
    factory, owner = workspace["factory"], workspace["owner_a"]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "17890000000000001"})
        return httpx.Response(200, json={"id": "container-42"})

    async with client_for(factory, owner, publisher=dispatcher(handler)) as client:
        await _connect(
            client,
            "instagram",
            "@lumenlight",
            token=IG_TOKEN,
            external_account_id="17841400000000000",
        )
        post = await _create_post(
            client,
            "instagram",
            caption="Workshop reel",
            hashtags=["#worklight"],
            media_urls=["https://cdn.example.com/reel.mp4"],
        )
        response = await client.post(
            f"{BASE}/posts/{post['id']}/publish", json={"execute_remote": True}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    # The Graph `media_publish` id is what the platform confirmed, not the container id.
    assert body["external_post_id"] == "17890000000000001"
    assert body["delivery_mode"] == "external_api"
    assert body["external_publish_confirmed"] is True
    assert IG_TOKEN not in response.text

    assert len(calls) == 2
    container_request, publish_request = calls
    assert container_request.url.path == "/v21.0/17841400000000000/media"
    assert container_request.headers["authorization"] == f"Bearer {IG_TOKEN}"
    container_body = container_request.read().decode()
    assert "https://cdn.example.com/reel.mp4" in container_body
    assert "REELS" in container_body
    assert publish_request.url.path == "/v21.0/17841400000000000/media_publish"
    assert publish_request.read().decode() == '{"creation_id":"container-42"}'


# --------------------------------------------------------------------------- fail closed
async def test_live_publish_without_a_stored_token_answers_409(
    workspace: dict[str, Any],
) -> None:
    factory, tenant, owner = workspace["factory"], workspace["tenant_a"], workspace["owner_a"]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never used
        calls.append(request)
        return httpx.Response(200, json={"id": "should-not-happen"})

    async with client_for(factory, owner, publisher=dispatcher(handler)) as client:
        await _connect(client, "instagram", "@lumenlight", token=None)
        post = await _create_post(
            client,
            "instagram",
            caption="No token here",
            media_urls=["https://cdn.example.com/work.jpg"],
        )
        response = await client.post(
            f"{BASE}/posts/{post['id']}/publish", json={"execute_remote": True}
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ERR_SOCIAL_TOKEN_MISSING"
    # Nothing left the building and the post was not faked into `published`.
    assert calls == []
    async with tenant_session(factory, tenant) as session:
        assert (
            await session.scalar(
                text("SELECT status FROM social_posts WHERE id = :pid"),
                {"pid": UUID(post["id"])},
            )
        ) == "draft"


async def test_live_publish_refuses_manual_export_platforms(workspace: dict[str, Any]) -> None:
    factory, owner = workspace["factory"], workspace["owner_a"]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never used
        calls.append(request)
        return httpx.Response(200, json={"id": "should-not-happen"})

    async with client_for(factory, owner, publisher=dispatcher(handler)) as client:
        post = await _create_post(client, "xiaohongshu", caption="笔记草稿，人工到创作中心发布")
        response = await client.post(
            f"{BASE}/posts/{post['id']}/publish", json={"execute_remote": True}
        )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "ERR_SOCIAL_MANUAL_PUBLISH_ONLY"
    assert calls == []


async def test_live_publish_maps_a_revoked_token_to_401_without_faking_a_publish(
    workspace: dict[str, Any],
) -> None:
    factory, tenant, owner = workspace["factory"], workspace["tenant_a"], workspace["owner_a"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"title": "Unauthorized", "type": "about:blank"})

    async with client_for(factory, owner, publisher=dispatcher(handler)) as client:
        account = await _connect(client, "x", "@lumenlight", token=REVOKED_TOKEN)
        post = await _create_post(client, "x", caption="Revoked token post")
        response = await client.post(
            f"{BASE}/posts/{post['id']}/publish", json={"execute_remote": True}
        )

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "ERR_SOCIAL_TOKEN_EXPIRED"
    assert REVOKED_TOKEN not in response.text
    # The HTTP route rolls back, so nothing is persisted as published *or* failed: the
    # caller sees the 401 and the post stays retryable after re-authorization.
    async with tenant_session(factory, tenant) as session:
        status, metrics = (
            await session.execute(
                text("SELECT status, metrics FROM social_posts WHERE id = :pid"),
                {"pid": UUID(post["id"])},
            )
        ).one()
        assert status == "draft"
        assert metrics == {}
        assert (
            await session.scalar(
                text("SELECT status FROM social_accounts WHERE id = :aid"),
                {"aid": UUID(account["id"])},
            )
        ) == "connected"


async def test_live_publish_answers_503_when_no_publisher_is_wired(
    workspace: dict[str, Any],
) -> None:
    factory, tenant, owner = workspace["factory"], workspace["tenant_a"], workspace["owner_a"]
    async with client_for(factory, owner) as client:
        await _connect(client, "x", "@lumenlight", token=X_TOKEN)
        post = await _create_post(client, "x", caption="Deployment without a publisher")
        response = await client.post(
            f"{BASE}/posts/{post['id']}/publish", json={"execute_remote": True}
        )

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "ERR_SOCIAL_PUBLISHER_UNAVAILABLE"
    async with tenant_session(factory, tenant) as session:
        assert (
            await session.scalar(
                text("SELECT status FROM social_posts WHERE id = :pid"),
                {"pid": UUID(post["id"])},
            )
        ) == "draft"


# --------------------------------------------------------------------------- cron sweep
async def test_scheduled_dispatch_publishes_due_posts_in_two_isolated_tenants(
    workspace: dict[str, Any],
    migrator_session_factory: Factory,
    runtime_session_factory: Factory,
) -> None:
    now = datetime.now(UTC)
    due = now - timedelta(minutes=5)
    future = now + timedelta(days=1)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            201, json={"data": {"id": f"18000000000000{len(calls):04d}", "text": "ok"}}
        )

    publisher = dispatcher(handler)
    posts: dict[str, dict[str, Any]] = {}
    for key in ("owner_a", "owner_b"):
        principal: CurrentPrincipal = workspace[key]
        async with client_for(workspace["factory"], principal, publisher=publisher) as client:
            await _connect(client, "x", f"@{key}", token=f"{X_TOKEN}-{key}")
            posts[f"due_{key}"] = await _create_post(
                client, "x", caption=f"Due post {key}", scheduled_at=due
            )
            posts[f"future_{key}"] = await _create_post(
                client, "x", caption=f"Future post {key}", scheduled_at=future
            )

    scoped_tenants = [workspace["tenant_a"], workspace["tenant_b"]]
    totals = await dispatch_due_social_posts_cron(
        broker_session_factory=migrator_session_factory,
        runtime_session_factory=runtime_session_factory,
        publisher=publisher,
        now=now,
        tenant_ids=scoped_tenants,
    )
    assert totals == {"tenants": 2, "scanned": 2, "published": 2, "failed": 0}

    # Every due row is now published with the receipt the platform returned; only one
    # outbound call per due post (the future rows were never touched).
    assert len(calls) == 2
    sent_tokens = {request.headers["authorization"] for request in calls}
    assert sent_tokens == {f"Bearer {X_TOKEN}-owner_a", f"Bearer {X_TOKEN}-owner_b"}

    for key in ("owner_a", "owner_b"):
        async with tenant_session(
            runtime_session_factory, workspace["tenant_" + key[-1]]
        ) as session:
            due_row = (
                await session.execute(
                    text(
                        "SELECT status, external_post_id, metrics FROM social_posts "
                        "WHERE id = :pid"
                    ),
                    {"pid": UUID(posts[f"due_{key}"]["id"])},
                )
            ).one()
            assert due_row.status == "published"
            assert due_row.external_post_id
            assert due_row.metrics["delivery_mode"] == "external_api"
            assert due_row.metrics["external_publish_confirmed"] is True
            future_status = await session.scalar(
                text("SELECT status FROM social_posts WHERE id = :pid"),
                {"pid": UUID(posts[f"future_{key}"]["id"])},
            )
            assert future_status == "scheduled"

    # A second sweep finds nothing due: no double publish, no extra outbound call.
    again = await dispatch_due_social_posts_cron(
        broker_session_factory=migrator_session_factory,
        runtime_session_factory=runtime_session_factory,
        publisher=publisher,
        now=datetime.now(UTC),
        tenant_ids=scoped_tenants,
    )
    assert again == {"tenants": 0, "scanned": 0, "published": 0, "failed": 0}
    assert len(calls) == 2


async def test_scheduled_dispatch_persists_a_revoked_token_as_failed_once(
    workspace: dict[str, Any],
    migrator_session_factory: Factory,
    runtime_session_factory: Factory,
) -> None:
    now = datetime.now(UTC)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"title": "Unauthorized"})

    publisher = dispatcher(handler)
    async with client_for(
        workspace["factory"], workspace["owner_a"], publisher=publisher
    ) as client:
        account = await _connect(client, "x", "@revoked", token=REVOKED_TOKEN)
        post = await _create_post(
            client, "x", caption="Revoked token scheduled", scheduled_at=now - timedelta(minutes=1)
        )

    totals = await dispatch_due_social_posts_cron(
        broker_session_factory=migrator_session_factory,
        runtime_session_factory=runtime_session_factory,
        publisher=publisher,
        now=now,
        tenant_ids=[workspace["tenant_a"]],
    )
    assert totals == {"tenants": 1, "scanned": 1, "published": 0, "failed": 1}
    assert len(calls) == 1

    async with tenant_session(runtime_session_factory, workspace["tenant_a"]) as session:
        status, metrics = (
            await session.execute(
                text("SELECT status, metrics FROM social_posts WHERE id = :pid"),
                {"pid": UUID(post["id"])},
            )
        ).one()
        assert status == "failed"
        assert metrics["publish_error"] == "ERR_SOCIAL_TOKEN_EXPIRED"
        assert REVOKED_TOKEN not in metrics["publish_error_detail"]
        assert (
            await session.scalar(
                text("SELECT status FROM social_accounts WHERE id = :aid"),
                {"aid": UUID(account["id"])},
            )
        ) == "token_expired"

    # The terminal `failed` row is not retried, so a dead token cannot hammer the API.
    again = await dispatch_due_social_posts_cron(
        broker_session_factory=migrator_session_factory,
        runtime_session_factory=runtime_session_factory,
        publisher=publisher,
        now=datetime.now(UTC),
        tenant_ids=[workspace["tenant_a"]],
    )
    assert again == {"tenants": 0, "scanned": 0, "published": 0, "failed": 0}
    assert len(calls) == 1


async def test_scheduled_dispatch_survives_a_platform_outage(
    workspace: dict[str, Any],
    migrator_session_factory: Factory,
    runtime_session_factory: Factory,
) -> None:
    """A transport failure is a 502-class `failed` row, not a crashed sweep."""
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream unreachable", request=request)

    publisher = dispatcher(handler)
    async with client_for(
        workspace["factory"], workspace["owner_a"], publisher=publisher
    ) as client:
        await _connect(client, "x", "@outage", token=X_TOKEN)
        post = await _create_post(
            client, "x", caption="Platform outage", scheduled_at=now - timedelta(minutes=1)
        )

    totals = await dispatch_due_social_posts_cron(
        broker_session_factory=migrator_session_factory,
        runtime_session_factory=runtime_session_factory,
        publisher=publisher,
        now=now,
        tenant_ids=[workspace["tenant_a"]],
    )

    assert totals == {"tenants": 1, "scanned": 1, "published": 0, "failed": 1}
    async with tenant_session(runtime_session_factory, workspace["tenant_a"]) as session:
        status, metrics = (
            await session.execute(
                text("SELECT status, metrics FROM social_posts WHERE id = :pid"),
                {"pid": UUID(post["id"])},
            )
        ).one()
    assert status == "failed"
    assert metrics["publish_error"] == "ERR_SOCIAL_PUBLISH_FAILED"
    assert X_TOKEN not in metrics["publish_error_detail"]


# --------------------------------------------------------------------------- worker wiring
def test_worker_registers_the_social_dispatch_cron() -> None:
    tasks = worker_main.broker.get_all_tasks()
    assert CRON_TASK_NAME in tasks
    assert tasks[CRON_TASK_NAME].labels["schedule"] == [{"cron": "* * * * *"}]
    assert worker_main.SOCIAL_DISPATCH_INTERVAL_SECONDS == 60.0
    # The registered callable is the real sweep, not a placeholder.
    assert tasks[CRON_TASK_NAME].original_func is dispatch_due_social_posts_cron


async def test_worker_loop_survives_a_failing_social_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    calls = {"drain": 0, "social": 0}

    async def drain(factory: Any, redis: Any) -> dict[str, int]:
        calls["drain"] += 1
        if calls["drain"] >= 2:
            stop.set()
        return {channel: 0 for channel in worker_main.COMMERCE_TRACKING_CHANNELS}

    async def boom() -> dict[str, int]:
        calls["social"] += 1
        raise RuntimeError("social platform outage")

    async def reconcile() -> dict[str, int]:  # pragma: no cover - interval is huge
        return {}

    async def stalled() -> int:  # pragma: no cover - interval is huge
        return 0

    monkeypatch.setattr(worker_main, "drain_commerce_and_tracking_once", drain)
    monkeypatch.setattr(worker_main, "dispatch_due_social_posts_cron", boom)
    monkeypatch.setattr(worker_main, "reconcile_ecommerce_orders_cron", reconcile)
    monkeypatch.setattr(worker_main, "monitor_stalled_shipments_cron", stalled)

    await asyncio.wait_for(
        worker_main._run_commerce_and_tracking_loop(
            stop,
            worker_main._broker_session_factory,
            worker_main._redis,
            drain_interval=0.0,
            reconcile_interval=1e9,
            stalled_interval=1e9,
            social_interval=0.0,
            periodic_crons=True,
        ),
        timeout=5.0,
    )

    # The drain kept running and the crash was contained to the social tick.
    assert calls["drain"] >= 2
    assert calls["social"] >= 1
