"""Pack C: real OSM/Overpass collection, public-website e-mail enrichment, real outreach.

Every test runs against the real PostgreSQL test database (`erp_saas_test`) with the
fail-closed RLS policies in force, and **nothing reaches the network**: the Overpass
interpreter, Nominatim, the prospect websites and the Resend API are all served by
``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs
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
from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.growth.collectors import CollectRequest, fallback_catalogue
from erp_core.growth.router import _service
from erp_core.growth.router import router as growth_router
from erp_core.growth.service import OPT_OUT_FOOTER, GrowthService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
BASE = "/api/v1/growth"
COLLECTOR_UA = "AI-Native-ERP-LeadCollector/1.0"
RESEND_KEY = "re_pack_c_key"
PUBLIC_IP = "93.184.216.34"

DUBAI_BBOX = "25.000000,55.100000,25.400000,55.400000"
MILAN_BBOX = "45.400000,9.100000,45.550000,9.250000"

NOMINATIM_RESPONSES: dict[str, list[dict[str, Any]]] = {
    "Dubai, AE": [
        {
            "lat": "25.2048",
            "lon": "55.2708",
            "display_name": "Dubai, United Arab Emirates",
            "boundingbox": ["25.0000", "25.4000", "55.1000", "55.4000"],
        }
    ],
    "Milan, IT": [
        {
            "lat": "45.4642",
            "lon": "9.1900",
            "display_name": "Milano, Italia",
            "boundingbox": ["45.4000", "45.5500", "9.1000", "9.2500"],
        }
    ],
}

OVERPASS_DUBAI: dict[str, Any] = {
    "elements": [
        {
            "type": "node",
            "id": 910001,
            "lat": 25.2048,
            "lon": 55.2708,
            "tags": {
                "name": "Gulf Lighting Trading LLC",
                "shop": "lighting",
                "phone": "+971 4 555 0100",
                "website": "https://www.gulflighting.ae/about",
                "email": "sales@gulflighting.ae",
                "addr:city": "Dubai",
                "addr:country": "AE",
            },
        },
        {
            "type": "way",
            "id": 910002,
            "center": {"lat": 25.1189, "lon": 55.2033},
            "tags": {
                "name": "Al Quoz LED Supply FZE",
                "office": "company",
                "contact:phone": "+971 4 555 0110",
            },
        },
        {"type": "node", "id": 910003, "tags": {"shop": "lighting"}},
    ]
}

OVERPASS_MILAN: dict[str, Any] = {
    "elements": [
        {
            "type": "node",
            "id": 920001,
            "lat": 45.4722,
            "lon": 9.1922,
            "tags": {
                "name": "Illuminazione Brera SRL",
                "shop": "lighting",
                "phone": "+39 02 5555 0111",
                "website": "https://illuminazionebrera.it",
                "addr:city": "Milano",
                "addr:country": "IT",
            },
        }
    ]
}

NORDLICHT_HOME = b"""<html><body>
  <h1>Nordlicht Technik GmbH</h1>
  <p>LED components for industrial buyers.</p>
  <a href="/contact">Kontakt</a>
</body></html>"""

NORDLICHT_CONTACT = b"""<html><body>
  <p>Purchasing desk:</p>
  <a href="mailto:purchasing@nordlicht-lichttechnik.de?subject=RFQ">purchasing</a>
  <img src="/assets/logo@2x.png" alt="logo">
  <p>Template placeholders: you@example.com</p>
