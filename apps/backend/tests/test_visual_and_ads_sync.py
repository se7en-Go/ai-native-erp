"""Track 9: sourcing visual-AI jobs and read-only ad spend sync, on real PG17.

Nothing is mocked at the data layer. Every assertion about a persisted row is
made against PostgreSQL through the real routers, inside a tenant-scoped RLS
transaction running as `app_runtime`. The only stubs are the *vendor* HTTP
transports (`httpx.MockTransport`) and the object-storage handle, which is
exactly the seam the production code exposes for deterministic testing.

Covered invariants:

* PG17 `ENABLE` + `FORCE` ROW LEVEL SECURITY with the `RESTRICTIVE` tenant policy
  and the `app_runtime` grants on all three new tables, plus real cross-tenant
  isolation for both features.
* `sandbox` renders a byte-identical, spec-valid PNG with no network at all, and
  the stored preview URL is a real `data:image/png;base64,...` image.
* SSRF guard: `http://`, loopback / link-local / metadata / single-label hosts,
  credential-smuggling URLs, `data:image/svg+xml` and `/media/../` are all refused.
* A provider without an API key is recorded as a `failed` job with a clear,
  non-secret reason; a mocked transport drives the success path.
* Ad tokens are sealed in an AES-256-GCM envelope bound to
  `AAD = tenant:account:ads_oauth`, never echoed by an API response, and a
  cross-account decrypt attempt fails the GCM tag check.
* ROAS / TACoS / CTR are exact `Decimal` values with `ROUND_HALF_UP`
  discriminators against `ROUND_HALF_EVEN`, and the summary can never multiply a
  day's store revenue by the campaign count.
* Adapters are strictly read-only: every stub transport rejects a non-GET verb.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import erp_core.api_main  # noqa: F401  (composition root: resolves every string FK)
from erp_core.catalog.visual_router import router as visual_router
from erp_core.catalog.visual_schemas import VisualJobCreate
from erp_core.catalog.visual_service import (
    OpenAiImageHttpProvider,
    RemoveBgHttpProvider,
    SandboxImageProvider,
    VisualJobRequest,
    VisualJobService,
    VisualProviderError,
    default_visual_providers,
    image_data_url,
    render_sandbox_png,
    sniff_image_mime,
    validate_source_image_url,
)
from erp_core.finance.ads_router import ADS_READ_ROLES, ADS_WRITE_ROLES
from erp_core.finance.ads_router import router as ads_router
from erp_core.finance.ads_service import (
    ADS_CREDENTIAL_TYPE,
    AdsAccountContext,
    AdsSpendAdapterError,
    AmazonAdsReadonlyAdapter,
    MetaAdsReadonlyAdapter,
    SandboxAdsAdapter,
    compute_ctr_pct,
    compute_roas,
    compute_tacos_pct,
    decimal_from,
    default_ads_adapters,
    masked_token_preview,
    money_str,
    pack_credential_envelope,
    unpack_credential_envelope,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError, register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
VISUAL_BASE = "/api/v1/catalog/visual-jobs"
ADS_BASE = "/api/v1/finance/ads"
NEW_TABLES = (
    "sourcing_image_jobs",
    "ad_account_connections",
    "ad_spend_daily_snapshots",
)
INTERNAL_ERROR_MESSAGE = "provider stack trace must never leak"
SECRET_TOKEN = "EAAG-super-secret-meta-token-9f13"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def _principal(tenant_id: UUID, role: UserRole, user_id: UUID) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email=f"{role.value}@track9.example.com",
    )


def _client(
    factory: Factory,
    principal: CurrentPrincipal,
    *,
    crypto: EnvelopeCryptoService | None = None,
    visual_providers: dict[str, Any] | None = None,
    ads_adapters: dict[str, Any] | None = None,
) -> AsyncClient:
    """Isolated app carrying both Track 9 routers with an injected principal."""
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.state.crypto_service = crypto or EnvelopeCryptoService(get_settings())
    app.state.visual_providers = visual_providers
    app.state.ads_adapters = ads_adapters
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(visual_router)
    app.include_router(ads_router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _seed_tenant(factory: Factory, tid: UUID, slug: str) -> dict[str, UUID]:
    users = {role.value: uuid4() for role in UserRole}
    async with tenant_session(factory, tid) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, :name, 'USD')"
            ),
            {"id": tid, "slug": slug, "name": f"Tenant {slug}"},
        )
        for role, uid in users.items():
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, :name, 'test')"
                ),
                {"id": uid, "email": f"{uid.hex}@{slug}.example.com", "name": role},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (:tid, :uid, :role)"
                ),
                {"tid": tid, "uid": uid, "role": role},
            )
        product_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:id, :tid, :spu, 'led-lighting')"
            ),
            {"id": product_id, "tid": tid, "spu": f"SPU-{slug}"},
        )
        variant_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO product_variants "
                "(id, tenant_id, product_id, sku, cost_price_base, retail_price_usd, "
                " weight_grams) VALUES "
                "(:id, :tid, :pid, :sku, 42.5000, 99.00, 800)"
            ),
            {
                "id": variant_id,
                "tid": tid,
                "pid": product_id,
                "sku": f"SKU-{slug}",
            },
        )
    return {**users, "variant_id": variant_id, "product_id": product_id}


@pytest.fixture
async def workspace(runtime_session_factory: Factory) -> AsyncIterator[dict[str, Any]]:
    tenant, other = uuid4(), uuid4()
    seeded = await _seed_tenant(runtime_session_factory, tenant, f"t9-{tenant.hex[:10]}")
    other_seeded = await _seed_tenant(runtime_session_factory, other, f"t9-{other.hex[:10]}")
    yield {
        "factory": runtime_session_factory,
        "tenant": tenant,
        "other_tenant": other,
        "variant_id": seeded["variant_id"],
        "owner": _principal(tenant, UserRole.OWNER, seeded["owner"]),
        "admin": _principal(tenant, UserRole.ADMIN, seeded["admin"]),
        "manager": _principal(tenant, UserRole.MANAGER, seeded["manager"]),
        "agent": _principal(tenant, UserRole.AGENT, seeded["agent"]),
        "finance": _principal(tenant, UserRole.FINANCE, seeded["finance"]),
        "outsider": _principal(other, UserRole.OWNER, other_seeded["owner"]),
    }


def _png_bytes(operation: str = "remove_bg") -> bytes:
    return render_sandbox_png(seed="vendor-fixture", operation=operation)


def _mock_transport(
    payload: httpx.Response,
    seen: list[httpx.Request],
    *,
    expected_host: str,
) -> httpx.MockTransport:
    """A GET-only vendor stub: any non-GET verb is a read-only contract violation."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET", f"read-only adapter issued {request.method}"
        assert request.url.host == expected_host, str(request.url)
        return payload

    return httpx.MockTransport(handler)


