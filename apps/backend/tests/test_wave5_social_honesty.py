"""Wave 5 honesty fixes verified on real PostgreSQL 17.

Three audit findings are covered here, all of them about *not lying*:

* a `published` social post says exactly how far its delivery got
  (`delivery_mode` + `external_publish_confirmed`) instead of implying an outbound
  publish that never happened;
* a social mention maps to a customer by `(platform, lower(author_handle))` — proven
  by a prior lead — never by the mutable, collision-prone `customers.display_name`;
* re-running AI profiling rewrites the single "AI customer profile:" follow-up note
  in place instead of spamming one note per run.

Nothing is mocked at the data layer: every assertion is made against rows written
inside a tenant-scoped RLS transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Configure ORM modules so string FKs (tenants/users/customers/leads) resolve.
import erp_core.crm.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.social.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.ai_engine.auto_reply_service import AutoReplyService
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.social.router import router as social_router
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
BASE = "/api/v1/social"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def client_for(factory: Factory, principal: CurrentPrincipal) -> AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.state.crypto_service = EnvelopeCryptoService(get_settings())
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(social_router, prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def workspace(runtime_session_factory: Factory) -> dict[str, Any]:
    tenant, other = uuid4(), uuid4()
    owner, manager, agent = uuid4(), uuid4(), uuid4()
    async with tenant_session(runtime_session_factory, tenant) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Wave 5 Social')"),
            {"id": tenant, "slug": f"wave5-social-{tenant.hex[:10]}"},
        )
        for uid, role in (
            (owner, UserRole.OWNER),
            (manager, UserRole.MANAGER),
            (agent, UserRole.AGENT),
        ):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, :name, 'test')"
                ),
                {"id": uid, "email": f"{uid.hex}@wave5-social.example.com", "name": role.value},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (:tid, :uid, :role)"
                ),
                {"tid": tenant, "uid": uid, "role": role.value},
            )
    async with tenant_session(runtime_session_factory, other) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Wave 5 Other')"),
            {"id": other, "slug": f"wave5-other-{other.hex[:10]}"},
        )
    return {
        "factory": runtime_session_factory,
        "tenant": tenant,
        "other_tenant": other,
        "owner": CurrentPrincipal(
            user_id=owner, tenant_id=tenant, role=UserRole.OWNER, email="owner@wave5.example.com"
        ),
        "manager": CurrentPrincipal(
            user_id=manager,
            tenant_id=tenant,
            role=UserRole.MANAGER,
            email="manager@wave5.example.com",
        ),
        "agent": CurrentPrincipal(
            user_id=agent, tenant_id=tenant, role=UserRole.AGENT, email="agent@wave5.example.com"
        ),
    }


async def _connect_instagram(client: AsyncClient, handle: str = "@lumenlight") -> dict[str, Any]:
    response = await client.post(
        f"{BASE}/accounts/connect",
        json={"platform": "instagram", "handle": handle, "display_name": "Lumen Light"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_post(
    client: AsyncClient,
    platform: str,
    caption: str,
    *,
    status: str = "draft",
) -> dict[str, Any]:
    response = await client.post(
        f"{BASE}/posts", json={"platform": platform, "caption": caption, "status": status}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 1. publish honesty
# --------------------------------------------------------------------------- #
async def test_publish_without_external_post_id_is_recorded_locally(workspace) -> None:
    factory, owner = workspace["factory"], workspace["owner"]
    async with client_for(factory, owner) as client:
        await _connect_instagram(client)
        post = await _create_post(client, "instagram", "Factory tour reel")

        # A draft is honest by default: nothing was delivered.
        assert post["delivery_mode"] == "recorded_local"
        assert post["external_publish_confirmed"] is False

        published = await client.post(f"{BASE}/posts/{post['id']}/publish", json={})
        assert published.status_code == 200, published.text
        body = published.json()
        assert body["status"] == "published"
        assert body["external_post_id"] is None
        # API platform, but no platform-side id came back -> nothing provably left.
        assert body["delivery_mode"] == "recorded_local"
        assert body["external_publish_confirmed"] is False
        assert body["metrics"]["publish_channel"] == "api"

        # The retried publish is idempotent and keeps telling the same truth.
        again = await client.post(f"{BASE}/posts/{post['id']}/publish", json={})
        assert again.status_code == 200
        assert again.json()["delivery_mode"] == "recorded_local"
        assert again.json()["external_publish_confirmed"] is False

        listed = (await client.get(f"{BASE}/posts")).json()["items"]
        assert [item["delivery_mode"] for item in listed] == ["recorded_local"]
        assert [item["external_publish_confirmed"] for item in listed] == [False]


async def test_publish_with_external_post_id_confirms_external_delivery(workspace) -> None:
    factory, owner = workspace["factory"], workspace["owner"]
    async with client_for(factory, owner) as client:
        await _connect_instagram(client)
        post = await _create_post(client, "instagram", "Workshop light reel")
        published = await client.post(
            f"{BASE}/posts/{post['id']}/publish",
            json={"external_post_id": "ig_media_1789456"},
        )
        assert published.status_code == 200, published.text
        body = published.json()
        assert body["external_post_id"] == "ig_media_1789456"
        assert body["delivery_mode"] == "external_api"
        assert body["external_publish_confirmed"] is True
        assert body["metrics"]["publish_channel"] == "api"

        listed = (await client.get(f"{BASE}/posts", params={"status": "published"})).json()
        assert listed["items"][0]["delivery_mode"] == "external_api"
        assert listed["items"][0]["external_publish_confirmed"] is True

    async with tenant_session(factory, workspace["tenant"]) as session:
        metrics = await session.scalar(text("SELECT metrics FROM social_posts"))
        assert metrics["delivery_mode"] == "external_api"
        assert metrics["external_publish_confirmed"] is True


async def test_manual_export_publish_is_reported_as_manual_confirmation(workspace) -> None:
    factory, owner = workspace["factory"], workspace["owner"]
    async with client_for(factory, owner) as client:
        post = await _create_post(client, "xiaohongshu", "工地照明怎么选，看这三点。")
        blocked = await client.post(f"{BASE}/posts/{post['id']}/publish", json={})
        assert blocked.status_code == 422
        assert blocked.json()["code"] == "ERR_SOCIAL_MANUAL_PUBLISH_ONLY"

        confirmed = await client.post(
            f"{BASE}/posts/{post['id']}/publish", json={"manual_confirmation": True}
        )
        assert confirmed.status_code == 200, confirmed.text
        body = confirmed.json()
        assert body["status"] == "published"
        # No external id: the human published in the official console and confirmed it.
        assert body["delivery_mode"] == "manual_confirmed"
        assert body["external_publish_confirmed"] is False
        assert body["metrics"]["publish_channel"] == "manual_console"

        # A manually confirmed publish that carries the console-side id is confirmed.
        other_post = await _create_post(client, "douyin", "工厂实拍短视频")
        with_id = await client.post(
            f"{BASE}/posts/{other_post['id']}/publish",
            json={"manual_confirmation": True, "external_post_id": "dy_9912"},
        )
        assert with_id.status_code == 200, with_id.text
        assert with_id.json()["delivery_mode"] == "manual_confirmed"
        assert with_id.json()["external_publish_confirmed"] is True

        draft = await _create_post(client, "douyin", "草稿：还没发")
        assert draft["delivery_mode"] == "recorded_local"
        assert draft["external_publish_confirmed"] is False


# --------------------------------------------------------------------------- #
# 2. stable mention author identity
# --------------------------------------------------------------------------- #
async def _mention(client: AsyncClient, platform: str, handle: str) -> str:
    response = await client.post(
        f"{BASE}/mentions",
        json={
            "platform": platform,
            "author_handle": handle,
            "content_text": "Where can I buy 500 pcs of the COB strip?",
            "sentiment": "lead_intent",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _convert(client: AsyncClient, mention_id: str) -> dict[str, Any]:
    response = await client.post(f"{BASE}/mentions/{mention_id}/convert-lead")
    assert response.status_code == 200, response.text
    return response.json()


async def test_mention_conversion_reuses_customer_per_platform_and_handle(workspace) -> None:
    factory, tenant, owner = workspace["factory"], workspace["tenant"], workspace["owner"]
    async with client_for(factory, owner) as client:
        first = await _convert(client, await _mention(client, "instagram", "@Alex.Lumen"))
        # Same platform + same handle, different casing -> the same person.
        second = await _convert(client, await _mention(client, "instagram", "@alex.lumen"))
        assert second["customer_id"] == first["customer_id"]
        assert second["lead_id"] != first["lead_id"]

        # Same handle on a DIFFERENT platform is a different person.
        tiktok = await _convert(client, await _mention(client, "tiktok", "@alex.lumen"))
        assert tiktok["customer_id"] != first["customer_id"]

    async with tenant_session(factory, tenant) as session:
        assert await session.scalar(text("SELECT count(*) FROM customers")) == 2
        assert await session.scalar(text("SELECT count(*) FROM leads")) == 3
        platforms = (
            await session.execute(
                text(
                    "SELECT DISTINCT extracted_requirements->>'platform' FROM leads "
                    "WHERE source_channel = 'social_mention'"
                )
            )
        ).scalars()
        assert set(platforms) == {"instagram", "tiktok"}


async def test_mention_conversion_never_collides_with_unrelated_display_name(workspace) -> None:
    factory, tenant, owner = workspace["factory"], workspace["tenant"], workspace["owner"]
    # A pre-existing B2B customer whose display name happens to equal a social handle.
    unrelated_id = uuid4()
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2b_contact', '@weibo_watch')"
            ),
            {"id": unrelated_id, "tid": tenant},
        )

    async with client_for(factory, owner) as client:
        converted = await _convert(client, await _mention(client, "weibo", "@weibo_watch"))
        assert UUID(converted["customer_id"]) != unrelated_id
        # A second mention of the same handle on the same platform reuses the new row.
        again = await _convert(client, await _mention(client, "weibo", "@Weibo_Watch"))
        assert again["customer_id"] == converted["customer_id"]

    async with tenant_session(factory, tenant) as session:
        assert await session.scalar(text("SELECT count(*) FROM customers")) == 2
        social_customer = (
            await session.execute(
                text(
                    "SELECT c.display_name FROM customers c "
                    "JOIN leads l ON l.customer_id = c.id AND l.tenant_id = c.tenant_id "
                    "WHERE l.source_channel = 'social_mention' LIMIT 1"
                )
            )
        ).scalar()
        assert social_customer == "@weibo_watch"


# --------------------------------------------------------------------------- #
# 3. idempotent AI profile follow-up
# --------------------------------------------------------------------------- #
async def _seed_profiling_conversation(factory: Factory, tenant: UUID, owner: UUID) -> UUID:
    """One channel account, one customer, one conversation with an inbound enquiry."""
    account_id, customer_id, conversation_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC).replace(microsecond=0)
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text(
                "INSERT INTO channel_accounts "
                "(id, tenant_id, channel_type, external_account_id, display_name, status, "
                "owner_user_id, visibility_scope) "
                "VALUES (:id, :tid, 'email', :ext, 'Email Sales', 'active', NULL, 'shared')"
            ),
            {"id": account_id, "tid": tenant, "ext": f"sales-{tenant.hex[:8]}@inbound.test"},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2b_contact', 'Lena Fischer')"
            ),
            {"id": customer_id, "tid": tenant},
        )
        await session.execute(
            text(
                "INSERT INTO conversations "
                "(id, tenant_id, customer_id, channel_account_id, channel_type, "
                "external_thread_id, conversation_mode, status, unread_count, "
                "assigned_user_id, channel_policy_state, updated_at) "
                "VALUES (:id, :tid, :cid, :aid, 'email', :thread, 'b2b_inquiry', 'open', 0, "
                "NULL, '{}'::jsonb, :now)"
            ),
            {
                "id": conversation_id,
                "tid": tenant,
                "cid": customer_id,
                "aid": account_id,
                "thread": f"lena-{tenant.hex[:8]}@example.de",
                "now": now,
            },
        )
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, tenant_id, conversation_id, customer_id, channel_type, direction, "
                "sender_type, sender_id, content_type, body_text, attachments, "
                "delivery_status, source_timestamp) "
                "VALUES (:id, :tid, :conv, :cid, 'email', 'inbound', 'customer', :sender, "
                "'text', :body, '[]'::jsonb, 'delivered', :ts)"
            ),
            {
                "id": uuid4(),
                "tid": tenant,
                "conv": conversation_id,
                "cid": customer_id,
                "sender": f"lena-{tenant.hex[:8]}@example.de",
                "body": "We are a distributor in Germany, quote 5000 m of LED-COB24V-IP67 FOB.",
                "ts": now - timedelta(minutes=5),
            },
        )
    return conversation_id


async def test_extract_profile_updates_the_single_followup_in_place(workspace) -> None:
    factory, tenant, owner = workspace["factory"], workspace["tenant"], workspace["owner"]
    conversation_id = await _seed_profiling_conversation(factory, tenant, owner.user_id)
    service = AutoReplyService()

    async with tenant_session(factory, tenant) as session:
        first = await service.extract_profile(session, owner, conversation_id)
    assert first.synced_followup_id is not None

    async with tenant_session(factory, tenant) as session:
        second = await service.extract_profile(session, owner, conversation_id)
    assert second.synced_followup_id == first.synced_followup_id

    async with tenant_session(factory, tenant) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id, followup_type, summary, author_user_id, next_action_at "
                        "FROM customer_followups WHERE tenant_id = :tid"
                    ),
                    {"tid": tenant},
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == first.synced_followup_id
    assert row["followup_type"] == "note"
    assert row["summary"].startswith("AI customer profile:")
    assert row["author_user_id"] == owner.user_id
    # A qualified enquiry keeps the one-day next-action window after the in-place update.
    assert row["next_action_at"] is not None
    assert first.purchase_intent_score >= 40
