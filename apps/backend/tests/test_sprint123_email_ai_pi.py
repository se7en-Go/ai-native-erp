"""Sprint 1-3 backend: dual-engine email, grounded AI proposals, and export PI templates."""

from __future__ import annotations

import base64
import io
import json
import os
import smtplib
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.message import Message
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_auth_rbac import _register_owner

import erp_core.ai_engine.router as ai_router_module
import erp_core.channels.ingress_worker as ingress_worker_module
import erp_core.crm.router as crm_router_module
from erp_core.ai_engine.proposal_executor import ProposalExecutorService
from erp_core.ai_engine.proposal_generator import AiProposalGeneratorService
from erp_core.ai_engine.router_llm import LlmProviderResponse
from erp_core.channels.adapters.base import AdapterRegistry
from erp_core.channels.adapters.email import EmailAdapter, EmailAttachment
from erp_core.channels.ecommerce.base import CredentialVaultHelper
from erp_core.channels.ingress_worker import process_raw_webhook_event
from erp_core.channels.webhook_store import accept_raw_webhook_event
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal

RuntimeFactory = async_sessionmaker[AsyncSession]
PDF_MAGIC = b"%PDF-"


# --------------------------------------------------------------------------------------
# Harness helpers
# --------------------------------------------------------------------------------------


class OfflineLlmClient:
    """Deterministic LLM stub: `has_key` False forces the grounded local draft path."""

    def has_key(self, provider: str) -> bool:
        return False

    async def complete(
        self,
        *,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        raise AssertionError("offline client must not be called")


async def _register_member(
    async_client: Any, owner_token: str, role: str
) -> tuple[str, str]:
    suffix = uuid4().hex[:8]
    email = f"{role}-{suffix}@factory.example.com"
    created = await async_client.post(
        "/api/v1/tenants/memberships",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "email": email,
            "full_name": f"{role.title()} Person",
            "password": "StrongPassword2026!",
            "role": role,
        },
    )
    assert created.status_code == 201, created.text
    login = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "StrongPassword2026!"},
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"], login.json()["principal"]["user_id"]


async def _seed_quote(
    conn: AsyncSession, tenant_id: UUID, owner_id: UUID, *, suffix: str = ""
) -> dict[str, UUID]:
    customer_id, opportunity_id, product_id, variant_id, quote_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    params = {
        "tid": tenant_id,
        "cid": customer_id,
        "oid": opportunity_id,
        "pid": product_id,
        "vid": variant_id,
        "qid": quote_id,
        "owner": owner_id,
        "spu": f"SPU-COB-STRIP{suffix.upper()}",
        "quote_no": f"PI-202611-ABC123{suffix.upper()}",
        "sku": f"LED-COB24V-IP67{suffix.upper()}",
    }
    await conn.execute(
        text(
            "INSERT INTO customers (id, tenant_id, customer_type, display_name, primary_email) "
            "VALUES (:cid, :tid, 'b2b_contact', 'Marcus Weber', 'marcus@lichttechnik.test')"
        ),
        params,
    )
    await conn.execute(
        text(
            "INSERT INTO opportunities (id, tenant_id, customer_id, title, stage, "
            "expected_amount, currency, owner_user_id) "
            "VALUES (:oid, :tid, :cid, 'COB strip RFQ', 'quoting', 42180.00, 'USD', :owner)"
        ),
        params,
    )
    await conn.execute(
        text(
            "INSERT INTO products (id, tenant_id, spu_code, category) "
            "VALUES (:pid, :tid, :spu, 'LED Strip')"
        ),
        params,
    )
    await conn.execute(
        text(
            "INSERT INTO product_variants (id, tenant_id, product_id, sku, base_currency, "
            "cost_price_base, retail_price_usd, weight_grams, lead_time_days) "
            "VALUES (:vid, :tid, :pid, :sku, 'USD', 4.2000, 8.40, 120, 12)"
        ),
        params,
    )
    await conn.execute(
        text(
            "INSERT INTO quotes (id, tenant_id, opportunity_id, quote_no, version, "
            "is_proforma_invoice, incoterm, port_of_loading, port_of_destination, "
            "payment_terms, base_currency, quote_currency, fx_rate_snapshot, fx_locked_until, "
            "freight_fee, insurance_fee, customs_duty_estimate, total_amount, status) "
            "VALUES (:qid, :tid, :oid, :quote_no, 2, true, 'FOB', 'Xiamen, China', "
            "'Hamburg, Germany', '30% T/T deposit, 70% before shipment', 'CNY', 'USD', "
            "7.123456, now() + interval '7 days', 180.00, 0.00, 0.00, 42180.00, 'draft')"
        ),
        params,
    )
    await conn.execute(
        text(
            "INSERT INTO quote_items (tenant_id, quote_id, variant_id, sku_snapshot, "
            "description_i18n, quantity, unit_price_quote_ccy, unit_cost_base_snapshot, "
            "line_total_quote_ccy) VALUES (:tid, :qid, :vid, :sku, "
            "'24V COB LED strip 4000K IP67', 5000, 8.4000, 4.2000, 42000.00)"
        ),
        params,
    )
    return {
        "customer_id": customer_id,
        "opportunity_id": opportunity_id,
        "product_id": product_id,
        "variant_id": variant_id,
        "quote_id": quote_id,
    }


