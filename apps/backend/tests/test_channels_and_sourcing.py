"""Unit coverage for marketplace onboarding + sourcing collect/price/import/publish.

Every test here runs without PostgreSQL: the routers are mounted on a throwaway
FastAPI app with a recording fake session, and the sourcing helpers are pure.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from erp_core.catalog.sourcing_service import (
    SourcingCollectRequest,
    SourcingService,
    build_english_title,
    build_external_sku,
    build_selling_points,
    build_suggested_sku,
    channel_sku_prefix,
    compute_sourcing_pricing,
    parse_page_metadata,
    parse_supplier_copy,
    plan_sourcing_draft,
    validate_source_url,
)
from erp_core.channels.ecommerce import marketplace_connect_router as marketplace
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError, register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

public_html = """<!doctype html>
<html><head>
  <title>1688 - 厂家直销 LED Flood Light 100W</title>
  <meta property="og:title" content="Solar LED Flood Light 100W &amp; Waterproof" />
  <meta property="og:image" content="https://cbu01.alicdn.com/img/ibank/flood-100w.jpg" />
  <meta property="og:description"
        content="Factory direct solar flood light, MOQ 100 pcs, CE/RoHS" />
  <meta property="og:price:amount" content="12.50" />
  <meta property="og:price:currency" content="CNY" />
