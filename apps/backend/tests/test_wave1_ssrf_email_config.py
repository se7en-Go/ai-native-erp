"""Wave-1 security regression coverage.

Four hardened surfaces, each covered without PostgreSQL or the open internet:

* ``catalog.sourcing_service`` — DNS-rebinding SSRF defence + 2 MiB streaming cap.
* ``channels.ecommerce.woocommerce`` — store-URL normalisation + outbound host guard.
* ``channels.adapters.email`` — Svix replay window + malformed ``whsec_`` secrets.
* ``shared.config`` — production boots must not inherit development secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from erp_core.catalog.sourcing_service import (
    MAX_SOURCE_FETCH_BYTES,
    SourcingService,
    resolve_and_validate_public_url,
)
from erp_core.channels.adapters.email import EmailAdapter
from erp_core.channels.ecommerce.woocommerce import WooCommerceAdapter, validate_store_url
from erp_core.shared.config import Settings
from erp_core.shared.errors import AppError

Resolver = Callable[[str], Awaitable[Sequence[str]]]


def _fixed_resolver(*answers: str) -> Resolver:
    async def resolver(host: str) -> Sequence[str]:
        resolved = list(answers)
        assert host, "resolver must always be asked about a hostname"
        return resolved

    return resolver


# --------------------------------------------------------------------------------------
# 1. Sourcing: DNS-rebinding SSRF defence
# --------------------------------------------------------------------------------------


async def test_sourcing_dns_guard_blocks_non_public_resolutions() -> None:
    blocked: list[tuple[str, tuple[str, ...]]] = [
        # A public FQDN that resolves back to loopback (nip.io / rebinding services).
        ("http://127.0.0.1.nip.io/admin", ("127.0.0.1",)),
        # IPv4-mapped IPv6 pointed at the cloud metadata endpoint.
        ("https://rebind.example.com/hook", ("::ffff:169.254.169.254",)),
        # Carrier-grade NAT space (RFC 6598).
        ("https://cgnat.example.net/nat", ("100.64.0.1",)),
        # One public answer must not whitelist a private sibling answer.
        ("https://split.example.org/offer", ("93.184.216.34", "10.0.0.5")),
        # Empty DNS answers fail closed.
        ("https://nowhere.example.org/offer", ()),
    ]
    for url, answers in blocked:
        with pytest.raises(AppError) as err:
            await resolve_and_validate_public_url(url, dns_resolver=_fixed_resolver(*answers))
        assert err.value.code == "ERR_SOURCING_URL_BLOCKED", url
        assert err.value.status_code == 422

    # IP literals are still rejected by the textual guard before DNS is consulted.
    for literal in ("http://100.64.0.1/", "http://[::ffff:169.254.169.254]/"):
        with pytest.raises(AppError):
            await resolve_and_validate_public_url(
                literal, dns_resolver=_fixed_resolver("93.184.216.34")
            )


async def test_sourcing_dns_guard_allows_public_resolution() -> None:
    safe_url, addresses = await resolve_and_validate_public_url(
        "https://origin.example.com/catalog?page=2",
        dns_resolver=_fixed_resolver("93.184.216.34"),
    )
    assert safe_url == "https://origin.example.com/catalog?page=2"
    assert addresses == ["93.184.216.34"]


async def test_fetch_source_metadata_rejects_rebound_host_before_any_request() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text="<html><title>nope</title></html>")

    service = SourcingService(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        dns_resolver=_fixed_resolver("127.0.0.1"),
    )
    with pytest.raises(AppError) as err:
        await service.fetch_source_metadata("http://127.0.0.1.nip.io/admin")
    assert err.value.code == "ERR_SOURCING_URL_BLOCKED"
    assert requested == []


# --------------------------------------------------------------------------------------
# 2. Sourcing: streaming read cap
# --------------------------------------------------------------------------------------


class _CountingStream(httpx.AsyncByteStream):
    """Yields ``chunks`` identical blocks and records how much the consumer pulled."""

    def __init__(self, chunk: bytes, chunks: int) -> None:
        self._chunk = chunk
        self._chunks = chunks
        self.bytes_read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.bytes_read += len(self._chunk)
            yield self._chunk


class _ChunkedTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: _CountingStream) -> None:
        self._stream = stream
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            stream=self._stream,
        )


async def test_sourcing_stream_stops_at_two_mib_cap() -> None:
    chunk = b"x" * (256 * 1024)  # 256 KiB per chunk
    stream = _CountingStream(
        b"<html><head><title>Huge Page</title></head><body>" + chunk, 12
    )  # ~3 MiB available
    transport = _ChunkedTransport(stream)
    service = SourcingService(
        client_factory=lambda: httpx.AsyncClient(transport=transport),
        dns_resolver=_fixed_resolver("93.184.216.34"),
    )

    metadata = await service.fetch_source_metadata("https://origin.example.com/huge.html")

    # The head of the document is still parsed ...
    assert metadata.title == "Huge Page"
    # ... but the reader stopped early instead of draining the whole body.
    assert stream.bytes_read > MAX_SOURCE_FETCH_BYTES  # it detected the overflow
    assert stream.bytes_read <= MAX_SOURCE_FETCH_BYTES + (256 * 1024)
    assert stream.bytes_read < 12 * (256 * 1024)


# --------------------------------------------------------------------------------------
# 3. WooCommerce: URL normalisation + DNS guard
# --------------------------------------------------------------------------------------


async def test_woocommerce_normalizes_bare_domain() -> None:
    assert (
        await validate_store_url("store.example.com", dns_resolver=_fixed_resolver("93.184.216.34"))
        == "https://store.example.com"
    )
    assert await validate_store_url("https://store.example.com/", resolve_dns=False) == (
        "https://store.example.com"
    )
    assert (
        await validate_store_url("store.example.com", resolve_dns=False)
        == "https://store.example.com"
    )


async def test_woocommerce_blocks_private_and_local_targets() -> None:
    unsafe_answers: list[tuple[str, ...]] = [
        ("10.1.2.3",),
        ("127.0.0.1",),
        ("169.254.169.254",),
        ("100.64.0.1",),
        ("::ffff:127.0.0.1",),
        ("fd00::1",),
        (),
    ]
    for answers in unsafe_answers:
        with pytest.raises(AppError) as err:
            await validate_store_url("store.example.com", dns_resolver=_fixed_resolver(*answers))
        assert err.value.code == "ERR_WOOCOMMERCE_UNSAFE_HOST", answers
        assert err.value.status_code == 422

    for unsafe_url in ("http://127.0.0.1:8080", "https://localhost", "https://[::1]"):
        with pytest.raises(AppError) as err:
            await validate_store_url(unsafe_url, resolve_dns=False)
        assert err.value.code == "ERR_WOOCOMMERCE_UNSAFE_HOST", unsafe_url

    with pytest.raises(AppError) as err:
        await validate_store_url("ftp://store.example.com", resolve_dns=False)
    assert err.value.code == "ERR_INVALID_WOOCOMMERCE_STORE"


async def test_woocommerce_skips_dns_only_for_injected_mock_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = WooCommerceAdapter(
            store_url="mock-only.invalid",
            consumer_key="ck_test",
            consumer_secret="cs_test",
            http_client=client,
        )
        assert adapter.store_url == "https://mock-only.invalid"
        products, cursor = await adapter.fetch_products_updated_since(datetime.now(UTC))
    assert products == []
    assert cursor is None

    # Without an injected client the same hostname is refused (real network path).
    with pytest.raises(AppError) as err:
        await WooCommerceAdapter(
            store_url="mock-only.invalid",
            consumer_key="ck_test",
            consumer_secret="cs_test",
        ).fetch_products_updated_since(datetime.now(UTC))
    assert err.value.code == "ERR_WOOCOMMERCE_UNSAFE_HOST"


# --------------------------------------------------------------------------------------
# 4. Email: Svix replay window + malformed secret
# --------------------------------------------------------------------------------------


def _svix_headers(raw_secret: bytes, raw_body: bytes, timestamp: int) -> dict[str, str]:
    svix_id = "msg_wave1_test"
    to_sign = f"{svix_id}.{timestamp}.".encode() + raw_body
    signature = base64.b64encode(hmac.new(raw_secret, to_sign, hashlib.sha256).digest()).decode()
    return {
        "svix-id": svix_id,
        "svix-timestamp": str(timestamp),
        "svix-signature": f"v1,{signature}",
    }


def test_email_svix_rejects_replayed_and_future_timestamps() -> None:
    adapter = EmailAdapter()
    raw_secret = b"whsec_test_email_secret_32bytes!!"
    secret = "whsec_" + base64.b64encode(raw_secret).decode()
    body = b'{"type": "email.received"}'
    now = int(time.time())

    assert adapter.verify_webhook_signature(body, _svix_headers(raw_secret, body, now), secret)
    # Replay: signed 5 minutes and 1 second in the past.
    assert not adapter.verify_webhook_signature(
        body, _svix_headers(raw_secret, body, now - 301), secret
    )
    # Clock-skew abuse in the other direction from the 5-minute window.
    assert not adapter.verify_webhook_signature(
        body, _svix_headers(raw_secret, body, now + 400), secret
    )
    # Unparsable timestamp fails closed instead of raising.
    tampered = _svix_headers(raw_secret, body, now)
    tampered["svix-timestamp"] = "not-a-timestamp"
    assert not adapter.verify_webhook_signature(body, tampered, secret)


def test_email_svix_malformed_whsec_secret_returns_false() -> None:
    adapter = EmailAdapter()
    raw_secret = b"whsec_test_email_secret_32bytes!!"
    body = b'{"type": "email.received"}'
    headers = _svix_headers(raw_secret, body, int(time.time()))

    for malformed in ("whsec_a", "whsec_@@@@", "whsec_%%%%"):
        assert adapter.verify_webhook_signature(body, headers, malformed) is False

    # A correctly signed payload with an unencrypted (non-whsec_) secret still works.
    assert adapter.verify_webhook_signature(
        body, _svix_headers(b"plain-test-secret", body, int(time.time())), "plain-test-secret"
    )


# --------------------------------------------------------------------------------------
# 5. Settings: production secret validator
# --------------------------------------------------------------------------------------

_STRONG_PRODUCTION_VALUES: dict[str, Any] = {
    "MASTER_KEK_HEX": "aa" * 32,
    "DATABASE_RUNTIME_URL": (
        "postgresql+asyncpg://app_runtime:strong-runtime-password@pgbouncer.internal:6432/erp"
    ),
    "DATABASE_MIGRATOR_URL": (
        "postgresql+asyncpg://app_migrator:strong-migrator-password@postgres.internal:5432/erp"
    ),
    "S3_SECRET_KEY": "s3-production-secret-value",
    "JWT_SECRET": "prod-" + "A1b2C3d4e5F6g7H8" * 3,
}


def test_production_settings_reject_each_development_default() -> None:
    weak_overrides: list[dict[str, Any]] = [
        {"JWT_SECRET": Settings.model_fields["JWT_SECRET"].default},
        {"JWT_SECRET": "too-short-secret"},
        {"MASTER_KEK_HEX": Settings.model_fields["MASTER_KEK_HEX"].default},
        {"DATABASE_RUNTIME_URL": Settings.model_fields["DATABASE_RUNTIME_URL"].default},
        {"DATABASE_MIGRATOR_URL": Settings.model_fields["DATABASE_MIGRATOR_URL"].default},
        {"S3_SECRET_KEY": Settings.model_fields["S3_SECRET_KEY"].default},
    ]
    for override in weak_overrides:
        payload = {**_STRONG_PRODUCTION_VALUES, **override}
        with pytest.raises(ValueError):
            Settings(APP_ENV="production", _env_file=None, **payload)  # type: ignore[call-arg]


def test_production_settings_accept_real_secrets() -> None:
    settings = Settings(
        APP_ENV="production",
        _env_file=None,
        **_STRONG_PRODUCTION_VALUES,  # type: ignore[call-arg]
    )
    assert settings.APP_ENV == "production"
    assert len(settings.JWT_SECRET) >= 32


def test_development_settings_keep_the_shipped_defaults() -> None:
    settings = Settings(APP_ENV="development", _env_file=None)  # type: ignore[call-arg]
    # Development/test boots never run the production secret gate.
    assert settings.APP_ENV == "development"
    assert settings.JWT_SECRET