async def _seed_email_account(
    conn: AsyncSession,
    tenant_id: UUID,
    *,
    external: str,
    display_name: str,
    owner_user_id: UUID | None = None,
    visibility_scope: str = "shared",
    credentials: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> UUID:
    account_id = uuid4()
    await conn.execute(
        text(
            "INSERT INTO channel_accounts (id, tenant_id, channel_type, external_account_id, "
            "display_name, status, owner_user_id, visibility_scope, config) "
            "VALUES (:id, :tid, 'email', :ext, :name, 'active', :owner, :scope, "
            "CAST(:config AS jsonb))"
        ),
        {
            "id": account_id,
            "tid": tenant_id,
            "ext": external,
            "name": display_name,
            "owner": owner_user_id,
            "scope": visibility_scope,
            "config": json.dumps(config or {}),
        },
    )
    if credentials is not None:
        await CredentialVaultHelper().save_secret_json(
            conn, tenant_id, account_id, "messaging_provider", credentials
        )
    return account_id


async def _seed_conversation(
    conn: AsyncSession,
    tenant_id: UUID,
    customer_id: UUID,
    account_id: UUID,
    *,
    body_text: str,
    assigned_user_id: UUID | None = None,
) -> UUID:
    conversation_id = uuid4()
    await conn.execute(
        text(
            "INSERT INTO conversations (id, tenant_id, customer_id, channel_account_id, "
            "channel_type, external_thread_id, conversation_mode, status, assigned_user_id, "
            "unread_count) VALUES (:id, :tid, :cid, :aid, 'email', :thread, 'b2b_inquiry', "
            "'open', :assignee, 1)"
        ),
        {
            "id": conversation_id,
            "tid": tenant_id,
            "cid": customer_id,
            "aid": account_id,
            "thread": f"buyer-{conversation_id.hex[:8]}@import.example.com",
            "assignee": assigned_user_id,
        },
    )
    await conn.execute(
        text(
            "INSERT INTO messages (tenant_id, conversation_id, customer_id, channel_type, "
            "direction, sender_type, content_type, body_text, source_timestamp) "
            "VALUES (:tid, :cid_conv, :cid, 'email', 'inbound', 'customer', 'text', :body, now())"
        ),
        {
            "tid": tenant_id,
            "cid_conv": conversation_id,
            "cid": customer_id,
            "body": body_text,
        },
    )
    return conversation_id


async def _set_quote_pricing(conn: AsyncSession, tenant_id: UUID) -> None:
    await conn.execute(
        text(
            "UPDATE tenants SET ai_policy_settings = "
            "'{\"quote_pricing\":{\"incoterm_surcharge_pct\":{\"FOB\":\"0\"}}}'::jsonb "
            "WHERE id = :tid"
        ),
        {"tid": tenant_id},
    )


# --------------------------------------------------------------------------------------
# Track 1.1 - Resend engine with a real PDF attachment
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resend_send_direct_email_attaches_pdf_and_from_address() -> None:
    calls: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer re_test_sprint_key"
        calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"id": "re_sprint123_msg"})

    adapter = EmailAdapter(http_transport=httpx.MockTransport(_handler))
    pdf_bytes = b"%PDF-1.7 sprint PI body"
    result = await adapter.send_direct_email(
        provider="resend",
        credentials={"provider": "resend", "api_key": "re_test_sprint_key"},
        from_address="Export Sales <sales@ledfactory.example.com>",
        to_address="marcus@lichttechnik.example.com",
        subject="Proforma Invoice PI-202611-ABC123 v2",
        body_text="Please find the PI attached.",
        body_html="<p>Please find the PI attached.</p>",
        headers={"Message-ID": "<sprint-123@ledfactory.example.com>"},
        attachments=[EmailAttachment(filename="PI-202611-ABC123-v2.pdf", content_bytes=pdf_bytes)],
    )

    assert result.success is True
    assert result.external_message_id == "re_sprint123_msg"
    payload = calls[0]
    assert payload["from"] == "Export Sales <sales@ledfactory.example.com>"
    assert payload["to"] == ["marcus@lichttechnik.example.com"]
    assert payload["html"] == "<p>Please find the PI attached.</p>"
    assert payload["headers"]["Message-ID"] == "<sprint-123@ledfactory.example.com>"
    assert payload["attachments"][0]["filename"] == "PI-202611-ABC123-v2.pdf"
    assert base64.b64decode(payload["attachments"][0]["content"]) == pdf_bytes


@pytest.mark.asyncio
async def test_resend_missing_api_key_returns_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    adapter = EmailAdapter(http_transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    result = await adapter.send_direct_email(
        provider="resend",
        credentials={},
        from_address="sales@ledfactory.example.com",
        to_address="marcus@lichttechnik.example.com",
        subject="PI",
        body_text="body",
    )
    assert result.success is False
    assert result.error_message == "ERR_RESEND_API_KEY_MISSING"


# --------------------------------------------------------------------------------------
# Track 1.1 - Generic SMTP engine with STARTTLS + PDF attachment
# --------------------------------------------------------------------------------------


class _FakeSmtp:
    instances: list[_FakeSmtp] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ehlo_calls = 0
        self.starttls_calls = 0
        self.login_args: tuple[str, str] | None = None
        self.sent: list[Message] = []
        self.quit_called = False
        _FakeSmtp.instances.append(self)

    def ehlo(self) -> None:
        self.ehlo_calls += 1

    def starttls(self) -> None:
        self.starttls_calls += 1

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message: Message) -> None:
        self.sent.append(message)

    def quit(self) -> None:
        self.quit_called = True

    def close(self) -> None:  # pragma: no cover - only on failure paths
        self.quit_called = True


class _FakeSmtpSsl(_FakeSmtp):
    pass


