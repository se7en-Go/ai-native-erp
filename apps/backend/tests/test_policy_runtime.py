from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine import capabilities, compliance_filter, runtime
from erp_core.ai_engine.ingestion import (
    DeterministicHashEmbeddingProvider,
    DocumentIngestionService,
)
from erp_core.ai_engine.rag_service import HybridRagService
from erp_core.ai_engine.router_llm import LlmProviderResponse, TieredModelRouter
from erp_core.channels.consent_service import ConsentAndSuppressionService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import AiCapability, AutonomyLevel, ChannelType, ProposalType


class EchoEvidence:
    async def complete(self, *, model_id, provider, messages, max_tokens, temperature):
        context, _, query = messages[-1]["content"].partition("\n\nUser Input:\n")
        answer = context.removeprefix("Context:\n").splitlines()[0] if query else "Résumé traduit"
        return LlmProviderResponse(
            text=answer,
            model_id=model_id,
            provider=provider,
            prompt_tokens=50,
            completion_tokens=20,
        )


@pytest.fixture
async def db(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[AsyncSession, UUID]]:
    tenant = uuid4()
    async with tenant_session(runtime_session_factory, tenant) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Policy test', :slug)"),
            {"tid": tenant, "slug": f"policy-{tenant}"},
        )
        yield session, tenant
        await session.rollback()


@pytest.mark.parametrize(
    "channel,reply,metadata",
    [
        (ChannelType.TIKTOK_SHOP, "Get warranty help on wa.me/123", {}),
        (ChannelType.AMAZON_SP, "Send mail to a@example.com", {}),
        (ChannelType.SHOPEE, "https://evil.example/?shopee.com", {}),
        (ChannelType.INSTAGRAM_DM, "How may I help?", {"message_tag": "HUMAN_AGENT"}),
        (ChannelType.WEB_LIVECHAT, "A forbidden claim", {"blocked_terms": ["Forbidden"]}),
    ],
)
def test_compliance_blocks_outbound_traps(channel, reply, metadata) -> None:
    result = compliance_filter.OutboundComplianceFilter().check_outbound(
        channel, reply, channel_metadata=metadata
    )
    assert not result.allowed
    assert result.blocked_reason in result.violations


@pytest.mark.asyncio
async def test_whatsapp_uses_persisted_windows_us_policy_and_stop(db) -> None:
    session, tid = db
    cid, customer, account = uuid4(), uuid4(), uuid4()
    params = {
        "tid": tid,
        "cid": cid,
        "customer": customer,
        "account": account,
        "fep": datetime.now(UTC) + timedelta(hours=1),
    }
    await session.execute(
        text("""
        INSERT INTO customers (id, tenant_id, customer_type, display_name)
        VALUES (:customer, :tid, 'b2c_consumer', 'Test')
    """),
        params,
    )
    await session.execute(
        text("""
        INSERT INTO channel_accounts (
            id, tenant_id, external_account_id, channel_type, display_name
        )
        VALUES (:account, :tid, 'policy-wa', 'whatsapp_cloud', 'WA')
    """),
        params,
    )
    await session.execute(
        text("""
        INSERT INTO conversations (id, tenant_id, channel_account_id, customer_id, channel_type,
            conversation_mode, external_thread_id, fep_expires_at)
        VALUES (:cid, :tid, :account, :customer, 'whatsapp_cloud',
            'b2c_support', '14155552671', :fep)
    """),
        params,
    )
    gate = compliance_filter.OutboundComplianceFilter()
    conv = await gate.load_conversation(session, tid, cid)
    assert conv is not None
    assert (await gate.check_context(session, tid, conv, {})).allowed
    marketing = await gate.check_context(session, tid, conv, {"template_category": "marketing"})
    assert marketing.blocked_reason == "ERR_WHATSAPP_US_MARKETING_PROHIBITED"
    await session.execute(
        text("UPDATE conversations SET fep_expires_at = NULL WHERE id = :cid"), params
    )
    closed = await gate.check_context(session, tid, conv, {"csw_active": True})
    assert closed.blocked_reason == "ERR_WHATSAPP_CSW_EXPIRED"
    await ConsentAndSuppressionService().record_stop_opt_out(
        session, tid, customer, "whatsapp", "+14155552671", "STOP"
    )
    stopped = await gate.check_context(session, tid, conv, {})
    assert stopped.blocked_reason == "ERR_RECIPIENT_SUPPRESSED"


