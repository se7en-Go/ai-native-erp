"""Wave-5 honest-state coverage: 1688 publishing truthfulness + growth outreach safety.

Both audit fixes are locked down against the real PostgreSQL database with the
fail-closed RLS policies in force, and **nothing reaches the network**: every outbound
call (Overpass, Resend) is served by ``httpx.MockTransport`` and any unexpected
request fails the test.

Covered invariants:

* ``SourcingService.publish`` stores a *local SKU mapping* only. It must not present the
  internal SKU as a remote marketplace product id, and every result / history entry /
  audit record says ``mapping_ready`` until an external listing is confirmed.
* A production-configured ``GrowthService`` never silently substitutes the synthetic
  fallback catalogue for real leads, never syncs synthetic rows into the CRM, and never
  hands a synthetic prospect to Resend/SMTP.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Configure every ORM module so the string FKs used below resolve in this isolated
# test app, exactly like the api_main composition root.
import erp_core.crm.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.catalog.sourcing_service import (
    PENDING_LISTING_PREFIX,
    SourcingCollectRequest,
    SourcingPublishRequest,
    SourcingService,
    draft_listing_state,
)
from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.growth.collectors import FALLBACK_PROVIDER
from erp_core.growth.router import _service
from erp_core.growth.router import router as growth_router
from erp_core.growth.service import (
    REASON_SYNTHETIC_BLOCKED,
    GrowthService,
    dedupe_fingerprint,
    is_synthetic_prospect,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
BASE = "/api/v1/growth"
RESEND_KEY = "re_wave5_key"
PUBLIC_IP = "93.184.216.34"


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


async def public_dns_resolver(host: str) -> Sequence[str]:
    """Deterministic stand-in for DNS: every host resolves to a public address."""
    assert host
    return (PUBLIC_IP,)


@pytest.fixture
async def workspace(runtime_session_factory: Factory) -> dict[str, Any]:
    tenant = uuid4()
    owner, manager = (uuid4() for _ in range(2))
    async with tenant_session(runtime_session_factory, tenant) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Wave 5 B')"),
            {"id": tenant, "slug": f"wave5-b-{tenant.hex}"},
        )
        for uid, role in ((owner, UserRole.OWNER), (manager, UserRole.MANAGER)):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, :name, 'test')"
                ),
                {"id": uid, "email": f"{uid.hex}@wave5b.example.com", "name": role.value},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (:tid, :uid, :role)"
                ),
                {"tid": tenant, "uid": uid, "role": role.value},
            )
    return {
        "tenant": tenant,
        "owner_id": owner,
        "owner": CurrentPrincipal(
            user_id=owner, tenant_id=tenant, role=UserRole.OWNER, email="owner@wave5b.example.com"
        ),
        "manager": CurrentPrincipal(
            user_id=manager,
            tenant_id=tenant,
            role=UserRole.MANAGER,
            email="manager@wave5b.example.com",
        ),
    }


# --------------------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------------------


async def _seed_channel_account(
    session: AsyncSession, tenant_id: UUID, channel_type: str, external_account_id: str
) -> UUID:
    account_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO channel_accounts (id, tenant_id, channel_type, external_account_id, "
            "display_name, status, config) VALUES (:id, :tid, :ctype, :ext, :name, 'active', "
            "'{}'::jsonb)"
        ),
        {
            "id": account_id,
            "tid": tenant_id,
            "ctype": channel_type,
            "ext": external_account_id,
            "name": f"Wave5 {channel_type}",
        },
    )
    return account_id


async def _seed_email_account(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    external: str = "sales@ledfactory.example.com",
    display_name: str = "Export Sales",
) -> UUID:
    account_id = await _seed_channel_account(session, tenant_id, "email", external)
    await session.execute(
        text(
            "UPDATE channel_accounts SET display_name = :name WHERE id = :id"
        ),
        {"id": account_id, "name": display_name},
    )
    await CredentialVaultHelper().save_secret_json(
        session,
        tenant_id,
        account_id,
        "messaging_provider",
        {"provider": "resend", "api_key": RESEND_KEY},
    )
    return account_id


async def _seed_prospect(
    factory: Factory,
    tenant_id: UUID,
    *,
    company_name: str,
    email: str | None,
    raw_metadata: dict[str, Any],
    city: str = "Hamburg",
    country: str = "DE",
) -> UUID:
    """Insert an already-cleaned prospect with an explicit provenance marker."""
    prospect_id = uuid4()
    fingerprint = dedupe_fingerprint(
        phone_e164=None,
        email=email,
        website=None,
        company_name=company_name,
        city=city,
        country=country,
    )
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO collected_prospects (id, tenant_id, company_name, contact_name, "
                "email, country, city, grade, dedupe_fingerprint, sync_status, raw_metadata) "
                "VALUES (:id, :tid, :name, 'Purchasing', :email, :country, :city, 'A', :fp, "
                "'cleaned', CAST(:meta AS jsonb))"
            ),
            {
                "id": prospect_id,
                "tid": tenant_id,
                "name": company_name,
                "email": email,
                "country": country,
                "city": city,
                "fp": fingerprint,
                "meta": json.dumps(raw_metadata),
            },
        )
    return prospect_id


async def _count_rows(factory: Factory, tenant_id: UUID, sql: str) -> int:
    async with tenant_session(factory, tenant_id) as session:
        return int(await session.scalar(text(sql), {"tid": tenant_id}) or 0)


# --------------------------------------------------------------------------------------
# Growth test app: injectable service, no network
# --------------------------------------------------------------------------------------


def offline_transport(calls: list[str] | None = None) -> httpx.MockTransport:
    """Every live provider is unreachable — the only honest outcome is a failed job."""
    log = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(str(request.url))
        raise httpx.ConnectError("network is unreachable", request=request)

    return httpx.MockTransport(handler)


def resend_transport(
    sent: list[dict[str, Any]], unexpected: list[str]
) -> httpx.MockTransport:
    """Serve ``POST https://api.resend.com/emails`` and fail on anything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.resend.com":
            unexpected.append(f"{request.method} {request.url}")
            raise AssertionError(f"unexpected outbound request to {request.url}")
        assert request.headers["authorization"] == f"Bearer {RESEND_KEY}"
        sent.append(json.loads(request.content.decode()))
        return json_response({"id": f"re_msg_{len(sent)}"})

    return httpx.MockTransport(handler)