@pytest.mark.asyncio
async def test_smtp_send_direct_email_starttls_login_and_pdf_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSmtp.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSmtpSsl)

    adapter = EmailAdapter(http_transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    pdf_bytes = b"%PDF-1.7 smtp PI body"
    result = await adapter.send_direct_email(
        provider="smtp_imap",
        credentials={
            "provider": "smtp_imap",
            "smtp_host": "smtp.mailprovider.example.com",
            "smtp_port": 587,
            "smtp_username": "sales@ledfactory.example.com",
            "smtp_password": "smtp-app-password",
        },
        from_address="sales@ledfactory.example.com",
        to_address="marcus@lichttechnik.example.com",
        subject="Proforma Invoice PI-202611-ABC123 v2",
        body_text="Please find the PI attached.",
        body_html="<p>Please find the PI attached.</p>",
        reply_to="sales+th_1234@ledfactory.example.com",
        headers={
            "Message-ID": "<smtp-sprint-123@ledfactory.example.com>",
            "In-Reply-To": "<buyer-msg@import.example.com>",
            "References": "<buyer-msg@import.example.com>",
        },
        attachments=[EmailAttachment(filename="PI-202611-ABC123-v2.pdf", content_bytes=pdf_bytes)],
    )

    assert result.success is True, result.error_message
    client = _FakeSmtp.instances[0]
    assert client.host == "smtp.mailprovider.example.com"
    assert client.port == 587
    assert client.starttls_calls == 1
    assert client.ehlo_calls >= 2
    assert client.login_args == ("sales@ledfactory.example.com", "smtp-app-password")
    assert client.quit_called is True
    assert len(client.sent) == 1

    raw = client.sent[0].as_bytes()
    assert b"application/pdf" in raw
    parsed = message_from_bytes(raw)
    assert parsed["From"] == "sales@ledfactory.example.com"
    assert parsed["To"] == "marcus@lichttechnik.example.com"
    assert parsed["Reply-To"] == "sales+th_1234@ledfactory.example.com"
    assert parsed["In-Reply-To"] == "<buyer-msg@import.example.com>"
    assert parsed["Message-ID"] == "<smtp-sprint-123@ledfactory.example.com>"
    attachments = [part for part in parsed.walk() if part.get_filename()]
    assert [part.get_filename() for part in attachments] == ["PI-202611-ABC123-v2.pdf"]
    assert attachments[0].get_payload(decode=True) == pdf_bytes


@pytest.mark.asyncio
async def test_smtp_port_465_uses_implicit_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSmtp.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSmtpSsl)

    adapter = EmailAdapter(http_transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    result = await adapter.send_direct_email(
        provider="smtp_imap",
        credentials={"smtp_host": "smtp.secure.example.com", "smtp_password": "app-pass"},
        from_address="sales@ledfactory.example.com",
        to_address="marcus@lichttechnik.example.com",
        subject="PI",
        body_text="body",
        smtp_config={"smtp_port": 465},
    )
    assert result.success is True
    assert isinstance(_FakeSmtp.instances[0], _FakeSmtpSsl)
    assert _FakeSmtp.instances[0].starttls_calls == 0


@pytest.mark.asyncio
async def test_smtp_missing_credentials_is_typed_and_leaks_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSmtp.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    adapter = EmailAdapter(http_transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    missing = await adapter.send_direct_email(
        provider="smtp_imap",
        credentials={"smtp_host": "smtp.mailprovider.example.com"},
        from_address="sales@ledfactory.example.com",
        to_address="marcus@lichttechnik.example.com",
        subject="PI",
        body_text="body",
    )
    assert missing.success is False
    assert missing.error_message == "ERR_SMTP_CREDENTIALS_MISSING"

    class _BoomSmtp(_FakeSmtp):
        def send_message(self, message: Message) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"bad password super-secret-app-pass")

    monkeypatch.setattr(smtplib, "SMTP", _BoomSmtp)
    failed = await adapter.send_direct_email(
        provider="smtp_imap",
        credentials={
            "smtp_host": "smtp.mailprovider.example.com",
            "smtp_username": "sales@ledfactory.example.com",
            "smtp_password": "super-secret-app-pass",
        },
        from_address="sales@ledfactory.example.com",
        to_address="marcus@lichttechnik.example.com",
        subject="PI",
        body_text="body",
    )
    assert failed.success is False
    assert failed.retryable is True
    assert failed.error_message is not None
    assert failed.error_message.startswith("ERR_SMTP_SEND_FAILED")
    assert "super-secret-app-pass" not in failed.error_message


# --------------------------------------------------------------------------------------
# Track 3.1 - Tenant PI template configuration
# --------------------------------------------------------------------------------------


async def test_pi_template_defaults_patch_persistence_and_rbac(
    async_client: Any, runtime_session_factory: RuntimeFactory
) -> None:
    token, _, tenant_raw = await _register_owner(async_client)
    tenant_id = UUID(tenant_raw)
    headers = {"Authorization": f"Bearer {token}"}
    agent_token, _ = await _register_member(async_client, token, "agent")

    defaults = await async_client.get("/api/v1/tenants/pi-template", headers=headers)
    assert defaults.status_code == 200, defaults.text
    body = defaults.json()
    assert body["bank_name"]
    assert body["footer_signature_title"] == "Authorized Signature & Company Stamp"
    assert "Validity" in body["default_terms_notes"]

    # Preserve unrelated ai_policy_settings keys when writing the template.
    capabilities = await async_client.patch(
        "/api/v1/ai/capabilities",
        headers=headers,
        json={"settings": {"cs_support": "L1_proposal"}},
    )
    assert capabilities.status_code == 200, capabilities.text

    payload = {
        **body,
        "company_legal_name": "Quanzhou Lumen Export Co., Ltd",
        "company_address": "No.1 Export Road, Quanzhou, Fujian, China",
        "contact_phone": "+86 595 2200 0000",
        "contact_email": "export@lumen-export.example.com",
        "bank_name": "Bank of China, Quanzhou Branch",
        "bank_swift_code": "BKCHCNBJ74A",
        "bank_account_no": "4123 4567 8901 2345",
        "bank_beneficiary_name": "Quanzhou Lumen Export Co., Ltd",
        "default_terms_notes": "1. Validity: 15 days.\n2. Payment: 30% T/T deposit.",
    }
    patched = await async_client.patch(
        "/api/v1/tenants/pi-template", headers=headers, json=payload
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["bank_swift_code"] == "BKCHCNBJ74A"

    reread = await async_client.get("/api/v1/tenants/pi-template", headers=headers)
    assert reread.json()["company_legal_name"] == "Quanzhou Lumen Export Co., Ltd"

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        settings_row = (
            await session.execute(
                text("SELECT ai_policy_settings FROM tenants WHERE id = :tid"),
                {"tid": tenant_id},
            )
        ).scalar_one()
        assert settings_row["pi_template"]["bank_swift_code"] == "BKCHCNBJ74A"
        assert settings_row["cs_support"] == "L1_proposal"
        audited = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE action = 'tenant.pi_template_updated'"
                )
            )
        ).scalar_one()
        assert audited == 1

    # RBAC: agents read the letterhead but cannot rewrite bank details.
    agent_headers = {"Authorization": f"Bearer {agent_token}"}
    assert (
        await async_client.get("/api/v1/tenants/pi-template", headers=agent_headers)
    ).status_code == 200
    denied = await async_client.patch(
        "/api/v1/tenants/pi-template", headers=agent_headers, json=payload
    )
    assert denied.status_code == 403


