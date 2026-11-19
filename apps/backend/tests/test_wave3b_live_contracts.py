from __future__ import annotations

import base64
from uuid import UUID, uuid4

import dns.asyncresolver
import dns.rdata
import dns.rdataclass
import dns.rdatatype
import dns.resolver
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.router_llm import AsyncMultiProviderClient
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session


async def _register(client: AsyncClient) -> tuple[UUID, dict[str, str]]:
    slug = f"live-contract-{uuid4().hex[:12]}"
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{slug}@factory.example.com",
            "password": "StrongPassword2026!",
            "full_name": "Factory Owner",
            "tenant_name": "Factory",
            "tenant_slug": slug,
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    return UUID(data["principal"]["tenant_id"]), {"Authorization": f"Bearer {data['access_token']}"}


async def test_openai_compatible_gateway_uses_configured_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:8877/gateway/v1/")
    monkeypatch.setenv("OPENAI_API_KEY", "wave3b-gateway-test-value")
    get_settings.cache_clear()

    def provider(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:8877/gateway/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer wave3b-gateway-test-value"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Translation"}}],
                "usage": {"prompt_tokens": 34, "completion_tokens": 9},
            },
        )

    try:
        result = await AsyncMultiProviderClient(transport=httpx.MockTransport(provider)).complete(
            provider="openai",
            model_id="gpt-4.1-mini",
            max_tokens=100,
            temperature=0,
            messages=[{"role": "user", "content": "Translate this"}],
        )
        assert (result.text, result.prompt_tokens, result.completion_tokens) == (
            "Translation",
            34,
            9,
        )
    finally:
        get_settings.cache_clear()


async def test_channels_account_list_is_rls_scoped_and_excludes_config(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first, headers = await _register(async_client)
    second, _ = await _register(async_client)
    for tenant, name in ((first, "visible"), (second, "not visible")):
        async with tenant_session(runtime_session_factory, tenant) as session:
            await session.execute(
                text("""
                INSERT INTO channel_accounts
                    (id, tenant_id, channel_type, external_account_id, display_name, config)
                VALUES (:id, :tid, 'email', :external, :name, '{"internal": true}'::jsonb)
            """),
                {"id": uuid4(), "tid": tenant, "external": str(tenant), "name": name},
            )

    response = await async_client.get("/api/v1/channels/accounts?limit=1", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1 and body["has_more"] is False
    assert [row["display_name"] for row in body["items"]] == ["visible"]
    assert "config" not in body["items"][0]
    anonymous = await async_client.get("/api/v1/channels/accounts")
    assert anonymous.status_code == 401


async def test_sandbox_rejects_evidence_free_wismo(
    async_client: AsyncClient,
) -> None:
    _, headers = await _register(async_client)
    response = await async_client.post(
        "/api/v1/onboarding/sandbox-test",
        headers=headers,
        json={"scenario_id": "b2c_wismo", "message": "Where is order #SH-1042?"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["passed"] is False
    assert response.json()["source_tool_call_id"] is None


async def test_dns_requires_real_spf_and_2048_bit_dkim(
    async_client: AsyncClient, monkeypatch
) -> None:
    _, headers = await _register(async_client)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    b64 = base64.b64encode(der).decode()
    records = {
        ("mail.fixture.test.", "TXT"): '"v=spf1 include:example.test -all"',
        ("resend._domainkey.mail.fixture.test.", "TXT"): f'"v=DKIM1; p={b64[:180]}" "{b64[180:]}"',
        ("bounce.mail.fixture.test.", "MX"): "10 mx.fixture.test.",
    }

    async def resolve(self, name: str, kind: str, **kwargs):
        value = records.get((name, kind))
        if value is None:
            raise dns.resolver.NXDOMAIN()
        return [dns.rdata.from_text(dns.rdataclass.IN, kind, value)]

    monkeypatch.setattr(dns.asyncresolver.Resolver, "resolve", resolve)
    path = "/api/v1/onboarding/email/verify-dns"
    response = await async_client.post(path, headers=headers, json={"domain": "mail.fixture.test"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "spf_verified": True,
        "dkim_verified": True,
        "return_path_verified": True,
    }
    records[("resend._domainkey.mail.fixture.test.", "TXT")] = '"v=DKIM1; p=broken"'
    response = await async_client.post(path, headers=headers, json={"domain": "mail.fixture.test"})
    assert response.json()["dkim_verified"] is False