def client_for(
    factory: Factory,
    principal: CurrentPrincipal,
    *,
    collection: httpx.MockTransport | None = None,
    outreach: httpx.MockTransport | None = None,
    allow_synthetic_fallback: bool | None = None,
) -> AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    service = GrowthService(
        http_client=httpx.AsyncClient(transport=collection) if collection else None,
        overpass_min_interval_seconds=0.0,
        email_http_transport=outreach,
        dns_resolver=public_dns_resolver,
        allow_synthetic_fallback=allow_synthetic_fallback,
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[_service] = lambda: service
    app.include_router(growth_router, prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# --------------------------------------------------------------------------------------
# 1. 1688 sourcing: publishing is a local SKU mapping, never a live listing
# --------------------------------------------------------------------------------------


async def _collect_and_publish(
    session: AsyncSession, tenant_id: UUID, actor_id: str
) -> tuple[Any, list[Any], UUID]:
    account_id = await _seed_channel_account(
        session, tenant_id, "amazon_sp", "A2EUQ1WTGCTBG2"
    )
    service = SourcingService()
    draft = await service.collect(
        session,
        tenant_id,
        actor_id,
        SourcingCollectRequest(
            source_platform="1688",
            raw_title="LED太阳能投光灯100W户外防水",
            raw_price_cny=Decimal("45.00"),
            category="LED Lighting",
            hs_code="9405429000",
        ),
    )
    results = await service.publish(
        session,
        tenant_id,
        actor_id,
        draft.id,
        SourcingPublishRequest(channel_account_ids=[account_id]),
    )
    return draft, results, account_id


async def test_publish_reports_mapping_ready_and_a_pending_listing_id(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])
    async with tenant_session(runtime_session_factory, tenant) as session:
        draft, results, _account = await _collect_and_publish(session, tenant, actor)

        assert len(results) == 1
        result = results[0]
        assert result.listing_state == "mapping_ready"
        assert result.external_listing_confirmed is False
        assert result.publish_mode == "local_sku_mapping"
        assert result.external_sku.startswith("AMAZON-SRC-1688-")
        assert result.external_product_id == (
            f"{PENDING_LISTING_PREFIX}{result.external_sku}"
        )
        assert result.external_product_id.startswith("pending-listing:")

        # The 0012 DB CHECK still governs `status`: publishing keeps 'published'.
        row = (
            await session.execute(
                text(
                    "SELECT status, publish_history FROM sourcing_drafts "
                    "WHERE tenant_id = :tid AND id = :did"
                ),
                {"tid": tenant, "did": draft.id},
            )
        ).one()
        assert row.status == "published"
        entry = row.publish_history[-1]
        assert entry["listing_state"] == "mapping_ready"
        assert entry["external_listing_confirmed"] is False
        assert entry["publish_mode"] == "local_sku_mapping"
        assert entry["external_product_id"] == f"pending-listing:{result.external_sku}"

        mapping = (
            await session.execute(
                text(
                    "SELECT external_product_id, external_sku FROM platform_sku_mappings "
                    "WHERE tenant_id = :tid AND id = :mid"
                ),
                {"tid": tenant, "mid": result.mapping_id},
            )
        ).one()
        # The internal SKU is never dressed up as a live marketplace product id.
        assert mapping.external_product_id == f"pending-listing:{result.external_sku}"
        assert mapping.external_product_id != mapping.external_sku
        # The bare internal SKU is never presented as a remote marketplace id.
        assert mapping.external_product_id != draft.suggested_sku
        assert mapping.external_product_id.startswith("pending-listing:")

        read = await SourcingService().get_draft(session, tenant, draft.id)
        assert read.status == "published"
        assert read.listing_state == "mapping_ready"

        audit = (
            await session.execute(
                text(
                    "SELECT after_state FROM audit_events "
                    "WHERE action = 'catalog.sourcing_draft.published' "
                    "AND resource_id = :did"
                ),
                {"did": draft.id},
            )
        ).one()
        assert audit.after_state["listing_state"] == "mapping_ready"
        assert audit.after_state["external_listing_confirmed"] is False


async def test_listing_state_becomes_live_only_after_a_confirmed_external_listing(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    actor = str(workspace["owner_id"])
    service = SourcingService()
    async with tenant_session(runtime_session_factory, tenant) as session:
        draft, _results, _account = await _collect_and_publish(session, tenant, actor)
        assert (await service.get_draft(session, tenant, draft.id)).listing_state == (
            "mapping_ready"
        )

        history = [dict(entry) for entry in draft.publish_history]
        history.append(
            {
                "channel_account_id": str(uuid4()),
                "channel_type": "amazon_sp",
                "external_sku": "AMAZON-SRC-1688-LIVE",
                "price_usd": "10.42",
                "published_at": "2026-01-01T00:00:00+00:00",
                "listing_state": "live_published",
                "external_listing_confirmed": True,
                "publish_mode": "live_marketplace_api",
                "external_product_id": "B0EXAMPLE1",
            }
        )
        await session.execute(
            text(
                "UPDATE sourcing_drafts SET publish_history = CAST(:history AS jsonb) "
                "WHERE tenant_id = :tid AND id = :did"
            ),
            {"history": json.dumps(history), "tid": tenant, "did": draft.id},
        )
        upgraded = await service.get_draft(session, tenant, draft.id)
        assert upgraded.listing_state == "live_published"
        # The DB CHECK column is untouched by the honest derived state.
        assert upgraded.status == "published"


def test_draft_listing_state_derivation_is_pure_and_fail_closed() -> None:
    assert draft_listing_state("draft", []) == "draft"
    assert draft_listing_state("imported", []) == "imported"
    assert draft_listing_state("published", []) == "mapping_ready"
    assert (
        draft_listing_state("published", [{"external_listing_confirmed": False}])
        == "mapping_ready"
    )
    # Only an explicit boolean True confirms a live listing — nothing else.
    assert (
        draft_listing_state("published", [{"external_listing_confirmed": "true"}])
        == "mapping_ready"
    )
    assert (
        draft_listing_state("published", [{"external_listing_confirmed": True}])
        == "live_published"
    )


# --------------------------------------------------------------------------------------
# 2. Growth collection: production never substitutes synthetic leads
# --------------------------------------------------------------------------------------


def test_is_synthetic_prospect_marks_every_fallback_provenance() -> None:
    class Row:
        def __init__(self, raw_metadata: dict[str, Any] | None) -> None:
            self.raw_metadata = raw_metadata

    assert is_synthetic_prospect(None) is False
    assert is_synthetic_prospect(Row({})) is False  # type: ignore[arg-type]
    assert is_synthetic_prospect(Row({"provider": "overpass_osm"})) is False  # type: ignore[arg-type]
    assert is_synthetic_prospect(Row({"synthetic": True})) is True  # type: ignore[arg-type]
    assert is_synthetic_prospect(Row({"provider": FALLBACK_PROVIDER})) is True  # type: ignore[arg-type]
    assert is_synthetic_prospect(Row({"job_source": FALLBACK_PROVIDER})) is True  # type: ignore[arg-type]
    assert (
        is_synthetic_prospect(Row({"provider": "overpass_osm", "job_source": FALLBACK_PROVIDER}))
        is True
    )  # type: ignore[arg-type]


async def test_auto_mode_fails_the_job_instead_of_inserting_synthetic_leads(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    attempted: list[str] = []
    async with client_for(
        runtime_session_factory,
        workspace["owner"],
        collection=offline_transport(attempted),
        allow_synthetic_fallback=False,
    ) as client:
        response = await client.post(
            f"{BASE}/jobs",
            json={"cities": ["Dubai, AE"], "keywords": ["led strip"], "max_results": 4},
        )
        assert response.status_code == 502, response.text
        assert response.json()["code"] == "ERR_GROWTH_PROVIDER_UNAVAILABLE"

        # The live provider really was attempted before the job failed honestly.
        assert any("overpass-api.de" in url for url in attempted)

        jobs = (await client.get(f"{BASE}/jobs")).json()["items"]
        assert jobs[0]["status"] == "failed"
        assert jobs[0]["collected_count"] == 0
        assert jobs[0]["error_message"] is not None
        assert "auto:" in jobs[0]["error_message"]
        assert "Synthetic fallback collection is disabled" in jobs[0]["error_message"]

        # No fabricated prospect reached the database.
        assert (await client.get(f"{BASE}/prospects")).json()["total"] == 0

    assert (
        await _count_rows(
            runtime_session_factory,
            tenant,
            "SELECT count(*) FROM collected_prospects WHERE tenant_id = :tid",
        )
        == 0
    )


async def test_explicit_synthetic_mode_still_works_while_auto_fallback_is_disabled(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    async with client_for(
        runtime_session_factory,
        workspace["owner"],
        collection=offline_transport(),
        allow_synthetic_fallback=False,
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "provider_mode": "synthetic",
                "cities": ["Warsaw, PL"],
                "keywords": ["led strip"],
                "max_results": 3,
            },
        )
        assert created.status_code == 201, created.text
        job = created.json()
        assert job["status"] == "completed"
        assert job["collected_count"] == 3
        assert job["sources"] == {FALLBACK_PROVIDER: 3}

        rows = (await client.get(f"{BASE}/prospects")).json()["items"]
        assert len(rows) == 3
        assert all(row["raw_metadata"]["synthetic"] is True for row in rows)


async def test_default_service_in_a_non_production_env_still_allows_the_fallback(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    """``auto`` keeps its documented fallback outside production (APP_ENV=test)."""
    async with client_for(
        runtime_session_factory, workspace["owner"], collection=offline_transport()
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={"cities": ["Milan, IT"], "keywords": ["led strip"], "max_results": 2},
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "completed"
        assert created.json()["sources"] == {FALLBACK_PROVIDER: 2}


# --------------------------------------------------------------------------------------
# 3. Growth CRM sync: synthetic prospects never become leads/customers
# --------------------------------------------------------------------------------------


async def test_sync_to_crm_rejects_synthetic_prospects_and_creates_no_crm_rows(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    async with client_for(
        runtime_session_factory, workspace["owner"], collection=offline_transport()
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "provider_mode": "synthetic",
                "cities": ["Dubai, AE"],
                "keywords": ["led strip"],
                "max_results": 2,
            },
        )
        assert created.status_code == 201, created.text
        prospects = (await client.get(f"{BASE}/prospects")).json()["items"]
        assert len(prospects) == 2
        assert {row["raw_metadata"]["provider"] for row in prospects} == {FALLBACK_PROVIDER}

        cleaned = await client.post(
            f"{BASE}/prospects/clean",
            json={"prospect_ids": [row["id"] for row in prospects]},
        )
        assert cleaned.status_code == 200, cleaned.text

        rejected = await client.post(
            f"{BASE}/prospects/sync-crm",
            json={"prospect_ids": [row["id"] for row in prospects]},
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["code"] == "ERR_SYNTHETIC_PROSPECT_BLOCKED"
        assert "Synthetic fallback prospects" in rejected.json()["detail"]

    # Nothing was materialized in the CRM.
    assert (
        await _count_rows(
            runtime_session_factory, tenant, "SELECT count(*) FROM leads WHERE tenant_id = :tid"
        )
        == 0
    )
    assert (
        await _count_rows(
            runtime_session_factory,
            tenant,
            "SELECT count(*) FROM customers WHERE tenant_id = :tid",
        )
        == 0
    )
    assert (
        await _count_rows(
            runtime_session_factory,
            tenant,
            "SELECT count(*) FROM b2b_companies WHERE tenant_id = :tid",
        )
        == 0
    )
    # The prospects stay uncleaned-in-CRM: no lead id, no synced status.
    async with tenant_session(runtime_session_factory, tenant) as session:
        statuses = (
            await session.execute(
                text(
                    "SELECT sync_status, crm_lead_id FROM collected_prospects "
                    "WHERE tenant_id = :tid"
                ),
                {"tid": tenant},
            )
        ).all()
    assert {row.sync_status for row in statuses} == {"cleaned"}
    assert all(row.crm_lead_id is None for row in statuses)


# --------------------------------------------------------------------------------------
# 4. Campaigns: synthetic prospects are suppressed, never delivered
# --------------------------------------------------------------------------------------


async def _campaign_with_mixed_prospects(
    factory: Factory,
    client: AsyncClient,
    tenant_id: UUID,
) -> tuple[str, UUID, UUID]:
    """One synthetic (fallback) prospect and one real CSV-imported prospect."""
    synthetic = await _seed_prospect(
        factory,
        tenant_id,
        company_name="Dubai Synthetic Trading LLC",
        email="fake.buyer@dubai-synthetic.example",
        raw_metadata={
            "provider": FALLBACK_PROVIDER,
            "job_source": FALLBACK_PROVIDER,
            "synthetic": True,
        },
        city="Dubai",
        country="AE",
    )
    real = await _seed_prospect(
        factory,
        tenant_id,
        company_name="Hamburg Real Lighting GmbH",
        email="einkauf@hamburg-real-lighting.de",
        raw_metadata={"provider": "overpass_osm", "job_source": "overpass_osm"},
    )
    created = await client.post(
        f"{BASE}/campaigns",
        json={
            "name": "Wave 5 COB strip outreach",
            "channel_type": "email",
            "subject": "Catalogue for {{company_name}}",
            "body_template": "Hello {{contact_name}} at {{company_name}} in {{city}}.",
            "target_ids": [str(synthetic), str(real)],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["total_targets"] == 2
    return created.json()["id"], synthetic, real


async def test_preview_campaign_suppresses_synthetic_prospects(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    async with tenant_session(runtime_session_factory, tenant) as session:
        await _seed_email_account(session, tenant)

    sent: list[dict[str, Any]] = []
    unexpected: list[str] = []
    async with client_for(
        runtime_session_factory,
        workspace["owner"],
        outreach=resend_transport(sent, unexpected),
    ) as client:
        campaign_id, _synthetic, real_id = await _campaign_with_mixed_prospects(
            runtime_session_factory, client, tenant
        )
        preview = await client.post(f"{BASE}/campaigns/{campaign_id}/preview")
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["total_targets"] == 2
        assert body["eligible_count"] == 1
        assert body["suppressed_count"] == 1
        assert body["reasons"] == [{"reason": REASON_SYNTHETIC_BLOCKED, "count": 1}]
        assert REASON_SYNTHETIC_BLOCKED == "合成演示数据禁止外发"
        assert sent == [] and unexpected == []

        # launch_campaign re-runs the same gate inside the transaction.
        launched = await client.post(f"{BASE}/campaigns/{campaign_id}/launch")
        assert launched.status_code == 200, launched.text
        assert launched.json()["sent"] == 1
        assert launched.json()["suppressed"] == 1
        assert launched.json()["reasons"] == [
            {"reason": REASON_SYNTHETIC_BLOCKED, "count": 1}
        ]

        recipients = (await client.get(f"{BASE}/campaigns/{campaign_id}/recipients")).json()
        by_prospect = {
            row["prospect_id"]: row for row in recipients["items"] if row["prospect_id"]
        }
        assert by_prospect[str(real_id)]["status"] == "sent"
        suppressed = [row for row in recipients["items"] if row["status"] == "suppressed"]
        assert len(suppressed) == 1
        assert suppressed[0]["error_reason"] == REASON_SYNTHETIC_BLOCKED


async def test_send_campaign_real_never_contacts_the_mailer_for_synthetic_prospects(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    async with tenant_session(runtime_session_factory, tenant) as session:
        await _seed_email_account(session, tenant)

    sent: list[dict[str, Any]] = []
    unexpected: list[str] = []
    async with client_for(
        runtime_session_factory,
        workspace["owner"],
        outreach=resend_transport(sent, unexpected),
    ) as client:
        campaign_id, synthetic_id, real_id = await _campaign_with_mixed_prospects(
            runtime_session_factory, client, tenant
        )

        dry = await client.post(
            f"{BASE}/campaigns/{campaign_id}/send-real", json={"dry_run": True}
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["sent_count"] == 1
        assert dry.json()["suppressed_count"] == 1
        assert dry.json()["failed_count"] == 0
        # Not even a dry run is allowed to consider the synthetic row sendable.
        assert sent == []

        response = await client.post(
            f"{BASE}/campaigns/{campaign_id}/send-real", json={"max_send": 10}
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["provider"] == "resend"
        assert result["sent_count"] == 1
        assert result["suppressed_count"] == 1
        assert result["failed_count"] == 0

        # Exactly one outbound HTTP call, and only for the real prospect.
        assert unexpected == []
        assert len(sent) == 1
        payload = sent[0]
        assert payload["from"] == "Export Sales <sales@ledfactory.example.com>"
        assert payload["to"] == ["einkauf@hamburg-real-lighting.de"]
        assert payload["subject"] == "Catalogue for Hamburg Real Lighting GmbH"
        assert "Hello Purchasing at Hamburg Real Lighting GmbH" in payload["text"]
        assert "fake.buyer@dubai-synthetic.example" not in json.dumps(sent)

        recipients = (await client.get(f"{BASE}/campaigns/{campaign_id}/recipients")).json()
        by_prospect = {
            row["prospect_id"]: row for row in recipients["items"] if row["prospect_id"]
        }
        blocked = by_prospect[str(synthetic_id)]
        assert blocked["status"] == "suppressed"
        assert blocked["error_reason"] == REASON_SYNTHETIC_BLOCKED
        assert by_prospect[str(real_id)]["status"] == "sent"
        assert by_prospect[str(real_id)]["sent_at"] is not None

    async with tenant_session(runtime_session_factory, tenant) as session:
        audit = (
            await session.execute(
                text(
                    "SELECT after_state FROM audit_events "
                    "WHERE action = 'growth.campaign_sent_real' AND resource_id = :cid"
                ),
                {"cid": campaign_id},
            )
        ).one()
    assert audit.after_state["sent"] == 1
    assert audit.after_state["suppressed"] == 1