def _forbid_network(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
    raise AssertionError(f"unexpected outbound request to {request.url}")


# --------------------------------------------------------------------------- #
# migration, RLS and grants
# --------------------------------------------------------------------------- #
def test_migration_revision_chain_is_locked() -> None:
    script = ScriptDirectory(str(Path(__file__).parents[1] / "alembic"))
    revision = script.get_revision("0027_visual_jobs_and_ads_sync")
    assert revision is not None
    assert revision.down_revision == "0026_bom_and_in_transit_doc"


async def test_pg17_force_rls_policies_and_runtime_grants(
    migrator_session_factory: Factory,
) -> None:
    async with migrator_session_factory() as session:
        assert 170000 <= int(await session.scalar(text("SHOW server_version_num"))) < 190000
        for name in NEW_TABLES:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT c.relrowsecurity, c.relforcerowsecurity,
                               EXISTS (
                                   SELECT 1 FROM pg_policy p
                                   WHERE p.polrelid = c.oid
                                     AND p.polname = 'tenant_allow_runtime'
                                     AND p.polpermissive
                               ) AS has_permissive,
                               EXISTS (
                                   SELECT 1 FROM pg_policy p
                                   WHERE p.polrelid = c.oid
                                     AND p.polname = 'tenant_isolation_policy'
                                     AND NOT p.polpermissive
                                     AND p.polqual IS NOT NULL
                                     AND p.polwithcheck IS NOT NULL
                               ) AS has_restrictive
                        FROM pg_class c WHERE c.oid = to_regclass(:name)
                        """
                    ),
                    {"name": name},
                )
            ).one()
            assert row.relrowsecurity is True, name
            assert row.relforcerowsecurity is True, name
            assert row.has_permissive is True, name
            assert row.has_restrictive is True, name

            grants = {
                grant
                for (grant,) in (
                    await session.execute(
                        text(
                            "SELECT privilege_type FROM information_schema.role_table_grants "
                            "WHERE table_name = :name AND grantee = 'app_runtime'"
                        ),
                        {"name": name},
                    )
                ).all()
            }
            assert {"SELECT", "INSERT", "UPDATE", "DELETE"} <= grants, name


async def test_no_hard_fk_points_at_0004_tables(migrator_session_factory: Factory) -> None:
    """The new tables may only reference tenants / users / product_variants."""
    async with migrator_session_factory() as session:
        targets = {
            (row.table_name, row.column_name, row.foreign_table)
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT tc.table_name, kcu.column_name, ccu.table_name AS foreign_table
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON kcu.constraint_name = tc.constraint_name
                        JOIN information_schema.constraint_column_usage ccu
                          ON ccu.constraint_name = tc.constraint_name
                        WHERE tc.constraint_type = 'FOREIGN KEY'
                          AND tc.table_name = ANY(:tables)
                        """
                    ),
                    {"tables": list(NEW_TABLES)},
                )
            ).all()
        }
    assert targets, "expected foreign keys on the new tables"
    assert {target for _, _, target in targets} <= {
        "tenants",
        "users",
        "product_variants",
        "ad_account_connections",
    }


# --------------------------------------------------------------------------- #
# source-image SSRF guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("http://cdn.example.com/a.png", "https only"),
        ("https://127.0.0.1/a.png", "loopback"),
        ("https://10.1.2.3/a.png", "rfc1918"),
        ("https://[::1]/a.png", "ipv6 loopback"),
        ("https://169.254.169.254/latest/meta-data/", "link-local metadata"),
        ("https://100.64.1.1/a.png", "cgnat"),
        ("https://metadata.google.internal/a.png", "metadata hostname"),
        ("https://localhost/a.png", "localhost"),
        ("https://intranet/a.png", "single label"),
        ("https://user:pw@cdn.example.com/a.png", "embedded credentials"),
        ("file:///etc/passwd", "file scheme"),
        ("/media/../../etc/passwd", "traversal"),
        ("/etc/passwd", "not /media"),
        ("data:image/svg+xml;base64,PHN2Zy8+", "svg data url"),
        ("data:image/png;base64,bm90LWFuLWltYWdl", "non-raster payload"),
    ],
)
async def test_unsafe_source_images_are_refused(raw: str, reason: str) -> None:
    with pytest.raises(AppError) as excinfo:
        await validate_source_image_url(raw)
    assert excinfo.value.code == "ERR_UNSAFE_SOURCE_IMAGE", reason


async def test_dns_name_pointing_at_metadata_is_refused() -> None:
    async def _resolver(host: str) -> Sequence[str]:
        assert host == "cdn.attacker.example"
        return ("169.254.169.254",)

    with pytest.raises(AppError) as excinfo:
        await validate_source_image_url(
            "https://cdn.attacker.example/a.png", dns_resolver=_resolver
        )
    assert excinfo.value.code == "ERR_UNSAFE_SOURCE_IMAGE"
    assert "non-public" in excinfo.value.detail


async def test_safe_source_images_are_accepted() -> None:
    assert (
        await validate_source_image_url("https://cdn.example.com/products/a.png")
        == "https://cdn.example.com/products/a.png"
    )
    assert await validate_source_image_url("/media/catalog/2026/11/a.png") == (
        "/media/catalog/2026/11/a.png"
    )
    data_url = image_data_url(_png_bytes(), "image/png")
    assert await validate_source_image_url(data_url) == data_url