# --------------------------------------------------------------------------------------
# Track 1.3 + 3.2 - Direct PDF download renders the configured letterhead & bank wire
# --------------------------------------------------------------------------------------


async def test_quote_pdf_download_renders_configured_letterhead_and_bank_wire(
    async_client: Any, runtime_session_factory: RuntimeFactory
) -> None:
    from pypdf import PdfReader

    token, _, tenant_raw = await _register_owner(async_client)
    tenant_id = UUID(tenant_raw)
    headers = {"Authorization": f"Bearer {token}"}
    me = await async_client.get("/api/v1/auth/me", headers=headers)
    owner_id = UUID(me.json()["user_id"])

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        seeded = await _seed_quote(session, tenant_id, owner_id)
        await session.execute(
            text(
                "UPDATE tenants SET ai_policy_settings = "
                "'{\"pi_template\":{\"company_legal_name\":\"Lumen Export Co Ltd\","
                "\"company_address\":\"No.1 Export Road, Quanzhou\","
                "\"bank_name\":\"Bank of China, Quanzhou Branch\","
                "\"bank_swift_code\":\"BKCHCNBJ74A\","
                "\"bank_account_no\":\"4123 4567 8901 2345\","
                "\"bank_beneficiary_name\":\"Lumen Export Co Ltd\","
                "\"default_terms_notes\":\"1. Validity: 15 days.\"}}'::jsonb "
                "WHERE id = :tid"
            ),
            {"tid": tenant_id},
        )

    response = await async_client.get(
        f"/api/v1/b2b/quotes/{seeded['quote_id']}/pdf/download", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert (
        response.headers["content-disposition"]
        == 'inline; filename="PI-202611-ABC123-v2.pdf"'
    )
    assert response.content.startswith(PDF_MAGIC)

    reader = PdfReader(io.BytesIO(response.content))
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "BKCHCNBJ74A" in extracted
    assert "Lumen Export Co Ltd" in extracted
    assert "LED-COB24V-IP67" in extracted
    assert "PROFORMA INVOICE" in extracted.upper()

    zh = await async_client.get(
        f"/api/v1/b2b/quotes/{seeded['quote_id']}/pdf/download?language=zh", headers=headers
    )
    assert zh.status_code == 200
    assert zh.content.startswith(PDF_MAGIC)
    assert zh.content != response.content


# --------------------------------------------------------------------------------------
# Track 1.3 - One-click send PI PDF email (Resend), status transition and audit
# --------------------------------------------------------------------------------------


async def test_send_quote_email_resend_transitions_quote_and_audits(
    async_client: Any,
    runtime_session_factory: RuntimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _, tenant_raw = await _register_owner(async_client)
    tenant_id = UUID(tenant_raw)
    headers = {"Authorization": f"Bearer {token}"}
    me = await async_client.get("/api/v1/auth/me", headers=headers)
    owner_id = UUID(me.json()["user_id"])

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        seeded = await _seed_quote(session, tenant_id, owner_id)
        account_id = await _seed_email_account(
            session,
            tenant_id,
            external="export@ledfactory.example.com",
            display_name="Export Sales",
            credentials={"provider": "resend", "api_key": "re_test_sprint_key"},
            config={"provider": "resend"},
        )

    sent: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"id": "re_pi_delivery_1"})

    adapter = EmailAdapter(http_transport=httpx.MockTransport(_handler))
    monkeypatch.setattr(crm_router_module, "build_email_adapter", lambda request: adapter)

    response = await async_client.post(
        f"/api/v1/b2b/quotes/{seeded['quote_id']}/send-email",
        headers=headers,
        json={
            "recipient_email": "marcus@lichttechnik.example.com",
            "subject": "Proforma Invoice PI-202611-ABC123 v2",
            "message_body": "Dear Marcus, attached is the PI.",
            "language": "en",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quote_id"] == str(seeded["quote_id"])
    assert body["provider"] == "resend"
    assert body["quote_status"] == "sent"
    assert body["from_email"] == "Export Sales <export@ledfactory.example.com>"
    assert body["pdf_filename"] == "PI-202611-ABC123-v2.pdf"
    assert body["pdf_size_bytes"] > 0
    assert body["external_message_id"] == "re_pi_delivery_1"

    payload = sent[0]
    assert payload["to"] == ["marcus@lichttechnik.example.com"]
    assert payload["subject"] == "Proforma Invoice PI-202611-ABC123 v2"
    assert payload["attachments"][0]["filename"] == "PI-202611-ABC123-v2.pdf"
    attached = base64.b64decode(payload["attachments"][0]["content"])
    assert attached.startswith(PDF_MAGIC)
    assert len(attached) == body["pdf_size_bytes"]

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        row = (
            await session.execute(
                text("SELECT status, pdf_r2_key FROM quotes WHERE id = :qid"),
                {"qid": seeded["quote_id"]},
            )
        ).one()
        assert row.status == "sent"
        assert row.pdf_r2_key
        audit = (
            await session.execute(
                text(
                    "SELECT after_state FROM audit_events "
                    "WHERE action = 'quote.emailed' AND resource_id = :qid"
                ),
                {"qid": seeded["quote_id"]},
            )
        ).one()
        assert audit.after_state["provider"] == "resend"
        assert audit.after_state["recipient_email"] == "marcus@lichttechnik.example.com"
        assert str(account_id) == audit.after_state["channel_account_id"]


async def test_send_quote_email_smtp_account_and_failure_mapping(
    async_client: Any,
    runtime_session_factory: RuntimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _, tenant_raw = await _register_owner(async_client)
    tenant_id = UUID(tenant_raw)
    headers = {"Authorization": f"Bearer {token}"}
    me = await async_client.get("/api/v1/auth/me", headers=headers)
    owner_id = UUID(me.json()["user_id"])

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        seeded = await _seed_quote(session, tenant_id, owner_id)
        await _seed_email_account(
            session,
            tenant_id,
            external="export@ledfactory.example.com",
            display_name="Export Sales",
            credentials={
                "provider": "smtp_imap",
                "smtp_host": "smtp.mailprovider.example.com",
                "smtp_port": 587,
                "smtp_username": "export@ledfactory.example.com",
                "smtp_password": "smtp-app-password",
            },
            config={"provider": "smtp_imap"},
        )

    _FakeSmtp.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSmtpSsl)
    adapter = EmailAdapter(http_transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    monkeypatch.setattr(crm_router_module, "build_email_adapter", lambda request: adapter)

    response = await async_client.post(
        f"/api/v1/b2b/quotes/{seeded['quote_id']}/send-email",
        headers=headers,
        json={"recipient_email": "marcus@lichttechnik.example.com", "language": "zh"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "smtp_imap"
    assert response.json()["from_email"] == "Export Sales <export@ledfactory.example.com>"
    smtp_client = _FakeSmtp.instances[0]
    assert smtp_client.login_args == ("export@ledfactory.example.com", "smtp-app-password")
    assert smtp_client.starttls_calls == 1
    assert any(part.get_filename() for part in smtp_client.sent[0].walk())

    # A transport failure maps to a typed 502 instead of a silent success.
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        second = await _seed_quote(session, tenant_id, owner_id, suffix="b")

    class _FailingSmtp(_FakeSmtp):
        def login(self, username: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"denied")

    monkeypatch.setattr(smtplib, "SMTP", _FailingSmtp)
    failed = await async_client.post(
        f"/api/v1/b2b/quotes/{second['quote_id']}/send-email",
        headers=headers,
        json={"recipient_email": "marcus@lichttechnik.example.com"},
    )
    assert failed.status_code == 502
    assert failed.json()["code"] == "ERR_QUOTE_EMAIL_SEND_FAILED"
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        status_after = (
            await session.execute(
                text("SELECT status FROM quotes WHERE id = :qid"),
                {"qid": second["quote_id"]},
            )
        ).scalar_one()
        assert status_after == "draft"


async def test_agent_cannot_send_from_another_agents_personal_mailbox(
    async_client: Any, runtime_session_factory: RuntimeFactory
) -> None:
    token, _, tenant_raw = await _register_owner(async_client)
    tenant_id = UUID(tenant_raw)
    owner_headers = {"Authorization": f"Bearer {token}"}
    me = await async_client.get("/api/v1/auth/me", headers=owner_headers)
    owner_id = UUID(me.json()["user_id"])
    agent_token, agent_user_raw = await _register_member(async_client, token, "agent")
    other_agent_id = uuid4()

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        seeded = await _seed_quote(session, tenant_id, owner_id)
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:uid, :email, 'Other Agent', 'x')"
                ),
                {"uid": other_agent_id, "email": f"other-{other_agent_id.hex[:8]}@factory.test"},
            )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:mid, :tid, :uid, 'agent')"
            ),
            {"mid": uuid4(), "tid": tenant_id, "uid": other_agent_id},
        )
        foreign_account = await _seed_email_account(
            session,
            tenant_id,
            external="other.agent@ledfactory.example.com",
            display_name="Other Agent",
            owner_user_id=other_agent_id,
            visibility_scope="personal",
            credentials={"provider": "resend", "api_key": "re_test_sprint_key"},
        )
        await _seed_email_account(
            session,
            tenant_id,
            external="export@ledfactory.example.com",
            display_name="Export Sales",
            credentials={"provider": "resend", "api_key": "re_test_sprint_key"},
        )

    agent_headers = {"Authorization": f"Bearer {agent_token}"}
    denied = await async_client.post(
        f"/api/v1/b2b/quotes/{seeded['quote_id']}/send-email",
        headers=agent_headers,
        json={
            "recipient_email": "marcus@lichttechnik.example.com",
            "channel_account_id": str(foreign_account),
        },
    )
    assert denied.status_code == 404
    assert denied.json()["code"] == "ERR_EMAIL_ACCOUNT_UNAVAILABLE"
    assert agent_user_raw


