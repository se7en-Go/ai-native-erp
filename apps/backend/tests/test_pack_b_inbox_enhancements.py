"""Pack B — inbox export communication enhancements (pinning/tags + attachments).

Covers:
* conversation pinning/tagging stored inside `conversations.channel_policy_state`
  (no Alembic revision) with pinned-first ordering, tag and account filters;
* the untouched AGENT private-customer isolation rules on every new entry point;
* inbound-workbench attachment upload/download (size + MIME gates, storage
  fallback) and outbound delivery metadata;
* `TelegramAdapter.send_message` dispatching `sendPhoto`/`sendDocument` as
  multipart uploads while the TEXT path stays on JSON `sendMessage`.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from erp_core.channels.adapters.base import OutboundMessageRequest
from erp_core.channels.inbox_router import _require_inbox_access, inbox_router
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal

RuntimeFactory = async_sessionmaker[AsyncSession]

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"pack-b-product-image" * 8
PDF_BYTES = b"%PDF-1.7\n" + b"pack-b-quotation" * 8


class FakeAttachmentStorage:
    """In-memory ObjectStorageService stand-in (no network, no MinIO dependency)."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.downloads: list[str] = []

    async def upload_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        self.objects[key] = (data, content_type)
        return key

    async def download_bytes(self, key: str) -> bytes:
        self.downloads.append(key)
        return self.objects[key][0]


class BrokenAttachmentStorage:
    """Simulates an unreachable S3/MinIO endpoint for the inline fallback path."""

    async def upload_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        raise RuntimeError("storage offline")

    async def download_bytes(self, key: str) -> bytes:
        raise RuntimeError("storage offline")


@dataclass
class InboxSeed:
    tenant_id: UUID
    agent_a: UUID
    agent_b: UUID
    telegram_account: UUID
    email_account: UUID
    personal_account: UUID
    customer_id: UUID
    conv_pinned: UUID
    conv_recent: UUID
    conv_email: UUID
    conv_private_b: UUID


@dataclass
class Harness:
    app: FastAPI
    enqueued: list[dict[str, Any]] = field(default_factory=list)


def principal_for(seed: InboxSeed, *, user: UUID, role: UserRole) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=user,
        tenant_id=seed.tenant_id,
        role=role,
        email=f"{user.hex[:8]}@factory.test",
    )


def build_app(
    *,
    seed: InboxSeed,
    session_factory: RuntimeFactory,
    redis: aioredis.Redis,
    principal: CurrentPrincipal,
    storage: Any,
) -> Harness:
    enqueued: list[dict[str, Any]] = []

    async def _fake_enqueue(req_dict: dict[str, Any]) -> None:
        enqueued.append(req_dict)

    app = FastAPI()
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.storage = storage
    app.state.outbound_enqueue_fn = _fake_enqueue
    app.include_router(inbox_router)
    app.dependency_overrides[_require_inbox_access] = lambda: principal
    return Harness(app=app, enqueued=enqueued)


