"""Phase 3D social marketing APIs verified against real PostgreSQL and real crypto.

Nothing here reaches the network: AI copy generation runs through a deterministic
in-process LLM provider stub, and publishing never performs an outbound post.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Configure every ORM module so string FKs (tenants/users/customers/leads) resolve
# in this isolated test app, exactly like the api_main composition root.
import erp_core.crm.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.social.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.ai_engine.router_llm import LlmProviderResponse, TieredModelRouter
from erp_core.ai_engine.social_caption import AiEngineCaptionProvider
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.social.router import router as social_router
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
BASE = "/api/v1/social"
STUB_CAPTION = "Workshop-ready IP65 work light, FOB pricing shared on request."
SECRET_TOKEN = "IGQVJ-demo-social-oauth-token-never-echoed"


class StubProvider:
    """Deterministic stand-in for a configured LLM provider (no network, no API key)."""

    def __init__(self, text: str = STUB_CAPTION) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        self.calls.append({"model_id": model_id, "provider": provider, "messages": messages})
        return LlmProviderResponse(
            text=self.text,
            prompt_tokens=180,
            cached_tokens=0,
            completion_tokens=60,
            model_id=model_id,
            provider=provider,
        )


def client_for(
    factory: Factory,
    principal: CurrentPrincipal,
    *,
    provider: StubProvider | None = None,
) -> AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.state.crypto_service = EnvelopeCryptoService(get_settings())
    if provider is not None:
        app.state.social_caption_provider = AiEngineCaptionProvider(
            llm_router=TieredModelRouter(provider_client=provider)
        )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(social_router, prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def workspace(runtime_session_factory: Factory):
    tenant, other = uuid4(), uuid4()
    owner, manager, agent, finance, outsider = (uuid4() for _ in range(5))
    for tid in (tenant, other):
        async with tenant_session(runtime_session_factory, tid) as session:
            await session.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Social')"),
                {"id": tid, "slug": f"social-{tid.hex}"},
            )
    async with tenant_session(runtime_session_factory, tenant) as session:
        for uid, role in (
            (owner, UserRole.OWNER),
            (manager, UserRole.MANAGER),
            (agent, UserRole.AGENT),
            (finance, UserRole.FINANCE),
        ):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, :name, 'test')"
                ),
                {"id": uid, "email": f"{uid.hex}@social.example.com", "name": role.value},
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
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, 'outsider', 'test')"
            ),
            {"id": outsider, "email": f"{outsider.hex}@social.example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                "VALUES (:tid, :uid, 'owner')"
            ),
            {"tid": other, "uid": outsider},
        )
    yield {
        "factory": runtime_session_factory,
        "tenant": tenant,
        "other_tenant": other,
        "owner": CurrentPrincipal(
            user_id=owner, tenant_id=tenant, role=UserRole.OWNER, email="owner@social.example.com"
        ),
        "manager": CurrentPrincipal(
            user_id=manager,
            tenant_id=tenant,
            role=UserRole.MANAGER,
            email="manager@social.example.com",
        ),
        "agent": CurrentPrincipal(
            user_id=agent, tenant_id=tenant, role=UserRole.AGENT, email="agent@social.example.com"
        ),
        "finance": CurrentPrincipal(
            user_id=finance,
            tenant_id=tenant,
            role=UserRole.FINANCE,
            email="finance@social.example.com",
        ),
        "outsider": CurrentPrincipal(
            user_id=outsider,
            tenant_id=other,
            role=UserRole.OWNER,
            email="outsider@social.example.com",
        ),
    }


async def _connect_instagram(client: AsyncClient, handle: str = "@lumenlight") -> dict[str, Any]:
    response = await client.post(
        f"{BASE}/accounts/connect",
        json={
            "platform": "instagram",
            "handle": handle,
            "display_name": "Lumen Light",
            "followers_count": 12345,
            "access_token": SECRET_TOKEN,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_platform_catalog_ships_fourteen_playbooks_with_honest_publish_modes(
    workspace,
) -> None:
    async with client_for(workspace["factory"], workspace["agent"]) as client:
        response = await client.get(f"{BASE}/platforms")
    assert response.status_code == 200, response.text
    platforms = response.json()
    assert len(platforms) == 14
    assert {item["platform"] for item in platforms} == {
        "instagram",
        "tiktok",
        "facebook",
        "youtube",
        "linkedin",
        "pinterest",
        "x",
        "xiaohongshu",
        "douyin",
        "kuaishou",
        "bilibili",
        "wechat_video",
        "wechat_oa",
        "weibo",
    }
    by_platform = {item["platform"]: item for item in platforms}
    assert len([item for item in platforms if item["region"] == "domestic"]) == 7
    assert len([item for item in platforms if item["region"] == "overseas"]) == 7
    # Domestic networks without a public publishing API are declared manual honestly.
    assert by_platform["xiaohongshu"]["publish_mode"] == "manual_export"
    assert by_platform["xiaohongshu"]["supports_scheduled_publish"] is False
    assert by_platform["wechat_video"]["supports_scheduled_publish"] is False
    assert by_platform["instagram"]["publish_mode"] == "api"
    assert by_platform["instagram"]["supports_scheduled_publish"] is True
    assert by_platform["wechat_oa"]["publish_mode"] == "api"
    assert by_platform["instagram"]["region"] == "overseas"
    assert by_platform["weibo"]["region"] == "domestic"
    for item in platforms:
        assert item["audience"]
        assert item["acquisition_plays"] and item["best_practices"] and item["content_formats"]
        assert item["compliance_note"]
        assert item["label"] and item["emoji"]


async def test_account_connect_encrypts_oauth_token_and_never_echoes_it(workspace) -> None:
    factory, tenant, owner = workspace["factory"], workspace["tenant"], workspace["owner"]
    async with client_for(factory, owner) as client:
        created = await _connect_instagram(client)
        assert SECRET_TOKEN not in str(created)
        assert "access_token" not in created
        assert "token_ciphertext" not in created
        assert created["platform"] == "instagram"
        assert created["region"] == "overseas"
        assert created["status"] == "connected"
        assert created["has_stored_token"] is True

        # Re-authorization of the same (platform, handle) is an upsert, not a duplicate:
        # the matrix "re-authorize" button posts the same handle with a fresh token.
        reauthorized = await client.post(
            f"{BASE}/accounts/connect",
            json={
                "platform": "instagram",
                "handle": "@lumenlight",
                "display_name": "Lumen Light Reels",
                "followers_count": 12400,
                "access_token": SECRET_TOKEN + "-rotated",
            },
        )
        assert reauthorized.status_code == 200, reauthorized.text
        assert reauthorized.json()["id"] == created["id"]
        assert reauthorized.json()["display_name"] == "Lumen Light Reels"
        assert reauthorized.json()["status"] == "connected"
        assert reauthorized.json()["followers_count"] == 12400

        listed = await client.get(f"{BASE}/accounts")
        assert listed.status_code == 200
        assert SECRET_TOKEN not in listed.text
        assert listed.json()["total"] == 1

        domestic = await client.post(
            f"{BASE}/accounts/connect",
            json={"platform": "xiaohongshu", "handle": "流明照明", "display_name": "流明照明"},
        )
        assert domestic.status_code == 201, domestic.text
        assert domestic.json()["region"] == "domestic"
        assert domestic.json()["has_stored_token"] is False

        filtered = await client.get(f"{BASE}/accounts", params={"platform": "xiaohongshu"})
        assert [item["handle"] for item in filtered.json()["items"]] == ["流明照明"]

    account_id = UUID(created["id"])
    async with tenant_session(factory, tenant) as session:
        row = (
            await session.execute(
                text(
                    "SELECT token_ciphertext, token_nonce, token_auth_tag, token_key_version "
                    "FROM social_accounts WHERE id = :id"
                ),
                {"id": account_id},
            )
        ).one()
        ciphertext, nonce, auth_tag, key_version = row
        assert ciphertext and nonce and auth_tag and key_version == 1
        assert SECRET_TOKEN.encode() not in bytes(ciphertext)
        # The AAD contract is what makes the ciphertext bound to this tenant + account.
        decryptor = EnvelopeCryptoService(get_settings())
        async with tenant_session(factory, tenant) as decrypt_session:
            plaintext = await decryptor.decrypt_secret(
                decrypt_session,
                tenant,
                account_id,
                "social_oauth",
                bytes(ciphertext),
                bytes(nonce),
                bytes(auth_tag),
                int(key_version),
            )
        # The upsert rewrote the envelope with the rotated token.
        assert plaintext == SECRET_TOKEN + "-rotated"

    async with client_for(factory, owner) as client:
        disconnected = await client.post(f"{BASE}/accounts/{account_id}/disconnect")
        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json()["status"] == "disconnected"
        assert disconnected.json()["has_stored_token"] is False
    async with tenant_session(factory, tenant) as session:
        assert (
            await session.scalar(
                text("SELECT token_ciphertext FROM social_accounts WHERE id = :id"),
                {"id": account_id},
            )
            is None
        )


async def test_manual_publish_platforms_reject_scheduled_posts(workspace) -> None:
    factory, owner = workspace["factory"], workspace["owner"]
    async with client_for(factory, owner) as client:
        manual = await client.post(
            f"{BASE}/posts",
            json={
                "platform": "xiaohongshu",
                "title": "工地照明笔记",
                "caption": "工地照明怎么选，看这三点。",
                "status": "scheduled",
                "scheduled_at": "2026-11-20T09:30:00Z",
            },
        )
        assert manual.status_code == 422, manual.text
        assert manual.json()["code"] == "ERR_SOCIAL_MANUAL_PUBLISH_ONLY"

        missing_time = await client.post(
            f"{BASE}/posts",
            json={"platform": "instagram", "caption": "IP65 work light", "status": "scheduled"},
        )
        assert missing_time.status_code == 422, missing_time.text
        assert missing_time.json()["code"] == "ERR_SOCIAL_SCHEDULE_TIME_REQUIRED"

        scheduled = await client.post(
            f"{BASE}/posts",
            json={
                "platform": "instagram",
                "title": "Work light reel",
                "caption": "IP65 work light for the workshop",
                "hashtags": ["#worklight", "#lighting"],
                "sku_ref": "LED-WORK-100",
                "status": "scheduled",
                "scheduled_at": "2026-11-20T09:30:00Z",
            },
        )
        assert scheduled.status_code == 201, scheduled.text
        body = scheduled.json()
        assert body["status"] == "scheduled"
        assert body["hashtags"] == ["#worklight", "#lighting"]
        assert body["sku_ref"] == "LED-WORK-100"

        draft = await client.post(
            f"{BASE}/posts",
            json={"platform": "xiaohongshu", "caption": "先存成草稿，人工到创作中心发布。"},
        )
        assert draft.status_code == 201, draft.text

        promote = await client.patch(
            f"{BASE}/posts/{draft.json()['id']}",
            json={"status": "scheduled", "scheduled_at": "2026-11-21T09:30:00Z"},
        )
        assert promote.status_code == 422, promote.text
        assert promote.json()["code"] == "ERR_SOCIAL_MANUAL_PUBLISH_ONLY"

        edited = await client.patch(
            f"{BASE}/posts/{body['id']}",
            json={"caption": "IP65 work light, factory direct", "hashtags": ["#worklight"]},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["caption"] == "IP65 work light, factory direct"
        assert edited.json()["hashtags"] == ["#worklight"]

    async with tenant_session(factory, workspace["tenant"]) as session:
        assert await session.scalar(text("SELECT count(*) FROM social_posts")) == 2


async def test_publish_requires_a_connected_api_account_and_records_manual_confirmation(
    workspace,
) -> None:
    factory, owner = workspace["factory"], workspace["owner"]
    async with client_for(factory, owner) as client:
        api_post = await client.post(
            f"{BASE}/posts",
            json={
                "platform": "instagram",
                "caption": "Factory tour reel",
                "status": "scheduled",
                "scheduled_at": "2026-11-20T09:30:00Z",
            },
        )
        assert api_post.status_code == 201, api_post.text

        blocked = await client.post(f"{BASE}/posts/{api_post.json()['id']}/publish", json={})
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "ERR_SOCIAL_ACCOUNT_NOT_CONNECTED"

        await _connect_instagram(client)
        published = await client.post(f"{BASE}/posts/{api_post.json()['id']}/publish", json={})
        assert published.status_code == 200, published.text
        body = published.json()
        assert body["status"] == "published"
        assert body["published_at"] is not None
        # No unauthorized outbound publish ever happens: no fabricated external id.
        assert body["external_post_id"] is None

        again = await client.post(f"{BASE}/posts/{api_post.json()['id']}/publish", json={})
        assert again.status_code == 200
        assert again.json()["published_at"] == body["published_at"]

        manual_post = await client.post(
            f"{BASE}/posts", json={"platform": "douyin", "caption": "工厂实拍短视频"}
        )
        assert manual_post.status_code == 201
        manual_blocked = await client.post(
            f"{BASE}/posts/{manual_post.json()['id']}/publish", json={}
        )
        assert manual_blocked.status_code == 422
        assert manual_blocked.json()["code"] == "ERR_SOCIAL_MANUAL_PUBLISH_ONLY"

        confirmed = await client.post(
            f"{BASE}/posts/{manual_post.json()['id']}/publish",
            json={"manual_confirmation": True},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "published"

        summary = (await client.get(f"{BASE}/summary")).json()
        assert summary["supported_platforms"] == 14
        assert summary["total_posts"] == 2
        assert summary["published_posts"] == 2
        assert summary["scheduled_posts"] == 0
        assert summary["connected_accounts"] == 1


async def test_mention_conversion_is_atomic_idempotent_and_tenant_scoped(workspace) -> None:
    factory, tenant, owner, outsider = (
        workspace["factory"],
        workspace["tenant"],
        workspace["owner"],
        workspace["outsider"],
    )
    async with client_for(factory, owner) as client:
        created = await client.post(
            f"{BASE}/mentions",
            json={
                "platform": "tiktok",
                "author_handle": "@bulk_buyer",
                "content_text": "Where can I buy 500 pcs of the COB strip for my shop?",
                "sentiment": "lead_intent",
                "occurred_at": "2026-11-19T08:00:00Z",
            },
        )
        assert created.status_code == 201, created.text
        mention_id = created.json()["id"]
        assert created.json()["converted_lead_id"] is None

        summary = (await client.get(f"{BASE}/summary")).json()
        assert summary["social_leads"] == 1
        assert summary["converted_leads"] == 0

        converted = await client.post(f"{BASE}/mentions/{mention_id}/convert-lead")
        assert converted.status_code == 200, converted.text
        result = converted.json()
        assert result["already_converted"] is False
        assert result["lead_id"] and result["customer_id"]

        repeated = await client.post(f"{BASE}/mentions/{mention_id}/convert-lead")
        assert repeated.status_code == 200
        assert repeated.json()["lead_id"] == result["lead_id"]
        assert repeated.json()["already_converted"] is True

        listed = await client.get(f"{BASE}/mentions", params={"sentiment": "lead_intent"})
        assert listed.json()["total"] == 1
        assert listed.json()["items"][0]["converted_lead_id"] == result["lead_id"]

        after = (await client.get(f"{BASE}/summary")).json()
        assert after["converted_leads"] == 1
        assert after["social_leads"] == 1

    async with tenant_session(factory, tenant) as session:
        assert await session.scalar(text("SELECT count(*) FROM customers")) == 1
        assert await session.scalar(text("SELECT count(*) FROM leads")) == 1
        source_channel, lead_type, requirements = (
            await session.execute(
                text("SELECT source_channel, lead_type, extracted_requirements FROM leads")
            )
        ).one()
        assert source_channel == "social_mention"
        assert lead_type == "b2b_inquiry"
        assert requirements["platform"] == "tiktok"
        assert requirements["mention_id"] == mention_id
        assert requirements["author_handle"] == "@bulk_buyer"

    # A different tenant sees nothing and cannot convert this mention.
    async with client_for(factory, outsider) as client:
        assert (await client.get(f"{BASE}/mentions")).json()["total"] == 0
        assert (await client.get(f"{BASE}/posts")).json()["total"] == 0
        hidden = await client.post(f"{BASE}/mentions/{mention_id}/convert-lead")
        assert hidden.status_code == 404


async def test_role_permissions_and_draft_failures_are_explicit(workspace) -> None:
    factory, agent, finance, manager = (
        workspace["factory"],
        workspace["agent"],
        workspace["finance"],
        workspace["manager"],
    )
    async with client_for(factory, agent) as client:
        read_only = await client.post(
            f"{BASE}/accounts/connect",
            json={"platform": "tiktok", "handle": "@lumen", "display_name": "Lumen"},
        )
        assert read_only.status_code == 403
        assert (await client.get(f"{BASE}/accounts")).status_code == 200
        assert (await client.get(f"{BASE}/mentions")).status_code == 200
        assert (await client.get(f"{BASE}/summary")).status_code == 200

    async with client_for(factory, finance) as client:
        assert (
            await client.post(f"{BASE}/posts", json={"platform": "instagram", "caption": "x"})
        ).status_code == 403
        # Finance may still read the marketing KPIs.
        assert (await client.get(f"{BASE}/summary")).status_code == 200

    async with client_for(factory, manager) as client:
        created = await client.post(
            f"{BASE}/posts", json={"platform": "linkedin", "caption": "Case study draft"}
        )
        assert created.status_code == 201, created.text
        failed = await client.patch(
            f"{BASE}/posts/{created.json()['id']}", json={"status": "failed"}
        )
        assert failed.status_code == 422, failed.text
        assert failed.json()["code"] == "ERR_INVALID_POST_STATUS"


async def test_ai_caption_generation_fails_closed_and_grounds_on_catalog_skus(
    workspace,
) -> None:
    factory, tenant = workspace["factory"], workspace["tenant"]
    agent, manager = workspace["agent"], workspace["manager"]
    payload = {
        "platform": "instagram",
        "prompt": "夏季工地照明新品，突出 IP65 与工厂直供",
        "sku_ref": "LED-WORK-100",
        "language": "zh",
    }
    async with client_for(factory, agent) as agent_client:
        denied = await agent_client.post(f"{BASE}/caption/generate", json=payload)
    assert denied.status_code == 403

    async with client_for(factory, manager) as no_provider:
        unavailable = await no_provider.post(f"{BASE}/caption/generate", json=payload)
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "ERR_AI_PROVIDER_UNAVAILABLE"
    assert STUB_CAPTION not in unavailable.text
    async with tenant_session(factory, tenant) as session:
        assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 0

    provider = StubProvider()
    async with client_for(factory, manager, provider=provider) as client:
        empty = await client.post(f"{BASE}/caption/generate", json={**payload, "prompt": " "})
        assert empty.status_code == 422

        generated = await client.post(f"{BASE}/caption/generate", json=payload)
        assert generated.status_code == 200, generated.text
        body = generated.json()
        assert body["platform"] == "instagram"
        assert body["text"] == STUB_CAPTION
        assert body["model_used"]
        assert Decimal(body["credits_consumed"]) > 0
        # An unknown SKU yields no fabricated ERP facts.
        assert body["grounded_tool_call_id"] is None
    assert len(provider.calls) == 1
    prompt_text = " ".join(message["content"] for message in provider.calls[0]["messages"])
    assert "instagram" in prompt_text.lower()
    assert "夏季工地照明新品" in prompt_text

    async with tenant_session(factory, tenant) as session:
        assert await session.scalar(text("SELECT count(*) FROM usage_ledger")) == 1


async def test_phase3d_demo_seed_is_idempotent_and_encrypts_tokens(workspace) -> None:
    """The `--with-demo-data` rows stay reconcilable and never store a plaintext token."""
    import importlib.util
    import sys
    from pathlib import Path

    script = Path(__file__).parents[1] / "scripts" / "seed_hybrid_factory.py"
    spec = importlib.util.spec_from_file_location("seed_hybrid_factory_3d", script)
    assert spec is not None and spec.loader is not None
    seed_mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = seed_mod
    spec.loader.exec_module(seed_mod)

    factory, tenant = workspace["factory"], workspace["tenant"]
    crypto = EnvelopeCryptoService(get_settings())
    async with tenant_session(factory, tenant) as session:
        for _ in range(2):
            await seed_mod._seed_phase3d_social(
                session,
                crypto=crypto,
                tenant_id=tenant,
                owner_id=workspace["owner"].user_id,
            )

    async with tenant_session(factory, tenant) as session:
        accounts = (
            await session.execute(
                text(
                    "SELECT platform, status, token_ciphertext IS NOT NULL AS has_token "
                    "FROM social_accounts ORDER BY platform"
                )
            )
        ).all()
        assert [(row.platform, row.status, row.has_token) for row in accounts] == [
            ("instagram", "connected", True),
            ("tiktok", "connected", True),
            ("xiaohongshu", "connected", False),
        ]
        statuses = {
            row.status
            for row in (await session.execute(text("SELECT status FROM social_posts"))).all()
        }
        assert statuses == {"published", "scheduled", "draft"}
        sentiments = {
            row.sentiment
            for row in (await session.execute(text("SELECT sentiment FROM social_mentions"))).all()
        }
        assert sentiments == {"lead_intent", "positive", "negative"}
        stored = (
            await session.execute(
                text("SELECT token_ciphertext FROM social_accounts WHERE platform = 'instagram'")
            )
        ).scalar()
        assert b"IGQVJ-demo-quanzhou-instagram-oauth-token" not in bytes(stored)
