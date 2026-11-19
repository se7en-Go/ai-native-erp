from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import OutboundMessageRequest
from erp_core.channels.ecommerce.base import CredentialVaultHelper, resolve_routing_tenant
from erp_core.channels.ingress_worker import _persist_inbound_event
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, IdentityType, UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

RuntimeFactory = async_sessionmaker[AsyncSession]


def require_adapter(module: str) -> None:
    assert find_spec(f"erp_core.channels.adapters.{module}") is not None


def sign(body: bytes, secret: str = "local-meta-signature-test") -> str:
    return "sha256=" + hmac.digest(secret.encode(), body, hashlib.sha256).hex()


def meta_payload(account: str, peer: str, mid: str, when: datetime, **message) -> dict:
    return {
        "object": "page",
        "entry": [
            {
                "id": account,
                "messaging": [
                    {
                        "sender": {"id": peer},
                        "recipient": {"id": account},
                        "timestamp": int(when.timestamp() * 1000),
                        "message": {"mid": mid, **message},
                    }
                ],
            }
        ],
    }


@pytest.fixture
async def tenant(runtime_session_factory: RuntimeFactory) -> AsyncIterator[UUID]:
    tid = uuid4()
    async with tenant_session(runtime_session_factory, tid) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Messaging Tests')"),
            {"id": tid, "slug": f"messaging-{tid.hex}"},
        )
    try:
        yield tid
    finally:
        admin = create_async_engine(
            os.environ.get(
                "DATABASE_ADMIN_URL",
                "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
            )
        )
        try:
            async with admin.begin() as connection:
                await connection.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tid})
        finally:
            await admin.dispose()


def override_principal(app: FastAPI, factory: RuntimeFactory, tenant_id: UUID) -> None:
    app.state.session_factory = factory
    app.dependency_overrides[get_current_principal] = lambda: CurrentPrincipal(
        tenant_id=tenant_id,
        user_id=uuid4(),
        role=UserRole.ADMIN,
        email="admin@example.com",
    )


@pytest.mark.asyncio
async def test_messenger_connect_and_verify_raw_bytes(
    tenant: UUID,
    runtime_session_factory: RuntimeFactory,
) -> None:
    require_adapter("messenger")
    from erp_core.channels.adapters.messenger import MessengerAdapter, messenger_router

    page = str(uuid4().int)[:18]

    def graph(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/me")
        assert request.headers["Authorization"] == "Bearer test-page-access-token"
        return httpx.Response(200, json={"id": page})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(messenger_router)
    override_principal(app, runtime_session_factory, tenant)
    app.state.messaging_http_transport = httpx.MockTransport(graph)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        invalid = await client.post(
            "/api/v1/channels/messenger/connect",
            json={
                "page_id": "123",
                "page_access_token": "test-page-access-token",
                "display_name": "Page",
            },
        )
        assert invalid.status_code == 422
        assert "test-page-access-token" not in invalid.text
        connected = await client.post(
            "/api/v1/channels/messenger/connect",
            json={
                "page_id": page,
                "page_access_token": "test-page-access-token",
                "display_name": "Page",
            },
        )
        assert connected.status_code == 200, connected.text
        aid = UUID(connected.json()["channel_account_id"])
        assert "test-page-access-token" not in connected.text

    async with tenant_session(runtime_session_factory, tenant) as session:
        assert await resolve_routing_tenant(session, "messenger", page) == (tenant, aid)
        credentials, _ = await CredentialVaultHelper().load_secret_json(
            session, tenant, aid, "messaging_provider"
        )
        assert credentials == {"page_access_token": "test-page-access-token"}

    body = b'{ "entry": [] }'
    headers = {"X-Hub-Signature-256": sign(body)}
    assert MessengerAdapter().verify_webhook_signature(body, headers, "local-meta-signature-test")
    assert not MessengerAdapter().verify_webhook_signature(
        body + b" ", headers, "local-meta-signature-test"
    )


async def make_account(session: AsyncSession, tenant: UUID, channel: str, external: str) -> UUID:
    aid = uuid4()
    await session.execute(
        text(
            "INSERT INTO channel_accounts "
            "(id, tenant_id, channel_type, external_account_id, display_name) "
            "VALUES (:id, :tid, :channel, :external, 'Test account')"
        ),
        {"id": aid, "tid": tenant, "channel": channel, "external": external},
    )
    return aid


async def outbound_request(session, tenant, aid, channel, account, peer, cid):
    message_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO messages (id, tenant_id, conversation_id, customer_id, channel_type, "
            "direction, sender_type, sender_id, content_type, body_text, source_timestamp) "
            "SELECT :mid, tenant_id, id, customer_id, channel_type, 'outbound', 'human_agent', "
            "'test-agent', 'text', 'Agent reply', now() FROM conversations WHERE id = :cid"
        ),
        {"mid": message_id, "cid": cid},
    )
    return OutboundMessageRequest(
        tenant_id=tenant,
        channel_account_id=aid,
        channel_type=channel,
        external_account_id=account,
        recipient_id=peer,
        conversation_id=cid,
        message_id=message_id,
        body_text="Agent reply",
    )