# --------------------------------------------------------------------------------------
# Track 2 - Grounded AI proposal generation, approval dispatch and the stale gate
# --------------------------------------------------------------------------------------


async def test_grounded_propose_reply_persists_tool_provenance_and_approves(
    async_client: Any,
    runtime_session_factory: RuntimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _, tenant_raw = await _register_owner(async_client)
    tenant_id = UUID(tenant_raw)
    headers = {"Authorization": f"Bearer {token}"}

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await _set_quote_pricing(session, tenant_id)
        customer_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name, primary_email) "
                "VALUES (:cid, :tid, 'b2b_contact', 'Marcus Weber', 'marcus@lichttechnik.test')"
            ),
            {"cid": customer_id, "tid": tenant_id},
        )
        account_id = await _seed_email_account(
            session,
            tenant_id,
            external="support@ledfactory.example.com",
            display_name="Support",
        )
        product_id, variant_id = uuid4(), uuid4()
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:pid, :tid, 'SPU-COB', 'LED Strip')"
            ),
            {"pid": product_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO product_variants (id, tenant_id, product_id, sku, base_currency, "
                "cost_price_base, retail_price_usd, weight_grams, lead_time_days) "
                "VALUES (:vid, :tid, :pid, 'LED-COB24V-IP67', 'USD', 4.2000, 8.40, 120, 12)"
            ),
            {"vid": variant_id, "tid": tenant_id, "pid": product_id},
        )
        await session.execute(
            text(
                "INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, "
                "on_hand_qty, reserved_qty) VALUES (:tid, :wid, :vid, 5200, 200)"
            ),
            {"tid": tenant_id, "wid": uuid4(), "vid": variant_id},
        )
        conversation_id = await _seed_conversation(
            session,
            tenant_id,
            customer_id,
            account_id,
            body_text=(
                "Hi, please quote FOB Xiamen price and MOQ for SKU LED-COB24V-IP67, "
                "we need 5,000 meters."
            ),
        )

    monkeypatch.setattr(
        ai_router_module,
        "_proposal_generator",
        lambda request: AiProposalGeneratorService(llm_client=OfflineLlmClient()),
    )

    proposed = await async_client.post(
        f"/api/v1/ai/conversations/{conversation_id}/propose-reply", headers=headers
    )
    assert proposed.status_code == 200, proposed.text
    proposal = proposed.json()
    assert proposal["status"] == "pending"
    assert proposal["proposal_type"] == "reply_draft"
    assert proposal["capability"] == "cs_support"
    assert proposal["source_tool_call_id"] is not None
    payload = proposal["proposed_payload"]
    assert payload["verified_tool_call_ids"]
    assert payload["tool_evidence"][0]["tool_name"] == "get_live_sku_pricing_and_stock"
    assert payload["tool_evidence"][0]["data"]["sku"] == "LED-COB24V-IP67"
    assert payload["tool_evidence"][0]["data"]["available_qty"] == 5000
    assert "8.40" in payload["body_text"]
    assert Decimal_equals(proposal["confidence_score"], "0.920")

    approved = await async_client.post(
        f"/api/v1/ai/proposals/{proposal['id']}/approve", headers=headers
    )
    assert approved.status_code == 200, approved.text
    reviewed = approved.json()
    assert reviewed["status"] == "approved", reviewed.get("execution_result")
    result = reviewed["execution_result"]
    assert result["queued_for_delivery"] is True
    assert result["message_id"]

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        outbound = (
            await session.execute(
                text(
                    "SELECT direction, delivery_status, body_text FROM messages "
                    "WHERE id = :mid"
                ),
                {"mid": UUID(result["message_id"])},
            )
        ).one()
        assert outbound.direction == "outbound"
        assert outbound.delivery_status == "queued"
        assert "LED-COB24V-IP67" in outbound.body_text