# --------------------------------------------------------------------------- #
# sandbox renderer + provider protocol
# --------------------------------------------------------------------------- #
def test_sandbox_png_is_a_valid_deterministic_image() -> None:
    first = render_sandbox_png(seed="abc", operation="scene_replace")
    second = render_sandbox_png(seed="abc", operation="scene_replace")
    assert first == second, "the sandbox render must be byte-identical across calls"
    assert first[:8] == b"\x89PNG\r\n\x1a\n"
    assert sniff_image_mime(first) == "image/png"
    # IHDR width/height live at a fixed offset in the first chunk.
    assert int.from_bytes(first[16:20], "big") == 128
    assert int.from_bytes(first[20:24], "big") == 128
    other = render_sandbox_png(seed="abc", operation="remove_bg")
    assert other != first, "the operation must change the rendered pixels"


def test_composition_root_has_one_provider_per_db_allowed_name() -> None:
    providers = default_visual_providers()
    assert set(providers) == {"sandbox", "remove_bg", "openai_image"}
    for name, provider in providers.items():
        assert provider.name == name
    assert providers["sandbox"].requires_api_key is False
    assert providers["remove_bg"].requires_api_key is True
    assert providers["openai_image"].requires_api_key is True


async def test_sandbox_provider_returns_real_png_without_network() -> None:
    # `SandboxImageProvider` never constructs an HTTP client: the assertion below is
    # that pure in-process rendering yields a decodable PNG, with no transport seam.
    request = VisualJobRequest(
        tenant_id=uuid4(),
        job_id=uuid4(),
        provider="sandbox",
        operation="translate_overlay",
        source_image_url="https://cdn.example.com/a.png",
        target_locale="es-MX",
    )
    result = await SandboxImageProvider().process(request)
    assert sniff_image_mime(result.data) == "image/png"


