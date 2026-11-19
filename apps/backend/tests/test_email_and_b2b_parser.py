from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_core.channels.adapters.base import AdapterRegistry, OutboundMessageRequest
from erp_core.channels.adapters.email import EmailAdapter, S3AttachmentStore
from erp_core.channels.ingress_worker import process_raw_webhook_event
from erp_core.channels.parsers.b2b_inquiry_email import B2BInquiryEmailParser
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, MessageContentType


class _FakeS3AttachmentStore(S3AttachmentStore):
    def __init__(self) -> None:
        self.uploaded: dict[str, tuple[bytes, str]] = {}

    async def upload_bytes(self, *, key: str, data: bytes, content_type: str) -> str:
        self.uploaded[key] = (data, content_type)
        return key


def test_b2b_inquiry_parser_made_in_china_and_alibaba() -> None:
    parser = B2BInquiryEmailParser()

    mic_body = """
    Dear Supplier,
    You have received a new business inquiry from Made-in-China.com!

    Inquiry ID: MIC-20261119-88421
    Product Name: 24V COB LED Strip Light Waterproof IP67 (SKU: LED-COB24V-IP67)
    Purchase Quantity: 5,000 meters
    Buyer Name: Marcus Weber
    Email: marcus@lichttechnik-gmbh.de
    Company Name: Lichttechnik Weber GmbH
    Country/Region: Germany
    Message:
    Hi team, we need 5,000 meters of 24V COB LED Strip 4000K with CE and RoHS
    certificates. Please quote FOB Xiamen.
    """
    parsed = parser.parse(
        sender_email="en_notification@made-in-china.com",
        subject="Inquiry about 24V COB LED Strip Light Waterproof IP67 from Germany",
        body_text=mic_body,
        reply_to="marcus@lichttechnik-gmbh.de",
    )
    assert parsed is not None
    assert parsed.platform == "made_in_china"
    assert parsed.channel_type == ChannelType.MADE_IN_CHINA_EMAIL
    assert parsed.buyer_name == "Marcus Weber"
    assert parsed.buyer_email == "marcus@lichttechnik-gmbh.de"
    assert parsed.company_name == "Lichttechnik Weber GmbH"
    assert parsed.country == "Germany"
    assert "LED-COB24V-IP67" in (parsed.product_sku_or_title or "")
    assert parsed.target_quantity == "5,000 meters"
    assert parsed.target_quantity_numeric == 5000

    ali_body = """
    Alibaba.com Trade Inquiry
    Buyer Name: Elena Petrova
    Buyer Email: elena@nordiclight.se
    Company: NordicLight AB
    Country: Sweden
    Product: High Bay LED 150W MeanWell Driver (SKU: HB-150W-MW)
    Target Quantity: 300 pcs
    Inquiry Details: Can you do OEM laser logo and DDP Stockholm?
    """
    ali_parsed = parser.parse(
        sender_email="notice@service.alibaba.com",
        subject="You have a new inquiry on Alibaba.com",
        body_text=ali_body,
    )
    assert ali_parsed is not None
    assert ali_parsed.platform == "alibaba"
    assert ali_parsed.channel_type == ChannelType.ALIBABA_EMAIL
    assert ali_parsed.buyer_email == "elena@nordiclight.se"
    assert ali_parsed.target_quantity_numeric == 300


def test_email_nh3_xss_and_tracking_pixel_stripping() -> None:
    adapter = EmailAdapter()
    dirty_html = (
        "<div><p>Hello, quote attached.</p>"
        '<script>alert("xss")</script>'
        '<iframe src="https://evil.example/payload"></iframe>'
        '<img src="https://track.example/open.gif" width="1" height="1" />'
        '<a href="javascript:alert(1)" onclick="steal()">Click</a>'
        "<p><strong>MOQ: 500 pcs</strong></p></div>"
    )
    clean = adapter.sanitize_email_html(dirty_html)
    assert "<script" not in clean.lower()
    assert "alert(" not in clean
    assert "<iframe" not in clean.lower()
    assert "track.example/open.gif" not in clean
    assert "onclick" not in clean.lower()
    assert "<strong>MOQ: 500 pcs</strong>" in clean