@pytest.fixture
async def seeded_inbox(
    runtime_session_factory: RuntimeFactory,
) -> AsyncIterator[InboxSeed]:
    seed = InboxSeed(
        tenant_id=uuid4(),
        agent_a=uuid4(),
        agent_b=uuid4(),
        telegram_account=uuid4(),
        email_account=uuid4(),
        personal_account=uuid4(),
        customer_id=uuid4(),
        conv_pinned=uuid4(),
        conv_recent=uuid4(),
        conv_email=uuid4(),
        conv_private_b=uuid4(),
    )
    now = datetime.now(UTC).replace(microsecond=0)
    async with tenant_session(runtime_session_factory, seed.tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Pack B Inbox')"),
            {"id": seed.tenant_id, "slug": f"pack-b-{seed.tenant_id.hex[:10]}"},
        )
        for agent_id, agent_name in (
            (seed.agent_a, "Agent A"),
            (seed.agent_b, "Agent B"),
        ):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, :name, :ph)"
                ),
                {
                    "id": agent_id,
                    "email": f"pack-b-{agent_id.hex[:10]}@factory.test",
                    "name": agent_name,
                    "ph": "not-a-real-hash",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                    "VALUES (:id, :tid, :uid, 'agent')"
                ),
                {"id": uuid4(), "tid": seed.tenant_id, "uid": agent_id},
            )
        await session.execute(
            text(
                "INSERT INTO channel_accounts "
                "(id, tenant_id, channel_type, external_account_id, display_name, status, "
                "owner_user_id, visibility_scope) VALUES "
                "(:tg, :tid, 'telegram', :tg_ext, 'Telegram Sales', 'active', NULL, 'shared'), "
                "(:em, :tid, 'email', :em_ext, 'Email Sales', 'active', NULL, 'shared'), "
                "(:ps, :tid, 'telegram', :ps_ext, 'Agent B Private', 'active', :agent_b, "
                "'personal')"
            ),
            {
                "tg": seed.telegram_account,
                "em": seed.email_account,
                "ps": seed.personal_account,
                "tid": seed.tenant_id,
                "tg_ext": "8123456789",
                "em_ext": f"sales-{seed.tenant_id.hex[:8]}@inbound.example.com",
                "ps_ext": "8987654321",
                "agent_b": seed.agent_b,
            },
        )
        await session.execute(
            text(
                "INSERT INTO customers "
                "(id, tenant_id, customer_type, display_name, primary_phone_e164) "
                "VALUES (:cid, :tid, 'b2b_contact', 'Lena Fischer', '+491709999888')"
            ),
            {"cid": seed.customer_id, "tid": seed.tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO conversations "
                "(id, tenant_id, customer_id, channel_account_id, channel_type, "
                "external_thread_id, conversation_mode, status, unread_count, "
                "assigned_user_id, channel_policy_state, updated_at) VALUES "
                "(:pinned, :tid, :cid, :tg, 'telegram', '8123456789', 'b2c_support', 'open', 0, "
                "NULL, CAST(:pinned_state AS jsonb), :older), "
                "(:recent, :tid, :cid, :tg, 'telegram', '8123456789', 'b2c_support', 'open', 1, "
                "NULL, '{}'::jsonb, :newer), "
                "(:email, :tid, :cid, :em, 'email', 'lena@example.de', 'b2b_inquiry', 'open', 0, "
                "NULL, '{}'::jsonb, :oldest), "
                "(:private, :tid, :cid, :ps, 'telegram', '8987654321', 'b2c_support', 'open', 0, "
                ":agent_b, '{}'::jsonb, :older)"
            ),
            {
                "pinned": seed.conv_pinned,
                "recent": seed.conv_recent,
                "email": seed.conv_email,
                "private": seed.conv_private_b,
                "tid": seed.tenant_id,
                "cid": seed.customer_id,
                "tg": seed.telegram_account,
                "em": seed.email_account,
                "ps": seed.personal_account,
                "agent_b": seed.agent_b,
                "pinned_state": json.dumps(
                    {"is_pinned": True, "tags": ["vip", "urgent"], "ai_handoff": "kept"}
                ),
                "older": now - timedelta(minutes=30),
                "newer": now,
                "oldest": now - timedelta(minutes=60),
            },
        )
        # A private Agent-B conversation already carries a delivered attachment.
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, tenant_id, conversation_id, customer_id, channel_type, direction, "
                "sender_type, sender_id, content_type, body_text, attachments, "
                "delivery_status, source_timestamp) VALUES "
                "(:mid, :tid, :cid_conv, :cust, 'telegram', 'outbound', 'human_agent', "
                ":sender, 'media', 'confidential.pdf', CAST(:atts AS jsonb), 'sent', :ts)"
            ),
            {
                "mid": uuid4(),
                "tid": seed.tenant_id,
                "cid_conv": seed.conv_private_b,
                "cust": seed.customer_id,
                "sender": str(seed.agent_b),
                "atts": json.dumps(
                    [
                        {
                            "filename": "confidential.pdf",
                            "mime_type": "application/pdf",
                            "size_bytes": 4,
                            "storage_key": f"tenants/{seed.tenant_id}/inbox/private/doc.pdf",
                        }
                    ]
                ),
                "ts": now,
            },
        )
    try:
        yield seed
    finally:
        admin = create_async_engine(
            os.environ.get(
                "DATABASE_ADMIN_URL",
                "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
            )
        )
        try:
            async with admin.begin() as connection:
                await connection.execute(
                    text("DELETE FROM tenants WHERE id = :id"), {"id": seed.tenant_id}
                )
        finally:
            await admin.dispose()