@pytest.mark.asyncio
async def test_messenger_persists_psid_windows_and_enforces_human_and_stop(
    tenant: UUID,
    runtime_session_factory: RuntimeFactory,
) -> None:
    require_adapter("messenger")
    from erp_core.channels.adapters.messenger import MessengerAdapter

    now = datetime.now(UTC).replace(microsecond=0)
    page, psid = "10101", "20202"
    calls = []

    def graph(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/me/messages")
        assert request.headers["Authorization"] == "Bearer test-token"
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"recipient_id": psid, "message_id": "mid.reply"})

    async with tenant_session(runtime_session_factory, tenant) as session:
        aid = await make_account(session, tenant, "messenger", page)
        adapter = MessengerAdapter(http_transport=httpx.MockTransport(graph), clock=lambda: now)
        payload = meta_payload(
            page,
            psid,
            "mid.inbound",
            now,
            text="Hi",
            attachments=[
                {
                    "type": "image",
                    "payload": {"url": "https://example.com/image.png"},
                }
            ],
        )
        batch = await adapter.parse_webhook(session, tenant, aid, payload, {})
        [event] = batch.inbound_events
        assert event.sender_identity_type == IdentityType.FB_PSID
        assert event.sender_identity_value == psid
        assert event.body_text == "Hi"
        assert event.attachments[0].external_url == "https://example.com/image.png"
        read_payload = meta_payload(page, psid, "unused", now + timedelta(minutes=3))
        read = read_payload["entry"][0]["messaging"][0]
        del read["message"]
        read["read"] = {"watermark": int(now.timestamp() * 1000)}
        receipt = await adapter.parse_webhook(session, tenant, aid, read_payload, {})
        assert receipt.inbound_events == []
        assert receipt.status_events[0].delivery_status == "read"
        assert receipt.status_events[0].read_through_recipient_id == psid
        cid, _ = await _persist_inbound_event(session, tenant, aid, "messenger", event)

        row = (
            await session.execute(
                text(
                    "SELECT csw_expires_at, human_agent_window_expires_at "
                    "FROM conversations WHERE id=:cid"
                ),
                {"cid": cid},
            )
        ).one()
        assert row == (now + timedelta(hours=24), now + timedelta(days=7))
        assert (
            await session.scalar(
                text("SELECT identity_value FROM customer_identities WHERE identity_type='fb_psid'")
            )
            == psid
        )
        request = await outbound_request(
            session, tenant, aid, ChannelType.MESSENGER, page, psid, cid
        )
        credentials = {"page_access_token": "test-token"}
        sent = await adapter.send_message(session, tenant, credentials, request)
        assert sent.success and sent.external_message_id == "mid.reply"
        assert calls[-1] == {
            "recipient": {"id": psid},
            "message": {"text": "Agent reply"},
            "messaging_type": "RESPONSE",
        }

        def at(hours: float) -> MessengerAdapter:
            return MessengerAdapter(
                http_transport=httpx.MockTransport(graph),
                clock=lambda: now + timedelta(hours=hours),
            )

        for boundary in (24, 7 * 24):
            assert (await at(boundary).send_message(session, tenant, credentials, request)).success
            assert calls[-1]["tag"] == "HUMAN_AGENT"
            assert calls[-1]["messaging_type"] == "MESSAGE_TAG"
        rejected = await at(169).send_message(session, tenant, credentials, request)
        assert rejected.error_message == "ERR_MESSENGER_WINDOW_EXPIRED" and not rejected.retryable
        await session.execute(
            text("UPDATE messages SET sender_type='ai_agent' WHERE id=:id"),
            {"id": request.message_id},
        )
        forged = request.model_copy(update={"metadata": {"sender_type": "human_agent"}})
        assert (
            await at(25).send_message(session, tenant, credentials, forged)
        ).error_message == "ERR_MESSENGER_WINDOW_EXPIRED"

        stop = meta_payload(page, psid, "mid.stop", now, text=" STOP ")
        stop_batch = await adapter.parse_webhook(session, tenant, aid, stop, {})
        await _persist_inbound_event(
            session, tenant, aid, "messenger", stop_batch.inbound_events[0]
        )
        assert (
            await adapter.send_message(session, tenant, credentials, request)
        ).error_message == "ERR_RECIPIENT_SUPPRESSED"
        assert len(calls) == 3