@pytest.mark.asyncio
async def test_email_svix_verify_rfc5233_threading_attachments_and_mic_ingest() -> None:
    runtime_engine = create_async_engine(os.environ["DATABASE_RUNTIME_URL"], future=True)
    session_factory = async_sessionmaker(
        runtime_engine, class_=AsyncSession, expire_on_commit=False
    )
    redis = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

    fake_s3 = _FakeS3AttachmentStore()
    outbound_calls: list[dict] = []

    def _mock_resend(request: httpx.Request) -> httpx.Response:
        outbound_calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"id": f"re_{uuid.uuid4().hex[:12]}"})

    adapter = EmailAdapter(
        storage_store=fake_s3,
        http_transport=httpx.MockTransport(_mock_resend),
    )
    registry = AdapterRegistry()
    registry.register("email", adapter)
    registry.register(ChannelType.EMAIL.value, adapter)

    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    inbound_addr = f"factory-{tenant_id.hex[:6]}@inbound.mail.example.com"

    try:
        async with tenant_session(session_factory, tenant_id) as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'LED Export Co')"),
                {"id": tenant_id, "slug": f"email-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status, "
                    "rate_limit_profile) "
                    "VALUES (:id, :tid, 'email', :ext, 'Sales Inbox', 'active', "
                    "'{\"mps\": 50}'::jsonb)"
                ),
                {"id": account_id, "tid": tenant_id, "ext": inbound_addr},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_routing_directory "
                    "(channel_type, external_account_id, tenant_id, channel_account_id) "
                    "VALUES ('email', :ext, :tid, :aid)"
                ),
                {"ext": inbound_addr, "tid": tenant_id, "aid": account_id},
            )

        # Verify Svix HMAC-SHA256 webhook signature
        raw_secret = b"whsec_test_email_secret_32bytes!!"
        svix_secret = "whsec_" + base64.b64encode(raw_secret).decode()
        svix_id = f"msg_{uuid.uuid4().hex[:8]}"
        svix_ts = str(int(time.time()))

        mic_payload = {
            "type": "email.received",
            "data": {
                "email_id": f"re_in_{uuid.uuid4().hex[:10]}",
                "from": "en_notification@made-in-china.com",
                "to": [inbound_addr],
                "reply_to": ["marcus@lichttechnik-gmbh.de"],
                "subject": "Inquiry about 24V COB LED Strip Light",
                "message_id": "<mic-lead-001@made-in-china.com>",
                "text": (
                    "Product Name: 24V COB LED Strip (SKU: LED-COB24V-IP67)\n"
                    "Purchase Quantity: 5,000 meters\n"
                    "Buyer Name: Marcus Weber\n"
                    "Email: marcus@lichttechnik-gmbh.de\n"
                    "Company Name: Lichttechnik Weber GmbH\n"
                    "Country/Region: Germany\n"
                    "Message: Need FOB Xiamen quote with LM-80 report."
                ),
                "html": "<p>Need FOB Xiamen quote</p><script>evil()</script>",
                "attachments": [
                    {
                        "filename": "led_drawing.pdf",
                        "content_type": "application/pdf",
                        "content_base64": base64.b64encode(b"%PDF-1.4 test").decode(),
                    }
                ],
            },
        }
        raw_bytes = json.dumps(mic_payload).encode()
        to_sign = f"{svix_id}.{svix_ts}.".encode() + raw_bytes
        sig_b64 = base64.b64encode(hmac.new(raw_secret, to_sign, hashlib.sha256).digest()).decode()
        headers = {
            "svix-id": svix_id,
            "svix-timestamp": svix_ts,
            "svix-signature": f"v1,{sig_b64}",
        }
        assert adapter.verify_webhook_signature(raw_bytes, headers, svix_secret) is True

        raw_id_1 = uuid.uuid4()
        async with tenant_session(session_factory, tenant_id) as conn:
            await conn.execute(
                text(
                    "INSERT INTO raw_webhook_events "
                    "(id, tenant_id, channel_type, external_event_id, external_account_id, "
                    "headers, raw_payload) "
                    "VALUES (:id, :tid, 'email', :eid, :eaid, CAST(:hdr AS jsonb), CAST(:payload "
                    "AS jsonb))"
                ),
                {
                    "id": raw_id_1,
                    "tid": tenant_id,
                    "eid": svix_id,
                    "eaid": inbound_addr,
                    "hdr": json.dumps(headers),
                    "payload": json.dumps(mic_payload),
                },
            )
        assert (
            await process_raw_webhook_event(session_factory, redis, raw_id_1, registry)
            == "processed"
        )

        async with tenant_session(session_factory, tenant_id) as sess:
            conv = (
                (
                    await sess.execute(
                        text(
                            "SELECT conv.id, conv.conversation_mode, conv.channel_type, "
                            "cust.primary_email, comp.legal_name "
                            "FROM conversations conv "
                            "JOIN customers cust ON cust.id = conv.customer_id "
                            "LEFT JOIN b2b_companies comp ON comp.id = cust.company_id "
                            "LIMIT 1"
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert conv["conversation_mode"] == "b2b_inquiry"
            assert conv["channel_type"] == ChannelType.MADE_IN_CHINA_EMAIL.value
            assert conv["primary_email"] == "marcus@lichttechnik-gmbh.de"
            assert conv["legal_name"] == "Lichttechnik Weber GmbH"
            assert len(fake_s3.uploaded) == 1

            # Outbound send sets RFC 5322 Message-ID + RFC 5233 Reply-To +th_<conv_id>
            send_res = await adapter.send_message(
                session=sess,
                tenant_id=tenant_id,
                credentials={"api_key": "re_test_key"},
                request=OutboundMessageRequest(
                    tenant_id=tenant_id,
                    channel_account_id=account_id,
                    channel_type=ChannelType.EMAIL,
                    external_account_id=inbound_addr,
                    recipient_id="marcus@lichttechnik-gmbh.de",
                    conversation_id=conv["id"],
                    message_id=uuid.uuid4(),
                    content_type=MessageContentType.TEXT,
                    subject="Re: 24V COB LED Strip Quote",
                    body_text="Attached PI for 5,000m.",
                    from_address="sales@ledfactory.example.com",
                ),
            )
            assert send_res.success is True
            assert len(outbound_calls) == 1
            assert f"+th_{conv['id']}@" in outbound_calls[0]["reply_to"]

        # Reply WITHOUT In-Reply-To / References and WITH changed subject MUST still
        # match via RFC 5233 Reply-To
        reply_payload = {
            "type": "email.received",
            "data": {
                "email_id": f"re_in_{uuid.uuid4().hex[:10]}",
                "from": "marcus@lichttechnik-gmbh.de",
                "to": [f"support+th_{conv['id']}@inbound.mail.example.com"],
                "subject": "Completely different subject line",
                "message_id": "<reply-no-headers-002@lichttechnik-gmbh.de>",
                "text": "Confirmed, please send PI PDF.",
            },
        }
        raw_id_2 = uuid.uuid4()
        async with tenant_session(session_factory, tenant_id) as conn:
            await conn.execute(
                text(
                    "INSERT INTO raw_webhook_events "
                    "(id, tenant_id, channel_type, external_event_id, external_account_id, "
                    "raw_payload) "
                    "VALUES (:id, :tid, 'email', :eid, :eaid, CAST(:payload AS jsonb))"
                ),
                {
                    "id": raw_id_2,
                    "tid": tenant_id,
                    "eid": f"svix_{uuid.uuid4().hex[:8]}",
                    "eaid": inbound_addr,
                    "payload": json.dumps(reply_payload),
                },
            )
        assert (
            await process_raw_webhook_event(session_factory, redis, raw_id_2, registry)
            == "processed"
        )

        async with tenant_session(session_factory, tenant_id) as sess:
            msg_count = (
                await sess.execute(
                    text("SELECT count(*) FROM messages WHERE conversation_id = :cid"),
                    {"cid": conv["id"]},
                )
            ).scalar_one()
            assert msg_count == 2
    finally:
        async with tenant_session(session_factory, tenant_id) as conn:
            for tbl in (
                "messages",
                "conversations",
                "customer_identities",
                "customers",
                "b2b_companies",
                "channel_routing_directory",
                "channel_accounts",
                "raw_webhook_events",
            ):
                await conn.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id = :tid"), {"tid": tenant_id}
                )
            await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
        await redis.aclose()
        await runtime_engine.dispose()