</body></html>"""

SOLO_HOME = b"""<html><body><p>Solo Packaging SRL - carton converting.</p></body></html>"""


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


async def public_dns_resolver(host: str) -> Sequence[str]:
    """Deterministic stand-in for DNS: every prospect host resolves to a public IP."""
    return (PUBLIC_IP,)


def collection_transport(
    *, calls: list[tuple[str, str]] | None = None, overpass_status: int = 200
) -> httpx.MockTransport:
    """Serve Nominatim + the Overpass interpreter and record what was requested."""
    log = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "nominatim.openstreetmap.org":
            assert request.headers["User-Agent"] == COLLECTOR_UA
            assert request.url.params["format"] == "jsonv2"
            query = request.url.params["q"]
            log.append(("nominatim", query))
            return json_response(NOMINATIM_RESPONSES.get(query, []))
        if request.url.host == "overpass-api.de":
            assert request.headers["User-Agent"].startswith("ai-native-erp-growth/")
            body = parse_qs(request.content.decode())["data"][0]
            log.append(("overpass", body))
            if overpass_status != 200:
                return httpx.Response(overpass_status, json={"error": "unavailable"})
            if DUBAI_BBOX in body:
                return json_response(OVERPASS_DUBAI)
            if MILAN_BBOX in body:
                return json_response(OVERPASS_MILAN)
            return json_response({"elements": []})
        raise AssertionError(f"unexpected outbound request to {request.url}")

    return httpx.MockTransport(handler)


def offline_transport() -> httpx.MockTransport:
    """Every public endpoint is down: collection must fall back, never crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network is unreachable", request=request)

    return httpx.MockTransport(handler)


