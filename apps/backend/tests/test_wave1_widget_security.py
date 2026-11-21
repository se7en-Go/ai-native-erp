"""Wave-1 widget security regression tests (session hijack, visitor signing, CORS, TTL)."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import jwt
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.widget_router import (
    sign_visitor_id,
    verify_signed_visitor_id,
    widget_router,
)
from erp_core.identity.service import IdentityResolverService
from erp_core.shared.config import get_settings
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import IdentityType

VICTIM_EMAIL = "victim@example.com"
VICTIM_PHONE = "+14155551234"
ALLOWED_ORIGIN = "https://ledstore.example.com"
UNTRUSTED_ORIGIN = "https://evil-phishing.example"


# --------------------------------------------------------------------------- helpers


def _secret() -> str:
    return get_settings().JWT_SECRET


@dataclass
class WidgetEnv:
    app: FastAPI
    client: AsyncClient
    session_factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    site_key: str


@pytest.fixture
async def widget_env() -> AsyncIterator[WidgetEnv]:
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    site_key = f"site_{uuid.uuid4().hex[:10]}"

    app = FastAPI()
    app.state.session_factory = session_factory
    app.include_router(widget_router)

    async with tenant_session(session_factory, tenant_id) as sess:
        await sess.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Widget Store')"),
            {"id": tenant_id, "slug": f"wid-{tenant_id.hex[:8]}"},
        )
        await sess.execute(
            text(
                "INSERT INTO channel_accounts "
                "(id, tenant_id, channel_type, external_account_id, display_name, status, "
                "rate_limit_profile, config) VALUES ("
                ":id, :tid, 'web_livechat', :skey, 'LED Store Chat', 'active', "
                "'{\"mps\": 100}'::jsonb, CAST(:cfg AS jsonb))"
            ),
            {
                "id": account_id,
                "tid": tenant_id,
                "skey": site_key,
                "cfg": json.dumps(
                    {
                        "allowed_origins": [ALLOWED_ORIGIN],
                        "business_name": "Quanzhou LED Co.",
                    }
                ),
            },
        )
        await sess.execute(
            text(
                "SELECT register_channel_routing("
                "'web_livechat', :skey, CAST(:tid AS uuid), CAST(:aid AS uuid), true)"
            ),
            {"skey": site_key, "tid": tenant_id, "aid": account_id},
        )

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    env = WidgetEnv(
        app=app,
        client=client,
        session_factory=session_factory,
        tenant_id=tenant_id,
        account_id=account_id,
        site_key=site_key,
    )
    try:
        yield env
    finally:
        await client.aclose()
        async with tenant_session(session_factory, tenant_id) as conn:
            await conn.execute(
                text(
                    "SELECT register_channel_routing("
                    "'web_livechat', :skey, CAST(:tid AS uuid), CAST(:aid AS uuid), false)"
                ),
                {"skey": site_key, "tid": tenant_id, "aid": account_id},
            )
        await runtime_engine.dispose()


# ---------------------------------------------------------------- visitor id signing


def test_sign_and_verify_visitor_id_roundtrip() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    raw_vid = str(uuid.uuid4())

    token = sign_visitor_id(
        raw_vid, tenant_id=tenant_id, account_id=account_id, secret="unit-secret"
    )

    raw_part, separator, signature = token.partition(".")
    assert separator == "."
    assert raw_part == raw_vid
    assert len(signature) == 24
    assert (
        verify_signed_visitor_id(
            token, tenant_id=tenant_id, account_id=account_id, secret="unit-secret"
        )
        == raw_vid
    )


def test_verify_rejects_cross_account_cross_tenant_tampered_and_raw_uuids() -> None:
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    raw_vid = str(uuid.uuid4())
    token = sign_visitor_id(
        raw_vid, tenant_id=tenant_id, account_id=account_id, secret="unit-secret"
    )
    raw_part, _, signature = token.partition(".")

    # Same signature replayed against a different channel account / tenant must fail.
    assert (
        verify_signed_visitor_id(
            token, tenant_id=tenant_id, account_id=other_account_id, secret="unit-secret"
        )
        is None
    )
    assert (
        verify_signed_visitor_id(
            token, tenant_id=other_tenant_id, account_id=account_id, secret="unit-secret"
        )
        is None
    )
    # Different signing secret (rotated/foreign deployment) must fail.
    assert (
        verify_signed_visitor_id(
            token, tenant_id=tenant_id, account_id=account_id, secret="other-secret"
        )
        is None
    )
    # Tampered MAC.
    tampered_mac = "0" * 24 if signature != "0" * 24 else "1" * 24
    assert (
        verify_signed_visitor_id(
            f"{raw_part}.{tampered_mac}",
            tenant_id=tenant_id,
            account_id=account_id,
            secret="unit-secret",
        )
        is None
    )
    # Swapped visitor id with a valid-looking signature.
    other_raw = str(uuid.uuid4())
    assert (
        verify_signed_visitor_id(
            f"{other_raw}.{signature}",
            tenant_id=tenant_id,
            account_id=account_id,
            secret="unit-secret",
        )
        is None
    )
    # Unsigned raw UUID (the attacker-guessed identifier) and junk input.
    for bad in (raw_vid, f"{raw_vid}.", ".{signature}", "not-a-uuid.deadbeef", "", None):
        assert (
            verify_signed_visitor_id(
                bad, tenant_id=tenant_id, account_id=account_id, secret="unit-secret"
            )
            is None
        )


# ------------------------------------------------------------------ session semantics


async def test_session_never_resolves_unverified_prechat_email_or_phone(
    widget_env: WidgetEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[IdentityType, str]] = []
    original = IdentityResolverService.resolve_or_create_customer

    async def spy(
        self: IdentityResolverService,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        identity_type: IdentityType,
        raw_value: str,
        display_name: str,
        country_hint: str | None = None,
    ) -> tuple[uuid.UUID, bool]:
        calls.append((identity_type, raw_value))
        return await original(
            self, session, tenant_id, identity_type, raw_value, display_name, country_hint
        )

    monkeypatch.setattr(IdentityResolverService, "resolve_or_create_customer", spy)

    resp = await widget_env.client.post(
        "/api/v1/widget/session",
        json={
            "site_key": widget_env.site_key,
            "page_url": "https://ledstore.example.com/products/cob-24v",
            "prechat_opt_in": {
                "display_name": "Drive-by Visitor",
                "email": VICTIM_EMAIL,
                "whatsapp_phone": VICTIM_PHONE,
                "marketing_opt_in": True,
            },
        },
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert resp.status_code == 200
    data = resp.json()

    # 1. Only WEB_VISITOR_ID may ever be resolved by this unauthenticated endpoint.
    assert calls, "session must resolve the anonymous visitor identity"
    assert {identity_type for identity_type, _ in calls} == {IdentityType.WEB_VISITOR_ID}
    resolved_values = [raw_value for _, raw_value in calls]
    assert VICTIM_EMAIL not in resolved_values
    assert VICTIM_PHONE not in resolved_values

    # 2. The visitor id handed back is server-signed, and its raw UUID is what got resolved.
    signed_visitor_id = data["visitor_id"]
    raw_vid = verify_signed_visitor_id(
        signed_visitor_id,
        tenant_id=widget_env.tenant_id,
        account_id=widget_env.account_id,
        secret=_secret(),
    )
    assert raw_vid is not None
    assert calls == [(IdentityType.WEB_VISITOR_ID, raw_vid)]

    customer_id = uuid.UUID(data["customer_id"])
    conversation_id = uuid.UUID(data["conversation_id"])

    async with tenant_session(widget_env.session_factory, widget_env.tenant_id) as sess:
        identity_rows = (
            (
                await sess.execute(
                    text(
                        "SELECT identity_type, identity_value FROM customer_identities "
                        "WHERE tenant_id = :tid AND customer_id = :cid"
                    ),
                    {"tid": widget_env.tenant_id, "cid": customer_id},
                )
            )
            .mappings()
            .all()
        )
        assert [(r["identity_type"], r["identity_value"]) for r in identity_rows] == [
            ("web_visitor_id", raw_vid)
        ]

        # 3. Pre-chat contact details survive only as unverified leads on the conversation.
        policy = (
            await sess.execute(
                text(
                    "SELECT channel_policy_state FROM conversations "
                    "WHERE tenant_id = :tid AND id = :cid"
                ),
                {"tid": widget_env.tenant_id, "cid": conversation_id},
            )
        ).scalar_one()
        assert policy["unverified_prechat"] == {
            "email": VICTIM_EMAIL,
            "whatsapp_phone": VICTIM_PHONE,
            "marketing_opt_in": True,
        }

        # 4. No address was written to any identity or suppression ledger path.
        leaked = (
            await sess.execute(
                text(
                    "SELECT count(*) FROM customer_identities "
                    "WHERE tenant_id = :tid AND identity_value IN (:email, :phone)"
                ),
                {"tid": widget_env.tenant_id, "email": VICTIM_EMAIL, "phone": VICTIM_PHONE},
            )
        ).scalar_one()
        assert leaked == 0


async def test_signed_visitor_resumes_but_unsigned_or_forged_gets_fresh_session(
    widget_env: WidgetEnv,
) -> None:
    async def open_session(visitor_id: str | None) -> dict[str, object]:
        payload: dict[str, object] = {"site_key": widget_env.site_key}
        if visitor_id is not None:
            payload["visitor_id"] = visitor_id
        resp = await widget_env.client.post(
            "/api/v1/widget/session",
            json=payload,
            headers={"Origin": ALLOWED_ORIGIN},
        )
        assert resp.status_code == 200
        body: dict[str, object] = resp.json()
        return body

    first = await open_session(None)
    signed_vid = str(first["visitor_id"])
    raw_vid = verify_signed_visitor_id(
        signed_vid,
        tenant_id=widget_env.tenant_id,
        account_id=widget_env.account_id,
        secret=_secret(),
    )
    assert raw_vid is not None

    # Legitimate browser replaying its stored signed visitor id resumes the same thread.
    resumed = await open_session(signed_vid)
    assert resumed["visitor_id"] == signed_vid
    assert resumed["customer_id"] == first["customer_id"]
    assert resumed["conversation_id"] == first["conversation_id"]

    # Attacker guessing/supplying the bare UUID must NOT land in the victim's session.
    forged = await open_session(raw_vid)
    assert forged["visitor_id"] != signed_vid
    assert str(forged["visitor_id"]).partition(".")[0] != raw_vid
    assert forged["customer_id"] != first["customer_id"]
    assert forged["conversation_id"] != first["conversation_id"]
    assert (
        verify_signed_visitor_id(
            str(forged["visitor_id"]),
            tenant_id=widget_env.tenant_id,
            account_id=widget_env.account_id,
            secret=_secret(),
        )
        is not None
    )

    # A token signed for a different channel account is equally useless.
    foreign_signed = sign_visitor_id(
        raw_vid,
        tenant_id=widget_env.tenant_id,
        account_id=uuid.uuid4(),
        secret=_secret(),
    )
    foreign = await open_session(foreign_signed)
    assert foreign["conversation_id"] != first["conversation_id"]
    assert foreign["customer_id"] != first["customer_id"]

    # Tampering with the stored signature yields a brand-new anonymous session.
    tampered = f"{raw_vid}.{'f' * 24}"
    tampered_session = await open_session(tampered)
    assert tampered_session["customer_id"] != first["customer_id"]


async def test_widget_jwt_expires_within_two_hours(widget_env: WidgetEnv) -> None:
    resp = await widget_env.client.post(
        "/api/v1/widget/session",
        json={"site_key": widget_env.site_key},
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert resp.status_code == 200
    settings = get_settings()
    decoded = jwt.decode(
        resp.json()["session_token"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
    )
    ttl_seconds = int(decoded["exp"]) - int(decoded["iat"])
    assert ttl_seconds == 2 * 60 * 60
    assert ttl_seconds <= 7200


# ------------------------------------------------------------------------ preflight


async def test_options_preflight_never_grants_credentials(widget_env: WidgetEnv) -> None:
    resp = await widget_env.client.options(
        "/api/v1/widget/session",
        headers={
            "Origin": UNTRUSTED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 204
    lower_headers = {key.lower() for key in resp.headers}
    assert "access-control-allow-credentials" not in lower_headers

    # Even a trusted origin gets no credentialed grant on the unvalidated preflight.
    trusted = await widget_env.client.options(
        "/api/v1/widget/session",
        headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert trusted.status_code == 204
    assert "access-control-allow-credentials" not in {key.lower() for key in trusted.headers}
