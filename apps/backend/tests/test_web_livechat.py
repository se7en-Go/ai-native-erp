from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.widget_router import widget_router
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import IdentityType


@pytest.mark.asyncio
async def test_web_livechat_cors_session_prechat_optin_and_hmac_identify(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET", "widget-test-jwt-secret-key-2026")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")

    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    crypto = EnvelopeCryptoService(get_settings())

    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    site_key = f"site_{uuid.uuid4().hex[:10]}"
    identify_secret = "shop_hmac_shared_secret_2026"
    existing_email = "returning.vip@ledbuyer.de"

    app = FastAPI()
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.include_router(widget_router)

    try:
        async with tenant_session(session_factory, tenant_id) as sess:
            await sess.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Widget Store')"),
                {"id": tenant_id, "slug": f"wid-{tenant_id.hex[:8]}"},
            )
            await crypto.provision_tenant_dek(sess, tenant_id)
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
                            "allowed_origins": ["https://ledstore.example.com"],
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
            ct, nonce, tag, kv = await crypto.encrypt_secret(
                sess, tenant_id, account_id, "widget_hmac_secret", identify_secret
            )
            await sess.execute(
                text(
                    "INSERT INTO channel_credentials "
                    "(id, tenant_id, channel_account_id, credential_type, "
                    "ciphertext, nonce, auth_tag, key_version) VALUES ("
                    ":id, :tid, :aid, 'widget_hmac_secret', :ct, :nonce, :tag, :kv)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tenant_id,
                    "aid": account_id,
                    "ct": ct,
                    "nonce": nonce,
                    "tag": tag,
                    "kv": kv,
                },
            )
            # Pre-create identified customer for HMAC merge assertion
            from erp_core.identity.service import IdentityResolverService

            vip_customer_id, _ = await IdentityResolverService().resolve_or_create_customer(
                sess,
                tenant_id,
                IdentityType.EMAIL,
                existing_email,
                "VIP Buyer",
                country_hint="DE",
            )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Untrusted Origin blocked with 403
            bad_origin = await client.post(
                "/api/v1/widget/session",
                json={"site_key": site_key},
                headers={"Origin": "https://evil-phishing.example"},
            )
            assert bad_origin.status_code == 403

            # 2. Trusted Origin succeeds and issues anonymous JWT + records Pre-Chat opt-in
            sess_resp = await client.post(
                "/api/v1/widget/session",
                json={
                    "site_key": site_key,
                    "page_url": "https://ledstore.example.com/products/cob-24v",
                    "sku": "LED-COB-24V",
                    "prechat_opt_in": {
                        "display_name": "Anonymous Shopper",
                        "whatsapp_phone": "+491701112233",
                        "marketing_opt_in": True,
                    },
                },
                headers={"Origin": "https://ledstore.example.com"},
            )
            assert sess_resp.status_code == 200
            assert (
                sess_resp.headers.get("access-control-allow-origin")
                == "https://ledstore.example.com"
            )
            data = sess_resp.json()
            token = data["session_token"]
            visitor_id = data["visitor_id"]
            anon_customer_id = uuid.UUID(data["customer_id"])
            conv_id = uuid.UUID(data["conversation_id"])

            # Verify Pre-Chat opt-in recorded in consent_records
            async with tenant_session(session_factory, tenant_id) as sess:
                consent_status = (
                    await sess.execute(
                        text(
                            "SELECT consent_status FROM consent_records "
                            "WHERE customer_id = :cid ORDER BY recorded_at DESC LIMIT 1"
                        ),
                        {"cid": anon_customer_id},
                    )
                ).scalar_one()
                assert consent_status == "opted_in"

                # Insert an internal_note to verify visitor GET /messages never leaks internal notes
                await sess.execute(
                    text(
                        "INSERT INTO messages "
                        "(id, tenant_id, conversation_id, channel_type, direction, sender_type, "
                        "content_type, body_text, delivery_status, source_timestamp) VALUES ("
                        ":id, :tid, :conv_id, 'web_livechat', 'internal_note', 'human_agent', "
                        "'text', 'SECRET INTERNAL MARGIN NOTE', 'delivered', now())"
                    ),
                    {"id": uuid.uuid4(), "tid": tenant_id, "conv_id": conv_id},
                )

            # 3. Visitor sends message and lists messages (internal_note hidden!)
            msg_resp = await client.post(
                "/api/v1/widget/messages",
                json={"body_text": "Is IP67 strip dimmable?"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://ledstore.example.com",
                },
            )
            assert msg_resp.status_code == 201

            list_resp = await client.get(
                "/api/v1/widget/messages",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://ledstore.example.com",
                },
            )
            assert list_resp.status_code == 200
            bodies = [m["body_text"] for m in list_resp.json()["items"]]
            assert "Is IP67 strip dimmable?" in bodies
            assert "SECRET INTERNAL MARGIN NOTE" not in bodies

            # 4. Invalid HMAC on /identify returns 401
            ts_now = int(time.time())
            bad_ident = await client.post(
                "/api/v1/widget/identify",
                json={
                    "email": existing_email,
                    "timestamp": ts_now,
                    "hmac_signature": "bad_sig",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://ledstore.example.com",
                },
            )
            assert bad_ident.status_code == 401

            # 5. Valid HMAC merges anonymous session into existing VIP customer
            canonical_payload = f"{visitor_id}:{existing_email}:::{ts_now}"
            valid_sig = hmac.new(
                identify_secret.encode("utf-8"),
                canonical_payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            ok_ident = await client.post(
                "/api/v1/widget/identify",
                json={
                    "email": existing_email,
                    "timestamp": ts_now,
                    "hmac_signature": valid_sig,
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://ledstore.example.com",
                },
            )
            assert ok_ident.status_code == 200
            assert uuid.UUID(ok_ident.json()["customer_id"]) == vip_customer_id
            assert ok_ident.json()["merged"] is True
    finally:
        async with tenant_session(session_factory, tenant_id) as conn:
            await conn.execute(
                text(
                    "SELECT register_channel_routing("
                    "'web_livechat', :skey, CAST(:tid AS uuid), CAST(:aid AS uuid), false)"
                ),
                {"skey": site_key, "tid": tenant_id, "aid": account_id},
            )
        await redis.aclose()
        await runtime_engine.dispose()