</head><body><p>批发价 ¥12.50 起</p></body></html>
"""


# --------------------------------------------------------------------------------------
# SSRF guard
# --------------------------------------------------------------------------------------


def test_source_url_ssrf_guard_rejects_private_and_non_http_targets() -> None:
    blocked = [
        "http://localhost/admin",
        "http://LOCALHOST:8080/",
        "https://127.0.0.1/",
        "http://127.1.2.3/",
        "http://10.1.2.3/offer/1.html",
        "http://172.16.5.5/offer/1.html",
        "http://172.31.255.254/",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0:80/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://intranet/item/1.html",
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",
        "ftp://example.com/firmware.bin",
        "https://user:pass@detail.1688.com/offer/1.html",
        "",
    ]
    for candidate in blocked:
        with pytest.raises(AppError) as err:
            validate_source_url(candidate)
        assert err.value.code == "ERR_UNSAFE_SOURCE_URL", candidate
        assert err.value.status_code == 422

    allowed = [
        "https://detail.1688.com/offer/681234567890.html",
        "http://example.com/catalog?page=2",
        "https://www.alibaba.com/product-detail/LED-Flood-Light_1600123.html",
    ]
    for candidate in allowed:
        assert validate_source_url(candidate) == candidate


async def test_ssrf_guard_blocks_before_any_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network access must never happen for {request.url}")

    service = SourcingService(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(AppError) as err:
        await service.fetch_source_metadata("http://169.254.169.254/latest/meta-data/")
    assert err.value.code == "ERR_UNSAFE_SOURCE_URL"


# --------------------------------------------------------------------------------------
# Decimal pricing
# --------------------------------------------------------------------------------------


def test_decimal_moq_ladder_uses_round_half_up_and_never_float() -> None:
    pricing = compute_sourcing_pricing(
        Decimal("12.50"), Decimal("7.200000"), Decimal("35.00")
    )
    assert isinstance(pricing.cost_usd, Decimal)
    assert isinstance(pricing.target_price_usd, Decimal)
    assert pricing.cost_usd == Decimal("1.7361")
    assert pricing.target_price_usd == Decimal("2.67")
    assert [(tier.min_qty, tier.unit_price_usd) for tier in pricing.moq_tiers] == [
        (100, Decimal("2.67")),
        (500, Decimal("2.54")),
        (2000, Decimal("2.40")),
    ]


def test_pricing_rejects_invalid_margin_price_and_fx() -> None:
    with pytest.raises(AppError) as margin_err:
        compute_sourcing_pricing(Decimal("10.00"), Decimal("7.2"), Decimal("90.00"))
    assert margin_err.value.code == "ERR_INVALID_SOURCING_MARGIN"

    with pytest.raises(AppError) as price_err:
        compute_sourcing_pricing(Decimal("-1.00"), Decimal("7.2"), Decimal("35.00"))
    assert price_err.value.code == "ERR_INVALID_SOURCING_PRICE"

    with pytest.raises(AppError) as fx_err:
        compute_sourcing_pricing(Decimal("10.00"), Decimal("0"), Decimal("35.00"))
    assert fx_err.value.code == "ERR_INVALID_SOURCING_FX"


# --------------------------------------------------------------------------------------
# 1688 copy parsing + SEO/plan generation
# --------------------------------------------------------------------------------------


def test_1688_supplier_copy_parsing() -> None:
    raw_title = "【厂家直销】LED太阳能投光灯100W户外防水 庭院灯"
    raw_text = (
        "阿里巴巴 1688.com\n"
        "批发价：¥12.50 起，≥100件 12.50元\n"
        "图片 https://cbu01.alicdn.com/img/ibank/abc.jpg "
        "https://cbu01.alicdn.com/img/ibank/def.png "
        "https://cbu01.alicdn.com/img/ibank/abc.jpg\n"
    )
    source = parse_supplier_copy(raw_title, raw_text)
    assert source.title == "LED太阳能投光灯100W户外防水 庭院灯"
    assert source.price_cny == Decimal("12.50")
    assert source.images == [
        "https://cbu01.alicdn.com/img/ibank/abc.jpg",
        "https://cbu01.alicdn.com/img/ibank/def.png",
    ]
    assert "批发价" in (source.description or "")

    # Pasted copy with no explicit title falls back to the first non-empty line.
    only_text = parse_supplier_copy(None, "太阳能壁灯 20W\n价 ￥88.00\n")
    assert only_text.title == "太阳能壁灯 20W"
    assert only_text.price_cny == Decimal("88.00")


def test_english_title_bullets_and_canonical_sku_are_generated() -> None:
    payload = SourcingCollectRequest(
        source_platform="1688",
        source_url="https://detail.1688.com/offer/681234567890.html",
        raw_title="LED太阳能投光灯100W户外防水",
        raw_price_cny=Decimal("45.00"),
        target_margin_pct=Decimal("40.00"),
        category="LED Lighting",
        hs_code="9405429000",
    )
    planned = plan_sourcing_draft(
        payload, parse_supplier_copy(payload.raw_title, payload.raw_text)
    )
    assert planned.optimized_title_en == "LED Solar Flood Light 100W Outdoor Waterproof"
    assert planned.target_price_usd == Decimal("10.42")
    assert [tier.unit_price_usd for tier in planned.moq_tiers] == [
        Decimal("10.42"),
        Decimal("9.90"),
        Decimal("9.38"),
    ]
    assert len(planned.bullet_points) == 5
    assert "9405429000" in planned.bullet_points[3]
    assert re.fullmatch(r"SRC-1688-[0-9A-F]{4}", planned.suggested_sku)
    assert planned.suggested_hs_code == "9405429000"
    assert planned.attributes["sourcing_platform"] == "1688"
    assert build_suggested_sku(
        "1688", payload.source_url, payload.raw_title or "", Decimal("45.00")
    ) == planned.suggested_sku


def test_title_generation_falls_back_for_ascii_and_empty_sources() -> None:
    assert build_english_title("Solar LED Flood Light", "LED Lighting") == (
        "Solar LED Flood Light"
    )
    fallback = build_english_title("", "LED Lighting")
    assert "LED Lighting" in fallback
    assert "Wholesale" in fallback
    assert build_english_title("", "电缆").endswith("Wholesale Supplier Direct")
    points = build_selling_points(
        "Solar LED Flood Light",
        category="LED Lighting",
        pricing=compute_sourcing_pricing(Decimal("10"), Decimal("7.2"), Decimal("30")),
        hs_code=None,
    )
    assert len(points) == 5
    assert all(point for point in points)


def test_plan_requires_title_and_price() -> None:
    empty = SourcingCollectRequest(source_platform="manual")
    with pytest.raises(AppError) as title_err:
        plan_sourcing_draft(empty, parse_supplier_copy(None, None))
    assert title_err.value.code == "ERR_SOURCING_TITLE_REQUIRED"

    no_price = SourcingCollectRequest(source_platform="manual", raw_title="Solar Wall Light")
    with pytest.raises(AppError) as price_err:
        plan_sourcing_draft(no_price, parse_supplier_copy(no_price.raw_title, None))
    assert price_err.value.code == "ERR_SOURCING_PRICE_REQUIRED"


def test_channel_external_sku_prefixing() -> None:
    assert channel_sku_prefix("amazon_sp") == "AMAZON"
    assert channel_sku_prefix("ebay") == "EBAY"
    assert channel_sku_prefix("alibaba_email") == "ALIBABA"
    assert build_external_sku("amazon_sp", "SRC-1688-9F3A") == "AMAZON-SRC-1688-9F3A"


# --------------------------------------------------------------------------------------
# HTML metadata parsing
# --------------------------------------------------------------------------------------


def test_html_metadata_parsing_prefers_open_graph() -> None:
    meta = parse_page_metadata(public_html)
    assert meta.title == "1688 - 厂家直销 LED Flood Light 100W"
    assert meta.og_title == "Solar LED Flood Light 100W & Waterproof"
    assert meta.best_title == "Solar LED Flood Light 100W & Waterproof"
    assert meta.og_image == "https://cbu01.alicdn.com/img/ibank/flood-100w.jpg"
    assert meta.og_description is not None
    assert "MOQ 100 pcs" in meta.og_description
    assert meta.price_cny == Decimal("12.50")
    assert not meta.is_empty()


def test_html_metadata_ignores_foreign_currency_and_parses_empty_document() -> None:
    html = (
        "<html><head><title>Solar Light</title>"
        '<meta property="og:price:amount" content="9.99" />'
        '<meta property="og:price:currency" content="USD" />'
        "</head><body>工厂价 ￥8.80</body></html>"
    )
    assert parse_page_metadata(html).price_cny == Decimal("8.80")
    assert parse_page_metadata("").is_empty()


async def test_remote_fetch_parses_page_and_degrades_gracefully_when_blocked() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if "blocked" in request.url.path:
            return httpx.Response(403, text="<html>please complete the captcha</html>")
        return httpx.Response(
            200, text=public_html, headers={"content-type": "text/html; charset=utf-8"}
        )

    transport = httpx.MockTransport(handler)
    service = SourcingService(
        client_factory=lambda: httpx.AsyncClient(
            transport=transport, base_url="https://detail.1688.com"
        )
    )

    meta = await service.fetch_source_metadata("https://detail.1688.com/offer/681234.html")
    assert meta.og_title == "Solar LED Flood Light 100W & Waterproof"
    assert meta.price_cny == Decimal("12.50")

    blocked = await service.fetch_source_metadata("https://detail.1688.com/blocked/681234.html")
    assert blocked.is_empty()
    assert len(requested) == 2

    # Anti-bot page + user-provided copy => degrade to the pasted text, never bypass.
    payload = SourcingCollectRequest(
        source_platform="url",
        source_url="https://detail.1688.com/blocked/681234.html",
        raw_title="LED太阳能投光灯100W",
        raw_text="供应商报价 ¥45.00",
    )
    source = await service.resolve_source(payload)
    assert source.title == "LED太阳能投光灯100W"
    assert source.price_cny == Decimal("45.00")
    planned = plan_sourcing_draft(payload, source)
    assert planned.source_platform == "url"
    assert planned.moq_tiers[0].min_qty == 100


# --------------------------------------------------------------------------------------
# Credential encryption (AAD binding) + marketplace connect endpoints
# --------------------------------------------------------------------------------------


def test_credential_aad_binds_tenant_account_and_credential_type() -> None:
    tenant_id, account_id = uuid4(), uuid4()
    assert set(marketplace.CREDENTIAL_TYPES) == {
        "alibaba_email",
        "amazon_sp",
        "ebay",
        "shopee",
        "lazada",
        "temu",
        "shein",
        "aliexpress",
        "ozon",
        "walmart",
        "mercado_libre",
    }
    assert set(marketplace.CREDENTIAL_TYPES.values()) == {
        "alibaba_intl_app",
        "amazon_sp_api",
        "ebay_api",
        "shopee_app",
        "lazada_app",
        "temu_app",
        "shein_app",
        "aliexpress_app",
        "ozon_app",
        "walmart_app",
        "mercado_libre_app",
    }

    aad = EnvelopeCryptoService._build_aad(
        tenant_id, account_id, marketplace.ALIBABA_INTL_CREDENTIAL_TYPE
    )
    assert aad.decode("utf-8") == f"{tenant_id}:{account_id}:alibaba_intl_app"

    key, nonce = bytes(range(32)), bytes(range(12))
    plaintext = b'{"app_secret":"alibaba-secret-001"}'
    sealed = AESGCM(key).encrypt(nonce, plaintext, aad)
    assert AESGCM(key).decrypt(nonce, sealed, aad) == plaintext

    # Tampered AAD (wrong credential type or wrong account) must fail GCM verification.
    wrong_type = EnvelopeCryptoService._build_aad(
        tenant_id, account_id, marketplace.AMAZON_CREDENTIAL_TYPE
    )
    with pytest.raises(InvalidTag):
        AESGCM(key).decrypt(nonce, sealed, wrong_type)
    wrong_account = EnvelopeCryptoService._build_aad(
        tenant_id, uuid4(), marketplace.ALIBABA_INTL_CREDENTIAL_TYPE
    )
    with pytest.raises(InvalidTag):
        AESGCM(key).decrypt(nonce, sealed, wrong_account)


class _FakeResult:
    def one_or_none(self) -> None:
        return None


class _FakeSession:
    """Recording stand-in for AsyncSession: no DB, but SQL + params are observable."""

    def __init__(self, account_id: UUID) -> None:
        self.account_id = account_id
        self.statements: list[str] = []

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        self.statements.append(str(statement))
        return _FakeResult()

    async def scalar(self, statement: Any, params: Any = None) -> Any:
        sql = str(statement)
        self.statements.append(sql)
        if "audit_events" in sql:
            return 1
        return self.account_id


class _RecordingVault:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def save_secret_json(
        self,
        session: Any,
        *,
        tenant_id: UUID,
        channel_account_id: UUID,
        credential_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "channel_account_id": channel_account_id,
                "credential_type": credential_type,
                "payload": payload,
            }
        )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch, session: _FakeSession, vault: _RecordingVault
) -> None:
    @asynccontextmanager
    async def fake_tenant_session(factory: Any, tenant_id: UUID) -> AsyncIterator[Any]:
        yield session

    monkeypatch.setattr(marketplace, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(marketplace, "_credential_vault", lambda: vault)


def _build_app(role: UserRole, tenant_id: UUID) -> FastAPI:
    app = FastAPI()
    app.include_router(marketplace.router)
    register_exception_handlers(app)
    app.state.session_factory = SimpleNamespace()  # replaced by the faked tenant_session
    app.dependency_overrides[get_current_principal] = lambda: CurrentPrincipal(
        user_id=uuid4(), tenant_id=tenant_id, role=role, email="ops@erp.local"
    )
    return app


async def test_marketplace_connect_seals_credentials_and_never_echoes_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    account_id = uuid4()
    session = _FakeSession(account_id)
    vault = _RecordingVault()
    _install_fakes(monkeypatch, session, vault)
    app = _build_app(UserRole.ADMIN, tenant_id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        alibaba = await client.post(
            "/api/v1/channels/alibaba-intl/connect",
            json={
                "display_name": "Quanzhou LED Alibaba",
                "store_id": "ali-store-7788",
                "app_key": "ali-app-key",
                "app_secret": "alibaba-secret-001",
                "inquiry_email": "sales@qz-led.cn",
                "visibility_scope": "private",
            },
        )
        amazon = await client.post(
            "/api/v1/channels/amazon/connect",
            json={
                "display_name": "Amazon US Store",
                "seller_id": "A2EUQ1WTGCTBG2",
                "lwa_client_id": "amzn1.application-oa2-client.abc",
                "lwa_client_secret": "amazon-secret-002",
                "refresh_token": "Atzr|amazon-refresh-token-003",
            },
        )
        ebay = await client.post(
            "/api/v1/channels/ebay/connect",
            json={
                "display_name": "eBay US Seller",
                "seller_username": "qz_led_official",
                "app_id": "QZLed-EBAY-App",
                "cert_id": "ebay-cert-004-secret",
                "refresh_token": "v^1.1#ebay-refresh-005",
            },
        )

    assert alibaba.status_code == 201, alibaba.text
    assert amazon.status_code == 201, amazon.text
    assert ebay.status_code == 201, ebay.text

    assert alibaba.json()["channel_type"] == "alibaba_email"
    assert alibaba.json()["external_account_id"] == "ali-store-7788"
    assert alibaba.json()["credential_type"] == "alibaba_intl_app"
    assert alibaba.json()["visibility_scope"] == "private"
    assert amazon.json()["channel_type"] == "amazon_sp"
    assert amazon.json()["external_account_id"] == "A2EUQ1WTGCTBG2"
    assert amazon.json()["credential_type"] == "amazon_sp_api"
    assert ebay.json()["channel_type"] == "ebay"
    assert ebay.json()["external_account_id"] == "qz_led_official"
    assert ebay.json()["credential_type"] == "ebay_api"
    for response in (alibaba, amazon, ebay):
        assert str(account_id) in response.text
        for leak in (
            "alibaba-secret-001",
            "amazon-secret-002",
            "Atzr|amazon-refresh-token-003",
            "ebay-cert-004-secret",
            "v^1.1#ebay-refresh-005",
        ):
            assert leak not in response.text

    recorded = {call["credential_type"]: call for call in vault.calls}
    assert set(recorded) == {"alibaba_intl_app", "amazon_sp_api", "ebay_api"}
    assert recorded["alibaba_intl_app"]["payload"]["app_secret"] == "alibaba-secret-001"
    assert recorded["alibaba_intl_app"]["payload"]["visibility_scope"] == "private"
    assert recorded["amazon_sp_api"]["payload"]["lwa_client_secret"] == "amazon-secret-002"
    assert recorded["ebay_api"]["payload"]["cert_id"] == "ebay-cert-004-secret"
    for call in vault.calls:
        assert call["tenant_id"] == tenant_id
        assert call["channel_account_id"] == account_id
        aad = EnvelopeCryptoService._build_aad(
            call["tenant_id"], call["channel_account_id"], call["credential_type"]
        )
        assert aad.decode("utf-8").endswith(f":{call['credential_type']}")

    joined = "\n".join(session.statements)
    assert "INSERT INTO channel_accounts" in joined
    assert "register_channel_routing" in joined
    assert "audit_events" in joined
    # Defaults required by the spec are applied server-side of the request model.
    amazon_body = json.loads(amazon.text)
    assert amazon_body["channel_type"] == "amazon_sp"
    assert recorded["amazon_sp_api"]["payload"]["marketplace_id"] == "ATVPDKIKX0DER"
    assert recorded["ebay_api"]["payload"]["site_id"] == "EBAY_US"


async def test_marketplace_connect_rejects_non_admin_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(uuid4())
    vault = _RecordingVault()
    _install_fakes(monkeypatch, session, vault)
    app = _build_app(UserRole.AGENT, uuid4())

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/channels/alibaba-intl/connect",
            json={
                "display_name": "Rogue Store",
                "store_id": "ali-store-0000",
                "app_key": "k",
                "app_secret": "rogue-secret-1",
            },
        )
    assert response.status_code == 403
    assert response.json()["code"] == "ERR_INSUFFICIENT_ROLE"
    assert vault.calls == []
    assert session.statements == []