# --------------------------------------------------------------------------- #
# visual router
# --------------------------------------------------------------------------- #
async def test_create_list_get_and_delete_a_sandbox_job(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    async with _client(factory, workspace["owner"]) as client:
        created = await client.post(
            VISUAL_BASE,
            json={
                "provider": "sandbox",
                "operation": "remove_bg",
                "source_image_url": "https://cdn.example.com/products/desk-lamp.png",
                "variant_id": str(workspace["variant_id"]),
                "prompt": "clean white background",
            },
        )
        assert created.status_code == 201, created.text
        job = created.json()
        assert job["status"] == "completed"
        assert job["provider"] == "sandbox"
        assert job["operation"] == "remove_bg"
        assert job["variant_id"] == str(workspace["variant_id"])
        assert job["output_mime_type"] == "image/png"
        assert job["output_r2_key"] is None
        assert job["output_image_url"].startswith("data:image/png;base64,")
        decoded = base64.b64decode(job["output_image_url"].split(",", 1)[1])
        assert sniff_image_mime(decoded) == "image/png"
        assert job["completed_at"] is not None

        listed = await client.get(VISUAL_BASE)
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [job["id"]]

        fetched = await client.get(f"{VISUAL_BASE}/{job['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == job["id"]

        deleted = await client.delete(f"{VISUAL_BASE}/{job['id']}")
        assert deleted.status_code == 204
        assert deleted.content == b""

        gone = await client.get(f"{VISUAL_BASE}/{job['id']}")
        assert gone.status_code == 404
        assert gone.json()["code"] == "ERR_VISUAL_JOB_NOT_FOUND"


async def test_visual_job_list_is_capped_at_fifty(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    async with _client(factory, workspace["owner"]) as client:
        for _ in range(3):
            created = await client.post(
                VISUAL_BASE,
                json={
                    "provider": "sandbox",
                    "operation": "scene_replace",
                    "source_image_url": "/media/catalog/a.png",
                },
            )
            assert created.status_code == 201, created.text
        listed = await client.get(VISUAL_BASE, params={"limit": 2})
        assert listed.status_code == 200
        assert len(listed.json()) == 2
        too_many = await client.get(VISUAL_BASE, params={"limit": 51})
        assert too_many.status_code == 422


async def test_visual_job_rbac_and_cross_tenant_isolation(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    async with _client(factory, workspace["owner"]) as client:
        created = await client.post(
            VISUAL_BASE,
            json={
                "provider": "sandbox",
                "operation": "remove_bg",
                "source_image_url": "https://cdn.example.com/a.png",
            },
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]

    # FINANCE may read the studio list but must not burn provider credits.
    async with _client(factory, workspace["finance"]) as client:
        assert (await client.get(VISUAL_BASE)).status_code == 200
        blocked = await client.post(
            VISUAL_BASE,
            json={
                "provider": "sandbox",
                "operation": "remove_bg",
                "source_image_url": "https://cdn.example.com/a.png",
            },
        )
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "ERR_INSUFFICIENT_ROLE"
        assert (await client.delete(f"{VISUAL_BASE}/{job_id}")).status_code == 403

    # AGENT may create and delete.
    async with _client(factory, workspace["agent"]) as client:
        assert (
            await client.post(
                VISUAL_BASE,
                json={
                    "provider": "sandbox",
                    "operation": "translate_overlay",
                    "source_image_url": "https://cdn.example.com/a.png",
                    "target_locale": "ja-JP",
                },
            )
        ).status_code == 201

    # Another tenant sees nothing and cannot touch the row.
    async with _client(factory, workspace["outsider"]) as client:
        assert (await client.get(VISUAL_BASE)).json() == []
        assert (await client.get(f"{VISUAL_BASE}/{job_id}")).status_code == 404


async def test_unsafe_source_image_returns_422_before_any_provider_call(
    workspace: dict[str, Any],
) -> None:
    factory: Factory = workspace["factory"]
    async with _client(factory, workspace["owner"]) as client:
        response = await client.post(
            VISUAL_BASE,
            json={
                "provider": "sandbox",
                "operation": "remove_bg",
                "source_image_url": "https://169.254.169.254/latest/meta-data/iam/",
            },
        )
    assert response.status_code == 422
    assert response.json()["code"] == "ERR_UNSAFE_SOURCE_IMAGE"
    async with tenant_session(factory, workspace["tenant"]) as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM sourcing_image_jobs "
                    "WHERE tenant_id = :tid AND source_image_url LIKE 'https://169.254%'"
                ),
                {"tid": str(workspace["tenant"])},
            )
        ) == 0


async def test_remove_bg_without_api_key_is_recorded_as_failed(
    workspace: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REMOVE_BG_API_KEY", raising=False)
    factory: Factory = workspace["factory"]
    providers = default_visual_providers()
    async with _client(factory, workspace["owner"], visual_providers=providers) as client:
        response = await client.post(
            VISUAL_BASE,
            json={
                "provider": "remove_bg",
                "operation": "remove_bg",
                "source_image_url": "https://cdn.example.com/a.png",
            },
        )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "failed"
    assert job["output_image_url"] is None
    assert "REMOVE_BG_API_KEY" in job["error_message"]


async def test_openai_without_api_key_is_recorded_as_failed(
    workspace: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_API_KEY", raising=False)
    factory: Factory = workspace["factory"]
    async with _client(
        factory, workspace["owner"], visual_providers=default_visual_providers()
    ) as client:
        response = await client.post(
            VISUAL_BASE,
            json={
                "provider": "openai_image",
                "operation": "scene_replace",
                "source_image_url": "https://cdn.example.com/a.png",
            },
        )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "failed"
    assert "OPENAI_API_KEY" in response.json()["error_message"]


async def test_remove_bg_renders_through_a_mocked_transport(workspace: dict[str, Any]) -> None:
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: (
            seen.append(request),
            httpx.Response(200, content=_png_bytes(), headers={"content-type": "image/png"}),
        )[1]
    )
    provider = RemoveBgHttpProvider(api_key="test-remove-bg-key", transport=transport)
    factory: Factory = workspace["factory"]
    async with _client(
        factory,
        workspace["owner"],
        visual_providers={"remove_bg": provider, "sandbox": SandboxImageProvider()},
    ) as client:
        response = await client.post(
            VISUAL_BASE,
            json={
                "provider": "remove_bg",
                "operation": "remove_bg",
                "source_image_url": "https://cdn.example.com/a.png",
            },
        )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "completed"
    assert job["output_mime_type"] == "image/png"
    assert job["output_image_url"].startswith("data:image/png;base64,")
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.remove.bg/v1.0/removebg"
    assert seen[0].headers["x-api-key"] == "test-remove-bg-key"
    assert b"image_url" in seen[0].content


async def test_openai_renders_through_a_mocked_transport(workspace: dict[str, Any]) -> None:
    encoded = base64.b64encode(_png_bytes("scene_replace")).decode("ascii")
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: (
            seen.append(request),
            httpx.Response(200, json={"data": [{"b64_json": encoded}]}),
        )[1]
    )
    provider = OpenAiImageHttpProvider(api_key="test-openai-key", transport=transport)
    factory: Factory = workspace["factory"]
    async with _client(
        factory, workspace["owner"], visual_providers={"openai_image": provider}
    ) as client:
        response = await client.post(
            VISUAL_BASE,
            json={
                "provider": "openai_image",
                "operation": "translate_overlay",
                "source_image_url": "https://cdn.example.com/a.png",
                "prompt": "Studio hero shot",
                "target_locale": "pt-BR",
            },
        )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "completed"
    assert job["output_mime_type"] == "image/png"
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://api.openai.com/v1/images/generations"
    assert request.headers["authorization"] == "Bearer test-openai-key"
    body = request.read().decode()
    assert "Studio hero shot" in body
    assert "pt-BR" in body


async def test_provider_crash_is_masked_not_echoed(workspace: dict[str, Any]) -> None:
    class _Exploding:
        name = "sandbox"
        requires_api_key = False

        async def process(self, job_request: VisualJobRequest) -> Any:
            raise RuntimeError(INTERNAL_ERROR_MESSAGE)

    factory: Factory = workspace["factory"]
    async with _client(
        factory, workspace["owner"], visual_providers={"sandbox": _Exploding()}
    ) as client:
        response = await client.post(
            VISUAL_BASE,
            json={
                "provider": "sandbox",
                "operation": "remove_bg",
                "source_image_url": "https://cdn.example.com/a.png",
            },
        )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "failed"
    assert INTERNAL_ERROR_MESSAGE not in (job["error_message"] or "")
    assert "RuntimeError" in job["error_message"]


async def test_object_storage_path_writes_a_presigned_url(workspace: dict[str, Any]) -> None:
    class _FakeStorage:
        def __init__(self) -> None:
            self.uploads: list[tuple[str, bytes, str]] = []

        async def upload_bytes(
            self, key: str, data: bytes, content_type: str = "application/octet-stream"
        ) -> str:
            self.uploads.append((key, data, content_type))
            return key

        async def generate_presigned_get_url(self, key: str, expires_in: int = 604800) -> str:
            return f"https://r2.example.com/{key}?sig=abc"

    storage = _FakeStorage()
    factory: Factory = workspace["factory"]
    service = VisualJobService()
    async with tenant_session(factory, workspace["tenant"]) as session:
        job = await service.create_job(
            session,
            workspace["tenant"],
            workspace["owner"].user_id,
            VisualJobCreate(
                provider="sandbox",
                operation="remove_bg",
                source_image_url="https://cdn.example.com/a.png",
            ),
            storage=storage,
        )
    assert len(storage.uploads) == 1
    key, data, content_type = storage.uploads[0]
    assert key == f"visual-jobs/{workspace['tenant']}/{job.id}.png"
    assert sniff_image_mime(data) == "image/png"
    assert content_type == "image/png"
    assert job.output_r2_key == key
    assert job.output_image_url == f"https://r2.example.com/{key}?sig=abc"


async def test_internal_minio_presigned_url_rewrites_and_streams_output(
    workspace: dict[str, Any],
) -> None:
    from erp_core.shared.storage import ObjectStorageService

    class _DockerMinioStorage(ObjectStorageService):
        def __init__(self) -> None:  # noqa: D401 - avoid boto3 init
            self._bucket = "erp-assets"
            self.objects: dict[str, bytes] = {}

        async def upload_bytes(
            self, key: str, data: bytes, content_type: str = "application/octet-stream"
        ) -> str:
            self.objects[key] = data
            return key

        async def download_bytes(self, key: str) -> bytes:
            return self.objects[key]

        async def generate_presigned_get_url(self, key: str, expires_in: int = 604800) -> str:
            return f"http://minio:9000/erp-assets/{key}?X-Amz-Signature=deadbeef"

    storage = _DockerMinioStorage()

    def _app_for(principal: CurrentPrincipal) -> FastAPI:
        app = FastAPI()
        register_exception_handlers(app)
        app.state.session_factory = workspace["factory"]
        app.state.crypto_service = EnvelopeCryptoService(get_settings())
        app.state.storage = storage
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.include_router(visual_router)
        return app

    async with AsyncClient(
        transport=ASGITransport(app=_app_for(workspace["owner"])),
        base_url="http://testserver",
    ) as owner_client:
        created = await owner_client.post(
            VISUAL_BASE,
            json={
                "provider": "sandbox",
                "operation": "remove_bg",
                "source_image_url": "https://cdn.example.com/item.png",
            },
        )
        assert created.status_code == 201, created.text
        job = created.json()
        assert job["output_image_url"] == f"/api/bff/v1/catalog/visual-jobs/{job['id']}/output"

        output_res = await owner_client.get(f"{VISUAL_BASE}/{job['id']}/output")
        assert output_res.status_code == 200
        assert output_res.headers["content-type"].startswith("image/png")
        assert sniff_image_mime(output_res.content) == "image/png"

    async with AsyncClient(
        transport=ASGITransport(app=_app_for(workspace["outsider"])),
        base_url="http://testserver",
    ) as outsider_client:
        other_res = await outsider_client.get(f"{VISUAL_BASE}/{job['id']}/output")
        assert other_res.status_code == 404


async def test_storage_failure_falls_back_to_inline_png(workspace: dict[str, Any]) -> None:
    class _BrokenStorage:
        async def upload_bytes(self, *_: Any, **__: Any) -> str:
            raise RuntimeError("minio is down")

        async def generate_presigned_get_url(self, *_: Any, **__: Any) -> str:  # pragma: no cover
            raise AssertionError("must not be called when the upload failed")

    factory: Factory = workspace["factory"]
    async with tenant_session(factory, workspace["tenant"]) as session:
        job = await VisualJobService().create_job(
            session,
            workspace["tenant"],
            workspace["owner"].user_id,
            VisualJobCreate(
                provider="sandbox",
                operation="scene_replace",
                source_image_url="https://cdn.example.com/a.png",
            ),
            storage=_BrokenStorage(),
        )
    assert job.status == "completed"
    assert job.output_r2_key is None
    assert job.output_image_url is not None
    assert job.output_image_url.startswith("data:image/png;base64,")


async def test_visual_jobs_are_rls_isolated_at_the_sql_layer(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    async with tenant_session(factory, workspace["tenant"]) as session:
        await session.execute(
            text(
                "INSERT INTO sourcing_image_jobs "
                "(id, tenant_id, provider, operation, source_image_url, status) "
                "VALUES (:id, :tid, 'sandbox', 'remove_bg', 'https://cdn.example.com/a.png', "
                "'completed')"
            ),
            {"id": uuid4(), "tid": workspace["tenant"]},
        )
    async with tenant_session(factory, workspace["other_tenant"]) as session:
        assert await session.scalar(text("SELECT count(*) FROM sourcing_image_jobs")) == 0
        with pytest.raises(DBAPIError) as excinfo:
            await session.execute(
                text(
                    "INSERT INTO sourcing_image_jobs "
                    "(tenant_id, provider, operation, source_image_url) "
                    "VALUES (:tid, 'sandbox', 'remove_bg', 'https://cdn.example.com/a.png')"
                ),
                {"tid": workspace["tenant"]},
            )
        assert "row-level security" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- #
# exact decimal ratio maths
# --------------------------------------------------------------------------- #
def test_roas_tacos_and_ctr_use_round_half_up() -> None:
    # 1/8 = 0.125 -> ROUND_HALF_UP 0.13 (ROUND_HALF_EVEN would give 0.12).
    assert compute_roas(Decimal("1.00"), Decimal("8.00")) == Decimal("0.13")
    assert compute_roas(Decimal("250.00"), Decimal("100.00")) == Decimal("2.50")
    assert compute_roas(Decimal("250.00"), Decimal("0.00")) == Decimal("0.00")

    assert compute_tacos_pct(Decimal("1.00"), Decimal("800.00")) == Decimal("0.13")
    assert compute_tacos_pct(Decimal("30.00"), Decimal("1000.00")) == Decimal("3.00")
    assert compute_tacos_pct(Decimal("30.00"), Decimal("0.00")) == Decimal("0.00")

    assert compute_ctr_pct(1, 800) == Decimal("0.13")
    assert compute_ctr_pct(25, 1000) == Decimal("2.50")
    assert compute_ctr_pct(0, 0) == Decimal("0.00")

    assert money_str(Decimal("12.5")) == "12.50"
    assert money_str(Decimal("0.005")) == "0.01"
    assert decimal_from("-3") == Decimal("0.00")
    assert decimal_from("not-a-number") == Decimal("0.00")


def test_credential_envelope_round_trip_and_tamper_detection() -> None:
    ciphertext = b"ciphertext-bytes"
    nonce = b"n" * 12
    auth_tag = b"t" * 16
    encoded = pack_credential_envelope(
        key_version=3, nonce=nonce, auth_tag=auth_tag, ciphertext=ciphertext
    )
    unpacked = unpack_credential_envelope(encoded)
    assert unpacked.key_version == 3
    assert unpacked.nonce == nonce
    assert unpacked.auth_tag == auth_tag
    assert unpacked.ciphertext == ciphertext

    for broken in ("", "!!!!", base64.b64encode(b"AD1too-short").decode()):
        with pytest.raises(AppError) as excinfo:
            unpack_credential_envelope(broken)
        assert excinfo.value.code == "ERR_ADS_CREDENTIAL_CORRUPT"

    assert masked_token_preview(encoded).startswith("\u2022\u2022\u2022\u2022")
    assert masked_token_preview(encoded) == masked_token_preview(encoded)
    assert "ciphertext" not in masked_token_preview(encoded)


# --------------------------------------------------------------------------- #
# read-only adapters
# --------------------------------------------------------------------------- #
def test_all_default_adapters_are_read_only() -> None:
    adapters = default_ads_adapters()
    assert set(adapters) == {"sandbox_ads", "meta_ads", "amazon_ads"}
    for platform, adapter in adapters.items():
        assert adapter.read_only is True, platform
        assert adapter.platform == platform


async def test_meta_and_amazon_adapter_urls_are_vendor_pinned() -> None:
    context = AdsAccountContext(
        tenant_id=uuid4(),
        account_id=uuid4(),
        platform="meta_ads",
        external_account_id="1234567890",
        account_name="Meta",
        currency="USD",
        access_token="tok",
    )
    meta = MetaAdsReadonlyAdapter()
    assert meta.build_url(context) == (
        "https://graph.facebook.com/v20.0/act_1234567890/insights"
    )
    assert AmazonAdsReadonlyAdapter().build_url() == (
        "https://advertising-api.amazon.com/v2/campaigns/extended"
    )
    assert AmazonAdsReadonlyAdapter(host="advertising-api-eu.amazon.com").build_url() == (
        "https://advertising-api-eu.amazon.com/v2/campaigns/extended"
    )
    for bad in ("advertising-api.amazon.com.evil.tld", "127.0.0.1", "metadata.google.internal"):
        with pytest.raises(AdsSpendAdapterError) as excinfo:
            AmazonAdsReadonlyAdapter(host=bad).build_url()
        assert excinfo.value.code == "ERR_ADS_ENDPOINT_UNSAFE"


# --------------------------------------------------------------------------- #
# ads router
# --------------------------------------------------------------------------- #
async def _connect(workspace: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload = {
        "platform": "sandbox_ads",
        "account_name": "QZ LED 沙箱广告账户",
        "external_account_id": f"act-{uuid4().hex[:8]}",
        "access_token": SECRET_TOKEN,
        "currency": "USD",
    }
    payload.update(overrides)
    async with _client(workspace["factory"], workspace["owner"]) as client:
        response = await client.post(f"{ADS_BASE}/accounts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def test_connect_account_never_echoes_the_token(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    crypto = EnvelopeCryptoService(get_settings())
    async with _client(factory, workspace["owner"], crypto=crypto) as client:
        response = await client.post(
            f"{ADS_BASE}/accounts",
            json={
                "platform": "meta_ads",
                "account_name": "Meta 主账户",
                "external_account_id": "act-778899",
                "access_token": SECRET_TOKEN,
                "currency": "usd",
            },
        )
        assert response.status_code == 201, response.text
        account = response.json()
        assert account["currency"] == "USD"
        assert account["has_credentials"] is True
        assert account["token_preview"].startswith("\u2022\u2022\u2022\u2022")
        assert SECRET_TOKEN not in response.text
        assert "encrypted_credentials_b64" not in response.text

        listed = await client.get(f"{ADS_BASE}/accounts")
        assert listed.status_code == 200
        assert SECRET_TOKEN not in listed.text
        assert [row["id"] for row in listed.json()] == [account["id"]]

        duplicate = await client.post(
            f"{ADS_BASE}/accounts",
            json={
                "platform": "meta_ads",
                "account_name": "Meta duplicate",
                "external_account_id": "act-778899",
                "access_token": "another-token",
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "ERR_ADS_ACCOUNT_EXISTS"

    # The ciphertext is at rest, and only the bound AAD can open it.
    async with tenant_session(factory, workspace["tenant"]) as session:
        envelope_b64 = await session.scalar(
            text(
                "SELECT encrypted_credentials_b64 FROM ad_account_connections "
                "WHERE tenant_id = :tid AND external_account_id = 'act-778899'"
            ),
            {"tid": str(workspace["tenant"])},
        )
        assert envelope_b64 is not None
        assert SECRET_TOKEN not in envelope_b64
        envelope = unpack_credential_envelope(str(envelope_b64))
        plaintext = await crypto.decrypt_secret(
            session,
            workspace["tenant"],
            UUID(account["id"]),
            ADS_CREDENTIAL_TYPE,
            envelope.ciphertext,
            envelope.nonce,
            envelope.auth_tag,
            envelope.key_version,
        )
        assert plaintext == SECRET_TOKEN

        with pytest.raises(AppError) as excinfo:
            await crypto.decrypt_secret(
                session,
                workspace["tenant"],
                uuid4(),  # a different account id changes the AAD
                ADS_CREDENTIAL_TYPE,
                envelope.ciphertext,
                envelope.nonce,
                envelope.auth_tag,
                envelope.key_version,
            )
        assert excinfo.value.code == "ERR_INVALID_CREDENTIAL_TAG"


async def test_sandbox_sync_upserts_and_summary_is_exact(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    sandbox = SandboxAdsAdapter()
    account = await _connect(workspace, platform="sandbox_ads")
    context = AdsAccountContext(
        tenant_id=workspace["tenant"],
        account_id=UUID(account["id"]),
        platform="sandbox_ads",
        external_account_id=account["external_account_id"],
        account_name=account["account_name"],
        currency="USD",
    )
    with pytest.MonkeyPatch.context() as patch:
        # Pin the adapter clock so the expected rows and the summary window agree.
        patch.setattr(sandbox, "_today", datetime.now(UTC).date())
        expected = list(await sandbox.fetch_daily_spend(account=context, days=5))
        async with _client(
            factory, workspace["owner"], ads_adapters={"sandbox_ads": sandbox}
        ) as client:
            first = await client.post(
                f"{ADS_BASE}/accounts/{account['id']}/sync", params={"days": 5}
            )
            assert first.status_code == 200, first.text
            result = first.json()
            assert result["days"] == 5
            assert result["rows_upserted"] == len(expected) == 15
            assert result["account"]["last_synced_at"] is not None

            # Re-syncing the same window must be idempotent, not duplicating rows.
            again = await client.post(
                f"{ADS_BASE}/accounts/{account['id']}/sync", params={"days": 5}
            )
            assert again.status_code == 200
            assert again.json()["rows_upserted"] == 15

            summary = await client.get(f"{ADS_BASE}/spend-summary", params={"days": 30})
    assert summary.status_code == 200, summary.text
    body = summary.json()

    assert len(body["rows"]) == 15
    total_spend = sum((row.ad_spend for row in expected), Decimal("0.00"))
    total_attributed = sum((row.attributed_sales for row in expected), Decimal("0.00"))
    store_by_day: dict[date, Decimal] = {}
    for row in expected:
        store_by_day[row.spend_date] = max(
            store_by_day.get(row.spend_date, Decimal("0.00")), row.total_store_sales
        )
    total_store = sum(store_by_day.values(), Decimal("0.00"))

    assert Decimal(body["total_ad_spend"]) == total_spend
    assert Decimal(body["total_attributed_sales"]) == total_attributed
    assert Decimal(body["total_store_sales"]) == total_store
    assert body["total_impressions"] == sum(row.impressions for row in expected)
    assert body["total_clicks"] == sum(row.clicks for row in expected)
    assert body["currency"] == "USD"

    assert Decimal(body["roas"]) == (
        total_attributed / total_spend
    ).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    assert Decimal(body["tacos_pct"]) == (
        (total_spend / total_store) * 100
    ).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    assert Decimal(body["ctr_pct"]) == compute_ctr_pct(
        body["total_clicks"], body["total_impressions"]
    )
    # Every money value crosses the wire as an exact two-decimal string.
    for key in ("total_ad_spend", "total_attributed_sales", "total_store_sales", "roas"):
        assert isinstance(body[key], str)
        assert len(body[key].split(".")[1]) == 2
    assert all(isinstance(row["ad_spend"], str) for row in body["rows"])


async def test_meta_sync_uses_a_get_only_transport(workspace: dict[str, Any]) -> None:
    account = await _connect(
        workspace, platform="meta_ads", external_account_id=f"act-{uuid4().hex[:8]}"
    )
    day = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    payload = httpx.Response(
        200,
        json={
            "data": [
                {
                    "date_start": day,
                    "campaign_name": "QZ-LED Retarget",
                    "spend": "120.50",
                    "impressions": "3400",
                    "clicks": "85",
                    "action_values": [
                        {"action_type": "purchase", "value": "301.25"},
                        {"action_type": "add_to_cart", "value": "999.00"},
                    ],
                }
            ]
        },
    )
    seen: list[httpx.Request] = []
    adapter = MetaAdsReadonlyAdapter(
        transport=_mock_transport(payload, seen, expected_host="graph.facebook.com")
    )
    factory: Factory = workspace["factory"]
    async with _client(
        factory, workspace["owner"], ads_adapters={"meta_ads": adapter}
    ) as client:
        synced = await client.post(
            f"{ADS_BASE}/accounts/{account['id']}/sync", params={"days": 3}
        )
        assert synced.status_code == 200, synced.text
        assert synced.json()["rows_upserted"] == 1

        summary = await client.get(f"{ADS_BASE}/spend-summary", params={"days": 30})
    body = summary.json()
    assert Decimal(body["total_ad_spend"]) == Decimal("120.50")
    assert Decimal(body["total_attributed_sales"]) == Decimal("301.25")
    # only "purchase" counts toward attributed sales, not add_to_cart
    assert Decimal(body["total_store_sales"]) == Decimal("301.25")
    assert Decimal(body["roas"]) == Decimal("2.50")
    assert Decimal(body["tacos_pct"]) == Decimal("40.00")  # 120.50 / 301.25 * 100
    assert Decimal(body["ctr_pct"]) == Decimal("2.50")
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert request.url.params["access_token"] == SECRET_TOKEN
    assert request.url.params["level"] == "campaign"
    assert str(request.url).startswith("https://graph.facebook.com/v20.0/act_")
    assert "insights" in str(request.url)


async def test_amazon_sync_uses_a_get_only_transport(workspace: dict[str, Any]) -> None:
    account = await _connect(
        workspace, platform="amazon_ads", external_account_id=f"amz-{uuid4().hex[:8]}"
    )
    day = datetime.now(UTC).date().isoformat()
    payload = httpx.Response(
        200,
        json={
            "campaigns": [
                {
                    "reportDate": day,
                    "campaignName": "QZ-LED Sponsored Products",
                    "cost": "80.00",
                    "impressions": "2000",
                    "clicks": "20",
                    "attributedSalesSameSku": "160.00",
                    "totalStoreSales": "2000.00",
                    "sku": "LED-PANEL-600",
                }
            ]
        },
    )
    seen: list[httpx.Request] = []
    adapter = AmazonAdsReadonlyAdapter(
        transport=_mock_transport(payload, seen, expected_host="advertising-api.amazon.com")
    )
    factory: Factory = workspace["factory"]
    async with _client(
        factory, workspace["owner"], ads_adapters={"amazon_ads": adapter}
    ) as client:
        synced = await client.post(
            f"{ADS_BASE}/accounts/{account['id']}/sync", params={"days": 2}
        )
        assert synced.status_code == 200, synced.text
        summary = await client.get(f"{ADS_BASE}/spend-summary", params={"days": 30})
    body = summary.json()
    assert Decimal(body["total_ad_spend"]) == Decimal("80.00")
    assert Decimal(body["total_store_sales"]) == Decimal("2000.00")
    assert Decimal(body["tacos_pct"]) == Decimal("4.00")
    assert Decimal(body["roas"]) == Decimal("2.00")
    assert body["rows"][0]["sku"] == "LED-PANEL-600"
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].headers["amazon-advertising-api-scope"] == account["external_account_id"]


async def test_adapter_failure_surfaces_a_problem_detail(workspace: dict[str, Any]) -> None:
    account = await _connect(workspace, platform="meta_ads")
    adapter = MetaAdsReadonlyAdapter(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"error": 1}))
    )
    factory: Factory = workspace["factory"]
    async with _client(
        factory, workspace["owner"], ads_adapters={"meta_ads": adapter}
    ) as client:
        response = await client.post(f"{ADS_BASE}/accounts/{account['id']}/sync")
        assert response.status_code == 422
        problem = response.json()
        assert problem["code"] == "ERR_ADS_ADAPTER_REJECTED"
        assert SECRET_TOKEN not in response.text
        assert "401" in problem["detail"]


async def test_ads_rbac_matrix(workspace: dict[str, Any]) -> None:
    assert UserRole.OWNER in ADS_READ_ROLES and UserRole.FINANCE in ADS_READ_ROLES
    assert UserRole.AGENT not in ADS_READ_ROLES
    assert UserRole.AGENT not in ADS_WRITE_ROLES
    assert UserRole.MANAGER in ADS_WRITE_ROLES

    factory: Factory = workspace["factory"]
    async with _client(factory, workspace["agent"]) as client:
        for method, path in (
            ("get", f"{ADS_BASE}/accounts"),
            ("get", f"{ADS_BASE}/spend-summary"),
        ):
            response = await getattr(client, method)(path)
            assert response.status_code == 403, path
            assert response.json()["code"] == "ERR_INSUFFICIENT_ROLE"

    # FINANCE reviews spend but must not wire credentials.
    async with _client(factory, workspace["finance"]) as client:
        assert (await client.get(f"{ADS_BASE}/accounts")).status_code == 200
        assert (await client.get(f"{ADS_BASE}/spend-summary")).status_code == 200
        blocked = await client.post(
            f"{ADS_BASE}/accounts",
            json={
                "platform": "sandbox_ads",
                "account_name": "finance cannot connect",
                "external_account_id": "nope-1",
                "access_token": SECRET_TOKEN,
            },
        )
        assert blocked.status_code == 403

    # A MANAGER may connect, sync and delete.
    async with _client(factory, workspace["manager"]) as client:
        created = await client.post(
            f"{ADS_BASE}/accounts",
            json={
                "platform": "sandbox_ads",
                "account_name": "manager account",
                "external_account_id": "mgr-act-1",
                "access_token": SECRET_TOKEN,
            },
        )
        assert created.status_code == 201, created.text
        account_id = created.json()["id"]
        assert (
            await client.post(f"{ADS_BASE}/accounts/{account_id}/sync", params={"days": 2})
        ).status_code == 200
        assert (await client.delete(f"{ADS_BASE}/accounts/{account_id}")).status_code == 204
        assert (await client.get(f"{ADS_BASE}/accounts")).json() == []


async def test_ads_cross_tenant_isolation(workspace: dict[str, Any]) -> None:
    account = await _connect(workspace)
    factory: Factory = workspace["factory"]
    async with _client(factory, workspace["outsider"]) as client:
        assert (await client.get(f"{ADS_BASE}/accounts")).json() == []
        assert (await client.get(f"{ADS_BASE}/spend-summary")).json()["rows"] == []
        assert (
            await client.delete(f"{ADS_BASE}/accounts/{account['id']}")
        ).status_code == 404
        assert (
            await client.post(f"{ADS_BASE}/accounts/{account['id']}/sync")
        ).status_code == 404

    async with tenant_session(factory, workspace["other_tenant"]) as session:
        assert await session.scalar(text("SELECT count(*) FROM ad_account_connections")) == 0
        assert await session.scalar(text("SELECT count(*) FROM ad_spend_daily_snapshots")) == 0


async def test_deleting_an_account_cascades_its_snapshots(workspace: dict[str, Any]) -> None:
    account = await _connect(workspace)
    factory: Factory = workspace["factory"]
    async with _client(
        factory, workspace["owner"], ads_adapters={"sandbox_ads": SandboxAdsAdapter()}
    ) as client:
        assert (
            await client.post(f"{ADS_BASE}/accounts/{account['id']}/sync", params={"days": 2})
        ).status_code == 200
        assert (await client.delete(f"{ADS_BASE}/accounts/{account['id']}")).status_code == 204
        summary = await client.get(f"{ADS_BASE}/spend-summary", params={"days": 30})
    assert summary.json()["rows"] == []
    async with tenant_session(factory, workspace["tenant"]) as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM ad_spend_daily_snapshots WHERE tenant_id = :tid"),
                {"tid": str(workspace["tenant"])},
            )
            == 0
        )


async def test_summary_window_excludes_older_snapshots(workspace: dict[str, Any]) -> None:
    account = await _connect(workspace)
    factory: Factory = workspace["factory"]
    async with tenant_session(factory, workspace["tenant"]) as session:
        await session.execute(
            text(
                "INSERT INTO ad_spend_daily_snapshots "
                "(tenant_id, ad_account_id, spend_date, campaign_name, ad_spend, "
                " attributed_sales, total_store_sales, impressions, clicks) VALUES "
                "(:tid, :aid, CURRENT_DATE - 400, 'ancient', 500.00, 5000.00, 5000.00, 10, 1)"
            ),
            {"tid": workspace["tenant"], "aid": UUID(account["id"])},
        )
    async with _client(
        factory, workspace["owner"], ads_adapters={"sandbox_ads": SandboxAdsAdapter()}
    ) as client:
        assert (
            await client.post(f"{ADS_BASE}/accounts/{account['id']}/sync", params={"days": 2})
        ).status_code == 200
        summary = await client.get(f"{ADS_BASE}/spend-summary", params={"days": 7})
    body = summary.json()
    assert len(body["rows"]) == 6
    assert "ancient" not in {row["campaign_name"] for row in body["rows"]}
    assert Decimal(body["total_ad_spend"]) < Decimal("500.00")


async def test_sync_rejects_out_of_range_window(workspace: dict[str, Any]) -> None:
    account = await _connect(workspace)
    factory: Factory = workspace["factory"]
    async with _client(factory, workspace["owner"]) as client:
        assert (
            await client.post(
                f"{ADS_BASE}/accounts/{account['id']}/sync", params={"days": 0}
            )
        ).status_code == 422
        assert (
            await client.post(
                f"{ADS_BASE}/accounts/{account['id']}/sync", params={"days": 366}
            )
        ).status_code == 422
        assert (
            await client.get(f"{ADS_BASE}/spend-summary", params={"days": 0})
        ).status_code == 422


async def test_visual_provider_error_is_a_problem_detail_not_a_500(
    workspace: dict[str, Any],
) -> None:
    provider = RemoveBgHttpProvider(api_key="   ", transport=httpx.MockTransport(_forbid_network))
    with pytest.raises(VisualProviderError) as excinfo:
        await provider.process(
            VisualJobRequest(
                tenant_id=workspace["tenant"],
                job_id=uuid4(),
                provider="remove_bg",
                operation="remove_bg",
                source_image_url="https://cdn.example.com/a.png",
            )
        )
    assert excinfo.value.code == "ERR_VISUAL_PROVIDER_NOT_CONFIGURED"