@pytest.mark.asyncio
async def test_instagram_story_messages_connect_and_enforce_human_window(
    tenant: UUID,
    runtime_session_factory: RuntimeFactory,
) -> None:
    require_adapter("instagram_dm")
    from erp_core.channels.adapters.instagram_dm import InstagramDMAdapter, instagram_router

    account = str(uuid4().int)[:18]
    peer = "33445566"
    now = datetime.now(UTC).replace(microsecond=0)
    calls = []

    def graph(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-ig-token"
        if request.method == "GET":
            assert request.url.path.endswith(f"/{account}")
            return httpx.Response(200, json={"id": account})
        assert request.url.path.endswith(f"/{account}/messages")
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"message_id": "ig.out"})

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(instagram_router)
    override_principal(app, runtime_session_factory, tenant)
    app.state.messaging_http_transport = httpx.MockTransport(graph)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/channels/instagram/connect",
            json={
                "ig_account_id": account,
                "access_token": "test-ig-token",
                "display_name": "Instagram",
            },
        )
        assert response.status_code == 200, response.text
        aid = UUID(response.json()["channel_account_id"])

    adapter = InstagramDMAdapter(
        http_transport=httpx.MockTransport(graph), clock=lambda: now + timedelta(days=6)
    )
    payload = meta_payload(
        account,
        peer,
        "ig.story",
        now,
        text="Price?",
        attachments=[
            {"type": "story_mention", "payload": {"url": "https://example.com/mention"}},
            {"type": "image", "payload": {"url": "https://example.com/image"}},
        ],
        reply_to={"story": {"id": "story-1", "url": "https://example.com/story"}},
    )
    payload["object"] = "instagram"
    raw = json.dumps(payload).encode()
    assert adapter.verify_webhook_signature(
        raw, {"x-hub-signature-256": sign(raw)}, "local-meta-signature-test"
    )
    async with tenant_session(runtime_session_factory, tenant) as session:
        assert await resolve_routing_tenant(session, "instagram_dm", account) == (tenant, aid)
        credentials, _ = await CredentialVaultHelper().load_secret_json(
            session,
            tenant,
            aid,
            "messaging_provider",
        )
        assert credentials == {"access_token": "test-ig-token"}
        [event] = (await adapter.parse_webhook(session, tenant, aid, payload, {})).inbound_events
        assert event.sender_identity_type == IdentityType.IG_PSID
        assert event.sender_identity_value == peer
        assert event.attachments[1].mime_type == "image/*"
        assert event.channel_metadata["story_mention"] == {"url": "https://example.com/mention"}
        assert event.channel_metadata["story_reply"]["id"] == "story-1"
        cid, _ = await _persist_inbound_event(session, tenant, aid, "instagram_dm", event)
        assert (
            await session.scalar(
                text("SELECT identity_value FROM customer_identities WHERE identity_type='ig_psid'")
            )
            == peer
        )
        req = await outbound_request(
            session, tenant, aid, ChannelType.INSTAGRAM_DM, account, peer, cid
        )
        assert (await adapter.send_message(session, tenant, credentials, req)).success
        assert calls[-1]["tag"] == "HUMAN_AGENT"
        expired = InstagramDMAdapter(clock=lambda: now + timedelta(days=7, seconds=1))
        assert (await expired.send_message(session, tenant, credentials, req)).error_message == (
            "ERR_INSTAGRAM_WINDOW_EXPIRED"
        )
        payload = meta_payload(account, peer, "ig.stop", now, text="STOP")
        payload["object"] = "instagram"
        [event] = (await adapter.parse_webhook(session, tenant, aid, payload, {})).inbound_events
        await _persist_inbound_event(session, tenant, aid, "instagram_dm", event)
        assert (await adapter.send_message(session, tenant, credentials, req)).error_message == (
            "ERR_RECIPIENT_SUPPRESSED"
        )
        assert len(calls) == 1


def telegram_update(update_id: int, peer: int, *, business: str | None = None, **content) -> dict:
    message = {
        "message_id": update_id,
        "date": int(datetime.now(UTC).timestamp()),
        "from": {"id": peer, "is_bot": False, "first_name": "Telegram Buyer"},
        "chat": {"id": peer, "type": "private"},
        **content,
    }
    if business:
        message["business_connection_id"] = business
        return {"update_id": update_id, "business_message": message}
    return {"update_id": update_id, "message": message}