def Decimal_equals(value: Any, expected: str) -> bool:
    from decimal import Decimal

    return Decimal(str(value)).quantize(Decimal("0.001")) == Decimal(expected)


async def test_agent_cannot_propose_on_another_agents_private_conversation(
    async_client: Any,
    runtime_session_factory: RuntimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _, tenant_raw = await _register_owner(async_client)
    tenant_id = UUID(tenant_raw)
    headers = {"Authorization": f"Bearer {token}"}
    me = await async_client.get("/api/v1/auth/me", headers=headers)
    owner_id = UUID(me.json()["user_id"])
    agent_token, agent_user_raw = await _register_member(async_client, token, "agent")
    agent_user_id = UUID(agent_user_raw)
    other_agent_id = uuid4()

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        customer_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:cid, :tid, 'b2b_contact', 'Private Buyer')"
            ),
            {"cid": customer_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, 'Other Agent', 'x')"
            ),
            {"uid": other_agent_id, "email": f"other2-{other_agent_id.hex[:8]}@factory.test"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:mid, :tid, :uid, 'agent')"
            ),
            {"mid": uuid4(), "tid": tenant_id, "uid": other_agent_id},
        )
        private_account = await _seed_email_account(
            session,
            tenant_id,
            external="private@ledfactory.example.com",
            display_name="Private",
            owner_user_id=other_agent_id,
            visibility_scope="personal",
        )
        conversation_id = await _seed_conversation(
            session,
            tenant_id,
            customer_id,
            private_account,
            body_text="What is the MOQ?",
            assigned_user_id=other_agent_id,
        )

    monkeypatch.setattr(
        ai_router_module,
        "_proposal_generator",
        lambda request: AiProposalGeneratorService(llm_client=OfflineLlmClient()),
    )
    denied = await async_client.post(
        f"/api/v1/ai/conversations/{conversation_id}/propose-reply",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "ERR_CONVERSATION_ACCESS_DENIED"
    assert owner_id and agent_user_id


async def test_ungrounded_numeric_reply_still_raises_409_proposal_stale(
    async_client: Any, runtime_session_factory: RuntimeFactory
) -> None:
    token, _, tenant_raw = await _register_owner(async_client)
    tenant_id = UUID(tenant_raw)
    headers = {"Authorization": f"Bearer {token}"}
    me = await async_client.get("/api/v1/auth/me", headers=headers)
    owner_id = UUID(me.json()["user_id"])

    async with tenant_session(runtime_session_factory, tenant_id) as session:
        customer_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name, primary_email) "
                "VALUES (:cid, :tid, 'b2b_contact', 'Unverified Buyer', 'buyer@import.test')"
            ),
            {"cid": customer_id, "tid": tenant_id},
        )
        account_id = await _seed_email_account(
            session,
            tenant_id,
            external="support@ledfactory.example.com",
            display_name="Support",
        )
        conversation_id = await _seed_conversation(
            session,
            tenant_id,
            customer_id,
            account_id,
            body_text="What is your best price?",
        )
        proposal_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO ai_proposals (id, tenant_id, capability, proposal_type, "
                "conversation_id, proposed_payload, confidence_score, source_tool_call_id) "
                "VALUES (:pid, :tid, 'cs_support', 'reply_draft', :conv, "
                "CAST(:payload AS jsonb), 0.72, NULL)"
            ),
            {
                "pid": proposal_id,
                "tid": tenant_id,
                "conv": conversation_id,
                "payload": json.dumps({"body_text": "Our best price is $9.99 per meter."}),
            },
        )

    redis_client = aioredis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    )
    try:
        principal = CurrentPrincipal(
            user_id=owner_id,
            tenant_id=tenant_id,
            role=UserRole.OWNER,
            email="owner@factory.example.com",
        )
        executor = ProposalExecutorService(redis=redis_client)
        async with tenant_session(runtime_session_factory, tenant_id) as session:
            row = await executor._get(session, principal, proposal_id)
            with pytest.raises(AppError) as excinfo:
                await executor._execute(session, principal, row, row.proposed_payload)
        assert excinfo.value.status_code == 409
        assert excinfo.value.code == "ERR_PROPOSAL_STALE"
    finally:
        await redis_client.aclose()

    reviewed = await async_client.post(
        f"/api/v1/ai/proposals/{proposal_id}/approve", headers=headers
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "failed"
    assert reviewed.json()["execution_result"]["code"] == "ERR_PROPOSAL_STALE"


async def test_inbound_webhook_auto_triggers_grounded_proposal_without_dlq(
    runtime_session_factory: RuntimeFactory,
    migrator_session_factory: RuntimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = aioredis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    )
    tenant_id, customer_id, account_id = uuid4(), uuid4(), uuid4()
    inbound_addr = f"auto-{tenant_id.hex[:6]}@inbound.mail.example.com"
    session_factory = runtime_session_factory

    adapter = EmailAdapter(http_transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    registry = AdapterRegistry()
    registry.register("email", adapter)

    monkeypatch.setenv("AUTO_AI_PROPOSAL_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-placeholder-key")
    monkeypatch.setattr(
        ingress_worker_module,
        "_auto_proposal_service",
        lambda: AiProposalGeneratorService(llm_client=OfflineLlmClient()),
    )

    try:
        async with tenant_session(session_factory, tenant_id) as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, ai_policy_settings) VALUES "
                    "(:id, :slug, 'Auto Proposal Co', "
                    "'{\"quote_pricing\":{\"incoterm_surcharge_pct\":{\"FOB\":\"0\"}}}'::jsonb)"
                ),
                {"id": tenant_id, "slug": f"auto-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                    "VALUES (:cid, :tid, 'b2b_contact', 'Auto Buyer')"
                ),
                {"cid": customer_id, "tid": tenant_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                    "external_account_id, display_name, status, rate_limit_profile) VALUES "
                    "(:id, :tid, 'email', :ext, 'Auto Inbox', 'active', '{\"mps\": 50}'::jsonb)"
                ),
                {"id": account_id, "tid": tenant_id, "ext": inbound_addr},
            )
            await conn.execute(
                text(
                    "SELECT register_channel_routing("
                    "'email', :ext, CAST(:tid AS uuid), CAST(:aid AS uuid), true)"
                ),
                {"ext": inbound_addr, "tid": tenant_id, "aid": account_id},
            )
            product_id, variant_id = uuid4(), uuid4()
            await conn.execute(
                text(
                    "INSERT INTO products (id, tenant_id, spu_code, category) "
                    "VALUES (:pid, :tid, 'SPU-AUTO-COB', 'LED Strip')"
                ),
                {"pid": product_id, "tid": tenant_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO product_variants (id, tenant_id, product_id, sku, "
                    "base_currency, cost_price_base, retail_price_usd, weight_grams, "
                    "lead_time_days) VALUES (:vid, :tid, :pid, 'LED-COB24V-IP67', 'USD', "
                    "4.2000, 8.40, 120, 12)"
                ),
                {"vid": variant_id, "tid": tenant_id, "pid": product_id},
            )

        payload = {
            "type": "email.received",
            "data": {
                "email_id": f"re_in_{uuid4().hex[:10]}",
                "from": "auto.buyer@import.example.com",
                "to": [inbound_addr],
                "subject": "RFQ COB strip",
                "message_id": "<auto-proposal-001@import.example.com>",
                "text": "Please quote FOB price and MOQ for SKU LED-COB24V-IP67, 2000 pcs.",
            },
        }
        raw_event_id = uuid4()
        async with migrator_session_factory() as conn, conn.begin():
            await accept_raw_webhook_event(
                conn,
                event_id=raw_event_id,
                tenant_id=tenant_id,
                channel_type="email",
                external_event_id=f"svix_{uuid4().hex[:8]}",
                external_account_id=inbound_addr,
                headers_json="{}",
                payload_json=json.dumps(payload),
            )

        outcome = await process_raw_webhook_event(
            session_factory,
            redis_client,
            raw_event_id,
            registry,
            broker_session_factory=migrator_session_factory,
        )
        assert outcome == "processed"

        async with tenant_session(session_factory, tenant_id) as sess:
            row = (
                await sess.execute(
                    text(
                        "SELECT id, source_tool_call_id, proposed_payload, status "
                        "FROM ai_proposals WHERE tenant_id = :tid"
                    ),
                    {"tid": tenant_id},
                )
            ).one_or_none()
            assert row is not None
            assert row.status == "pending"
            assert row.source_tool_call_id is not None
            assert row.proposed_payload["tool_evidence"][0]["data"]["sku"] == "LED-COB24V-IP67"
            processing_status = (
                await sess.execute(
                    text(
                        "SELECT processing_status FROM raw_webhook_events WHERE id = :rid"
                    ),
                    {"rid": raw_event_id},
                )
            ).scalar_one()
            assert processing_status == "processed"
    finally:
        await redis_client.aclose()


async def test_auto_proposal_never_breaks_webhook_on_ai_failure(
    runtime_session_factory: RuntimeFactory,
    migrator_session_factory: RuntimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = aioredis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    )
    tenant_id, customer_id, account_id = uuid4(), uuid4(), uuid4()
    inbound_addr = f"fail-{tenant_id.hex[:6]}@inbound.mail.example.com"
    adapter = EmailAdapter(http_transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    registry = AdapterRegistry()
    registry.register("email", adapter)

    class _ExplodingGenerator:
        async def generate_reply_proposal(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("provider exploded")

    monkeypatch.setenv("AUTO_AI_PROPOSAL_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder-key")
    monkeypatch.setattr(
        ingress_worker_module, "_auto_proposal_service", lambda: _ExplodingGenerator()
    )

    try:
        async with tenant_session(runtime_session_factory, tenant_id) as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Resilient Co')"
                ),
                {"id": tenant_id, "slug": f"resilient-{tenant_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                    "VALUES (:cid, :tid, 'b2b_contact', 'Resilient Buyer')"
                ),
                {"cid": customer_id, "tid": tenant_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                    "external_account_id, display_name, status, rate_limit_profile) VALUES "
                    "(:id, :tid, 'email', :ext, 'Fail Inbox', 'active', '{\"mps\": 50}'::jsonb)"
                ),
                {"id": account_id, "tid": tenant_id, "ext": inbound_addr},
            )
            await conn.execute(
                text(
                    "SELECT register_channel_routing("
                    "'email', :ext, CAST(:tid AS uuid), CAST(:aid AS uuid), true)"
                ),
                {"ext": inbound_addr, "tid": tenant_id, "aid": account_id},
            )

        raw_event_id = uuid4()
        async with migrator_session_factory() as conn, conn.begin():
            await accept_raw_webhook_event(
                conn,
                event_id=raw_event_id,
                tenant_id=tenant_id,
                channel_type="email",
                external_event_id=f"svix_{uuid4().hex[:8]}",
                external_account_id=inbound_addr,
                headers_json="{}",
                payload_json=json.dumps(
                    {
                        "type": "email.received",
                        "data": {
                            "email_id": f"re_in_{uuid4().hex[:10]}",
                            "from": "resilient.buyer@import.example.com",
                            "to": [inbound_addr],
                            "subject": "Hello",
                            "message_id": "<resilient-001@import.example.com>",
                            "text": "Do you ship to Spain?",
                        },
                    }
                ),
            )

        outcome = await process_raw_webhook_event(
            runtime_session_factory,
            redis_client,
            raw_event_id,
            registry,
            broker_session_factory=migrator_session_factory,
        )
        assert outcome == "processed"
        async with tenant_session(runtime_session_factory, tenant_id) as sess:
            assert (
                await sess.execute(
                    text("SELECT count(*) FROM messages WHERE direction = 'inbound'")
                )
            ).scalar_one() == 1
            assert (
                await sess.execute(text("SELECT count(*) FROM ai_proposals"))
            ).scalar_one() == 0
    finally:
        await redis_client.aclose()


def test_pi_html_contains_bank_box_terms_and_dual_signature() -> None:
    from decimal import Decimal

    from erp_core.crm.pdf_renderer import PiLineRenderItem, PiRenderContext, build_pi_html

    ctx = PiRenderContext(
        tenant_id=uuid4(),
        seller_name="Lumen Export Co Ltd",
        buyer_company="Lichttechnik Weber GmbH",
        buyer_contact="Marcus Weber",
        buyer_email="marcus@lichttechnik.example.com",
        quote_no="PI-202611-ABC123",
        version=2,
        is_proforma_invoice=True,
        incoterm="FOB",
        port_of_loading="Xiamen, China",
        port_of_destination="Hamburg, Germany",
        payment_terms="30% T/T deposit, 70% before shipment",
        quote_currency="USD",
        fx_rate_snapshot=Decimal("7.123456"),
        fx_locked_until=datetime.now(UTC) + timedelta(days=7),
        issued_at=datetime.now(UTC),
        freight_fee=Decimal("180.00"),
        insurance_fee=Decimal("0.00"),
        customs_duty_estimate=Decimal("0.00"),
        total_amount=Decimal("42180.00"),
        items=[
            PiLineRenderItem(
                sku="LED-COB24V-IP67",
                description="24V COB LED strip 4000K IP67",
                quantity=5000,
                unit_price=Decimal("8.4000"),
                line_total=Decimal("42000.00"),
            )
        ],
        seller_address="No.1 Export Road, Quanzhou, Fujian, China",
        seller_phone="+86 595 2200 0000",
        seller_email="export@lumen-export.example.com",
        seller_website="www.lumen-export.example.com",
        seller_tax_no="91350500MAxxxx",
        bank_beneficiary_name="Lumen Export Co Ltd",
        bank_name="Bank of China, Quanzhou Branch",
        bank_account_no="4123 4567 8901 2345",
        bank_swift_code="BKCHCNBJ74A",
        bank_address="No.1 Export Road, Quanzhou, Fujian, China",
        terms_notes="1. Validity: 15 days.\n2. Payment: 30% T/T deposit.",
        footer_signature_title="Authorized Signature & Company Stamp",
    )
    rendered = build_pi_html(ctx)
    assert "Bank Wire Transfer (T/T) Information" in rendered
    assert "BKCHCNBJ74A" in rendered
    assert "Commercial Terms &amp; Conditions" in rendered or "Commercial Terms & Conditions" in (
        rendered
    )
    assert "Seller Authorized Signature" in rendered
    assert "Buyer Confirmation" in rendered
    assert "PROFORMA INVOICE" in rendered
    assert "91350500MAxxxx" in rendered
