"""Wave 4 PG17 integration coverage: AI rewrite -> draft edit -> import -> live publish.

Every test runs against the **real PostgreSQL 17** database with tenant RLS forced on and
real AES-256-GCM envelope credentials. The copywriter is an in-process deterministic stub
(no LLM, no network, no credits beyond the exactly asserted ``Decimal``); the only HTTP is
Shopee's Open Platform served by ``httpx.MockTransport``.

Covered invariants:

* ``collect`` -> ``ai_rewrite_draft`` -> ``update_draft`` -> ``import_to_catalog`` ->
  ``publish(push_to_marketplace=True)`` pushes the *channel-localized* copy deck (Thai for
  Shopee, Russian for Ozon) plus ``marketplace_category_attrs`` to the target channel only.
* ``marketplace_category_attrs[channel]["category_id"]`` reaches the dispatcher, which
  overrides ``payload.category`` for the eight global marketplaces (and therefore the
  ``category_id`` Shopee actually receives) while leaving the empty-attribute path
  byte-identical.
* No injected rewriter means deterministic localized copy with zero credits and honest
  ``deterministic_fallback`` provenance — never a fake ``llm`` claim.
* Tenant B cannot read, patch or rewrite Tenant A's draft (RLS + explicit tenant filter).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Configure every ORM module so the string FKs used below resolve in this isolated app.
import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.catalog.sourcing_copy_protocol import (
    SourcingAiRewriteRequest,
    SourcingCopyRewriteInput,
    SourcingCopyRewriteOutput,
)
from erp_core.catalog.sourcing_router import router as sourcing_router
from erp_core.catalog.sourcing_service import (
    MarketplaceListingDispatchReceipt,
    MarketplaceListingPayload,
    SourcingCollectRequest,
    SourcingDraftRead,
    SourcingDraftUpdateRequest,
    SourcingPublishRequest,
    SourcingService,
)
from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.channels.ecommerce.listing_dispatcher import MarketplaceListingDispatcher
from erp_core.channels.ecommerce.shopee import (
    ADD_ITEM_PATH,
    LOGISTICS_CHANNEL_LIST_PATH,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError, register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]

SHOPEE_CHANNEL = "shopee"
SHOPEE_CREDENTIAL_TYPE = "shopee_app"
SHOPEE_SHOP_ID = "778899"
SHOPEE_PARTNER_ID = "2010123"
SHOPEE_PARTNER_KEY = "shopee-partner-key-integration-secret"
SHOPEE_ACCESS_TOKEN = "shopee-access-token-integration-secret"
SHOPEE_ITEM_ID = "550012"
SHOPEE_CATEGORY_ID = "100630"
SHOPEE_LOGISTIC_ID = 80001

RAW_TITLE = "100W 太阳能投光灯 一体式户外防水 IP65"
RAW_TEXT = "厂家直销 太阳能投光灯 100W 防水 IP65，起订 100 件"

#: Hand-written stub copies — every quoted money figure is copied from the draft verbatim.
_OZON_RU_TITLE = "Светодиодный прожектор 100 Вт IP65 с солнечной панелью"
_OZON_RU_BULLETS = [
    "Уличный прожектор на солнечной панели мощностью 100 Вт",
    "Класс защиты IP65: дождь, пыль и мороз не страшны",
    "Монтаж на стену или столб за 15 минут без электрика",
    "Аккумулятор держит свет всю ночь после полного заряда",
    "Гарантия 12 месяцев и паспорт изделия с кодом ТН ВЭД",
]
_OZON_RU_DESCRIPTION = (
    "Светодиодный прожектор 100 Вт IP65 для покупателей Ozon. Комплект поставки: "
    "прожектор, солнечная панель, крепёж и паспорт изделия."
)
_SHOPEE_TH_TITLE = "ไฟสปอตไลต์ LED 100W กันน้ำ IP65 พร้อมแผงโซลาร์"
_SHOPEE_TH_BULLETS = [
    "สปอตไลต์ LED 100W พร้อมแผงโซลาร์ชาร์จไฟได้เอง",
    "กันน้ำมาตรฐาน IP65 ใช้กลางแจ้งได้ตลอดปี",
    "ติดตั้งง่ายทั้งผนังและเสา ไม่ต้องเดินสายไฟ",
    "แบตเตอรี่ชาร์จเต็มใช้งานได้ตลอดคืน",
    "รับประกัน 12 เดือน พร้อมคู่มือและเอกสาร HS",
]
_SHOPEE_TH_DESCRIPTION = (
    "ไฟสปอตไลต์ LED 100W กันน้ำ IP65 สำหรับผู้ซื้อ Shopee "
    "ในกล่องมีโคมไฟ แผงโซลาร์ อุปกรณ์ติดตั้ง และคู่มือภาษาไทย"
)
_SHOPEE_TH_STUB_CREDITS = Decimal("0.4200")


# --------------------------------------------------------------------------------------
# Deterministic stand-ins (no LLM, no network)
# --------------------------------------------------------------------------------------


class StubCopyRewriter:
    """Deterministic LLM stand-in: per-language copy, real credit accounting."""

    def __init__(self) -> None:
        self.calls: list[SourcingCopyRewriteInput] = []

    async def rewrite_copy(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: SourcingCopyRewriteInput,
    ) -> SourcingCopyRewriteOutput:
        self.calls.append(payload)
        if payload.target_language == "ru":
            title, bullets, description = (
                _OZON_RU_TITLE,
                _OZON_RU_BULLETS,
                _OZON_RU_DESCRIPTION,
            )
        elif payload.target_language == "th":
            title, bullets, description = (
                _SHOPEE_TH_TITLE,
                _SHOPEE_TH_BULLETS,
                _SHOPEE_TH_DESCRIPTION,
            )
        else:  # pragma: no cover - the tests only request ru/th
            raise AssertionError(f"unexpected language {payload.target_language}")
        return SourcingCopyRewriteOutput(
            target_channel=payload.target_channel,
            target_language=payload.target_language,
            tone=payload.tone,
            title=title,
            bullet_points=bullets,
            description=description,
            seo_keywords=["solar flood light", "IP65", "100W"],
            rewrite_mode="llm",
            model_used="stub-copy-llm-v1",
            credits_consumed=_SHOPEE_TH_STUB_CREDITS,
            fallback_reason=None,
            generated_at=datetime.now(UTC),
        )


class RecordingPublisher:
    """``MarketplaceListingPublisher`` stand-in that records the exact live payload."""

    def __init__(self, external_product_id: str = "SHOPEE-ITEM-LIVE-1") -> None:
        self.external_product_id = external_product_id
        self.payloads: list[MarketplaceListingPayload] = []

    async def publish_listing(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        payload: MarketplaceListingPayload,
    ) -> MarketplaceListingDispatchReceipt:
        self.payloads.append(payload)
        return MarketplaceListingDispatchReceipt(
            external_product_id=self.external_product_id,
            external_sku=payload.external_sku,
            status="NORMAL",
            raw_response={"item_id": self.external_product_id},
            variant_receipts=[],
        )


# --------------------------------------------------------------------------------------
# Workspace fixture: two real PG17 tenants, RLS forced, clean teardown
# --------------------------------------------------------------------------------------


def _admin_database_url() -> str:
    return os.environ.get(
        "DATABASE_ADMIN_URL",
        "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
    )


@asynccontextmanager
async def _admin_session() -> AsyncIterator[AsyncConnection]:
    """Superuser (RLS-bypassing) connection, used only to prove data existence."""
    engine = create_async_engine(_admin_database_url())
    try:
        async with engine.connect() as connection:
            yield connection
            await connection.rollback()
    finally:
        await engine.dispose()


async def _purge_tenant(tenant_id: UUID) -> None:
    admin = create_async_engine(_admin_database_url())
    try:
        async with admin.begin() as conn:
            for statement in (
                "DELETE FROM product_media_assets WHERE tenant_id = :tid",
                "DELETE FROM platform_sku_mappings WHERE tenant_id = :tid",
                "DELETE FROM product_variants WHERE tenant_id = :tid",
                "DELETE FROM products WHERE tenant_id = :tid",
                "DELETE FROM sourcing_drafts WHERE tenant_id = :tid",
                "DELETE FROM channel_credentials WHERE tenant_id = :tid",
                "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                "DELETE FROM audit_events WHERE tenant_id = :tid",
                "DELETE FROM tenant_memberships WHERE tenant_id = :tid",
            ):
                await conn.execute(text(statement), {"tid": tenant_id})
            user_rows = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email LIKE :pattern"),
                    {"pattern": f"%{tenant_id.hex}@sourcing-rewrite.example.com"},
                )
            ).all()
            for row in user_rows:
                await conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": row.id})
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
    finally:
        await admin.dispose()


async def _seed_tenant(
    factory: Factory, *, label: str
) -> tuple[UUID, UUID, CurrentPrincipal]:
    tenant = uuid4()
    owner = uuid4()
    email = f"{owner.hex}@sourcing-rewrite.example.com"
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, :name, 'CNY')"
            ),
            {"id": tenant, "slug": f"src-rewrite-{label}-{tenant.hex[:12]}", "name": label},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, :name, 'test')"
            ),
            {"id": owner, "email": email, "name": label},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                "VALUES (:tid, :uid, 'owner')"
            ),
            {"tid": tenant, "uid": owner},
        )
    return (
        tenant,
        owner,
        CurrentPrincipal(
            user_id=owner, tenant_id=tenant, role=UserRole.OWNER, email=email
        ),
    )


@pytest.fixture
async def workspace(runtime_session_factory: Factory) -> AsyncIterator[dict[str, Any]]:
    tenant_a, owner_a, principal_a = await _seed_tenant(runtime_session_factory, label="Alpha")
    tenant_b, owner_b, principal_b = await _seed_tenant(runtime_session_factory, label="Bravo")
    try:
        yield {
            "tenant_a": tenant_a,
            "owner_a_id": owner_a,
            "principal_a": principal_a,
            "tenant_b": tenant_b,
            "owner_b_id": owner_b,
            "principal_b": principal_b,
        }
    finally:
        await _purge_tenant(tenant_a)
        await _purge_tenant(tenant_b)


def _build_app(
    factory: Factory,
    principal: CurrentPrincipal,
    *,
    copy_rewriter: Any | None = None,
) -> FastAPI:
    """Throwaway app mounting the sourcing router with an injected principal."""
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.include_router(sourcing_router)
    app.dependency_overrides[get_current_principal] = lambda: principal
    if copy_rewriter is not None:
        app.state.sourcing_copy_rewriter = copy_rewriter
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# --------------------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------------------


async def _seed_shopee_account(session: AsyncSession, tenant_id: UUID) -> UUID:
    account_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO channel_accounts (id, tenant_id, channel_type, external_account_id, "
            "display_name, status, config) "
            "VALUES (:id, :tid, 'shopee', :ext, 'Shopee TH', 'active', '{}'::jsonb)"
        ),
        {"id": account_id, "tid": tenant_id, "ext": SHOPEE_SHOP_ID},
    )
    await CredentialVaultHelper().save_secret_json(
        session,
        tenant_id,
        account_id,
        SHOPEE_CREDENTIAL_TYPE,
        {
            "shop_id": SHOPEE_SHOP_ID,
            "partner_id": SHOPEE_PARTNER_ID,
            "partner_key": SHOPEE_PARTNER_KEY,
            "access_token": SHOPEE_ACCESS_TOKEN,
        },
    )
    return account_id


async def _collect(session: AsyncSession, tenant_id: UUID, actor: str) -> SourcingDraftRead:
    return await SourcingService().collect(
        session,
        tenant_id,
        actor,
        SourcingCollectRequest(
            source_platform="1688",
            raw_title=RAW_TITLE,
            raw_text=RAW_TEXT,
            raw_price_cny=Decimal("52.50"),
            target_margin_pct=Decimal("40.00"),
            fx_rate_usd_cny=Decimal("7.200000"),
            category="LED Lighting",
            hs_code="9405.40",
        ),
    )


def _envelope(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": "",
            "message": "",
            "warning": "",
            "request_id": "req-integration-1",
            "response": payload,
        },
    )


def _shopee_handler(calls: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "path": request.url.path,
                "body": json.loads(request.content) if request.content else {},
            }
        )
        if request.url.path == LOGISTICS_CHANNEL_LIST_PATH:
            return _envelope(
                {"logistic_channel_list": [{"logistics_channel_id": SHOPEE_LOGISTIC_ID}]}
            )
        if request.url.path == ADD_ITEM_PATH:
            return _envelope({"item_id": int(SHOPEE_ITEM_ID), "item_status": "NORMAL"})
        return httpx.Response(404, json={"error": "error_path_not_found"})

    return handler


async def _audit_actions(
    session: AsyncSession, tenant_id: UUID, draft_id: UUID
) -> list[tuple[str, dict[str, Any]]]:
    rows = (
        await session.execute(
            text(
                "SELECT action, after_state FROM audit_events "
                "WHERE tenant_id = :tid AND resource_id = :rid ORDER BY created_at, id"
            ),
            {"tid": tenant_id, "rid": draft_id},
        )
    ).all()
    return [(str(row.action), dict(row.after_state or {})) for row in rows]


# --------------------------------------------------------------------------------------
# Full flow: rewrite (ru + th) -> edit -> import -> live publish with a stub publisher
# --------------------------------------------------------------------------------------


async def test_rewrite_edit_import_publish_carries_localized_copy_and_category(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant_a"]
    actor = str(workspace["owner_a_id"])
    rewriter = StubCopyRewriter()
    service = SourcingService()

    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_shopee_account(session, tenant)
        draft = await _collect(session, tenant, actor)
    assert draft.localized_copies == {}
    assert draft.marketplace_category_attrs == {}
    assert draft.suggested_category == "LED Lighting"

    # -- Ozon (Russian) then Shopee (Thai): both decks persist, per channel --------------
    async with tenant_session(runtime_session_factory, tenant) as session:
        ozon = await service.ai_rewrite_draft(
            session,
            tenant,
            actor,
            draft.id,
            SourcingAiRewriteRequest(
                target_channel="ozon", target_language="ru", tone="seo", keywords=["IP65"]
            ),
            copy_rewriter=rewriter,
        )
        assert ozon.rewrite.rewrite_mode == "llm"
        assert ozon.rewrite.credits_consumed == _SHOPEE_TH_STUB_CREDITS
        assert "ozon:ru" in ozon.draft.localized_copies
        assert ozon.draft.localized_copies["ozon:ru"]["title"] == _OZON_RU_TITLE
        assert ozon.draft.localized_copies["ozon:ru"]["optimized_description"] == (
            _OZON_RU_DESCRIPTION
        )
        assert ozon.draft.optimized_title_en == _OZON_RU_TITLE[:200]
        assert ozon.draft.bullet_points == _OZON_RU_BULLETS
        first_call = rewriter.calls[-1]
        assert first_call.target_channel == "ozon"
        assert first_call.target_language == "ru"
        assert first_call.raw_title == RAW_TITLE
        assert first_call.category == "LED Lighting"
        assert first_call.hs_code == "9405.40"
        assert first_call.target_price_usd == draft.target_price_usd
        assert first_call.moq_tiers == [
            {"min_qty": tier.min_qty, "unit_price_usd": str(tier.unit_price_usd)}
            for tier in draft.moq_tiers
        ]
        assert first_call.keywords == ["IP65"]

    async with tenant_session(runtime_session_factory, tenant) as session:
        shopee = await service.ai_rewrite_draft(
            session,
            tenant,
            actor,
            draft.id,
            SourcingAiRewriteRequest(target_channel="shopee", target_language="th"),
            copy_rewriter=rewriter,
        )
        assert {"ozon:ru", "shopee:th"} <= set(shopee.draft.localized_copies)
        assert shopee.draft.localized_copies["shopee:th"]["title"] == _SHOPEE_TH_TITLE
        assert shopee.draft.localized_copies["shopee:th"]["bullet_points"] == _SHOPEE_TH_BULLETS
        assert shopee.draft.optimized_title_en == _SHOPEE_TH_TITLE[:200]

    # -- price + per-marketplace category edit over HTTP ---------------------------------
    app = _build_app(runtime_session_factory, workspace["principal_a"], copy_rewriter=rewriter)
    async with _client(app) as client:
        patched_resp = await client.patch(
            f"/api/v1/catalog/sourcing/drafts/{draft.id}",
            json={
                "target_price_usd": "21.90",
                "marketplace_category_attrs": {
                    "shopee": {"category_id": SHOPEE_CATEGORY_ID}
                },
            },
        )
        assert patched_resp.status_code == 200, patched_resp.text
        patched = patched_resp.json()
    assert Decimal(patched["target_price_usd"]) == Decimal("21.90")
    assert patched["marketplace_category_attrs"] == {
        "shopee": {"category_id": SHOPEE_CATEGORY_ID}
    }
    # The edit kept every localized deck untouched.
    assert patched["localized_copies"]["shopee:th"]["title"] == _SHOPEE_TH_TITLE
    assert patched["localized_copies"]["ozon:ru"]["title"] == _OZON_RU_TITLE

    # -- import + live publish (stub publisher records the exact marketplace payload) ----
    publisher = RecordingPublisher()
    async with tenant_session(runtime_session_factory, tenant) as session:
        imported = await service.import_to_catalog(session, tenant, actor, draft.id)
        assert imported.created is True
    async with tenant_session(runtime_session_factory, tenant) as session:
        results = await service.publish(
            session,
            tenant,
            actor,
            draft.id,
            SourcingPublishRequest(
                channel_account_ids=[account_id],
                push_to_marketplace=True,
                initial_stock_quantity=40,
            ),
            listing_publisher=publisher,
        )
    assert len(publisher.payloads) == 1
    live_payload = publisher.payloads[0]
    assert live_payload.channel_type == SHOPEE_CHANNEL
    # Shopee receives the *Shopee-localized Thai* copy, not the Ozon/Russian or source text.
    assert live_payload.title == _SHOPEE_TH_TITLE
    assert live_payload.bullet_points == _SHOPEE_TH_BULLETS
    assert live_payload.description == _SHOPEE_TH_DESCRIPTION
    assert live_payload.category_attributes == {"category_id": SHOPEE_CATEGORY_ID}
    assert live_payload.quantity == 40
    assert live_payload.price_usd == Decimal("21.90")

    assert len(results) == 1
    assert results[0].listing_state == "live_published"
    assert results[0].external_listing_confirmed is True
    assert results[0].external_product_id == publisher.external_product_id
    assert results[0].publish_mode == "live_marketplace_api"

    # -- persisted draft + audit trail ---------------------------------------------------
    async with tenant_session(runtime_session_factory, tenant) as session:
        reloaded = await service.get_draft(session, tenant, draft.id)
        actions = await _audit_actions(session, tenant, draft.id)
    assert reloaded.status == "published"
    assert reloaded.listing_state == "live_published"
    assert reloaded.publish_history[-1]["external_listing_confirmed"] is True
    assert reloaded.marketplace_category_attrs == {
        "shopee": {"category_id": SHOPEE_CATEGORY_ID}
    }
    assert {"ozon:ru", "shopee:th"} <= set(reloaded.localized_copies)

    action_names = [action for action, _state in actions]
    assert "catalog.sourcing_draft.ai_rewritten" in action_names
    assert "catalog.sourcing_draft.updated" in action_names
    rewrite_states = [state for action, state in actions if action.endswith("ai_rewritten")]
    assert len(rewrite_states) == 2
    assert all(state["rewrite_mode"] == "llm" for state in rewrite_states)
    assert all(state["model_used"] == "stub-copy-llm-v1" for state in rewrite_states)
    assert all(state["credits_consumed"] == "0.4200" for state in rewrite_states)
    assert {state["target_language"] for state in rewrite_states} == {"ru", "th"}

    # Cross-check from the migrator (RLS-bypassing) connection: only one draft row exists
    # for this tenant and it really stores both decks.
    async with tenant_session(runtime_session_factory, tenant) as session:
        stored = (
            await session.execute(
                text(
                    "SELECT localized_copies->'shopee:th'->>'title' AS th_title, "
                    "marketplace_category_attrs->'shopee'->>'category_id' AS shopee_cat "
                    "FROM sourcing_drafts WHERE tenant_id = :tid AND id = :did"
                ),
                {"tid": tenant, "did": draft.id},
            )
        ).one()
    assert stored.th_title == _SHOPEE_TH_TITLE
    assert stored.shopee_cat == SHOPEE_CATEGORY_ID


# --------------------------------------------------------------------------------------
# Dispatcher: category_attributes override reaches Shopee's real category_id
# --------------------------------------------------------------------------------------


async def test_dispatcher_category_attributes_override_reaches_shopee(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant_a"]
    actor = str(workspace["owner_a_id"])
    calls: list[dict[str, Any]] = []
    dispatcher = MarketplaceListingDispatcher(
        transport=httpx.MockTransport(_shopee_handler(calls))
    )

    # Control: no category_attributes => the historic ``payload.category`` is used verbatim,
    # and Shopee rejects the non-numeric catalog category exactly as before.
    async with tenant_session(runtime_session_factory, tenant) as session:
        account_id = await _seed_shopee_account(session, tenant)
    async with tenant_session(runtime_session_factory, tenant) as session:
        with pytest.raises(AppError) as no_category:
            await dispatcher.publish_listing(
                session,
                tenant,
                MarketplaceListingPayload(
                    channel_account_id=account_id,
                    channel_type=SHOPEE_CHANNEL,
                    external_sku="LEGACY-SKU-1",
                    title="LED Flood Light 100W",
                    description="Legacy listing without marketplace category attributes.",
                    bullet_points=["IP65", "100W"],
                    price_usd=Decimal("19.90"),
                    quantity=10,
                    images=[],
                    category="LED Lighting",
                    hs_code="9405.40",
                ),
            )
    assert no_category.value.code == "ERR_SHOPEE_CATEGORY_REQUIRED"
    assert calls == []

    # With category_attributes the dispatcher forwards the numeric marketplace category.
    async with tenant_session(runtime_session_factory, tenant) as session:
        receipt = await dispatcher.publish_listing(
            session,
            tenant,
            MarketplaceListingPayload(
                channel_account_id=account_id,
                channel_type=SHOPEE_CHANNEL,
                external_sku="CAT-ATTR-SKU-1",
                title=_SHOPEE_TH_TITLE,
                description=_SHOPEE_TH_DESCRIPTION,
                bullet_points=_SHOPEE_TH_BULLETS,
                price_usd=Decimal("21.90"),
                quantity=12,
                images=[],
                category="LED Lighting",
                hs_code="9405.40",
                category_attributes={"category_id": SHOPEE_CATEGORY_ID},
            ),
        )
    assert receipt.external_product_id == SHOPEE_ITEM_ID
    add_item_bodies = [call["body"] for call in calls if call["path"] == ADD_ITEM_PATH]
    assert len(add_item_bodies) == 1
    assert add_item_bodies[0]["category_id"] == int(SHOPEE_CATEGORY_ID)
    assert add_item_bodies[0]["item_name"] == _SHOPEE_TH_TITLE

    # End-to-end: the drafted attribute drives the same override through ``publish``.
    calls.clear()
    service = SourcingService()
    end_to_end = MarketplaceListingDispatcher(
        transport=httpx.MockTransport(_shopee_handler(calls))
    )
    async with tenant_session(runtime_session_factory, tenant) as session:
        draft = await _collect(session, tenant, actor)
        await service.update_draft(
            session,
            tenant,
            actor,
            draft.id,
            SourcingDraftUpdateRequest(
                marketplace_category_attrs={"Shopee": {"category_id": SHOPEE_CATEGORY_ID}}
            ),
        )
        await service.import_to_catalog(session, tenant, actor, draft.id)
        results = await service.publish(
            session,
            tenant,
            actor,
            draft.id,
            SourcingPublishRequest(
                channel_account_ids=[account_id], push_to_marketplace=True
            ),
            listing_publisher=end_to_end,
        )
    assert [result.listing_state for result in results] == ["live_published"]
    end_to_end_bodies = [call["body"] for call in calls if call["path"] == ADD_ITEM_PATH]
    assert len(end_to_end_bodies) == 1
    assert end_to_end_bodies[0]["category_id"] == int(SHOPEE_CATEGORY_ID)
    # No localized deck existed, so the draft's own (English) title is still used.
    assert end_to_end_bodies[0]["item_name"] == draft.optimized_title_en[:255]


# --------------------------------------------------------------------------------------
# No rewriter configured: deterministic localized copy, honest zero-credit provenance
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Collect + patch plumbing: JSONB round-trip through every read path
# --------------------------------------------------------------------------------------


async def test_collect_and_patch_round_trip_localized_copies_and_category_attrs(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant_a"]
    actor = str(workspace["owner_a_id"])
    collected_attrs = {
        "shopee": {"category_id": SHOPEE_CATEGORY_ID},
        "ozon": {"primary_category": "Электроника"},
    }
    service = SourcingService()
    async with tenant_session(runtime_session_factory, tenant) as session:
        draft = await service.collect(
            session,
            tenant,
            actor,
            SourcingCollectRequest(
                source_platform="1688",
                raw_title=RAW_TITLE,
                raw_text=RAW_TEXT,
                raw_price_cny=Decimal("52.50"),
                category="LED Lighting",
                marketplace_category_attrs=collected_attrs,
            ),
        )
    # collect() persists the per-marketplace attributes; localized decks start empty.
    assert draft.marketplace_category_attrs == collected_attrs
    assert draft.localized_copies == {}

    # list_drafts / get_draft deserialize the same JSONB without losing a key.
    async with tenant_session(runtime_session_factory, tenant) as session:
        listed = await service.list_drafts(session, tenant)
        fetched = await service.get_draft(session, tenant, draft.id)
    assert [item.id for item in listed] == [draft.id]
    assert listed[0].marketplace_category_attrs == collected_attrs
    assert fetched.marketplace_category_attrs == collected_attrs

    # A rewrite adds a deck; a later PATCH merges a second deck instead of replacing it.
    async with tenant_session(runtime_session_factory, tenant) as session:
        await service.ai_rewrite_draft(
            session,
            tenant,
            actor,
            draft.id,
            SourcingAiRewriteRequest(target_channel="shopee", target_language="th"),
            copy_rewriter=StubCopyRewriter(),
        )
    async with tenant_session(runtime_session_factory, tenant) as session:
        merged = await service.update_draft(
            session,
            tenant,
            actor,
            draft.id,
            SourcingDraftUpdateRequest(
                bullet_points=[f"bullet {index}" for index in range(1, 8)],
                localized_copies={"custom:de": {"title": "LED Flutlicht 100W"}},
            ),
        )
    assert set(merged.localized_copies) == {"shopee:th", "custom:de"}
    assert merged.localized_copies["shopee:th"]["title"] == _SHOPEE_TH_TITLE
    assert merged.localized_copies["custom:de"] == {"title": "LED Flutlicht 100W"}
    # Edited bullets are capped to five, and untouched category attributes survive.
    assert merged.bullet_points == [f"bullet {index}" for index in range(1, 6)]
    assert merged.marketplace_category_attrs == collected_attrs

    # The HTTP surface serializes both JSONB columns verbatim.
    app = _build_app(runtime_session_factory, workspace["principal_a"])
    async with _client(app) as client:
        response = await client.get(f"/api/v1/catalog/sourcing/drafts/{draft.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["marketplace_category_attrs"] == collected_attrs
    assert set(body["localized_copies"]) == {"shopee:th", "custom:de"}
    assert body["bullet_points"] == [f"bullet {index}" for index in range(1, 6)]


async def test_ai_rewrite_without_rewriter_is_deterministic_and_free(
    runtime_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant = workspace["tenant_a"]
    actor = str(workspace["owner_a_id"])
    async with tenant_session(runtime_session_factory, tenant) as session:
        draft = await _collect(session, tenant, actor)

    # No ``app.state.sourcing_copy_rewriter`` at all: the router must degrade to the
    # catalog-side deterministic writer instead of failing or inventing an ``llm`` claim.
    app = _build_app(runtime_session_factory, workspace["principal_a"])
    async with _client(app) as client:
        response = await client.post(
            f"/api/v1/catalog/sourcing/drafts/{draft.id}/ai-rewrite",
            json={"target_channel": "ozon", "target_language": "ru"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    rewrite = body["rewrite"]
    assert rewrite["rewrite_mode"] == "deterministic_fallback"
    assert rewrite["model_used"] == "deterministic-rule-v1"
    assert rewrite["fallback_reason"] == "ai_rewriter_not_configured"
    assert Decimal(rewrite["credits_consumed"]) == Decimal("0.0000")
    assert rewrite["target_channel"] == "ozon"
    assert rewrite["target_language"] == "ru"
    # The Russian copy is real Russian and quotes the draft's exact retail price.
    assert rewrite["title"].startswith("Светодиодное освещение")
    assert len(rewrite["bullet_points"]) == 5
    expected_price_text = "$" + format(draft.target_price_usd, "f").rstrip("0").rstrip(".")
    assert expected_price_text in rewrite["description"]

    localized = body["draft"]["localized_copies"]["ozon:ru"]
    assert localized["rewrite_mode"] == "deterministic_fallback"
    assert localized["generated_at"]
    assert body["draft"]["optimized_title_en"] == rewrite["title"][:200]

    async with tenant_session(runtime_session_factory, tenant) as session:
        actions = await _audit_actions(session, tenant, draft.id)
    states = [state for action, state in actions if action.endswith("ai_rewritten")]
    assert len(states) == 1
    assert states[0]["rewrite_mode"] == "deterministic_fallback"
    assert states[0]["credits_consumed"] == "0.0000"

    # An unknown target language is rejected before any copy is produced or billed.
    async with _client(app) as client:
        bad = await client.post(
            f"/api/v1/catalog/sourcing/drafts/{draft.id}/ai-rewrite",
            json={"target_channel": "ozon", "target_language": "xx"},
        )
    assert bad.status_code == 422
    assert bad.json()["code"] == "ERR_UNSUPPORTED_REWRITE_LANGUAGE"


# --------------------------------------------------------------------------------------
# RLS: Tenant B can never read, patch or rewrite Tenant A's draft
# --------------------------------------------------------------------------------------


async def test_tenant_b_cannot_read_patch_or_rewrite_tenant_a_draft(
    runtime_session_factory: Factory,
    migrator_session_factory: Factory,
    workspace: dict[str, Any],
) -> None:
    tenant_a = workspace["tenant_a"]
    tenant_b = workspace["tenant_b"]
    actor_a = str(workspace["owner_a_id"])
    rewriter = StubCopyRewriter()
    async with tenant_session(runtime_session_factory, tenant_a) as session:
        draft = await _collect(session, tenant_a, actor_a)

    app_b = _build_app(runtime_session_factory, workspace["principal_b"], copy_rewriter=rewriter)
    async with _client(app_b) as client_b:
        read = await client_b.get(f"/api/v1/catalog/sourcing/drafts/{draft.id}")
        patched = await client_b.patch(
            f"/api/v1/catalog/sourcing/drafts/{draft.id}",
            json={"target_price_usd": "1.00"},
        )
        rewritten = await client_b.post(
            f"/api/v1/catalog/sourcing/drafts/{draft.id}/ai-rewrite",
            json={"target_channel": "shopee", "target_language": "th"},
        )
        listing = await client_b.get("/api/v1/catalog/sourcing/drafts")
    for response in (read, patched, rewritten):
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "ERR_SOURCING_DRAFT_NOT_FOUND"
    assert listing.status_code == 200
    assert listing.json() == []
    # Tenant B's rewrite attempt never reached the rewriter.
    assert rewriter.calls == []

    # Tenant A's draft is byte-identical: no copy deck, no price change, no category attrs.
    async with tenant_session(runtime_session_factory, tenant_a) as session:
        unchanged = await SourcingService().get_draft(session, tenant_a, draft.id)
    assert unchanged.localized_copies == {}
    assert unchanged.marketplace_category_attrs == {}
    assert unchanged.target_price_usd == draft.target_price_usd
    assert unchanged.optimized_title_en == draft.optimized_title_en

    # Even a raw cross-tenant SELECT through the runtime role returns nothing.
    async with tenant_session(runtime_session_factory, tenant_b) as session:
        leaked = await session.scalar(
            text("SELECT count(*) FROM sourcing_drafts WHERE id = :did"),
            {"did": draft.id},
        )
    assert leaked == 0

    # The migrator role owns the schema: it sees the columns and the forced RLS that make
    # the isolation above structural rather than application-level.
    async with migrator_session_factory() as session:
        schema = (
            await session.execute(
                text(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                    "  (SELECT count(*) FROM information_schema.columns "
                    "   WHERE table_name = 'sourcing_drafts' "
                    "     AND column_name IN ('localized_copies', "
                    "                       'marketplace_category_attrs')"
                    "     AND is_nullable = 'NO') AS jsonb_columns "
                    "FROM pg_class c WHERE c.oid = 'sourcing_drafts'::regclass"
                )
            )
        ).one()
    assert schema.relrowsecurity is True
    assert schema.relforcerowsecurity is True
    assert schema.jsonb_columns == 2

    # A superuser read proves the row genuinely exists: the 404 is isolation, not absence.
    async with _admin_session() as conn:
        stored = (
            await conn.execute(
                text(
                    "SELECT tenant_id, localized_copies, marketplace_category_attrs, "
                    "target_price_usd FROM sourcing_drafts WHERE id = :did"
                ),
                {"did": draft.id},
            )
        ).one()
    assert stored.tenant_id == tenant_a
    assert stored.localized_copies == {}
    assert stored.marketplace_category_attrs == {}
    assert stored.target_price_usd == draft.target_price_usd
    assert tenant_b != tenant_a