@pytest.mark.asyncio
async def test_telegram_business_connection_binding_scoped_identity_and_stop(
    tenant: UUID,
    runtime_session_factory: RuntimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_adapter("telegram")
    from erp_core.channels.adapters.telegram import (
        TELEGRAM_ROUTING_HEADER,
        TelegramAdapter,
        telegram_router,
    )

    bot = int(str(uuid4().int)[:14])
    peer = 8376472
    token = f"{bot}:test-bot-token-xxxxxxxxxx"
    calls = []

    def telegram(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org"
        assert request.url.path.startswith(f"/bot{token}/")
        method = request.url.path.rsplit("/", 1)[-1]
        data = json.loads(request.content)
        calls.append((method, data))
        if method == "getMe":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": bot,
                        "is_bot": True,
                        "first_name": "ERP Bot",
                        "username": "ExampleBot",
                    },
                },
            )
        if method == "setWebhook":
            assert data["url"] == f"https://erp.example.test/api/v1/webhooks/telegram/{bot}"
            assert data["allowed_updates"] == ["message", "business_message"]
            return httpx.Response(200, json={"ok": True, "result": True})
        assert method == "sendMessage"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 145}})

    transport = httpx.MockTransport(telegram)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(telegram_router)
    override_principal(app, runtime_session_factory, tenant)
    app.state.messaging_http_transport = transport
    monkeypatch.setenv("TELEGRAM_WEBHOOK_BASE_URL", "https://erp.example.test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/channels/telegram/connect",
            json={
                "bot_token": token,
                "display_name": "Telegram Business",
            },
        )
        assert response.status_code == 200, response.text
        aid = UUID(response.json()["channel_account_id"])
        assert token not in response.text
    assert [method for method, _ in calls] == ["getMe", "setWebhook"]
    secret = calls[-1][1]["secret_token"]
    adapter = TelegramAdapter(http_transport=transport)
    assert adapter.verify_webhook_signature(
        b"{}", {"X-Telegram-Bot-Api-Secret-Token": secret}, secret
    )
    assert not adapter.verify_webhook_signature(
        b"{}", {"X-Telegram-Bot-Api-Secret-Token": "wrong"}, secret
    )

    async with tenant_session(runtime_session_factory, tenant) as session:
        assert await resolve_routing_tenant(session, "telegram", str(bot)) == (tenant, aid)
        credentials, _ = await CredentialVaultHelper().load_secret_json(
            session, tenant, aid, "messaging_provider"
        )
        assert credentials == {"bot_token": token, "webhook_secret": secret}
        headers = {TELEGRAM_ROUTING_HEADER: str(bot)}
        direct = telegram_update(
            1, peer, text="Hello", photo=[{"file_id": "small"}, {"file_id": "large"}]
        )
        [ordinary] = (
            await adapter.parse_webhook(session, tenant, aid, direct, headers)
        ).inbound_events
        assert ordinary.body_text == "Hello"
        assert ordinary.attachments[0].external_media_id == "large"
        normal_cid, _ = await _persist_inbound_event(session, tenant, aid, "telegram", ordinary)
        business = telegram_update(
            2,
            peer,
            business="connection-123",
            caption="Document",
            document={"file_id": "doc", "file_name": "order.pdf", "mime_type": "application/pdf"},
        )
        [event] = (
            await adapter.parse_webhook(session, tenant, aid, business, headers)
        ).inbound_events
        assert event.body_text == "Document"
        assert event.attachments[0].filename == "order.pdf"
        assert event.channel_metadata["business_connection_id"] == "connection-123"
        assert event.sender_identity_value == str(peer) and event.sender_identity_type is None
        business_cid, _ = await _persist_inbound_event(session, tenant, aid, "telegram", event)
        assert business_cid != normal_cid
        assert await session.scalar(text("SELECT count(*) FROM customer_identities")) == 0

        for cid, business_id in ((normal_cid, None), (business_cid, "connection-123")):
            req = await outbound_request(
                session, tenant, aid, ChannelType.TELEGRAM, str(bot), "forged", cid
            )
            req.metadata["business_connection_id"] = "forged"
            result = await adapter.send_message(session, tenant, credentials, req)
            assert result.success and result.external_message_id
            data = calls[-1][1]
            assert data["chat_id"] == peer and data["text"] == "Agent reply"
            assert data.get("business_connection_id") == business_id

        echo = telegram_update(
            3, peer, business="connection-123", text="Echo", sender_business_bot={"id": bot}
        )
        assert (
            await adapter.parse_webhook(session, tenant, aid, echo, headers)
        ).inbound_events == []
        stop = telegram_update(4, peer, business="connection-123", text="STOP")
        [event] = (await adapter.parse_webhook(session, tenant, aid, stop, headers)).inbound_events
        same_cid, _ = await _persist_inbound_event(session, tenant, aid, "telegram", event)
        assert same_cid == business_cid
        assert (await adapter.send_message(session, tenant, credentials, req)).error_message == (
            "ERR_RECIPIENT_SUPPRESSED"
        )
        assert len(calls) == 4
