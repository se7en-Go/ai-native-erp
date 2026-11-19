"""Phase 3C growth APIs verified against real PostgreSQL, Redis and MockTransport.

Nothing here reaches the network: every provider call is served by
``httpx.MockTransport``, and campaign launch never performs an outbound send.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Configure every ORM module so string FKs (tenants/users/customers/leads) resolve
# in this isolated test app, exactly like the api_main composition root.
import erp_core.crm.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.growth.router import _service
from erp_core.growth.router import router as growth_router
from erp_core.growth.service import (
    GrowthService,
    csv_safe_cell,
    dedupe_fingerprint,
    grade_for,
    normalize_phone,
    website_domain,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
BASE = "/api/v1/growth"

OVERPASS_PAYLOAD: dict[str, Any] = {
    "elements": [
        {
            "type": "node",
            "id": 1001,
            "lat": 25.2048,
            "lon": 55.2708,
            "tags": {
                "name": "Gulf Lighting Trading LLC",
                "shop": "lighting",
                "phone": "+971 4 555 0100",
                "website": "https://www.gulflighting.example.com/about",
                "email": "sales@gulflighting.example.com",
                "addr:city": "Dubai",
                "addr:country": "AE",
            },
        },
        {
            "type": "way",
            "id": 2002,
            "center": {"lat": 34.0522, "lon": -118.2437},
            "tags": {
                "name": "Pacific LED Wholesale",
                "phone": "+1 213 555 0142",
                "website": "pacificled.example.com",
                "addr:city": "Los Angeles",
                "addr:country": "US",
            },
        },
        {
            "type": "node",
            "id": 3003,
            "lat": 25.2048,
            "lon": 55.2708,
            "tags": {
                "name": "Gulf Lighting Trading (duplicate listing)",
                "phone": "+97145550100",
                "addr:city": "Dubai",
                "addr:country": "AE",
            },
        },
        {"type": "node", "id": 4004, "tags": {"shop": "lighting"}},
    ]
}

GOOGLE_PLACES_PAYLOAD: dict[str, Any] = {
    "places": [
        {
            "id": "ChIJ-place-42",
            "displayName": {"text": "Milan Illuminazione SRL", "languageCode": "it"},
            "formattedAddress": "Via Brera 1, Milano, Italy",
            "rating": 4.6,
            "internationalPhoneNumber": "+39 02 5555 0111",
            "websiteUri": "https://milanilluminazione.example.it",
            "location": {"latitude": 45.4722, "longitude": 9.1922},
            "addressComponents": [
                {"longText": "Milano", "shortText": "MI", "types": ["locality"]},
                {"longText": "Italy", "shortText": "IT", "types": ["country"]},
            ],
        }
    ]
}


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def transport_for(
    *,
    overpass: dict[str, Any] | None = None,
    google: dict[str, Any] | None = None,
    google_status: int = 200,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "places.googleapis.com":
            assert request.headers["X-Goog-Api-Key"] == "test-places-key"
            assert "places.internationalPhoneNumber" in request.headers["X-Goog-FieldMask"]
            return json_response(google or GOOGLE_PLACES_PAYLOAD, google_status)
        if request.url.host == "overpass-api.de":
            assert request.headers["User-Agent"].startswith("ai-native-erp-growth/")
            body = request.content.decode()
            assert "area%5B" in body or "area[" in body
            return json_response(overpass or OVERPASS_PAYLOAD)
        raise AssertionError(f"unexpected outbound request to {request.url}")

    return httpx.MockTransport(handler)


def client_for(
    factory: Factory,
    principal: CurrentPrincipal,
    *,
    transport: httpx.MockTransport | None = None,
    google_places_api_key: str = "",
) -> AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    growth = GrowthService(
        http_client=httpx.AsyncClient(transport=transport) if transport else None,
        google_places_api_key=google_places_api_key,
        overpass_min_interval_seconds=0.0,
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[_service] = lambda: growth
    app.include_router(growth_router, prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def workspace(runtime_session_factory: Factory):
    tenant, other = uuid4(), uuid4()
    owner, manager, agent, outsider, other_owner = (uuid4() for _ in range(5))
    for tid in (tenant, other):
        async with tenant_session(runtime_session_factory, tid) as session:
            await session.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Growth')"),
                {"id": tid, "slug": f"growth-{tid.hex}"},
            )
    async with tenant_session(runtime_session_factory, tenant) as session:
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
                {"id": uid, "email": f"{uid.hex}@growth.example.com", "name": role.value},
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
            {"id": other_owner, "email": f"{other_owner.hex}@growth.example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                "VALUES (:tid, :uid, 'owner')"
            ),
            {"tid": other, "uid": other_owner},
        )
    yield {
        "tenant": tenant,
        "other": other,
        "owner": CurrentPrincipal(
            user_id=owner, tenant_id=tenant, role=UserRole.OWNER, email="owner@growth.example.com"
        ),
        "manager": CurrentPrincipal(
            user_id=manager,
            tenant_id=tenant,
            role=UserRole.MANAGER,
            email="manager@growth.example.com",
        ),
        "agent": CurrentPrincipal(
            user_id=agent, tenant_id=tenant, role=UserRole.AGENT, email="agent@growth.example.com"
        ),
        "outsider": CurrentPrincipal(
            user_id=other_owner,
            tenant_id=other,
            role=UserRole.OWNER,
            email="outsider@growth.example.com",
        ),
        "owner_id": owner,
    }


async def _create_customer(
    factory: Factory,
    tenant_id: UUID,
    *,
    email: str | None,
    phone: str | None,
    display_name: str = "Buyer",
) -> UUID:
    customer_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name, "
                "primary_email, primary_phone_e164) VALUES (:id, :tid, 'b2b_contact', "
                ":name, :email, :phone)"
            ),
            {
                "id": customer_id,
                "tid": tenant_id,
                "name": display_name,
                "email": email,
                "phone": phone,
            },
        )
    return customer_id


async def test_overpass_collection_persists_dedupes_and_is_idempotent(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    async with client_for(
        runtime_session_factory, workspace["owner"], transport=transport_for()
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "overpass_osm",
                "keywords": ["lighting distributor"],
                "target_region": "Dubai, AE",
                "max_results": 10,
                "min_rating": None,
                "require_phone": True,
            },
        )
        assert created.status_code == 201, created.text
        job = created.json()
        assert job["status"] == "pending"
        assert job["collected_count"] == 0

        run = await client.post(f"{BASE}/jobs/{job['id']}/run")
        assert run.status_code == 200, run.text
        completed = run.json()
        # The third element repeats the first phone number, so its fingerprint collides.
        assert completed["status"] == "completed"
        assert completed["collected_count"] == 2
        assert completed["completed_at"] is not None

        again = await client.post(f"{BASE}/jobs/{job['id']}/run")
        assert again.status_code == 200
        assert again.json()["collected_count"] == 2

        prospects = await client.get(f"{BASE}/prospects", params={"job_id": job["id"]})
        assert prospects.status_code == 200, prospects.text
        rows = prospects.json()["items"]
        assert len(rows) == 2
        dubai = next(row for row in rows if row["city"] == "Dubai")
        assert dubai["phone_e164"] == "+97145550100"
        assert dubai["email"] == "sales@gulflighting.example.com"
        assert dubai["latitude"] == pytest.approx(25.2048)
        assert dubai["raw_metadata"]["license"] == "ODbL-1.0"
        assert dubai["sync_status"] == "raw"
        angeles = next(row for row in rows if row["city"] == "Los Angeles")
        assert angeles["latitude"] == pytest.approx(34.0522)

        summary = await client.get(f"{BASE}/summary")
        assert summary.json() == {"collected": 2, "cleaned": 0, "synced": 0, "jobs": 1}


async def test_overture_collector_labels_open_dataset_license(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    async with client_for(
        runtime_session_factory, workspace["owner"], transport=transport_for()
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "overture_poi",
                "keywords": ["lighting"],
                "target_region": "Los Angeles, US",
                "max_results": 5,
            },
        )
        run = await client.post(f"{BASE}/jobs/{created.json()['id']}/run")
        assert run.status_code == 200, run.text
        rows = (await client.get(f"{BASE}/prospects")).json()["items"]
        assert rows[0]["raw_metadata"]["provider"] == "overture_poi"
        assert rows[0]["raw_metadata"]["license"] == "CDLA-Permissive-2.0"


async def test_google_places_requires_a_key_and_reports_a_failed_job(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    async with client_for(
        runtime_session_factory,
        workspace["manager"],
        transport=transport_for(),
        google_places_api_key="",
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "google_places",
                "keywords": ["led lighting"],
                "target_region": "Milan, IT",
                "max_results": 5,
            },
        )
        job_id = created.json()["id"]
        run = await client.post(f"{BASE}/jobs/{job_id}/run")
        assert run.status_code == 409, run.text
        assert run.json()["code"] == "ERR_GOOGLE_PLACES_KEY_MISSING"
        jobs = (await client.get(f"{BASE}/jobs")).json()["items"]
        assert jobs[0]["status"] == "failed"
        assert "GOOGLE_PLACES_API_KEY" in jobs[0]["error_message"]


async def test_google_places_collects_with_injected_transport(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    async with client_for(
        runtime_session_factory,
        workspace["owner"],
        transport=transport_for(),
        google_places_api_key="test-places-key",
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "google_places",
                "keywords": ["led lighting"],
                "target_region": "Milan, IT",
                "max_results": 5,
                "min_rating": 4.5,
            },
        )
        run = await client.post(f"{BASE}/jobs/{created.json()['id']}/run")
        assert run.status_code == 200, run.text
        rows = (await client.get(f"{BASE}/prospects")).json()["items"]
        assert len(rows) == 1
        assert rows[0]["company_name"] == "Milan Illuminazione SRL"
        assert rows[0]["rating"] == "4.60"
        assert rows[0]["phone_e164"] == "+390255550111"
        assert rows[0]["raw_metadata"]["google_place_id"] == "ChIJ-place-42"


async def test_google_places_provider_error_marks_the_job_failed(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, json={"error": {"message": "denied"}})
    )
    async with client_for(
        runtime_session_factory,
        workspace["owner"],
        transport=transport,
        google_places_api_key="test-places-key",
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "google_places",
                "keywords": ["led"],
                "target_region": "Milan, IT",
                "max_results": 5,
            },
        )
        run = await client.post(f"{BASE}/jobs/{created.json()['id']}/run")
        assert run.status_code == 502
        assert run.json()["code"] == "ERR_GROWTH_PROVIDER_UNAVAILABLE"
        jobs = (await client.get(f"{BASE}/jobs")).json()["items"]
        assert jobs[0]["status"] == "failed"


CSV_PAYLOAD = (
    "\ufeffcompany_name,contact_name,phone,email,website,country,city,latitude,longitude,rating\r\n"
    '=HYPERLINK("http://evil.example"),Aisha Khan,+971 4 555 0199,aisha@gulf-led.example.ae,'
    "https://gulf-led.example.ae,AE,Dubai,25.2048,55.2708,4.8\r\n"
    "Jakarta Cahaya Lighting,Budi Santoso,+62 21 5550 1234,,,ID,Jakarta,-6.2088,106.8456,4.2\r\n"
    "Shannon Retail,,,,shannon.ie,IE,Dublin,53.3498,-6.2603,\r\n"
    "HCMC LED Depot,Linh Tran,,linh@hcmc-led.example.vn,,VN,Ho Chi Minh,10.7769,106.7009,\r\n"
)


async def test_csv_import_clean_grades_export_and_crm_sync(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    async with client_for(runtime_session_factory, workspace["owner"]) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "csv_import",
                "max_results": 50,
                "require_phone": False,
                "csv_content": CSV_PAYLOAD,
            },
        )
        assert created.status_code == 201, created.text
        job = created.json()
        assert job["status"] == "completed"
        assert job["collected_count"] == 4

        clean = await client.post(f"{BASE}/prospects/clean", json={})
        assert clean.status_code == 200, clean.text
        result = clean.json()
        assert result["cleaned"] == 4
        assert result["discarded_duplicates"] == 0
        assert result["graded"] == {"A": 1, "B": 2, "C": 1}
        by_company = {row["company_name"]: row for row in result["items"]}
        gulf = by_company['=HYPERLINK("http://evil.example")']
        assert gulf["grade"] == "A"
        assert gulf["phone_e164"] == "+97145550199"
        assert gulf["sync_status"] == "cleaned"
        assert by_company["Jakarta Cahaya Lighting"]["grade"] == "B"
        assert by_company["Shannon Retail"]["grade"] == "C"
        assert by_company["HCMC LED Depot"]["grade"] == "B"
        assert by_company["Jakarta Cahaya Lighting"]["phone_e164"] == "+622155501234"

        exported = await client.get(f"{BASE}/prospects/export.csv")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("text/csv")
        body = exported.text
        assert "\r\n" in body
        # Formula injection is neutralized before RFC 4180 quoting.
        assert '"' + "'=HYPERLINK" in body
        assert "company_name" in body.splitlines()[0]

        sync = await client.post(
            f"{BASE}/prospects/sync-crm",
            json={"prospect_ids": [row["id"] for row in result["items"]]},
        )
        assert sync.status_code == 200, sync.text
        synced = sync.json()
        assert synced["synced"] == 4
        assert synced["skipped"] == 0
        assert len(synced["leads"]) == 4

        repeat = await client.post(
            f"{BASE}/prospects/sync-crm",
            json={"prospect_ids": [row["id"] for row in result["items"]]},
        )
        assert repeat.json() == {"synced": 0, "skipped": 4, "leads": []}

        rows = (await client.get(f"{BASE}/prospects")).json()["items"]
        assert {row["sync_status"] for row in rows if row["crm_lead_id"]} == {"synced_to_crm"}
        summary = await client.get(f"{BASE}/summary")
        assert summary.json() == {"collected": 4, "cleaned": 4, "synced": 4, "jobs": 1}

    async with tenant_session(runtime_session_factory, workspace["tenant"]) as session:
        assert await session.scalar(text("SELECT count(*) FROM leads")) == 4
        assert await session.scalar(text("SELECT count(*) FROM b2b_companies")) == 4
        assert await session.scalar(text("SELECT count(*) FROM customers")) == 4
        lead = (
            await session.execute(
                text(
                    "SELECT source_channel, lead_type, qualification_score, status, "
                    "extracted_requirements FROM leads WHERE qualification_score = 90"
                )
            )
        ).one()
        assert lead.source_channel == "growth_map"
        assert lead.lead_type == "b2b_inquiry"
        assert lead.status == "new"
        assert lead.extracted_requirements["grade"] == "A"


async def test_campaign_preview_and_launch_enforce_email_suppression_and_rfc8058(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    consent = ConsentAndSuppressionService()
    synced_email = await _create_customer(
        runtime_session_factory, tenant, email="synced@gulf-led.example.ae", phone=None
    )
    good_email = await _create_customer(
        runtime_session_factory, tenant, email="buyer@jakarta-lighting.example.id", phone=None
    )
    no_email = await _create_customer(
        runtime_session_factory, tenant, email=None, phone="+97145550188"
    )
    async with tenant_session(runtime_session_factory, tenant) as session:
        await consent.record_stop_opt_out(
            session, tenant, synced_email, "email", "synced@gulf-led.example.ae", "complaint"
        )

    async with client_for(runtime_session_factory, workspace["owner"]) as client:
        created = await client.post(
            f"{BASE}/campaigns",
            json={
                "name": "Dubai LED wholesale outreach",
                "channel_type": "email",
                "subject": "COB strip catalogue",
                "body_template": "Hello {{company_name}}, here is our 2026 catalogue.",
                "target_ids": [str(synced_email), str(good_email), str(no_email)],
            },
        )
        assert created.status_code == 201, created.text
        campaign = created.json()
        assert campaign["status"] == "draft"
        assert campaign["total_targets"] == 3

        preview = await client.post(f"{BASE}/campaigns/{campaign['id']}/preview")
        assert preview.status_code == 200, preview.text
        snapshot = preview.json()
        assert snapshot["eligible_count"] == 1
        assert snapshot["suppressed_count"] == 2
        reasons = {row["reason"]: row["count"] for row in snapshot["reasons"]}
        assert reasons["命中退订/抑制名单"] == 1
        assert reasons["地址缺失"] == 1
        assert campaign["id"] in snapshot["list_unsubscribe_header"]
        assert "mailto:unsubscribe@erp.local" in snapshot["list_unsubscribe_header"]

        launch = await client.post(f"{BASE}/campaigns/{campaign['id']}/launch")
        assert launch.status_code == 200, launch.text
        outcome = launch.json()
        assert outcome == {
            "campaign_id": campaign["id"],
            "status": "completed",
            "sent": 1,
            "suppressed": 2,
            "queued_remaining": 0,
            "reasons": snapshot["reasons"],
        }

        listed = await client.get(f"{BASE}/campaigns")
        assert listed.json()["items"][0]["sent_count"] == 1
        assert listed.json()["items"][0]["suppressed_count"] == 2

        recipients = await client.get(f"{BASE}/campaigns/{campaign['id']}/recipients")
        statuses = {row["address"]: row["status"] for row in recipients.json()["items"]}
        assert statuses["buyer@jakarta-lighting.example.id"] == "sent"
        assert statuses["synced@gulf-led.example.ae"] == "suppressed"

        relock = await client.post(f"{BASE}/campaigns/{campaign['id']}/launch")
        assert relock.status_code == 409
        assert relock.json()["code"] == "ERR_CAMPAIGN_NOT_LAUNCHABLE"

        edit = await client.patch(
            f"{BASE}/campaigns/{campaign['id']}", json={"name": "renamed"}
        )
        assert edit.status_code == 409


async def test_whatsapp_campaign_blocks_us_numbers_missing_opt_in_and_suppression(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    consent = ConsentAndSuppressionService()
    opted_in = await _create_customer(
        runtime_session_factory, tenant, email=None, phone="+97145550177", display_name="Opted In"
    )
    no_opt_in = await _create_customer(
        runtime_session_factory, tenant, email=None, phone="+6281234567890", display_name="No Opt"
    )
    us_number = await _create_customer(
        runtime_session_factory, tenant, email=None, phone="+14155550144", display_name="US Buyer"
    )
    async with tenant_session(runtime_session_factory, tenant) as session:
        await consent.record_opt_in(
            session,
            tenant,
            opted_in,
            "whatsapp_cloud",
            proof_source="click_to_whatsapp_ad",
            disclosed_business_name="Quanzhou Smart LED",
        )
        await consent.record_opt_in(
            session,
            tenant,
            us_number,
            "whatsapp_cloud",
            proof_source="click_to_whatsapp_ad",
        )

    async with client_for(runtime_session_factory, workspace["manager"]) as client:
        created = await client.post(
            f"{BASE}/campaigns",
            json={
                "name": "Opt-in WA broadcast",
                "channel_type": "whatsapp_cloud",
                "body_template": "New COB strip batches are ready.",
                "target_ids": [str(opted_in), str(no_opt_in), str(us_number)],
            },
        )
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]
        assert created.json()["subject"] is None

        preview = await client.post(f"{BASE}/campaigns/{campaign_id}/preview")
        assert preview.status_code == 200, preview.text
        snapshot = preview.json()
        assert snapshot["eligible_count"] == 1
        assert snapshot["suppressed_count"] == 2
        reasons = {row["reason"]: row["count"] for row in snapshot["reasons"]}
        assert reasons["WhatsApp 美国 +1 营销禁令"] == 1
        assert reasons["缺少 WhatsApp Opt-In 许可"] == 1
        assert snapshot["list_unsubscribe_header"] is None

        launch = await client.post(f"{BASE}/campaigns/{campaign_id}/launch")
        assert launch.status_code == 200, launch.text
        assert launch.json()["sent"] == 1
        assert launch.json()["suppressed"] == 2


async def test_campaign_validation_rejects_email_without_subject(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    customer = await _create_customer(
        runtime_session_factory, workspace["tenant"], email="x@example.com", phone=None
    )
    async with client_for(runtime_session_factory, workspace["owner"]) as client:
        response = await client.post(
            f"{BASE}/campaigns",
            json={
                "name": "No subject",
                "channel_type": "email",
                "body_template": "Body",
                "target_ids": [str(customer)],
            },
        )
        assert response.status_code == 422
        bad_source = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "facebook_scraper",
                "keywords": ["led"],
                "target_region": "Dubai, AE",
                "max_results": 5,
            },
        )
        assert bad_source.status_code == 422


async def test_rbac_and_cross_tenant_isolation(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    async with client_for(runtime_session_factory, workspace["owner"]) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "csv_import",
                "max_results": 10,
                "csv_content": (
                    "company_name,phone,email\n"
                    "Alpha LED,+97145550100,a@alpha.example.ae\n"
                ),
            },
        )
        assert created.status_code == 201
        prospect_id = (await client.get(f"{BASE}/prospects")).json()["items"][0]["id"]

    async with client_for(runtime_session_factory, workspace["agent"]) as client:
        # Reads are allowed for field agents, writes are not.
        assert (await client.get(f"{BASE}/jobs")).status_code == 200
        assert (await client.get(f"{BASE}/prospects")).status_code == 200
        assert (await client.get(f"{BASE}/prospects/export.csv")).status_code == 200
        create = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "csv_import",
                "max_results": 5,
                "csv_content": "company_name\nBeta LED\n",
            },
        )
        assert create.status_code == 403
        assert create.json()["code"] == "ERR_INSUFFICIENT_ROLE"
        assert (
            await client.post(
                f"{BASE}/prospects/clean", json={"prospect_ids": [prospect_id]}
            )
        ).status_code == 403

    async with client_for(runtime_session_factory, workspace["outsider"]) as client:
        assert (await client.get(f"{BASE}/jobs")).json()["total"] == 0
        assert (await client.get(f"{BASE}/prospects")).json()["total"] == 0
        assert (await client.get(f"{BASE}/summary")).json()["collected"] == 0
        assert (
            await client.post(f"{BASE}/prospects/clean", json={"prospect_ids": [prospect_id]})
        ).json()["cleaned"] == 0
        assert (
            await client.post(
                f"{BASE}/prospects/sync-crm", json={"prospect_ids": [prospect_id]}
            )
        ).json() == {"synced": 0, "skipped": 0, "leads": []}
        assert (await client.get(f"{BASE}/prospects/export.csv")).text.count("\r\n") == 1


async def test_growth_helpers_are_deterministic() -> None:
    assert normalize_phone("+971 4 555 0100", None) == "+97145550100"
    assert normalize_phone("021 5550 1234", "ID") == "+622155501234"
    # A local number without any region hint is dropped instead of being invented.
    assert normalize_phone("021 5550 1234", None) is None
    assert normalize_phone("not a phone", None) is None
    assert website_domain("https://www.Gulf-Led.Example.ae/about?x=1") == "gulf-led.example.ae"
    assert website_domain("not a domain") is None

    fingerprint = dedupe_fingerprint(
        phone_e164="+97145550100",
        email="sales@gulf.example.ae",
        website="https://gulf.example.ae",
        company_name="Gulf Lighting",
        city="Dubai",
        country="AE",
    )
    assert fingerprint == dedupe_fingerprint(
        phone_e164="+97145550100",
        email="other@other.example",
        website=None,
        company_name="Different name",
        city=None,
        country=None,
    )
    assert fingerprint != dedupe_fingerprint(
        phone_e164=None,
        email="sales@gulf.example.ae",
        website=None,
        company_name="Gulf Lighting",
        city="Dubai",
        country="AE",
    )

    phone_and_email = grade_for(
        phone_e164="+97145550100", email="a@b.example", website=None, rating=None
    )
    email_only_high_rating = grade_for(
        phone_e164=None, email="a@b.example", website=None, rating=Decimal("4.9")
    )
    website_only = grade_for(phone_e164=None, email=None, website="a.example", rating=None)
    assert (phone_and_email, email_only_high_rating, website_only) == ("A", "B", "C")

    assert csv_safe_cell("=1+1") == "'=1+1"
    assert csv_safe_cell("normal") == "normal"
    assert csv_safe_cell(None) == ""
    assert json.dumps({"ok": True})


async def test_clean_discards_a_canonical_fingerprint_duplicate(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    """A legacy importer may store the same phone twice; cleaning must collapse it."""
    tenant, owner = workspace["tenant"], workspace["owner_id"]
    async with tenant_session(runtime_session_factory, tenant) as session:
        job_id = await session.scalar(
            text(
                "INSERT INTO lead_collection_jobs (tenant_id, source_provider, max_results, "
                "created_by) VALUES (:tid, 'csv_import', 10, :owner) RETURNING id"
            ),
            {"tid": tenant, "owner": owner},
        )
        for name, phone, fingerprint in (
            ("Gulf Lighting Trading LLC", "+97145550100", "legacy-a"),
            ("Gulf Lighting Trading L.L.C.", "97145550100", "legacy-b"),
        ):
            await session.execute(
                text(
                    "INSERT INTO collected_prospects (tenant_id, job_id, company_name, "
                    "phone_e164, country, city, dedupe_fingerprint) VALUES (:tid, :job, :name, "
                    ":phone, 'AE', 'Dubai', :fp)"
                ),
                {
                    "tid": tenant,
                    "job": job_id,
                    "name": name,
                    "phone": phone,
                    "fp": fingerprint,
                },
            )

    async with client_for(runtime_session_factory, workspace["owner"]) as client:
        clean = await client.post(f"{BASE}/prospects/clean", json={})
        assert clean.status_code == 200, clean.text
        result = clean.json()
        assert result["cleaned"] == 1
        assert result["discarded_duplicates"] == 1

        rows = (await client.get(f"{BASE}/prospects")).json()["items"]
        assert len(rows) == 2
        kept = next(row for row in rows if row["sync_status"] == "cleaned")
        discarded = next(row for row in rows if row["sync_status"] == "discarded")
        assert kept["phone_e164"] == "+97145550100"
        assert discarded["raw_metadata"]["discarded_reason"] == "duplicate_fingerprint"
        assert discarded["raw_metadata"]["duplicate_of"] == kept["id"]


def _load_seed_module() -> Any:
    import importlib.util
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    path = repo_root / "apps" / "backend" / "scripts" / "seed_hybrid_factory.py"
    spec = importlib.util.spec_from_file_location("seed_hybrid_factory_3c", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def test_growth_demo_seed_is_idempotent_and_graded(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    seed_mod = _load_seed_module()
    tenant, owner = workspace["tenant"], workspace["owner_id"]
    for _ in range(2):
        async with tenant_session(runtime_session_factory, tenant) as session:
            await seed_mod._seed_phase3c_growth(session, tenant_id=tenant, owner_id=owner)

    async with client_for(runtime_session_factory, workspace["owner"]) as client:
        jobs = (await client.get(f"{BASE}/jobs")).json()
        assert jobs["total"] == 1
        assert jobs["items"][0]["status"] == "completed"
        assert jobs["items"][0]["collected_count"] == 6
        assert jobs["items"][0]["source_provider"] == "overture_poi"

        prospects = (await client.get(f"{BASE}/prospects")).json()
        assert prospects["total"] == 6
        grades = sorted(row["grade"] for row in prospects["items"])
        assert grades == ["A", "A", "A", "A", "B", "C"]
        # The demo rows carry real global coordinates so the Leaflet map can pin them.
        located = [
            row for row in prospects["items"] if row["latitude"] is not None and row["longitude"]
        ]
        assert len(located) == 6
        assert {"Dubai", "Los Angeles", "Milan", "Jakarta"} <= {
            row["city"] for row in prospects["items"]
        }

        campaigns = (await client.get(f"{BASE}/campaigns")).json()
        assert campaigns["total"] == 1
        campaign = campaigns["items"][0]
        assert campaign["channel_type"] == "email"
        assert campaign["status"] == "draft"
        assert campaign["total_targets"] == 2

        preview = await client.post(f"{BASE}/campaigns/{campaign['id']}/preview")
        assert preview.status_code == 200, preview.text
        assert preview.json()["eligible_count"] == 2
        assert preview.json()["suppressed_count"] == 0
