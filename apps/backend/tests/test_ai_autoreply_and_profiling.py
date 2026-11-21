"""Offline unit tests: AI auto-reply risk guardrails, grounding, and customer profiling.

No database, Redis, LLM or network access is required. The service's I/O seams are
overridden by deterministic stubs so the full decision path is exercised in-process.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from erp_core.ai_engine.auto_reply_service import (
    AutoReplyRunRequest,
    AutoReplyService,
    CustomerAiProfileRead,
    ProfileCustomer,
    ProfileMessage,
    ProfileOrder,
    ProfileSources,
    build_abstain_reply,
    build_grounded_reply,
    build_risk_hold_reply,
    compute_confidence,
    decide_auto_reply,
    default_profile,
    detect_reply_language,
    detect_risk_flags,
    evidence_summary_lines,
    extract_incoterm,
    extract_profile_from_sources,
    extract_sku_tokens,
    infer_country_code,
    normalize_country_code,
    profile_to_json,
    usd_order_total,
)
from erp_core.ai_engine.proposal_generator import ConversationContext, RecentMessage
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError
from erp_core.tenancy.dependencies import CurrentPrincipal

NOW = datetime(2026, 11, 21, 9, 30, tzinfo=UTC)
_DUMMY_SESSION = cast(AsyncSession, object())
_DIGITS = re.compile(r"\d+(?:\.\d+)?")


# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------


def _principal(role: UserRole = UserRole.MANAGER) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=uuid4(),
        tenant_id=uuid4(),
        role=role,
        email="owner@ledfactory.test",
    )


def _conversation(
    *,
    customer_id: UUID | None = None,
    assigned_user_id: UUID | None = None,
    visibility_scope: str = "shared",
    owner_user_id: UUID | None = None,
) -> ConversationContext:
    return ConversationContext(
        id=uuid4(),
        customer_id=customer_id or uuid4(),
        channel_type="email",
        assigned_user_id=assigned_user_id,
        visibility_scope=visibility_scope,
        owner_user_id=owner_user_id,
    )


def _sku_evidence(
    *,
    sku: str = "LED-COB24V-IP67",
    unit_price: str = "8.40",
    qty: int = 5000,
    available: int = 5200,
    lead_time: int = 12,
    tier: int | None = 5000,
) -> dict[str, Any]:
    return {
        "tool_call_id": "tc_3f1d2c3b4a5e4f6a8b9c0d1e2f3a4b5c",
        "tool_name": "get_live_sku_pricing_and_stock",
        "summary": "grounded",
        "data": {
            "sku": sku,
            "qty_requested": qty,
            "applied_moq_tier": tier,
            "applied_unit_price_usd": unit_price,
            "available_qty": available,
            "lead_time_days": lead_time,
            "incoterm": "FOB",
            "source_tool_call_id": "tc_3f1d2c3b4a5e4f6a8b9c0d1e2f3a4b5c",
        },
    }


def _kb_hit() -> dict[str, Any]:
    return {
        "chunk_id": str(uuid4()),
        "doc_id": str(uuid4()),
        "doc_title": "LED Strip Handling Guide",
        "snippet": "COB strips ship on anti-static reels and must be stored below 30 C.",
        "score": 0.81,
    }


def _profile_customer(
    *,
    customer_type: str = "b2b_contact",
    country_code: str | None = None,
    company_name: str | None = "Lichttechnik Handels GmbH",
) -> ProfileCustomer:
    return ProfileCustomer(
        customer_id=uuid4(),
        display_name="Marcus Weber",
        customer_type=customer_type,
        country_code=country_code,
        preferred_language="de",
        company_name=company_name,
        company_country_code=None,
    )


class _StubAutoReplyService(AutoReplyService):
    """Deterministic in-process double: no SQL, no Redis, no LLM."""

    def __init__(
        self,
        *,
        conversation: ConversationContext,
        enabled: bool = True,
        messages: Sequence[RecentMessage] = (),
        evidence: Sequence[dict[str, Any]] = (),
        kb_hits: Sequence[dict[str, Any]] = (),
        customer: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._conversation = conversation
        self._enabled = enabled
        self._messages = list(messages)
        self._evidence = list(evidence)
        self._kb_hits = list(kb_hits)
        self._customer = customer or {"name": "Marcus", "company": "Lichttechnik"}
        self.inserted_messages: list[dict[str, Any]] = []
        self.created_proposals: list[dict[str, Any]] = []
        self.audits: list[str] = []

    async def load_conversation(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> ConversationContext:
        return self._conversation

    async def _load_auto_reply_flag(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> bool:
        return self._enabled

    async def _recent_messages(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> list[RecentMessage]:
        return list(self._messages)

    async def _customer_summary(
        self, session: AsyncSession, tenant_id: UUID, customer_id: UUID
    ) -> dict[str, str]:
        return dict(self._customer)

    async def _search_kb(
        self, session: AsyncSession, tenant_id: UUID, query: str, top_k: int
    ) -> list[dict[str, Any]]:
        return list(self._kb_hits)[:top_k]

    async def _collect_tool_evidence(
        self, session: AsyncSession, tenant_id: UUID, customer_id: UUID, message_text: str
    ) -> list[dict[str, Any]]:
        return list(self._evidence)

    async def _insert_outbound_message(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        conversation: ConversationContext,
        body_text: str,
        metadata: dict[str, Any],
    ) -> UUID:
        message_id = uuid4()
        self.inserted_messages.append(
            {"id": message_id, "body_text": body_text, "metadata": metadata}
        )
        return message_id

    async def _create_proposal(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        conversation: ConversationContext,
        payload: dict[str, Any],
        confidence: Decimal,
        citations: list[dict[str, Any]],
        source_tool_call_id: UUID | None,
    ) -> UUID:
        proposal_id = uuid4()
        self.created_proposals.append(
            {
                "id": proposal_id,
                "payload": payload,
                "confidence": confidence,
                "citations": citations,
                "source_tool_call_id": source_tool_call_id,
            }
        )
        return proposal_id

    async def _record_audit(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        action: str,
        resource_id: UUID,
        *,
        after_state: dict[str, Any],
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.audits.append(action)


# --------------------------------------------------------------------------------------
# Risk guardrail
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_flag"),
    [
        ("We want a refund for the last shipment.", "refund"),
        ("请给我们退款，货有问题", "refund"),
        ("We will file a chargeback with our bank.", "chargeback"),
        ("This is a formal dispute with our lawyer.", "dispute"),
        ("We expect compensation for the downtime.", "compensation"),
        ("我们需要赔付这批不良品", "compensation"),
        ("We need to return the goods to your factory.", "goods_return"),
        ("Can you apply a discount override for 20% off?", "price_override"),
        ("必须降价 10% 否则取消订单", "price_override"),
        ("Please change the bank account for the next payment.", "bank_account_change"),
        ("请改账户，我们换银行了", "bank_account_change"),
        ("We paid by wire transfer yesterday.", "wire_transfer"),
        ("电汇已经发出，请查收", "wire_transfer"),
        ("Please confirm the payment receipt for ORD-1001.", "payment_reconciliation"),
        ("麻烦帮我做付款核销", "payment_reconciliation"),
    ],
)
def test_risk_guardrail_detects_payment_and_refund_red_lines(
    message: str, expected_flag: str
) -> None:
    flags = detect_risk_flags(message)
    assert expected_flag in flags


def test_risk_guardrail_is_quiet_on_plain_price_and_logistics_questions() -> None:
    message = (
        "Hi, please quote FOB Xiamen price and MOQ for SKU LED-COB24V-IP67, "
        "we need 5000 meters and the CE certificate."
    )
    assert detect_risk_flags(message) == []


def test_risk_guardrail_flags_are_order_stable_and_deduplicated() -> None:
    text = "refund now, refund again, and a chargeback"
    assert detect_risk_flags(text) == ["refund", "chargeback"]
    assert detect_risk_flags("   ") == []


def test_decide_auto_reply_forces_l1_proposal_even_when_auto_and_grounded() -> None:
    decision = decide_auto_reply(
        mode="auto",
        auto_reply_enabled=True,
        has_evidence=True,
        has_kb=True,
        risk_flags=["refund"],
    )
    assert decision.action == "proposal_created"
    assert decision.reason == "risk_guardrail_forced_l1"


def test_decide_auto_reply_abstains_without_any_grounding() -> None:
    decision = decide_auto_reply(
        mode="auto",
        auto_reply_enabled=True,
        has_evidence=False,
        has_kb=False,
        risk_flags=[],
    )
    assert decision.action == "abstained"
    assert decision.reason == "no_grounding_evidence"


def test_decide_auto_reply_requires_operator_toggle_for_auto_send() -> None:
    decision = decide_auto_reply(
        mode="auto",
        auto_reply_enabled=False,
        has_evidence=True,
        has_kb=False,
        risk_flags=[],
    )
    assert decision.action == "proposal_created"
    assert decision.reason == "auto_reply_disabled"


def test_decide_auto_reply_proposal_only_mode_never_sends() -> None:
    decision = decide_auto_reply(
        mode="proposal_only",
        auto_reply_enabled=True,
        has_evidence=True,
        has_kb=True,
        risk_flags=[],
    )
    assert decision.action == "proposal_created"
    assert decision.reason == "proposal_only_mode"


# --------------------------------------------------------------------------------------
# Grounded drafting + confidence
# --------------------------------------------------------------------------------------


def test_grounded_reply_quotes_only_numbers_copied_from_tool_evidence() -> None:
    evidence = [_sku_evidence()]
    kb_hits = [_kb_hit()]
    reply = build_grounded_reply(
        language="en",
        customer_name="Marcus",
        evidence=evidence,
        kb_hits=kb_hits,
    )
    # Both live ERP tool data and stored knowledge-base chunks are verifiable evidence.
    allowed = set(_DIGITS.findall(json.dumps([evidence, kb_hits], default=str)))
    assert set(_DIGITS.findall(reply)) <= allowed
    assert "LED-COB24V-IP67" in reply
    assert "8.40" in reply
    assert "LED Strip Handling Guide" in reply


def test_grounded_reply_numbers_are_evidence_scoped_for_chinese() -> None:
    evidence = [_sku_evidence(unit_price="7.35", qty=1200, available=900, lead_time=18)]
    reply = build_grounded_reply(
        language="zh", customer_name="王先生", evidence=evidence, kb_hits=[]
    )
    allowed = set(_DIGITS.findall(json.dumps(evidence, default=str)))
    assert set(_DIGITS.findall(reply)) <= allowed
    assert "7.35" in reply


def test_risk_and_abstain_templates_never_quote_a_single_figure() -> None:
    for language in ("en", "zh", "es"):
        assert not _DIGITS.findall(build_risk_hold_reply(language=language, customer_name="Bo"))
        assert not _DIGITS.findall(build_abstain_reply(language=language, customer_name="Bo"))


def test_evidence_summary_lines_cover_sku_and_tracking_tools() -> None:
    lines = evidence_summary_lines(
        [
            _sku_evidence(),
            {
                "tool_call_id": "tc_x",
                "tool_name": "get_live_order_tracking",
                "data": {
                    "order_no": "ORD-1001",
                    "fulfillment_status": "shipped",
                    "payment_status": "paid",
                    "shipments": [
                        {
                            "carrier_code": "DHL",
                            "tracking_number": "TRK-77",
                            "standardized_status": "InTransit",
                        }
                    ],
                },
            },
        ]
    )
    assert len(lines) == 2
    assert "MOQ tier 5000 pcs" in lines[0]
    assert "ORD-1001" in lines[1]
    assert "TRK-77" in lines[1]


def test_confidence_is_quantized_to_three_decimals_and_capped_on_risk() -> None:
    grounded = compute_confidence([_sku_evidence()], [_kb_hit()], [])
    assert grounded == Decimal("0.750")
    assert grounded.as_tuple().exponent == -3

    risky = compute_confidence([_sku_evidence()], [_kb_hit(), _kb_hit()], ["refund", "chargeback"])
    assert risky == Decimal("0.350")

    assert compute_confidence([], [], []) == Decimal("0.100")


def test_reply_language_detection_matches_customer_script() -> None:
    assert detect_reply_language("请报价 COB 灯带") == "zh"
    assert detect_reply_language("Hola, necesito precio FOB") == "es"
    assert detect_reply_language("Please send your best FOB price") == "en"


# --------------------------------------------------------------------------------------
# Deterministic profile extraction
# --------------------------------------------------------------------------------------


def _sources(
    *,
    customer: ProfileCustomer | None = None,
    messages: Sequence[ProfileMessage] = (),
    orders: Sequence[ProfileOrder] = (),
    quotes: Sequence[Any] = (),
) -> ProfileSources:
    return ProfileSources(
        customer=customer or _profile_customer(),
        messages=tuple(messages),
        orders=tuple(orders),
        quotes=tuple(quotes),
    )


def test_extract_profile_detects_distributor_intent_and_backfills_country() -> None:
    messages = [
        ProfileMessage(
            direction="inbound",
            sender_type="customer",
            body_text=(
                "Hello, we are a lighting distributor based in Germany. "
                "Please quote 5000 meters of SKU LED-COB24V-IP67 FOB Xiamen, "
                "we need CE and RoHS certificates and an IP65 rating."
            ),
        ),
        ProfileMessage(
            direction="outbound",
            sender_type="human_agent",
            body_text="Thanks, preparing the quotation.",
        ),
        ProfileMessage(
            direction="inbound",
            sender_type="customer",
            body_text="Also we need a sample order, your MOQ seems too high.",
        ),
    ]
    profile = extract_profile_from_sources(_sources(messages=messages), now=NOW)

    assert profile.buyer_type == "b2b_distributor"
    assert profile.country_code == "DE"
    assert profile.interested_skus == ["LED-COB24V-IP67"]
    assert profile.target_incoterm == "FOB"
    assert profile.preferred_language == "en"
    assert profile.purchase_intent_score == 80
    assert "ce" in profile.key_requirements
    assert "rohs" in profile.key_requirements
    assert "sample_order" in profile.key_requirements
    assert "moq_barrier" in profile.pain_points
    assert profile.tags[:2] == ["ai-profile", "b2b_distributor"]
    assert "intent-high" in profile.tags
    assert "country-de" in profile.tags
    assert "risk-review" not in profile.tags
    assert profile.updated_at == NOW


def test_extract_profile_prefers_crm_country_over_message_guess() -> None:
    customer = _profile_customer(country_code="fr")
    profile = extract_profile_from_sources(
        _sources(
            customer=customer,
            messages=[
                ProfileMessage(
                    direction="inbound",
                    sender_type="customer",
                    body_text="We are based in Germany but buy for our French stores.",
                )
            ],
        ),
        now=NOW,
    )
    assert profile.country_code == "FR"


def test_extract_profile_sums_usd_orders_with_exact_decimal_precision() -> None:
    orders = (
        ProfileOrder("ORD-1", Decimal("1000.10"), "USD"),
        ProfileOrder("ORD-2", Decimal("2500.20"), "USD"),
        ProfileOrder("ORD-3", Decimal("999.00"), "CNY"),
    )
    profile = extract_profile_from_sources(_sources(orders=orders), now=NOW)

    assert profile.estimated_order_value_usd == Decimal("3500.30")
    assert isinstance(profile.estimated_order_value_usd, Decimal)
    assert profile.purchase_intent_score >= 15
    assert "No USD order history" not in profile.ai_summary


def test_extract_profile_reports_no_value_without_usd_orders() -> None:
    profile = extract_profile_from_sources(
        _sources(orders=(ProfileOrder("ORD-9", Decimal("12000.00"), "EUR"),)), now=NOW
    )
    assert profile.estimated_order_value_usd is None
    assert usd_order_total(()) is None


def test_extract_profile_flags_refund_thread_for_human_review() -> None:
    profile = extract_profile_from_sources(
        _sources(
            messages=(
                ProfileMessage(
                    direction="inbound",
                    sender_type="customer",
                    body_text="The last batch is defective, we demand a refund now.",
                ),
            )
        ),
        now=NOW,
    )
    assert "risk-review" in profile.tags
    assert profile.next_best_action.startswith("Human owner must review")
    assert profile.price_sensitivity == "low"


def test_extract_profile_marks_price_sensitivity_high_on_pressure() -> None:
    profile = extract_profile_from_sources(
        _sources(
            messages=(
                ProfileMessage(
                    direction="inbound",
                    sender_type="customer",
                    body_text="Your price is too expensive, we need the cheapest option.",
                ),
            )
        ),
        now=NOW,
    )
    assert profile.price_sensitivity == "high"
    assert "price-sensitivity-high" in profile.tags


def test_profile_default_is_empty_but_valid() -> None:
    customer_id = uuid4()
    profile = default_profile(customer_id, now=NOW)
    assert profile.customer_id == customer_id
    assert profile.buyer_type == "unknown"
    assert profile.interested_skus == []
    assert profile.estimated_order_value_usd is None
    assert profile.purchase_intent_score == 0
    assert profile.synced_lead_id is None


def test_profile_json_roundtrip_keeps_money_exact_and_revalidates() -> None:
    original = CustomerAiProfileRead(
        customer_id=uuid4(),
        buyer_type="b2b_buyer",
        company_name="Lichttechnik",
        country_code="DE",
        preferred_language="en",
        interested_skus=["LED-COB24V-IP67"],
        estimated_order_value_usd=Decimal("3500.30"),
        target_incoterm="FOB",
        purchase_intent_score=70,
        price_sensitivity="medium",
        key_requirements=["ce"],
        pain_points=[],
        next_best_action="Send PI",
        ai_summary="summary",
        tags=["ai-profile"],
        updated_at=NOW,
    )
    payload = profile_to_json(original)
    assert payload["estimated_order_value_usd"] == "3500.30"
    assert not isinstance(payload["estimated_order_value_usd"], float)

    restored = CustomerAiProfileRead.model_validate(json.loads(json.dumps(payload)))
    assert restored.estimated_order_value_usd == Decimal("3500.30")
    assert restored.updated_at == NOW


def test_sku_extraction_ignores_payment_abbreviations_and_short_tokens() -> None:
    text = "SKU LED-COB24V-IP67 and COB-24V-8W, pay by T/T, C/O China"
    assert extract_sku_tokens(text) == ["LED-COB24V-IP67", "COB-24V-8W"]
    assert normalize_country_code(" de ") == "DE"
    assert normalize_country_code("Germany") is None
    assert infer_country_code("we are located in Spain") == "ES"
    assert extract_incoterm("please quote cif rotterdam") == "CIF"


# --------------------------------------------------------------------------------------
# Orchestration through overridden I/O seams
# --------------------------------------------------------------------------------------


async def test_run_auto_reply_sends_grounded_message_with_evidence_metadata() -> None:
    conversation = _conversation()
    messages = [
        RecentMessage(
            direction="inbound",
            sender_type="customer",
            body_text="Please quote FOB price and stock for SKU LED-COB24V-IP67, 5000 m.",
        )
    ]
    service = _StubAutoReplyService(
        conversation=conversation,
        enabled=True,
        messages=messages,
        evidence=[_sku_evidence()],
        kb_hits=[_kb_hit()],
    )
    principal = _principal()

    response = await service.run(
        _DUMMY_SESSION, principal, conversation.id, AutoReplyRunRequest(mode="auto")
    )

    assert response.action == "auto_replied"
    assert response.message_id is not None
    assert response.proposal_id is None
    assert response.risk_flags == []
    assert response.confidence == Decimal("0.750")
    assert response.reply_text is not None and "8.40" in response.reply_text
    assert service.created_proposals == []

    assert len(service.inserted_messages) == 1
    inserted = service.inserted_messages[0]
    metadata = inserted["metadata"]
    assert metadata["grounded_evidence"][0]["data"]["sku"] == "LED-COB24V-IP67"
    assert metadata["kb_citations"][0]["chunk_id"] == response.kb_hits[0]["chunk_id"]
    assert metadata["ai_auto_reply"] is True
    assert service.audits == ["ai.auto_reply.sent"]


async def test_run_auto_reply_risk_message_creates_pending_proposal_not_message() -> None:
    conversation = _conversation()
    service = _StubAutoReplyService(
        conversation=conversation,
        enabled=True,
        messages=[
            RecentMessage(
                direction="inbound",
                sender_type="customer",
                body_text="We demand a refund and will change the bank account for payment.",
            )
        ],
        evidence=[_sku_evidence()],
    )

    response = await service.run(
        _DUMMY_SESSION, _principal(), conversation.id, AutoReplyRunRequest(mode="auto")
    )

    assert response.action == "proposal_created"
    assert response.proposal_id is not None
    assert response.message_id is None
    assert set(response.risk_flags) >= {"refund", "bank_account_change"}
    assert response.confidence == Decimal("0.350")
    assert service.inserted_messages == []

    assert len(service.created_proposals) == 1
    payload = service.created_proposals[0]["payload"]
    assert payload["risk_flags"] == response.risk_flags
    assert payload["guardrail_reason"] == "risk_guardrail_forced_l1"
    assert response.reply_text is not None
    assert not _DIGITS.findall(response.reply_text)
    assert service.audits == ["ai.auto_reply.proposal_created"]


async def test_run_auto_reply_abstains_without_any_evidence_and_writes_nothing() -> None:
    conversation = _conversation()
    service = _StubAutoReplyService(
        conversation=conversation,
        enabled=True,
        messages=[
            RecentMessage(
                direction="inbound",
                sender_type="customer",
                body_text="Hello, do you sell lighting products?",
            )
        ],
        evidence=[],
        kb_hits=[],
    )

    response = await service.run(
        _DUMMY_SESSION, _principal(), conversation.id, AutoReplyRunRequest(mode="auto")
    )

    assert response.action == "abstained"
    assert response.message_id is None
    assert response.proposal_id is None
    assert response.confidence == Decimal("0.100")
    assert response.reply_text is not None
    assert not _DIGITS.findall(response.reply_text)
    assert service.inserted_messages == []
    assert service.created_proposals == []
    assert service.audits == []


async def test_run_auto_reply_honours_proposal_only_mode_and_max_kb_hits() -> None:
    conversation = _conversation()
    service = _StubAutoReplyService(
        conversation=conversation,
        enabled=True,
        messages=[
            RecentMessage(
                direction="inbound",
                sender_type="customer",
                body_text="What is the warranty policy for COB strips?",
            )
        ],
        kb_hits=[_kb_hit(), _kb_hit(), _kb_hit()],
    )

    response = await service.run(
        _DUMMY_SESSION,
        _principal(),
        conversation.id,
        AutoReplyRunRequest(mode="proposal_only", max_kb_hits=2),
    )

    assert response.action == "proposal_created"
    assert len(response.kb_hits) == 2
    assert response.tool_evidence == []
    assert service.inserted_messages == []
    assert service.created_proposals[0]["payload"]["verified_tool_call_ids"] == []


async def test_run_auto_reply_respects_disabled_toggle() -> None:
    conversation = _conversation()
    service = _StubAutoReplyService(
        conversation=conversation,
        enabled=False,
        messages=[
            RecentMessage(
                direction="inbound",
                sender_type="customer",
                body_text="Please quote SKU LED-COB24V-IP67, 5000 meters.",
            )
        ],
        evidence=[_sku_evidence()],
    )

    response = await service.run(
        _DUMMY_SESSION, _principal(), conversation.id, AutoReplyRunRequest(mode="auto")
    )

    assert response.action == "proposal_created"
    assert service.created_proposals[0]["payload"]["guardrail_reason"] == "auto_reply_disabled"
    assert service.inserted_messages == []


async def test_run_rejects_agent_on_another_agents_private_conversation() -> None:
    other_agent = uuid4()
    conversation = _conversation(assigned_user_id=other_agent)
    service = _StubAutoReplyService(conversation=conversation, enabled=True)
    agent = _principal(UserRole.AGENT)

    with pytest.raises(AppError) as excinfo:
        await service.run(_DUMMY_SESSION, agent, conversation.id, AutoReplyRunRequest(mode="auto"))

    assert excinfo.value.status_code == 403
    assert excinfo.value.code == "ERR_CONVERSATION_ACCESS_DENIED"
    assert service.inserted_messages == []


# --------------------------------------------------------------------------------------
# Profile orchestration
# --------------------------------------------------------------------------------------


class _StubProfileService(AutoReplyService):
    def __init__(self, conversation: ConversationContext, sources: ProfileSources) -> None:
        super().__init__()
        self._conversation = conversation
        self._sources = sources
        self.persisted: list[CustomerAiProfileRead] = []
        self.lead_calls = 0
        self.followup_calls = 0
        self.audits: list[str] = []

    async def load_conversation(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> ConversationContext:
        return self._conversation

    async def _load_profile_sources(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        conversation_id: UUID,
        customer_id: UUID,
    ) -> ProfileSources:
        return self._sources

    async def _persist_profile(
        self, session: AsyncSession, tenant_id: UUID, profile: CustomerAiProfileRead
    ) -> None:
        self.persisted.append(profile)

    async def _sync_crm_lead(
        self, session: AsyncSession, tenant_id: UUID, profile: CustomerAiProfileRead
    ) -> UUID:
        self.lead_calls += 1
        return UUID("11111111-1111-4111-8111-111111111111")

    async def _sync_crm_followup(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        profile: CustomerAiProfileRead,
        author_user_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        self.followup_calls += 1
        assert author_user_id is not None
        return UUID("22222222-2222-4222-8222-222222222222")

    async def _record_audit(
        self,
        session: AsyncSession,
        principal: CurrentPrincipal,
        action: str,
        resource_id: UUID,
        *,
        after_state: dict[str, Any],
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.audits.append(action)


async def test_extract_profile_persists_and_syncs_crm_records() -> None:
    conversation = _conversation()
    sources = _sources(
        messages=(
            ProfileMessage(
                direction="inbound",
                sender_type="customer",
                body_text=(
                    "We are a distributor in Germany, quote 5000 m of SKU LED-COB24V-IP67 FOB."
                ),
            ),
        ),
        orders=(ProfileOrder("ORD-1", Decimal("2500.20"), "USD"),),
    )
    service = _StubProfileService(conversation, sources)

    profile = await service.extract_profile(
        _DUMMY_SESSION, _principal(), conversation.id, sync_to_crm=True
    )

    assert len(service.persisted) == 1
    assert service.persisted[0].country_code == "DE"
    assert service.persisted[0].estimated_order_value_usd == Decimal("2500.20")
    assert service.lead_calls == 1
    assert service.followup_calls == 1
    assert profile.synced_lead_id == UUID("11111111-1111-4111-8111-111111111111")
    assert profile.synced_followup_id == UUID("22222222-2222-4222-8222-222222222222")
    assert service.audits == ["ai.customer_profile.extracted"]


async def test_extract_profile_skips_crm_sync_when_disabled() -> None:
    conversation = _conversation()
    service = _StubProfileService(
        conversation,
        _sources(
            messages=(
                ProfileMessage(
                    direction="inbound",
                    sender_type="customer",
                    body_text="Please send your catalogue.",
                ),
            )
        ),
    )

    profile = await service.extract_profile(
        _DUMMY_SESSION, _principal(), conversation.id, sync_to_crm=False
    )

    assert service.lead_calls == 0
    assert service.followup_calls == 0
    assert profile.synced_lead_id is None
    assert profile.synced_followup_id is None
    assert profile.buyer_type == "b2b_buyer"
    assert profile.purchase_intent_score == 5


def test_profile_next_action_matrix_is_monotonic() -> None:
    low = extract_profile_from_sources(
        _sources(messages=(ProfileMessage("inbound", "customer", "Hello, are you a factory?"),)),
        now=NOW,
    )
    high = extract_profile_from_sources(
        _sources(
            messages=(
                ProfileMessage(
                    "inbound",
                    "customer",
                    "We need 5000 pcs SKU LED-COB24V-IP67, FOB price and stock today.",
                ),
            )
        ),
        now=NOW,
    )
    assert low.purchase_intent_score < high.purchase_intent_score
    assert high.next_best_action.startswith("Send a grounded PI")
    assert low.next_best_action.startswith("Ask for the exact SKU")


def test_followup_next_action_window_is_one_day_for_qualified_leads() -> None:
    profile = extract_profile_from_sources(
        _sources(
            messages=(
                ProfileMessage(
                    "inbound",
                    "customer",
                    "Please quote 5000 meters SKU LED-COB24V-IP67 FOB.",
                ),
            )
        ),
        now=NOW,
    )
    expected = (NOW + timedelta(days=1)).replace(microsecond=0)
    assert profile.purchase_intent_score >= 40
    assert expected > NOW


# --------------------------------------------------------------------------------------
# Courtesy auto-reply, outbound Redis dispatch & inbound webhook trigger
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text_value", "expected"),
    [
        ("ok thx", True),
        ("thanks!", True),
        ("Hello", True),
        ("你好", True),
        ("好的，谢谢", False),
        ("谢谢！", True),
        ("What is the price of LED-COB-24V-5M?", False),
        ("ok I want a refund", False),
        ("1000 pcs", False),
    ],
)
def test_is_courtesy_message_only_matches_pure_short_acknowledgements(
    text_value: str, expected: bool
) -> None:
    from erp_core.ai_engine.auto_reply_service import is_courtesy_message

    assert is_courtesy_message(text_value) is expected


@pytest.mark.asyncio
async def test_auto_reply_enqueues_outbound_stream_and_publishes_sse() -> None:
    import json

    from erp_core.ai_engine.auto_reply_service import AutoReplyDispatchBundle

    class _FakeRedis:
        def __init__(self) -> None:
            self.xadds: list[tuple[str, dict[str, Any]]] = []
            self.publishes: list[tuple[str, str]] = []

        async def xadd(self, stream: str, fields: dict[str, Any]) -> str:
            self.xadds.append((stream, fields))
            return "1-0"

        async def publish(self, channel: str, payload: str) -> int:
            self.publishes.append((channel, payload))
            return 1

    principal = _principal()
    conversation = ConversationContext(
        id=uuid4(),
        customer_id=uuid4(),
        channel_type="telegram",
        assigned_user_id=None,
        visibility_scope="shared",
        owner_user_id=None,
    )

    class _DispatchingService(_StubAutoReplyService):
        async def _build_outbound_dispatch(
            self,
            session: AsyncSession,
            *,
            tenant_id: UUID,
            conversation: ConversationContext,
            message_id: UUID,
            body_text: str,
            metadata: dict[str, Any],
        ) -> AutoReplyDispatchBundle | None:
            return AutoReplyDispatchBundle(
                tenant_id=tenant_id,
                outbound_payload={
                    "tenant_id": str(tenant_id),
                    "channel_account_id": str(uuid4()),
                    "channel_type": conversation.channel_type,
                    "external_account_id": "9197431516",
                    "recipient_id": "7546465",
                    "conversation_id": str(conversation.id),
                    "message_id": str(message_id),
                    "content_type": "text",
                    "body_text": body_text,
                    "metadata": metadata,
                },
                sse_event={
                    "event": "message.created",
                    "conversation_id": str(conversation.id),
                    "message_id": str(message_id),
                    "direction": "outbound",
                },
            )

    service = _DispatchingService(
        conversation=conversation,
        enabled=True,
        messages=[RecentMessage("inbound", "customer", "ok thx")],
    )
    fake_redis = _FakeRedis()

    result = await service.run(
        _DUMMY_SESSION,
        principal,
        conversation.id,
        AutoReplyRunRequest(mode="auto"),
        redis=fake_redis,
    )

    assert result.action == "auto_replied"
    assert result.message_id is not None
    assert len(fake_redis.xadds) == 1
    stream_name, fields = fake_redis.xadds[0]
    assert stream_name == "stream:outbound:messages"
    payload = json.loads(fields["payload"])
    assert payload["channel_type"] == "telegram"
    assert payload["recipient_id"] == "7546465"
    assert payload["message_id"] == str(result.message_id)
    assert len(fake_redis.publishes) == 1
    sse_channel, sse_body = fake_redis.publishes[0]
    assert sse_channel == f"inbox:events:{principal.tenant_id}"
    assert json.loads(sse_body)["event"] == "message.created"


@pytest.mark.asyncio
async def test_ingress_worker_maybe_run_auto_reply_respects_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from erp_core.ai_engine.auto_reply_service import (
        AutoReplyDispatchBundle,
        AutoReplyRunResponse,
    )
    from erp_core.channels import ingress_worker

    tenant_id = uuid4()
    conv_id = uuid4()
    msg_id = uuid4()
    events_order: list[str] = []

    class _FakeSession:
        def __init__(self, enabled: bool) -> None:
            self._enabled = enabled

        async def scalar(self, *_args: Any, **_kwargs: Any) -> bool:
            return self._enabled

    class _FakeRedis:
        async def xadd(self, stream: str, fields: dict[str, Any]) -> str:
            events_order.append(f"xadd:{stream}")
            return "1-0"

        async def publish(self, channel: str, payload: str) -> int:
            events_order.append(f"publish:{channel}")
            return 1

    class _FakeService:
        async def run_with_dispatch(self, *_args: Any, **_kwargs: Any) -> Any:
            events_order.append("run_in_tx")
            return (
                AutoReplyRunResponse(
                    conversation_id=conv_id,
                    action="auto_replied",
                    message_id=msg_id,
                    reply_text="You're welcome!",
                    confidence=Decimal("0.850"),
                ),
                AutoReplyDispatchBundle(
                    tenant_id=tenant_id,
                    outbound_payload={"message_id": str(msg_id)},
                    sse_event={"event": "message.created", "message_id": str(msg_id)},
                ),
            )

    @asynccontextmanager
    async def _fake_tenant_session_enabled(_factory: Any, _tid: Any) -> Any:
        events_order.append("tx_enter")
        yield _FakeSession(True)
        events_order.append("tx_commit")

    @asynccontextmanager
    async def _fake_tenant_session_disabled(_factory: Any, _tid: Any) -> Any:
        yield _FakeSession(False)

    monkeypatch.setattr(ingress_worker, "_auto_reply_service", lambda: _FakeService())
    monkeypatch.setattr(ingress_worker, "tenant_session", _fake_tenant_session_enabled)

    handled = await ingress_worker._maybe_run_auto_reply(
        cast(Any, object()), cast(Any, _FakeRedis()), tenant_id, conv_id
    )
    assert handled is True
    # Outbound dispatch & SSE publish must happen strictly AFTER DB transaction commit.
    assert events_order == [
        "tx_enter",
        "run_in_tx",
        "tx_commit",
        "xadd:stream:outbound:messages",
        f"publish:inbox:events:{tenant_id}",
    ]

    monkeypatch.setattr(ingress_worker, "tenant_session", _fake_tenant_session_disabled)
    handled_disabled = await ingress_worker._maybe_run_auto_reply(
        cast(Any, object()), cast(Any, _FakeRedis()), tenant_id, conv_id
    )
    assert handled_disabled is False
