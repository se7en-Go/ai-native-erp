"""Phase 4 PG17 RLS & API tests: Collector tokens, /collect-from-extension, and extension CORS."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import erp_core.catalog.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.api_main import create_app
from erp_core.catalog.collector_token_service import hash_collector_token
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]


async def _cleanup_tenants(tenant_ids: list[UUID]) -> None:
    admin = create_async_engine(
        os.environ.get(
            "DATABASE_ADMIN_URL",
            "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
        )
    )
    try:
        async with admin.begin() as conn:
            for tid in tenant_ids:
                for stmt in (
                    "DELETE FROM collector_api_tokens WHERE tenant_id = :tid",
                    "DELETE FROM product_media_assets WHERE tenant_id = :tid",
                    "DELETE FROM sourcing_drafts WHERE tenant_id = :tid",
                    "DELETE FROM audit_events WHERE tenant_id = :tid",
                    "DELETE FROM tenant_memberships WHERE tenant_id = :tid",
                ):
                    await conn.execute(text(stmt), {"tid": tid})
                await conn.execute(
                    text("DELETE FROM users WHERE email LIKE :pat"),
                    {"pat": f"%{tid.hex}@collector-test.example.com"},
                )
                await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid})
    finally:
        await admin.dispose()


@pytest.fixture
async def rt_session_factory() -> AsyncIterator[tuple[Factory, list[UUID]]]:
    engine = create_async_engine(
        os.environ.get("DATABASE_RUNTIME_URL", get_settings().DATABASE_RUNTIME_URL)
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created: list[UUID] = []
    try:
        yield factory, created
    finally:
        await engine.dispose()
        if created:
            await _cleanup_tenants(created)


async def _seed_tenant(factory: Factory, tid: UUID, uid: UUID, label: str) -> None:
    async with tenant_session(factory, tid) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, :name, 'USD')"
            ),
            {"id": tid, "slug": f"col-{label}-{tid.hex[:8]}", "name": f"Collector {label}"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, :fn, 'hash')"
            ),
            {
                "id": uid,
                "email": f"{uid.hex}-{tid.hex}@collector-test.example.com",
                "fn": f"Owner {label}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, is_active) "
                "VALUES (:tid, :uid, 'owner', true)"
            ),
            {"tid": tid, "uid": uid},
        )


@pytest.mark.asyncio
async def test_collector_token_lifecycle_extension_collect_cors_and_rls(
    rt_session_factory: tuple[Factory, list[UUID]],
) -> None:
    factory, created_tenants = rt_session_factory
    tid_a, tid_b = uuid4(), uuid4()
    uid_a, uid_b = uuid4(), uuid4()
    created_tenants.extend([tid_a, tid_b])

    await _seed_tenant(factory, tid_a, uid_a, "a")
    await _seed_tenant(factory, tid_b, uid_b, "b")

    app = create_app()
    app.state.session_factory = factory
    holder = [
        CurrentPrincipal(
            user_id=uid_a,
            tenant_id=tid_a,
            role=UserRole.OWNER,
            email=f"{uid_a.hex}-{tid_a.hex}@collector-test.example.com",
        )
    ]
    app.dependency_overrides[get_current_principal] = lambda: holder[0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Chrome Extension CORS preflight on /collect-from-extension
        ext_origin = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        cors_res = await client.options(
            "/api/v1/catalog/sourcing/collect-from-extension",
            headers={
                "Origin": ext_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, x-collector-token",
            },
        )
        assert cors_res.status_code == 200, cors_res.text
        assert cors_res.headers.get("access-control-allow-origin") == ext_origin

        # 2. Token CRUD inside Tenant A
        create_res = await client.post(
            "/api/v1/catalog/sourcing/collector-tokens",
            json={"name": "Chrome 1688 插件"},
        )
        assert create_res.status_code == 201, create_res.text
        created_body = create_res.json()
        plain_token = created_body["plain_token"]
        token_meta = created_body["token"]
        token_id = token_meta["id"]
        assert plain_token.startswith("erp_col_")
        assert token_meta["token_prefix"] == plain_token[:16]
        assert "token_hash" not in token_meta
        assert "token_hash" not in created_body

        list_res = await client.get("/api/v1/catalog/sourcing/collector-tokens")
        assert list_res.status_code == 200
        assert [row["id"] for row in list_res.json()] == [token_id]
        assert list_res.json()[0]["last_used_at"] is None

        # 3. Invalid / missing / malformed collector tokens must 401 cleanly
        for bad_headers in (
            {},
            {"X-Collector-Token": "bad-prefix-token"},
            {"X-Collector-Token": "erp_col_nonexistent_token_123456"},
        ):
            unauth_res = await client.post(
                "/api/v1/catalog/sourcing/collect-from-extension",
                headers=bad_headers,
                json={
                    "source_platform": "1688",
                    "raw_title": "Test LED",
                    "raw_price_cny": "15.00",
                },
            )
            assert unauth_res.status_code == 401
            assert unauth_res.json()["code"] == "ERR_INVALID_COLLECTOR_TOKEN"

        # 4. Valid X-Collector-Token creates multi-variant draft & degrades safely if image fails
        collect_res = await client.post(
            "/api/v1/catalog/sourcing/collect-from-extension",
            headers={"X-Collector-Token": plain_token},
            json={
                "source_platform": "1688",
                "source_url": "https://detail.1688.com/offer/77889900.html",
                "raw_title": "户外太阳能防水LED投光灯 100W",
                "raw_text": "户外太阳能防水LED投光灯 100W ¥28.50 IP66",
                "raw_price_cny": "28.50",
                "images": ["https://127.0.0.1/ssrf-ignored.jpg"],
                "target_margin_pct": "35.00",
                "fx_rate_usd_cny": "7.200000",
                "options_spec": [{"name": "Color", "values": ["Warm 3000K", "Cool 6500K"]}],
                "variants_spec": [
                    {
                        "sku_suffix": "WARM",
                        "option_values": {"Color": "Warm 3000K"},
                        "cost_price_cny": "28.50",
                        "initial_stock": 120,
                    },
                    {
                        "sku_suffix": "COOL",
                        "option_values": {"Color": "Cool 6500K"},
                        "cost_price_cny": "31.00",
                        "initial_stock": 80,
                    },
                ],
            },
        )
        assert collect_res.status_code == 201, collect_res.text
        col_json = collect_res.json()
        assert col_json["catalog_url"] == "/catalog?tab=sourcing"
        assert col_json["mirrored_images_count"] == 0
        draft = col_json["draft"]
        assert draft["tenant_id"] == str(tid_a)
        assert draft["source_platform"] == "1688"
        assert len(draft["variants_spec"]) == 2
        assert Decimal(draft["target_price_usd"]) > Decimal("0")

        # Token last_used_at was updated
        after_use_list = await client.get("/api/v1/catalog/sourcing/collector-tokens")
        assert after_use_list.status_code == 200
        assert after_use_list.json()[0]["last_used_at"] is not None

        # Draft appears in Tenant A's sourcing box
        drafts_a = await client.get("/api/v1/catalog/sourcing/drafts")
        assert drafts_a.status_code == 200
        assert any(d["id"] == draft["id"] for d in drafts_a.json())

        # 5. Cross-tenant RLS isolation: Tenant B cannot see or revoke A's token/draft
        holder[0] = CurrentPrincipal(
            user_id=uid_b,
            tenant_id=tid_b,
            role=UserRole.OWNER,
            email=f"{uid_b.hex}-{tid_b.hex}@collector-test.example.com",
        )
        tokens_b = await client.get("/api/v1/catalog/sourcing/collector-tokens")
        assert tokens_b.status_code == 200
        assert tokens_b.json() == []
        revoke_cross = await client.delete(f"/api/v1/catalog/sourcing/collector-tokens/{token_id}")
        assert revoke_cross.status_code == 404
        drafts_b = await client.get("/api/v1/catalog/sourcing/drafts")
        assert drafts_b.status_code == 200
        assert all(d["id"] != draft["id"] for d in drafts_b.json())

        # 6. Expired token must be rejected with 401
        expired_plain = f"erp_col_expired_{uuid4().hex}"
        async with tenant_session(factory, tid_a) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO collector_api_tokens (
                        id, tenant_id, created_by_user_id, name,
                        token_prefix, token_hash, expires_at
                    ) VALUES (
                        :id, :tid, :uid, 'Expired Token',
                        :prefix, :thash, now() - interval '1 hour'
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "tid": tid_a,
                    "uid": uid_a,
                    "prefix": expired_plain[:16],
                    "thash": hash_collector_token(expired_plain),
                },
            )
        expired_res = await client.post(
            "/api/v1/catalog/sourcing/collect-from-extension",
            headers={"X-Collector-Token": expired_plain},
            json={
                "source_platform": "taobao",
                "raw_title": "Expired Item",
                "raw_price_cny": "19.90",
            },
        )
        assert expired_res.status_code == 401
        assert expired_res.json()["code"] == "ERR_INVALID_COLLECTOR_TOKEN"

        # 7. Tenant A revokes its token -> subsequent extension calls return 401
        holder[0] = CurrentPrincipal(
            user_id=uid_a,
            tenant_id=tid_a,
            role=UserRole.OWNER,
            email=f"{uid_a.hex}-{tid_a.hex}@collector-test.example.com",
        )
        revoke_res = await client.delete(f"/api/v1/catalog/sourcing/collector-tokens/{token_id}")
        assert revoke_res.status_code == 200
        assert revoke_res.json()["revoked_at"] is not None

        revoked_use = await client.post(
            "/api/v1/catalog/sourcing/collect-from-extension",
            headers={"X-Collector-Token": plain_token},
            json={
                "source_platform": "pdd",
                "raw_title": "Revoked Token Attempt",
                "raw_price_cny": "9.90",
            },
        )
        assert revoked_use.status_code == 401
        assert revoked_use.json()["code"] == "ERR_INVALID_COLLECTOR_TOKEN"