def outreach_transport(
    *, sent: list[dict[str, Any]], fetched: list[str]
) -> httpx.MockTransport:
    """Serve the prospect websites plus the Resend ``POST /emails`` endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host == "api.resend.com":
            assert request.headers["authorization"] == f"Bearer {RESEND_KEY}"
            sent.append(json.loads(request.content.decode()))
            return json_response({"id": f"re_msg_{len(sent)}"})
        fetched.append(str(request.url))
        assert request.headers["User-Agent"] == COLLECTOR_UA
        if host == "nordlicht-lichttechnik.de":
            if request.url.path == "/contact":
                return httpx.Response(
                    200, content=NORDLICHT_CONTACT, headers={"content-type": "text/html"}
                )
            return httpx.Response(
                200, content=NORDLICHT_HOME, headers={"content-type": "text/html"}
            )
        if host == "solo-packaging.it":
            return httpx.Response(
                200, content=SOLO_HOME, headers={"content-type": "text/html"}
            )
        raise AssertionError(f"unexpected prospect fetch to {request.url}")

    return httpx.MockTransport(handler)


def client_for(
    factory: Factory,
    principal: CurrentPrincipal,
    *,
    collection: httpx.MockTransport | None = None,
    outreach: httpx.MockTransport | None = None,
    google_places_api_key: str = "",
) -> AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    growth = GrowthService(
        http_client=httpx.AsyncClient(transport=collection) if collection else None,
        google_places_api_key=google_places_api_key,
        overpass_min_interval_seconds=0.0,
        email_http_transport=outreach,
        dns_resolver=public_dns_resolver,
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[_service] = lambda: growth
    app.include_router(growth_router, prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def workspace(runtime_session_factory: Factory):
    tenant = uuid4()
    owner, manager, agent = (uuid4() for _ in range(3))
    async with tenant_session(runtime_session_factory, tenant) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Pack C')"),
            {"id": tenant, "slug": f"pack-c-{tenant.hex}"},
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
                {"id": uid, "email": f"{uid.hex}@packc.example.com", "name": role.value},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (:tid, :uid, :role)"
                ),
                {"tid": tenant, "uid": uid, "role": role.value},
            )
    yield {
        "tenant": tenant,
        "owner_id": owner,
        "agent_id": agent,
        "owner": CurrentPrincipal(
            user_id=owner, tenant_id=tenant, role=UserRole.OWNER, email="owner@packc.example.com"
        ),
        "manager": CurrentPrincipal(
            user_id=manager,
            tenant_id=tenant,
            role=UserRole.MANAGER,
            email="manager@packc.example.com",
        ),
        "agent": CurrentPrincipal(
            user_id=agent, tenant_id=tenant, role=UserRole.AGENT, email="agent@packc.example.com"
        ),
    }


# --------------------------------------------------------------------------------------
# 1. Real Overpass + Nominatim multi-city batch collection
# --------------------------------------------------------------------------------------


async def test_multi_city_overpass_batch_geocodes_bboxes_and_extracts_real_poi_fields(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    calls: list[tuple[str, str]] = []
    transport = collection_transport(calls=calls)
    async with client_for(
        runtime_session_factory, workspace["manager"], collection=transport
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "cities": ["Dubai, AE", "Milan, IT"],
                "keywords": ["lighting distributor"],
                "max_results": 20,
            },
        )
        assert created.status_code == 201, created.text
        job = created.json()
        # A batch recipe is request-scoped, so it executes inside this request.
        assert job["status"] == "completed"
        assert job["collected_count"] == 3
        assert job["source_provider"] == "overpass_osm"
        assert job["target_region"] == "Dubai, AE; Milan, IT"
        assert job["completed_at"] is not None
        assert job["sources"] == {"overpass_osm": 3}

        # Both cities were geocoded once, politely, with the documented User-Agent.
        assert [payload for kind, payload in calls if kind == "nominatim"] == [
            "Dubai, AE",
            "Milan, IT",
        ]
        queries = [payload for kind, payload in calls if kind == "overpass"]
        assert len(queries) == 2
        assert all("area[" not in query for query in queries)
        assert all('nwr["shop"]' in query for query in queries)
        assert all('nwr["office"]' in query for query in queries)
        assert DUBAI_BBOX in queries[0]
        assert MILAN_BBOX in queries[1]
        assert "lighting|distributor" in queries[0]

        listed = await client.get(f"{BASE}/jobs")
        assert listed.json()["items"][0]["sources"] == {"overpass_osm": 3}

        rows = (await client.get(f"{BASE}/prospects")).json()["items"]
        assert len(rows) == 3
        gulf = next(row for row in rows if row["company_name"] == "Gulf Lighting Trading LLC")
        assert gulf["phone_e164"] == "+97145550100"
        assert gulf["email"] == "sales@gulflighting.ae"
        assert gulf["website"] == "https://www.gulflighting.ae/about"
        assert gulf["latitude"] == pytest.approx(25.2048)
        assert gulf["longitude"] == pytest.approx(55.2708)
        assert gulf["city"] == "Dubai"
        assert gulf["country"] == "AE"
        assert gulf["raw_metadata"]["provider"] == "overpass_osm"
        assert gulf["raw_metadata"]["job_source"] == "overpass_osm"
        assert gulf["raw_metadata"]["license"] == "ODbL-1.0"
        # `addr:city` is absent on this way, so the geocoded region labels the row.
        al_quoz = next(row for row in rows if row["company_name"] == "Al Quoz LED Supply FZE")
        assert al_quoz["city"] == "Dubai"
        assert al_quoz["phone_e164"] == "+97145550110"
        assert al_quoz["latitude"] == pytest.approx(25.1189)
        brera = next(row for row in rows if row["company_name"] == "Illuminazione Brera SRL")
        assert brera["city"] == "Milano"
        assert brera["country"] == "IT"
        assert brera["latitude"] == pytest.approx(45.4722)

        # A completed job is idempotent; a second run never duplicates rows.
        again = await client.post(f"{BASE}/jobs/{job['id']}/run")
        assert again.status_code == 200
        assert again.json()["collected_count"] == 3
        assert (await client.get(f"{BASE}/prospects")).json()["total"] == 3


async def test_auto_mode_falls_back_to_the_deterministic_catalogue_when_offline(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    async with client_for(
        runtime_session_factory, workspace["owner"], collection=offline_transport()
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "cities": ["Dubai, AE", "Milan, IT"],
                "keywords": ["led strip"],
                "max_results": 4,
            },
        )
        assert created.status_code == 201, created.text
        job = created.json()
        # The job never crashes: it completes with the labelled fallback catalogue.
        assert job["status"] == "completed"
        assert job["error_message"] is None
        assert job["collected_count"] == 4
        assert job["sources"] == {"fallback_catalog": 4}

        rows = (await client.get(f"{BASE}/prospects")).json()["items"]
        assert len(rows) == 4
        assert {row["raw_metadata"]["provider"] for row in rows} == {"fallback_catalog"}
        assert {row["city"] for row in rows} == {"Dubai", "Milan"}
        assert {row["country"] for row in rows} == {"AE", "IT"}
        for row in rows:
            assert row["raw_metadata"]["synthetic"] is True
            assert "overpass_osm" in row["raw_metadata"]["fallback_reason"]
            assert row["phone_e164"].startswith(("+971", "+39"))
            assert row["website"].startswith("https://www.")
            assert row["latitude"] is not None and row["longitude"] is not None
        assert any("Dubai" in row["company_name"] for row in rows)


async def test_synthetic_mode_is_deterministic_and_never_calls_the_network(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    async with client_for(
        runtime_session_factory, workspace["owner"], collection=offline_transport()
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
        assert job["sources"] == {"fallback_catalog": 3}

        rows = (await client.get(f"{BASE}/prospects")).json()["items"]
        expected = fallback_catalogue(
            CollectRequest(
                keywords=("led strip",),
                target_region="Warsaw, PL",
                max_results=3,
                cities=("Warsaw, PL",),
            ),
            reason="provider_mode=synthetic",
        )
        assert sorted((row["company_name"], row["phone_e164"]) for row in rows) == sorted(
            (record.company_name, record.phone_e164) for record in expected[:3]
        )
        assert all(row["city"] == "Warsaw" for row in rows)


async def test_legacy_single_region_job_keeps_the_area_query_and_strict_errors(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode())["data"][0]
        calls.append(("overpass", body))
        return json_response(OVERPASS_DUBAI)

    transport = httpx.MockTransport(handler)
    async with client_for(
        runtime_session_factory, workspace["owner"], collection=transport
    ) as client:
        created = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "overpass_osm",
                "keywords": ["lighting distributor"],
                "target_region": "Dubai, AE",
                "max_results": 10,
            },
        )
        assert created.status_code == 201, created.text
        job = created.json()
        # Legacy flow: still `pending` until an explicit run, and no Nominatim call.
        assert job["status"] == "pending"
        assert [kind for kind, _payload in calls] == []

        run = await client.post(f"{BASE}/jobs/{job['id']}/run")
        assert run.status_code == 200, run.text
        assert run.json()["collected_count"] == 2
        assert run.json()["sources"] == {"overpass_osm": 2}
        assert 'area["name"="Dubai"]["ISO3166-1"="AE"]->.searchArea;' in calls[0][1]
        assert "boundingbox" not in calls[0][1]


GOOGLE_PLACES_PAYLOAD: dict[str, Any] = {
    "places": [
        {
            "id": "ChIJ-pack-c-milan",
            "displayName": {"text": "Milan Illuminazione SRL", "languageCode": "it"},
            "formattedAddress": "Via Brera 1, Milano, Italy",
            "rating": 4.6,
            "internationalPhoneNumber": "+39 02 5555 0111",
            "websiteUri": "https://milanilluminazione.it",
            "location": {"latitude": 45.4722, "longitude": 9.1922},
            "addressComponents": [
                {"longText": "Milano", "shortText": "MI", "types": ["locality"]},
                {"longText": "Italy", "shortText": "IT", "types": ["country"]},
            ],
        }
    ]
}


async def test_auto_mode_prefers_google_places_when_a_key_is_configured(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "places.googleapis.com":
            calls.append("google_places")
            assert request.headers["X-Goog-Api-Key"] == "pack-c-google-key"
            assert "places.rating" in request.headers["X-Goog-FieldMask"]
            return json_response(GOOGLE_PLACES_PAYLOAD)
        if host == "overpass-api.de":
            calls.append("overpass_osm")
            return json_response(OVERPASS_DUBAI)
        if host == "nominatim.openstreetmap.org":
            # Only the explicit Overpass batch geocodes; Google mode never gets here.
            calls.append("nominatim")
            return json_response(NOMINATIM_RESPONSES[request.url.params["q"]])
        raise AssertionError(f"unexpected outbound request to {request.url}")

    async with client_for(
        runtime_session_factory,
        workspace["owner"],
        collection=httpx.MockTransport(handler),
        google_places_api_key="pack-c-google-key",
    ) as client:
        auto = await client.post(
            f"{BASE}/jobs",
            json={"cities": ["Milan, IT"], "keywords": ["led lighting"], "max_results": 5},
        )
        assert auto.status_code == 201, auto.text
        google_job = auto.json()
        assert google_job["status"] == "completed"
        assert google_job["sources"] == {"google_places": 1}
        assert calls == ["google_places"]

        rows = (await client.get(f"{BASE}/prospects")).json()["items"]
        assert rows[0]["company_name"] == "Milan Illuminazione SRL"
        assert rows[0]["raw_metadata"]["provider"] == "google_places"
        assert rows[0]["raw_metadata"]["google_place_id"] == "ChIJ-pack-c-milan"
        assert rows[0]["rating"] == "4.60"

        # An explicit `overpass` mode ignores the configured Google key.
        explicit = await client.post(
            f"{BASE}/jobs",
            json={
                "provider_mode": "overpass",
                "cities": ["Dubai, AE"],
                "keywords": ["lighting distributor"],
                "max_results": 5,
            },
        )
        assert explicit.status_code == 201, explicit.text
        assert explicit.json()["status"] == "completed"
        assert explicit.json()["sources"] == {"overpass_osm": 2}
        assert calls == ["google_places", "nominatim", "overpass_osm"]


# --------------------------------------------------------------------------------------
# 2. Compliant public website e-mail enrichment
# --------------------------------------------------------------------------------------


async def test_website_email_enrichment_extracts_public_inbox_upgrades_grade_and_blocks_ssrf(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    fetched: list[str] = []
    sent: list[dict[str, Any]] = []
    transport = outreach_transport(sent=sent, fetched=fetched)
    async with client_for(
        runtime_session_factory, workspace["owner"], outreach=transport
    ) as client:
        imported = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "csv_import",
                "max_results": 10,
                "csv_content": (
                    "company_name,phone,website,city,country\n"
                    "Nordlicht Technik GmbH,+49305550123,"
                    "https://nordlicht-lichttechnik.de,Hamburg,DE\n"
                    "Innere Quelle GmbH,+49305550124,http://127.0.0.1:8080/,Berlin,DE\n"
                    "Solo Packaging SRL,,https://solo-packaging.it,Milano,IT\n"
                ),
            },
        )
        assert imported.status_code == 201, imported.text
        cleaned = await client.post(f"{BASE}/prospects/clean", json={})
        assert cleaned.status_code == 200, cleaned.text
        listed = (await client.get(f"{BASE}/prospects")).json()["items"]
        prospects = {row["company_name"]: row for row in listed}
        nordlicht = prospects["Nordlicht Technik GmbH"]
        assert nordlicht["grade"] == "B"
        assert nordlicht["email"] is None

        response = await client.post(
            f"{BASE}/prospects/enrich-emails",
            json={
                "prospect_ids": [
                    nordlicht["id"],
                    prospects["Innere Quelle GmbH"]["id"],
                    prospects["Solo Packaging SRL"]["id"],
                ]
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["checked_count"] == 3
        assert result["enriched_count"] == 1
        statuses = {item["company_name"]: item["status"] for item in result["items"]}
        assert statuses == {
            "Nordlicht Technik GmbH": "enriched",
            "Innere Quelle GmbH": "blocked",
            "Solo Packaging SRL": "no_email_found",
        }
        enriched_item = next(
            item for item in result["items"] if item["company_name"] == "Nordlicht Technik GmbH"
        )
        assert enriched_item["email"] == "purchasing@nordlicht-lichttechnik.de"
        assert enriched_item["previous_email"] is None
        # phone (+2) + email (+2) now reaches the A band.
        assert enriched_item["grade"] == "A"

        # The home page had no address, so the contact page was read as well.
        assert set(fetched) == {
            "https://nordlicht-lichttechnik.de/",
            "https://nordlicht-lichttechnik.de/contact",
            "https://solo-packaging.it/",
            "https://solo-packaging.it/contact",
        }
        # SSRF guard: the loopback host was refused before any request was made.
        assert not any("127.0.0.1" in url for url in fetched)

        listed_rows = (await client.get(f"{BASE}/prospects")).json()["items"]
        rows = {row["company_name"]: row for row in listed_rows}
        assert rows["Nordlicht Technik GmbH"]["email"] == "purchasing@nordlicht-lichttechnik.de"
        assert rows["Nordlicht Technik GmbH"]["grade"] == "A"
        assert rows["Nordlicht Technik GmbH"]["raw_metadata"]["email_source"] == "public_website"
        assert rows["Innere Quelle GmbH"]["email"] is None
        assert rows["Innere Quelle GmbH"]["grade"] == "B"

        # Without `force`, an already-enriched prospect is skipped instead of refetched.
        fetched.clear()
        repeat = await client.post(
            f"{BASE}/prospects/enrich-emails",
            json={
                "prospect_ids": [
                    nordlicht["id"],
                    prospects["Innere Quelle GmbH"]["id"],
                    prospects["Solo Packaging SRL"]["id"],
                ]
            },
        )
        assert repeat.status_code == 200, repeat.text
        repeat_result = repeat.json()
        assert repeat_result["checked_count"] == 2
        assert repeat_result["enriched_count"] == 0
        repeat_statuses = {item["company_name"]: item["status"] for item in repeat_result["items"]}
        assert repeat_statuses["Nordlicht Technik GmbH"] == "already_has_email"
        assert set(fetched) == {
            "https://solo-packaging.it/",
            "https://solo-packaging.it/contact",
        }


async def test_enrichment_rejects_an_unknown_prospect_id_and_empty_payload(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    sent: list[dict[str, Any]] = []
    fetched: list[str] = []
    async with client_for(
        runtime_session_factory,
        workspace["owner"],
        outreach=outreach_transport(sent=sent, fetched=fetched),
    ) as client:
        empty = await client.post(f"{BASE}/prospects/enrich-emails", json={"prospect_ids": []})
        assert empty.status_code == 422
        missing = await client.post(
            f"{BASE}/prospects/enrich-emails", json={"prospect_ids": [str(uuid4())]}
        )
        assert missing.status_code == 200, missing.text
        assert missing.json() == {
            "checked_count": 0,
            "enriched_count": 0,
            "items": [
                {
                    "prospect_id": missing.json()["items"][0]["prospect_id"],
                    "company_name": "",
                    "website": None,
                    "email": None,
                    "previous_email": None,
                    "grade": "C",
                    "status": "not_found",
                }
            ],
        }
        assert fetched == []


# --------------------------------------------------------------------------------------
# 3. Real B2B outreach delivery through Resend with zero-delay STOP suppression
# --------------------------------------------------------------------------------------


async def _seed_email_account(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    external: str = "sales@ledfactory.example.com",
    display_name: str = "Export Sales",
    owner_user_id: UUID | None = None,
    visibility_scope: str = "shared",
    api_key: str = RESEND_KEY,
) -> UUID:
    account_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO channel_accounts (id, tenant_id, channel_type, external_account_id, "
            "display_name, status, owner_user_id, visibility_scope, config) "
            "VALUES (:id, :tid, 'email', :ext, :name, 'active', :owner, :scope, '{}'::jsonb)"
        ),
        {
            "id": account_id,
            "tid": tenant_id,
            "ext": external,
            "name": display_name,
            "owner": owner_user_id,
            "scope": visibility_scope,
        },
    )
    await CredentialVaultHelper().save_secret_json(
        session, tenant_id, account_id, "messaging_provider",
        {"provider": "resend", "api_key": api_key},
    )
    return account_id


async def _create_customer(
    factory: Factory,
    tenant_id: UUID,
    *,
    email: str | None,
    phone: str | None = None,
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


async def test_real_outreach_send_via_resend_personalizes_and_skips_stop_suppressed(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    sent: list[dict[str, Any]] = []
    fetched: list[str] = []
    stopped = await _create_customer(
        runtime_session_factory, tenant, email="stop@buyer-two.de", display_name="Stop Buyer"
    )
    no_address = await _create_customer(
        runtime_session_factory, tenant, email=None, phone="+97145550188", display_name="No Mail"
    )
    consent = ConsentAndSuppressionService()
    async with tenant_session(runtime_session_factory, tenant) as session:
        await consent.record_stop_opt_out(
            session, tenant, stopped, "email", "stop@buyer-two.de", "complaint"
        )
        await _seed_email_account(session, tenant)

    transport = outreach_transport(sent=sent, fetched=fetched)
    async with client_for(
        runtime_session_factory, workspace["owner"], outreach=transport
    ) as client:
        imported = await client.post(
            f"{BASE}/jobs",
            json={
                "source_provider": "csv_import",
                "max_results": 5,
                "csv_content": (
                    "company_name,contact_name,email,phone,city,country\n"
                    "Buyer One GmbH,Anna Weber,einkauf@buyer-one.de,+49305550199,Hamburg,DE\n"
                ),
            },
        )
        assert imported.status_code == 201, imported.text
        prospect = (await client.get(f"{BASE}/prospects")).json()["items"][0]

        created = await client.post(
            f"{BASE}/campaigns",
            json={
                "name": "COB strip B2B outreach",
                "channel_type": "email",
                "subject": "Catalogue for {{company_name}}",
                "body_template": (
                    "Hello {{contact_name}} at {{company_name}}, we supply COB strip "
                    "wholesalers in {{city}}."
                ),
                "target_ids": [prospect["id"], str(stopped), str(no_address)],
            },
        )
        assert created.status_code == 201, created.text
        campaign = created.json()
        assert campaign["total_targets"] == 3

        dry = await client.post(
            f"{BASE}/campaigns/{campaign['id']}/send-real", json={"dry_run": True}
        )
        assert dry.status_code == 200, dry.text
        assert dry.json() == {
            "campaign_id": campaign["id"],
            "provider": "resend",
            "from_email": "Export Sales <sales@ledfactory.example.com>",
            "sent_count": 1,
            "suppressed_count": 1,
            "failed_count": 1,
            "dry_run": True,
        }
        assert sent == []
        assert (await client.get(f"{BASE}/campaigns")).json()["items"][0]["sent_count"] == 0

        response = await client.post(
            f"{BASE}/campaigns/{campaign['id']}/send-real", json={"max_send": 10}
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["sent_count"] == 1
        assert result["suppressed_count"] == 1
        assert result["failed_count"] == 1
        assert result["dry_run"] is False
        assert result["provider"] == "resend"

        assert len(sent) == 1
        payload = sent[0]
        assert payload["from"] == "Export Sales <sales@ledfactory.example.com>"
        assert payload["to"] == ["einkauf@buyer-one.de"]
        assert payload["subject"] == "Catalogue for Buyer One GmbH"
        assert "Hello Anna Weber at Buyer One GmbH" in payload["text"]
        assert "in Hamburg" in payload["text"]
        assert payload["text"].rstrip().endswith(OPT_OUT_FOOTER)
        assert payload["headers"]["List-Unsubscribe"].startswith("<https://erp.local")
        # The suppressed address is never handed to the provider.
        assert all("stop@buyer-two.de" not in str(to) for to in payload["to"])

        recipients = (await client.get(f"{BASE}/campaigns/{campaign['id']}/recipients")).json()
        by_address = {row["address"]: row for row in recipients["items"]}
        assert by_address["einkauf@buyer-one.de"]["status"] == "sent"
        assert by_address["einkauf@buyer-one.de"]["sent_at"] is not None
        assert by_address["stop@buyer-two.de"]["status"] == "suppressed"
        assert by_address["stop@buyer-two.de"]["error_reason"] == "命中退订/抑制名单"
        missing = next(addr for addr in by_address if addr.startswith("missing:"))
        assert by_address[missing]["status"] == "failed"

        listed = (await client.get(f"{BASE}/campaigns")).json()["items"][0]
        assert listed["sent_count"] == 1
        assert listed["suppressed_count"] == 1

    async with tenant_session(runtime_session_factory, tenant) as session:
        audit = (
            await session.execute(
                text(
                    "SELECT after_state FROM audit_events "
                    "WHERE action = 'growth.campaign_sent_real' AND resource_id = :cid"
                ),
                {"cid": campaign["id"]},
            )
        ).one()
        assert audit.after_state["sent"] == 1
        assert audit.after_state["suppressed"] == 1
        assert audit.after_state["provider"] == "resend"


async def test_real_outreach_respects_agent_channel_ownership(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    sent: list[dict[str, Any]] = []
    fetched: list[str] = []
    async with tenant_session(runtime_session_factory, tenant) as session:
        await _seed_email_account(
            session,
            tenant,
            external="owner@ledfactory.example.com",
            display_name="Owner Mailbox",
            owner_user_id=workspace["owner_id"],
            visibility_scope="personal",
        )
    buyer = await _create_customer(
        runtime_session_factory, tenant, email="agent-buyer@buyer-three.de"
    )

    transport = outreach_transport(sent=sent, fetched=fetched)
    # Campaign authoring stays a manager/owner action; only delivery is agent-capable.
    async with client_for(
        runtime_session_factory, workspace["owner"], outreach=transport
    ) as client:
        created = await client.post(
            f"{BASE}/campaigns",
            json={
                "name": "Agent outreach",
                "channel_type": "email",
                "subject": "Hello {{company_name}}",
                "body_template": "Our new COB strip catalogue for {{city}}.",
                "target_ids": [str(buyer)],
            },
        )
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]

    async with client_for(
        runtime_session_factory, workspace["agent"], outreach=transport
    ) as client:
        denied = await client.post(
            f"{BASE}/campaigns/{campaign_id}/send-real", json={"dry_run": True}
        )
        assert denied.status_code == 404
        assert denied.json()["code"] == "ERR_EMAIL_ACCOUNT_UNAVAILABLE"
        assert sent == []

    async with tenant_session(runtime_session_factory, tenant) as session:
        await _seed_email_account(
            session, tenant, external="team@ledfactory.example.com", display_name="Team"
        )
    async with client_for(
        runtime_session_factory, workspace["agent"], outreach=transport
    ) as client:
        allowed = await client.post(
            f"{BASE}/campaigns/{campaign_id}/send-real", json={"dry_run": True}
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["from_email"] == "Team <team@ledfactory.example.com>"
        assert allowed.json()["sent_count"] == 1
        assert sent == []


async def test_real_outreach_rejects_a_whatsapp_campaign(
    runtime_session_factory: Factory, workspace: dict[str, Any]
) -> None:
    tenant = workspace["tenant"]
    opted_in = await _create_customer(
        runtime_session_factory, tenant, email=None, phone="+97145550177", display_name="WA Buyer"
    )
    consent = ConsentAndSuppressionService()
    async with tenant_session(runtime_session_factory, tenant) as session:
        await consent.record_opt_in(
            session,
            tenant,
            opted_in,
            "whatsapp_cloud",
            proof_source="click_to_whatsapp_ad",
            disclosed_business_name="Quanzhou Smart LED",
        )
        await _seed_email_account(session, tenant)

    sent: list[dict[str, Any]] = []
    fetched: list[str] = []
    async with client_for(
        runtime_session_factory,
        workspace["owner"],
        outreach=outreach_transport(sent=sent, fetched=fetched),
    ) as client:
        created = await client.post(
            f"{BASE}/campaigns",
            json={
                "name": "WA broadcast",
                "channel_type": "whatsapp_cloud",
                "body_template": "New COB strip batches are ready.",
                "target_ids": [str(opted_in)],
            },
        )
        assert created.status_code == 201, created.text
        rejected = await client.post(
            f"{BASE}/campaigns/{created.json()['id']}/send-real", json={}
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "ERR_CAMPAIGN_CHANNEL_UNSUPPORTED"
        assert sent == []
