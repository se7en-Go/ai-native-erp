from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import AdapterRegistry, OutboundMessageRequest
from erp_core.channels.adapters.whatsapp import WhatsAppAdapter
from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.channels.ingress_worker import process_raw_webhook_event
from erp_core.channels.webhook_store import accept_raw_webhook_event
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, MessageContentType


@pytest.mark.asyncio
async def test_whatsapp_embedded_signup_coexistence_bsuid_ctwa_stop_and_csw_gate(
    monkeypatch,
    migrator_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setenv("WHATSAPP_APP_ID", "meta_app_123")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "meta_secret_123")

    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    tenant_id = uuid.uuid4()
    phone_number_id = f"pn_{uuid.uuid4().hex[:10]}"
    waba_id = f"waba_{uuid.uuid4().hex[:10]}"
    crypto = EnvelopeCryptoService(get_settings())

    graph_calls: list[tuple[str, str, dict]] = []

    def _mock_transport_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content.decode()) if request.content else {}
        graph_calls.append((request.method, url, body))
        if "/oauth/access_token" in url:
            return httpx.Response(200, json={"access_token": "EAAG_SYSTEM_USER_TOK"})
        if "/subscribed_apps" in url:
            return httpx.Response(200, json={"success": True})
        if f"/{phone_number_id}?" in url or "fields=is_on_biz_app" in url:
            return httpx.Response(200, json={"is_on_biz_app": True, "platform_type": "CLOUD_API"})
        if "/smb_app_data" in url:
            return httpx.Response(200, json={"success": True})
        if "/messages" in url:
            return httpx.Response(
                200, json={"messages": [{"id": f"wamid.out.{uuid.uuid4().hex[:8]}"}]}
            )
        return httpx.Response(404, json={"error": "unhandled"})

    adapter = WhatsAppAdapter(http_transport=httpx.MockTransport(_mock_transport_handler))
    registry = AdapterRegistry()
    registry.register("whatsapp", adapter)
    registry.register(ChannelType.WHATSAPP_CLOUD.value, adapter)
    registry.register(ChannelType.WHATSAPP_COEXISTENCE.value, adapter)

    try:
        async with tenant_session(session_factory, tenant_id) as sess:
            await sess.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'WA Factory')"),
                {"id": tenant_id, "slug": f"wa-{tenant_id.hex[:8]}"},
            )
            await crypto.provision_tenant_dek(sess, tenant_id)
            channel_account_id = await adapter.complete_embedded_signup(
                session=sess,
                tenant_id=tenant_id,
                crypto_service=crypto,
                code="oauth_code_v4",
                waba_id=waba_id,
                phone_number_id=phone_number_id,
                is_coexistence=True,
                display_name="LED Factory WA",
            )

            acct_row = (
                (
                    await sess.execute(
                        text(
                            "SELECT channel_type, rate_limit_profile FROM channel_accounts WHERE "
                            "id = :id"
                        ),
                        {"id": channel_account_id},
                    )
                )
                .mappings()
                .one()
            )
            assert acct_row["channel_type"] == ChannelType.WHATSAPP_COEXISTENCE.value
            assert acct_row["rate_limit_profile"]["mps"] == 20
            assert acct_row["rate_limit_profile"]["coexistence"] is True

        # Ensure /register was NOT called for Coexistence and smb_app_data WAS called twice
        assert not any(url.endswith("/register") for _, url, _ in graph_calls)
        smb_sync_types = [b.get("sync_type") for _, url, b in graph_calls if "/smb_app_data" in url]
        assert smb_sync_types == ["smb_app_state_sync", "history"]

        # 1. Test BSUID without wa_id + CTWA 7-day FEP referral
        ctwa_wamid = f"wamid.ctwa.{uuid.uuid4().hex[:8]}"
        bsuid_val = f"US.BSUID.{uuid.uuid4().hex[:10]}"
        ctwa_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": phone_number_id},
                                "contacts": [
                                    {"profile": {"name": "BSUID Buyer"}, "user_id": bsuid_val}
                                ],
                                "messages": [
                                    {
                                        "id": ctwa_wamid,
                                        "from_user_id": bsuid_val,
                                        "timestamp": str(int(datetime.now(UTC).timestamp())),
                                        "type": "text",
                                        "text": {"body": "Saw your LED strip ad, MOQ?"},
                                        "referral": {
                                            "source_type": "ad",
                                            "source_id": "ad_998877",
                                            "ctwa_clid": "CTWA_CLICK_ABC_123",
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                }
            ]
        }
        raw_ctwa_id = uuid.uuid4()
        async with migrator_session_factory() as conn, conn.begin():
            await accept_raw_webhook_event(
                conn,
                event_id=raw_ctwa_id,
                tenant_id=tenant_id,
                channel_type="whatsapp_coexistence",
                external_event_id=ctwa_wamid,
                external_account_id=phone_number_id,
                headers_json="{}",
                payload_json=json.dumps(ctwa_payload),
            )
        assert (
            await process_raw_webhook_event(
                session_factory,
                redis,
                raw_ctwa_id,
                registry,
                broker_session_factory=migrator_session_factory,
            )
            == "processed"
        )

        async with tenant_session(session_factory, tenant_id) as sess:
            conv_row = (
                (
                    await sess.execute(
                        text(
                            "SELECT id, customer_id, ctwa_clid, fep_expires_at, csw_expires_at "
                            "FROM conversations WHERE ctwa_clid = 'CTWA_CLICK_ABC_123'"
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert conv_row["fep_expires_at"] is not None
            assert (conv_row["fep_expires_at"] - conv_row["csw_expires_at"]).days >= 5

            ident_row = (
                (
                    await sess.execute(
                        text(
                            "SELECT identity_type, identity_value FROM customer_identities "
                            "WHERE customer_id = :cid"
                        ),
                        {"cid": conv_row["customer_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert ident_row["identity_type"] == "whatsapp_bsuid"
            assert ident_row["identity_value"] == bsuid_val

            # Outbound during 7-day FEP is allowed with free_entry_point pricing
            fep_decision = await adapter.evaluate_send_policy(
                session=sess,
                tenant_id=tenant_id,
                conversation_id=conv_row["id"],
                customer_id=conv_row["customer_id"],
                recipient_address=bsuid_val,
                content_type=MessageContentType.TEXT,
            )
            assert fep_decision.allowed is True
            assert fep_decision.pricing_category == "free_entry_point"

        # 2. Test Coexistence smb_message_echoes does NOT extend csw_expires_at
        echo_wamid = f"wamid.echo.{uuid.uuid4().hex[:8]}"
        echo_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "smb_message_echoes",
                            "value": {
                                "metadata": {"phone_number_id": phone_number_id},
                                "message_echoes": [
                                    {
                                        "id": echo_wamid,
                                        "from": "8613800138000",
                                        "to_user_id": bsuid_val,
                                        "timestamp": str(
                                            int((datetime.now(UTC) + timedelta(days=2)).timestamp())
                                        ),
                                        "type": "text",
                                        "text": {"body": "Sent from phone app"},
                                    }
                                ],
                            },
                        }
                    ]
                }
            ]
        }
        raw_echo_id = uuid.uuid4()
        async with migrator_session_factory() as conn, conn.begin():
            await accept_raw_webhook_event(
                conn,
                event_id=raw_echo_id,
                tenant_id=tenant_id,
                channel_type="whatsapp_coexistence",
                external_event_id=echo_wamid,
                external_account_id=phone_number_id,
                headers_json="{}",
                payload_json=json.dumps(echo_payload),
            )
        assert (
            await process_raw_webhook_event(
                session_factory,
                redis,
                raw_echo_id,
                registry,
                broker_session_factory=migrator_session_factory,
            )
            == "processed"
        )

        async with tenant_session(session_factory, tenant_id) as sess:
            echo_msg = (
                (
                    await sess.execute(
                        text(
                            "SELECT direction, sender_type, channel_metadata "
                            "FROM messages WHERE external_message_id = :eid"
                        ),
                        {"eid": echo_wamid},
                    )
                )
                .mappings()
                .one()
            )
            assert echo_msg["direction"] == "outbound"
            assert echo_msg["sender_type"] == "human_agent"
            assert echo_msg["channel_metadata"]["coexistence_echo"] is True

        # 3. Test 24h expired non-template block & Oct 2026 utility template pricing
        async with tenant_session(session_factory, tenant_id) as sess:
            past = datetime.now(UTC) - timedelta(hours=48)
            await sess.execute(
                text(
                    "UPDATE conversations SET csw_expires_at = :past, fep_expires_at = NULL "
                    "WHERE id = :cid"
                ),
                {"past": past, "cid": conv_row["id"]},
            )
            expired_decision = await adapter.evaluate_send_policy(
                session=sess,
                tenant_id=tenant_id,
                conversation_id=conv_row["id"],
                customer_id=conv_row["customer_id"],
                recipient_address=bsuid_val,
                content_type=MessageContentType.TEXT,
            )
            assert expired_decision.allowed is False
            assert expired_decision.block_reason == "ERR_WHATSAPP_CSW_EXPIRED"

            # Inside CSW (no FEP), Oct 2026 rules charge utility templates as billable_regular
            future_csw = datetime.now(UTC) + timedelta(hours=12)
            await sess.execute(
                text("UPDATE conversations SET csw_expires_at = :fut WHERE id = :cid"),
                {"fut": future_csw, "cid": conv_row["id"]},
            )
            util_decision = await adapter.evaluate_send_policy(
                session=sess,
                tenant_id=tenant_id,
                conversation_id=conv_row["id"],
                customer_id=conv_row["customer_id"],
                recipient_address=bsuid_val,
                content_type=MessageContentType.TEMPLATE,
                template_category="utility",
            )
            assert util_decision.allowed is True
            assert util_decision.pricing_category == "billable_regular_2026_10"

        # 4. Test STOP auto-suppression blocking subsequent outbound sends
        stop_wamid = f"wamid.stop.{uuid.uuid4().hex[:8]}"
        stop_phone = "491701234567"
        stop_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": phone_number_id},
                                "contacts": [
                                    {"profile": {"name": "OptOut Buyer"}, "wa_id": stop_phone}
                                ],
                                "messages": [
                                    {
                                        "id": stop_wamid,
                                        "from": stop_phone,
                                        "timestamp": str(int(datetime.now(UTC).timestamp())),
                                        "type": "text",
                                        "text": {"body": "STOP"},
                                    }
                                ],
                            },
                        }
                    ]
                }
            ]
        }
        raw_stop_id = uuid.uuid4()
        async with migrator_session_factory() as conn, conn.begin():
            await accept_raw_webhook_event(
                conn,
                event_id=raw_stop_id,
                tenant_id=tenant_id,
                channel_type="whatsapp_coexistence",
                external_event_id=stop_wamid,
                external_account_id=phone_number_id,
                headers_json="{}",
                payload_json=json.dumps(stop_payload),
            )
        assert (
            await process_raw_webhook_event(
                session_factory,
                redis,
                raw_stop_id,
                registry,
                broker_session_factory=migrator_session_factory,
            )
            == "processed"
        )

        async with tenant_session(session_factory, tenant_id) as sess:
            supp_svc = ConsentAndSuppressionService()
            assert (
                await supp_svc.is_suppressed(sess, tenant_id, "whatsapp_cloud", "+491701234567")
                is True
            )

            stop_conv = (
                (
                    await sess.execute(
                        text(
                            "SELECT conv.id, conv.customer_id FROM conversations conv "
                            "JOIN customer_identities ci ON ci.customer_id = conv.customer_id "
                            "WHERE ci.identity_value = '+491701234567' LIMIT 1"
                        )
                    )
                )
                .mappings()
                .one()
            )
            send_res = await adapter.send_message(
                session=sess,
                tenant_id=tenant_id,
                credentials={"access_token": "EAAG_SYSTEM_USER_TOK"},
                request=OutboundMessageRequest(
                    tenant_id=tenant_id,
                    channel_account_id=channel_account_id,
                    channel_type=ChannelType.WHATSAPP_COEXISTENCE,
                    external_account_id=phone_number_id,
                    recipient_id="+491701234567",
                    conversation_id=stop_conv["id"],
                    message_id=uuid.uuid4(),
                    content_type=MessageContentType.TEXT,
                    body_text="Follow up promo",
                ),
            )
            assert send_res.success is False
            assert "ERR_RECIPIENT_SUPPRESSED" in (send_res.error_message or "")
    finally:
        await redis.aclose()
        await runtime_engine.dispose()