@pytest.mark.parametrize(
    "action",
    [
        "refund",
        "high_discount",
        "confirm_payment_settlement",
        "verify_bank_slip",
        "send_formal_quote",
    ],
)
@pytest.mark.asyncio
async def test_hard_l0_cannot_create_proposal_or_run_provider(db, action) -> None:
    session, tid = db
    policy = runtime.PolicyRuntime(llm_router=TieredModelRouter(provider_client=EchoEvidence()))
    result = await policy.run_capability(
        session, tid, AiCapability.CS_SUPPORT, {"action": action, "message": "Please process this"}
    )
    assert result.autonomy_applied == AutonomyLevel.L0_COPILOT
    assert result.l0_locked and not result.auto_executed and result.proposal_id is None
    assert not result.model_used
    assert await session.scalar(text("SELECT count(*) FROM ai_proposals")) == 0


@pytest.mark.asyncio
async def test_grounded_faq_and_tenant_policy_proposal(db) -> None:
    session, tid = db
    embed = DeterministicHashEmbeddingProvider()
    await DocumentIngestionService(embedding_provider=embed).ingest_document(
        session,
        tenant_id=tid,
        title="XJ-400Pro-EU specifications",
        source_type="markdown",
        raw_bytes=b"XJ-400Pro-EU floodlight IP rating is IP66 and warranty is five years",
    )
    policy = runtime.PolicyRuntime(
        rag_service=HybridRagService(embedding_provider=embed),
        llm_router=TieredModelRouter(provider_client=EchoEvidence()),
    )
    args = (
        session,
        tid,
        AiCapability.CS_SUPPORT,
        {"message": "What is the IP rating and warranty of XJ-400Pro-EU?"},
    )
    auto = await policy.run_capability(*args, strict_rag=True)
    assert auto.auto_executed and auto.numeric_guard_passed and not auto.should_abstain
    assert "IP66" in auto.reply_text and auto.citations
    await session.execute(
        text("""
        UPDATE tenants SET ai_policy_settings = '{"cs_support":"L1_proposal"}'::jsonb
        WHERE id = :tid
    """),
        {"tid": tid},
    )
    pending = await policy.run_capability(*args, strict_rag=True)
    assert not pending.auto_executed and pending.proposal_type == ProposalType.REPLY_DRAFT
    assert (
        await session.scalar(
            text("SELECT status FROM ai_proposals WHERE id = :id"), {"id": pending.proposal_id}
        )
        == "pending"
    )


@pytest.mark.asyncio
async def test_other_capabilities_do_not_promote_unsafe_actions(db) -> None:
    session, tid = db
    policy = runtime.PolicyRuntime(llm_router=TieredModelRouter(provider_client=EchoEvidence()))
    assert set(capabilities.CAPABILITY_SPECS) == set(AiCapability)
    translation = await policy.run_capability(
        session,
        tid,
        AiCapability.CONTENT_STUDIO,
        {"action": "translate", "message": "Translated summary", "target_lang": "fr"},
    )
    assert translation.reply_text == "Résumé traduit" and not translation.auto_executed
    quote = await policy.run_capability(
        session, tid, AiCapability.SALES_QUOTING, {"message": "Request product samples"}
    )
    assert quote.proposal_type == ProposalType.CRM_ENTITY_EXTRACT
    for cap, payload in [
        (AiCapability.ORDER_LOGISTICS, {"order_no": "Unknown"}),
        (AiCapability.BIZ_ANALYTICS, {"sql": "DELETE FROM orders"}),
    ]:
        result = await policy.run_capability(session, tid, cap, payload)
        assert result.should_abstain and not result.auto_executed
    finance = await policy.run_capability(
        session, tid, AiCapability.FINANCE_RECON, {"amount": "400.01", "currency": "USD"}
    )
    assert finance.l0_locked and not finance.auto_executed