@pytest.fixture
async def redis_client() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:
        yield client
    finally:
        await client.aclose()


async def policy_state(
    factory: RuntimeFactory, tenant_id: UUID, conversation_id: UUID
) -> dict[str, Any]:
    async with tenant_session(factory, tenant_id) as session:
        state = await session.scalar(
            text("SELECT channel_policy_state FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        )
    return dict(state or {})


async def next_pubsub_event(pubsub: Any, *, timeout: float = 5.0) -> dict[str, Any] | None:
    """Await the first real Pub/Sub payload, skipping subscribe confirmations."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = await pubsub.get_message(ignore_subscribe_messages=False, timeout=0.5)
        if message is not None and message.get("type") == "message":
            return dict(message)
    return None


async def items_of(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


@pytest.mark.asyncio
async def test_pinning_and_tags_survive_policy_state_and_drive_sorting(
    seeded_inbox: InboxSeed,
    runtime_session_factory: RuntimeFactory,
    redis_client: aioredis.Redis,
) -> None:
    seed = seeded_inbox
    principal = principal_for(seed, user=seed.agent_a, role=UserRole.MANAGER)
    harness = build_app(
        seed=seed,
        session_factory=runtime_session_factory,
        redis=redis_client,
        principal=principal,
        storage=FakeAttachmentStorage(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        # Pinned conversation sorts first even though it is the second-most recently
        # updated one, and the new read-model fields are exposed.
        listed = await items_of(await client.get("/api/v1/inbox/conversations"))
        assert [item["id"] for item in listed][:2] == [
            str(seed.conv_pinned),
            str(seed.conv_recent),
        ]
        first = listed[0]
        assert first["is_pinned"] is True
        assert first["tags"] == ["vip", "urgent"]
        assert listed[1]["is_pinned"] is False and listed[1]["tags"] == []

        # Tag normalisation: strip, case-insensitive dedupe, 32-char truncation, max 10.
        noisy = [f" tag-{index} " for index in range(5)] + ["TAG-0", "x" * 40, "", "   ", "tail"]
        patched = await client.patch(
            f"/api/v1/inbox/conversations/{seed.conv_recent}",
            json={"is_pinned": True, "tags": noisy},
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["is_pinned"] is True
        assert body["tags"] == [
            "tag-0",
            "tag-1",
            "tag-2",
            "tag-3",
            "tag-4",
            "x" * 32,
            "tail",
        ]
        assert all(len(tag) <= 32 for tag in body["tags"])

        # Both pinned conversations are now newest-first among the pinned block.
        listed = await items_of(await client.get("/api/v1/inbox/conversations"))
        assert [item["id"] for item in listed][:2] == [
            str(seed.conv_recent),
            str(seed.conv_pinned),
        ]

        # Patching tags alone must preserve pinning *and* every unrelated policy key.
        tagged = await client.patch(
            f"/api/v1/inbox/conversations/{seed.conv_pinned}",
            json={"tags": ["vip"]},
        )
        assert tagged.status_code == 200, tagged.text
        assert tagged.json()["is_pinned"] is True
        assert tagged.json()["tags"] == ["vip"]
        stored = await policy_state(runtime_session_factory, seed.tenant_id, seed.conv_pinned)
        assert stored == {"is_pinned": True, "tags": ["vip"], "ai_handoff": "kept"}

        # Unpinning leaves the tag list untouched.
        unpinned = await client.patch(
            f"/api/v1/inbox/conversations/{seed.conv_pinned}",
            json={"is_pinned": False},
        )
        assert unpinned.status_code == 200, unpinned.text
        assert unpinned.json()["is_pinned"] is False
        assert unpinned.json()["tags"] == ["vip"]

        # `tag`, `pinned_only` and `channel_account_id` filters.
        by_tag = await items_of(await client.get("/api/v1/inbox/conversations?tag=vip"))
        assert [item["id"] for item in by_tag] == [str(seed.conv_pinned)]
        unknown_tag = await items_of(
            await client.get("/api/v1/inbox/conversations?tag=does-not-exist")
        )
        assert unknown_tag == []

        pinned_only = await items_of(
            await client.get("/api/v1/inbox/conversations?pinned_only=true")
        )
        assert [item["id"] for item in pinned_only] == [str(seed.conv_recent)]

        by_account = await items_of(
            await client.get(f"/api/v1/inbox/conversations?channel_account_id={seed.email_account}")
        )
        assert [item["id"] for item in by_account] == [str(seed.conv_email)]

        # Filters compose: pinned_only + account + tag.
        composed = await items_of(
            await client.get(
                "/api/v1/inbox/conversations"
                f"?pinned_only=true&tag=tag-1&channel_account_id={seed.telegram_account}"
            )
        )
        assert [item["id"] for item in composed] == [str(seed.conv_recent)]


@pytest.mark.asyncio
async def test_pinning_and_tag_filters_still_hide_other_agents_private_chats(
    seeded_inbox: InboxSeed,
    runtime_session_factory: RuntimeFactory,
    redis_client: aioredis.Redis,
) -> None:
    seed = seeded_inbox
    principal = principal_for(seed, user=seed.agent_a, role=UserRole.AGENT)
    harness = build_app(
        seed=seed,
        session_factory=runtime_session_factory,
        redis=redis_client,
        principal=principal,
        storage=FakeAttachmentStorage(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        listed = await items_of(await client.get("/api/v1/inbox/conversations?pinned_only=false"))
        assert str(seed.conv_private_b) not in {item["id"] for item in listed}
        hidden_by_tag = await items_of(await client.get("/api/v1/inbox/conversations?tag=vip"))
        assert [item["id"] for item in hidden_by_tag] == [str(seed.conv_pinned)]
        hidden_by_account = await items_of(
            await client.get(
                f"/api/v1/inbox/conversations?channel_account_id={seed.personal_account}"
            )
        )
        assert hidden_by_account == []

        forbidden_patch = await client.patch(
            f"/api/v1/inbox/conversations/{seed.conv_private_b}",
            json={"is_pinned": True, "tags": ["stolen"]},
        )
        assert forbidden_patch.status_code == 403
        assert forbidden_patch.json()["detail"] == "ERR_CONVERSATION_ACCESS_DENIED"
        assert (
            await policy_state(runtime_session_factory, seed.tenant_id, seed.conv_private_b) == {}
        )


@pytest.mark.asyncio
async def test_attachment_upload_download_and_outbound_metadata(
    seeded_inbox: InboxSeed,
    runtime_session_factory: RuntimeFactory,
    redis_client: aioredis.Redis,
) -> None:
    seed = seeded_inbox
    storage = FakeAttachmentStorage()
    principal = principal_for(seed, user=seed.agent_a, role=UserRole.AGENT)
    harness = build_app(
        seed=seed,
        session_factory=runtime_session_factory,
        redis=redis_client,
        principal=principal,
        storage=storage,
    )
    channel = f"inbox:events:{seed.tenant_id}"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
        ) as client:
            uploaded = await client.post(
                f"/api/v1/inbox/conversations/{seed.conv_pinned}/attachments",
                data={"caption": "  Product photo for your quote  "},
                files={"file": ("Product Photo 1.png", PNG_BYTES, "image/png")},
            )
            assert uploaded.status_code == 201, uploaded.text
            body = uploaded.json()
            message = body["message"]
            assert body["queued_for_delivery"] is True
            assert message["content_type"] == "media"
            assert message["body_text"] == "Product photo for your quote"
            attachment = message["attachments"][0]
            assert attachment["filename"] == "Product_Photo_1.png"
            assert attachment["mime_type"] == "image/png"
            assert attachment["size_bytes"] == len(PNG_BYTES)
            expected_prefix = f"tenants/{seed.tenant_id}/inbox/{seed.conv_pinned}/"
            assert attachment["storage_key"].startswith(expected_prefix)
            assert attachment["download_url"] == (f"/api/v1/inbox/attachments/{message['id']}/0")
            assert storage.objects[attachment["storage_key"]] == (PNG_BYTES, "image/png")

            # Outbound worker payload carries the attachment for sendPhoto.
            assert len(harness.enqueued) == 1
            outbound = harness.enqueued[0]
            assert outbound["content_type"] == "media"
            assert outbound["recipient_id"] == "8123456789"
            assert outbound["body_text"] == "Product photo for your quote"
            queued_attachment = outbound["metadata"]["attachments"][0]
            assert queued_attachment["storage_key"] == attachment["storage_key"]
            assert queued_attachment["mime_type"] == "image/png"

            # SSE fan-out keeps the per-agent ownership envelope.
            event = await next_pubsub_event(pubsub)
            assert event is not None, "no inbox SSE event was published"
            payload = json.loads(event["data"])
            assert payload["event"] == "message.created"
            assert payload["conversation_id"] == str(seed.conv_pinned)
            assert payload["message_id"] == message["id"]
            assert payload["content_type"] == "media"
            assert payload["assigned_user_id"] is None
            assert payload["visibility_scope"] == "shared"

            # Download streams the stored bytes with the right headers.
            downloaded = await client.get(attachment["download_url"])
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.content == PNG_BYTES
            assert downloaded.headers["content-type"] == "image/png"
            assert "Product_Photo_1.png" in downloaded.headers["content-disposition"]
            assert attachment["storage_key"] in storage.downloads

            # A PDF without caption falls back to its filename and queues a document.
            pdf_upload = await client.post(
                f"/api/v1/inbox/conversations/{seed.conv_recent}/attachments",
                files={"file": ("quote-2026.pdf", PDF_BYTES, "application/pdf")},
            )
            assert pdf_upload.status_code == 201, pdf_upload.text
            pdf_body = pdf_upload.json()
            assert pdf_body["message"]["content_type"] == "media"
            assert pdf_body["message"]["body_text"] == "quote-2026.pdf"
            assert pdf_body["message"]["attachments"][0]["mime_type"] == "application/pdf"
            assert harness.enqueued[-1]["metadata"]["attachments"][0]["mime_type"] == (
                "application/pdf"
            )
            pdf_download = await client.get(pdf_body["message"]["attachments"][0]["download_url"])
            assert pdf_download.status_code == 200
            assert pdf_download.content == PDF_BYTES
            assert pdf_download.headers["content-type"] == "application/pdf"

            # MIME allow-list and 10 MB ceiling both fail closed with 422.
            rejected_mime = await client.post(
                f"/api/v1/inbox/conversations/{seed.conv_pinned}/attachments",
                files={"file": ("payload.zip", b"PK\x03\x04", "application/zip")},
            )
            assert rejected_mime.status_code == 422
            assert rejected_mime.json()["detail"] == "ERR_ATTACHMENT_TYPE_NOT_ALLOWED"
            oversized = await client.post(
                f"/api/v1/inbox/conversations/{seed.conv_pinned}/attachments",
                files={
                    "file": (
                        "huge.pdf",
                        b"0" * (10 * 1024 * 1024 + 1),
                        "application/pdf",
                    )
                },
            )
            assert oversized.status_code == 422
            assert oversized.json()["detail"] == "ERR_ATTACHMENT_TOO_LARGE"
            assert len(harness.enqueued) == 2

            # Unknown conversation / out-of-range index stay 404 (no tenant leakage).
            missing = await client.post(
                f"/api/v1/inbox/conversations/{uuid4()}/attachments",
                files={"file": ("a.png", PNG_BYTES, "image/png")},
            )
            assert missing.status_code == 404
            bad_index = await client.get(f"/api/v1/inbox/attachments/{message['id']}/4")
            assert bad_index.status_code == 404
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


@pytest.mark.asyncio
async def test_attachment_storage_outage_falls_back_to_inline_bytes(
    seeded_inbox: InboxSeed,
    runtime_session_factory: RuntimeFactory,
    redis_client: aioredis.Redis,
) -> None:
    seed = seeded_inbox
    harness = build_app(
        seed=seed,
        session_factory=runtime_session_factory,
        redis=redis_client,
        principal=principal_for(seed, user=seed.agent_a, role=UserRole.ADMIN),
        storage=BrokenAttachmentStorage(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app), base_url="http://test"
    ) as client:
        uploaded = await client.post(
            f"/api/v1/inbox/conversations/{seed.conv_recent}/attachments",
            files={"file": ("inline.png", PNG_BYTES, "image/png")},
        )
        assert uploaded.status_code == 201, uploaded.text
        attachment = uploaded.json()["message"]["attachments"][0]
        assert attachment["storage_key"] is None
        assert base64.b64decode(attachment["inline_bytes_b64"]) == PNG_BYTES
        assert harness.enqueued[0]["metadata"]["storage_fallback"] == "inline"

        downloaded = await client.get(attachment["download_url"])
        assert downloaded.status_code == 200
        assert downloaded.content == PNG_BYTES


@pytest.mark.asyncio
async def test_attachment_endpoints_enforce_agent_private_chat_isolation(
    seeded_inbox: InboxSeed,
    runtime_session_factory: RuntimeFactory,
    redis_client: aioredis.Redis,
) -> None:
    seed = seeded_inbox
    storage = FakeAttachmentStorage()
    private_key = f"tenants/{seed.tenant_id}/inbox/private/doc.pdf"
    storage.objects[private_key] = (PDF_BYTES, "application/pdf")
    async with tenant_session(runtime_session_factory, seed.tenant_id) as session:
        private_message_id = await session.scalar(
            text("SELECT id FROM messages WHERE conversation_id = :cid"),
            {"cid": seed.conv_private_b},
        )

    # Agent A may neither upload into nor download from Agent B's private chat.
    harness_a = build_app(
        seed=seed,
        session_factory=runtime_session_factory,
        redis=redis_client,
        principal=principal_for(seed, user=seed.agent_a, role=UserRole.AGENT),
        storage=storage,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness_a.app), base_url="http://test"
    ) as client:
        upload = await client.post(
            f"/api/v1/inbox/conversations/{seed.conv_private_b}/attachments",
            files={"file": ("intrusion.png", PNG_BYTES, "image/png")},
        )
        assert upload.status_code == 403
        assert upload.json()["detail"] == "ERR_CONVERSATION_ACCESS_DENIED"
        download = await client.get(f"/api/v1/inbox/attachments/{private_message_id}/0")
        assert download.status_code == 403
        assert download.json()["detail"] == "ERR_CONVERSATION_ACCESS_DENIED"
        assert harness_a.enqueued == []
        assert storage.objects.get(private_key) is not None

    # Agent B owns the conversation and can read the stored bytes back.
    harness_b = build_app(
        seed=seed,
        session_factory=runtime_session_factory,
        redis=redis_client,
        principal=principal_for(seed, user=seed.agent_b, role=UserRole.AGENT),
        storage=storage,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness_b.app), base_url="http://test"
    ) as client:
        allowed = await client.get(f"/api/v1/inbox/attachments/{private_message_id}/0")
        assert allowed.status_code == 200, allowed.text
        assert allowed.content == PDF_BYTES
        assert allowed.headers["content-type"] == "application/pdf"


def telegram_bot() -> tuple[str, str]:
    bot = str(uuid4().int)[:12]
    return bot, f"{bot}:pack-b-bot-token-xxxxxxxx"


async def telegram_outbound_context(
    session: AsyncSession,
    seed: InboxSeed,
    *,
    bot: str,
    conversation_id: UUID,
    content_type: str,
    body_text: str | None,
    attachments: list[dict[str, Any]],
) -> OutboundMessageRequest:
    message_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO messages (id, tenant_id, conversation_id, customer_id, channel_type, "
            "direction, sender_type, sender_id, content_type, body_text, attachments, "
            "delivery_status, source_timestamp) VALUES "
            "(:mid, :tid, :cid, :cust, 'telegram', 'outbound', 'human_agent', :sender, "
            ":ctype, :body, CAST(:atts AS jsonb), 'queued', now())"
        ),
        {
            "mid": message_id,
            "tid": seed.tenant_id,
            "cid": conversation_id,
            "cust": seed.customer_id,
            "sender": str(seed.agent_a),
            "ctype": content_type,
            "body": body_text,
            "atts": json.dumps(attachments),
        },
    )
    await session.execute(
        text("UPDATE channel_accounts SET external_account_id = :bot WHERE id = :aid"),
        {"bot": bot, "aid": seed.telegram_account},
    )
    return OutboundMessageRequest(
        tenant_id=seed.tenant_id,
        channel_account_id=seed.telegram_account,
        channel_type="telegram",
        external_account_id=bot,
        recipient_id="8123456789",
        conversation_id=conversation_id,
        message_id=message_id,
        content_type=content_type,  # type: ignore[arg-type]
        body_text=body_text,
        metadata={"attachments": attachments},
    )


@pytest.mark.asyncio
async def test_telegram_send_message_dispatches_media_and_keeps_text_path(
    seeded_inbox: InboxSeed,
    runtime_session_factory: RuntimeFactory,
) -> None:
    from erp_core.channels.adapters.telegram import TelegramAdapter

    seed = seeded_inbox
    bot, token = telegram_bot()
    storage = FakeAttachmentStorage()
    photo_key = f"tenants/{seed.tenant_id}/inbox/{seed.conv_pinned}/photo.png"
    document_key = f"tenants/{seed.tenant_id}/inbox/{seed.conv_pinned}/quote.pdf"
    storage.objects[photo_key] = (PNG_BYTES, "image/png")
    storage.objects[document_key] = (PDF_BYTES, "application/pdf")

    calls: list[tuple[str, httpx.Request]] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, request))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 321}})

    adapter = TelegramAdapter(
        http_transport=httpx.MockTransport(telegram), attachment_storage=storage
    )
    credentials = {"bot_token": token}

    async with tenant_session(runtime_session_factory, seed.tenant_id) as session:
        photo_req = await telegram_outbound_context(
            session,
            seed,
            bot=bot,
            conversation_id=seed.conv_pinned,
            content_type="media",
            body_text="Product photo for your quote",
            attachments=[
                {
                    "filename": "product.png",
                    "mime_type": "image/png",
                    "size_bytes": len(PNG_BYTES),
                    "storage_key": photo_key,
                }
            ],
        )
        photo_result = await adapter.send_message(session, seed.tenant_id, credentials, photo_req)
        assert photo_result.success, photo_result.error_message
        assert photo_result.external_message_id == f"{bot}:8123456789:321"
        method, request = calls[-1]
        assert method == "sendPhoto"
        assert request.method == "POST"
        assert request.headers["content-type"].startswith("multipart/form-data")
        body = request.read()
        assert b'name="chat_id"' in body and b"8123456789" in body
        assert b'name="caption"' in body and b"Product photo for your quote" in body
        assert b'name="photo"; filename="product.png"' in body
        assert b"Content-Type: image/png" in body
        assert PNG_BYTES in body
        assert photo_key in storage.downloads

        document_req = await telegram_outbound_context(
            session,
            seed,
            bot=bot,
            conversation_id=seed.conv_pinned,
            content_type="media",
            body_text="quote-2026.pdf",
            attachments=[
                {
                    "filename": "quote-2026.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": len(PDF_BYTES),
                    "storage_key": document_key,
                }
            ],
        )
        document_result = await adapter.send_message(
            session, seed.tenant_id, credentials, document_req
        )
        assert document_result.success, document_result.error_message
        method, request = calls[-1]
        assert method == "sendDocument"
        body = request.read()
        assert b'name="document"; filename="quote-2026.pdf"' in body
        assert b"Content-Type: application/pdf" in body
        assert PDF_BYTES in body

        # The legacy TEXT path is untouched: still JSON `sendMessage`, no multipart.
        text_req = await telegram_outbound_context(
            session,
            seed,
            bot=bot,
            conversation_id=seed.conv_pinned,
            content_type="text",
            body_text="We can ship within 7 days.",
            attachments=[],
        )
        text_result = await adapter.send_message(session, seed.tenant_id, credentials, text_req)
        assert text_result.success, text_result.error_message
        method, request = calls[-1]
        assert method == "sendMessage"
        assert request.headers["content-type"].startswith("application/json")
        assert json.loads(request.read()) == {
            "chat_id": 8123456789,
            "text": "We can ship within 7 days.",
        }

        # Business-connection threads forward `business_connection_id` on media sends.
        business_conv = uuid4()
        await session.execute(
            text(
                "INSERT INTO conversations "
                "(id, tenant_id, customer_id, channel_account_id, channel_type, "
                "external_thread_id, conversation_mode, status, unread_count, updated_at) "
                "VALUES (:cid, :tid, :cust, :aid, 'telegram', :thread, 'b2c_support', "
                "'open', 0, now())"
            ),
            {
                "cid": business_conv,
                "tid": seed.tenant_id,
                "cust": seed.customer_id,
                "aid": seed.telegram_account,
                "thread": "business:conn-999:8123456789",
            },
        )
        business_req = await telegram_outbound_context(
            session,
            seed,
            bot=bot,
            conversation_id=business_conv,
            content_type="media",
            body_text="Business invoice",
            attachments=[
                {
                    "filename": "invoice.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": len(PDF_BYTES),
                    "storage_key": document_key,
                }
            ],
        )
        business_result = await adapter.send_message(
            session, seed.tenant_id, credentials, business_req
        )
        assert business_result.success, business_result.error_message
        assert business_result.external_message_id == f"{bot}:business:conn-999:8123456789:321"
        method, request = calls[-1]
        assert method == "sendDocument"
        business_body = request.read()
        assert b'name="business_connection_id"' in business_body
        assert b"conn-999" in business_body

        # Attachment bytes that vanished from storage fail closed, never silently.
        missing_req = await telegram_outbound_context(
            session,
            seed,
            bot=bot,
            conversation_id=seed.conv_pinned,
            content_type="media",
            body_text=None,
            attachments=[
                {
                    "filename": "gone.png",
                    "mime_type": "image/png",
                    "size_bytes": 4,
                    "storage_key": "tenants/missing/gone.png",
                }
            ],
        )
        missing_result = await adapter.send_message(
            session, seed.tenant_id, credentials, missing_req
        )
        assert missing_result.success is False
        assert missing_result.error_message == "ERR_ATTACHMENT_UNAVAILABLE"
        assert [method for method, _ in calls] == [
            "sendPhoto",
            "sendDocument",
            "sendMessage",
            "sendDocument",
        ]
