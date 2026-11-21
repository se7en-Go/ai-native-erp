"""Wave 4 P1 fixes: paired price/fact numeric guard, multilingual redlines, per-turn
idempotency of the grounded auto-reply, and AGENT mailbox isolation on the AI profile read.

The guardrail/idempotency tests are deterministic and offline (I/O seams stubbed); the RBAC
test runs against real PostgreSQL 17 with RLS and the same tenant-scoped SQL as production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.auto_reply_router import router as auto_reply_router
from erp_core.ai_engine.auto_reply_service import (
    AutoReplyDispatchBundle,
    AutoReplyRunRequest,
    AutoReplyService,
    detect_risk_flags,
    verify_reply_numbers_grounded,
)
from erp_core.ai_engine.proposal_generator import ConversationContext, RecentMessage
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal
from erp_core.tenancy.security import hash_password

_DUMMY_SESSION = cast(AsyncSession, object())
RuntimeFactory = async_sessionmaker[AsyncSession]


# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------


def _principal(tenant_id: UUID, user_id: UUID, role: UserRole) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email=f"{role.value}-{user_id.hex[:8]}@wave4.test",
    )


def _conversation(*, channel_type: str = "email") -> ConversationContext:
    return ConversationContext(
        id=uuid4(),
        customer_id=uuid4(),
        channel_type=channel_type,
        assigned_user_id=None,
        visibility_scope="shared",
        owner_user_id=None,
    )


def _sku_evidence(
    *,
    sku: str = "LED-COB-24V-5M",
    unit_price: str = "12.00",
    qty: int = 1000,
    available: int = 4300,
    lead_time: int = 7,
    tier: int | None = 1000,
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


# --------------------------------------------------------------------------------------
# 1. Paired price/fact numeric guard
# --------------------------------------------------------------------------------------


def test_numeric_guard_rejects_stock_count_rendered_as_a_price() -> None:
    """stock=100 + unit_price=2.00 must never licence 'USD 100/pc'."""
    evidence = [_sku_evidence(unit_price="2.00", qty=100, available=100, lead_time=7, tier=100)]

    assert not verify_reply_numbers_grounded(
        "Confirmed: 100 pcs are available at USD 100/pc with a 7-day lead time.",
        evidence=evidence,
        kb_hits=[],
        buyer_message="How much for 100 pcs? And what is your stock?",
    )
    # Same facts, correctly paired -> accepted.
    assert verify_reply_numbers_grounded(
        "Confirmed: 100 pcs are available at USD 2.00/pc with a 7-day lead time.",
        evidence=evidence,
        kb_hits=[],
        buyer_message="How much for 100 pcs? And what is your stock?",
    )


def test_numeric_guard_rejects_buyer_counter_offer_price() -> None:
    """A buyer's own counter-offer (``$.50``/``$8.50``) is never a verified price."""
    evidence = [_sku_evidence(unit_price="12.00")]

    assert not verify_reply_numbers_grounded(
        "Your $.50/pc counter-offer is below our verified USD 12.00/pc price.",
        evidence=evidence,
        kb_hits=[],
        buyer_message="Your $.50 price is too high, can you do better?",
    )
    assert not verify_reply_numbers_grounded(
        "We could match your $8.50/pc target.",
        evidence=evidence,
        kb_hits=[],
        buyer_message="Our target is $8.50 per pc for 1000 pcs.",
    )
    assert verify_reply_numbers_grounded(
        "For 1,000 pcs, USD 12.00/pc is our current FOB price.",
        evidence=evidence,
        kb_hits=[],
        buyer_message="Our target is $8.50 per pc for 1000 pcs.",
    )


def test_numeric_guard_accepts_grounded_quote_and_stock() -> None:
    evidence = [
        _sku_evidence(sku="LED-COB-24V-5M", unit_price="12.00", qty=1000, available=4300)
    ]
    assert verify_reply_numbers_grounded(
        "Yes — USD 12.00/pc with 4,300 pcs in stock, FOB, 7-day lead time.",
        evidence=evidence,
        kb_hits=[],
        buyer_message="Do you have LED-COB-24V-5M in stock? Price for 1000 pcs?",
    )


def test_numeric_guard_reads_every_explicit_price_field() -> None:
    """``line_total_usd`` / ``retail_price_usd`` / MOQ-tier prices are all verified prices."""
    evidence = [
        {
            "tool_name": "get_live_quote",
            "data": {
                "line_total_usd": "24000.00",
                "retail_price_usd": "19.90",
                "b2b_moq_tiers": [
                    {"min_qty": 500, "unit_price_usd": "13.20"},
                    {"min_qty": 1000, "unit_price_usd": "12.00"},
                ],
            },
        }
    ]
    assert verify_reply_numbers_grounded(
        "Your 500-pc tier is USD 13.20/pc; the 1,000-pc tier is USD 12.00/pc.",
        evidence=evidence,
        kb_hits=[],
        buyer_message="What are your MOQ tiers?",
    )
    assert not verify_reply_numbers_grounded(
        "Your 500-pc tier is USD 9.90/pc today.",
        evidence=evidence,
        kb_hits=[],
        buyer_message="What are your MOQ tiers?",
    )


# --------------------------------------------------------------------------------------
# 2. Multilingual redlines + negation context
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_flag"),
    [
        ("Necesito un reembolso y disputa", "refund"),
        ("Necesito un reembolso y disputa", "dispute"),
        ("Voy a abrir un contracargo con mi banco", "chargeback"),
        ("Queremos una compensación por la demora", "compensation"),
        ("Exigimos una indemnización por el defecto", "compensation"),
        ("Necesito la devolución del lote completo", "goods_return"),
        ("Necesitamos un descuento especial del 20%", "price_override"),
        ("Puedes bajar el precio un poco mas?", "price_override"),
        ("Por favor cambiar de banco para el proximo pago", "bank_account_change"),
        ("Envieme los datos de su cuenta bancaria", "bank_account_change"),
        ("Pagamos por transferencia bancaria ayer", "wire_transfer"),
        ("Quiero presentar un reclamo legal", "dispute"),
    ],
)
def test_spanish_redlines_are_detected(message: str, expected_flag: str) -> None:
    assert expected_flag in detect_risk_flags(message)


@pytest.mark.parametrize(
    "message",
    [
        "All good, no refund needed, please quote 1000 pcs",
        "We don't need a refund, the goods arrived fine",
        "We are not asking for a refund, just a status update",
        "It's fine, no dispute needed at all",
        "无需退款，货物已收到，请报 1000 pcs 的价格",
        "不需要退款，只需要发货时间",
        "不用退款了，谢谢",
    ],
)
def test_negated_refund_and_dispute_are_not_flagged(message: str) -> None:
    assert detect_risk_flags(message) == []


def test_negation_does_not_suppress_a_later_real_redline() -> None:
    flags = detect_risk_flags("No refund was needed for lot 1. Now we need a refund for lot 2.")
    assert flags == ["refund"]


def test_negation_keeps_non_negatable_redlines_absolute() -> None:
    flags = detect_risk_flags("Please send a wire transfer and we may file a chargeback")
    assert "wire_transfer" in flags
    assert "chargeback" in flags


# --------------------------------------------------------------------------------------
# 3. Per-inbound-message turn idempotency
# --------------------------------------------------------------------------------------


class _IdempotentAutoReplyService(AutoReplyService):
    """In-memory double: every stored outbound reply is indexed by inbound message id."""

    def __init__(
        self,
        *,
        conversation: ConversationContext,
        messages: Sequence[RecentMessage],
        evidence: Sequence[dict[str, Any]],
    ) -> None:
        super().__init__(enable_llm_polish=False)
        self._conversation = conversation
        self._messages = list(messages)
        self._evidence = list(evidence)
        self.sent: list[dict[str, Any]] = []
        self.dispatches: list[AutoReplyDispatchBundle] = []
        self.audits: list[str] = []

    async def load_conversation(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> ConversationContext:
        return self._conversation

    async def _load_auto_reply_flag(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> bool:
        return True

    async def _recent_messages(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> list[RecentMessage]:
        return list(self._messages)

    async def _customer_summary(
        self, session: AsyncSession, tenant_id: UUID, customer_id: UUID
    ) -> dict[str, str]:
        return {"name": "Marcus", "company": "Lichttechnik"}

    async def _search_kb(
        self, session: AsyncSession, tenant_id: UUID, query: str, top_k: int
    ) -> list[dict[str, Any]]:
        return []

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
        self.sent.append({"id": message_id, "body_text": body_text, "metadata": metadata})
        return message_id

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
        bundle = AutoReplyDispatchBundle(
            tenant_id=tenant_id,
            outbound_payload={"message_id": str(message_id), "body_text": body_text},
            sse_event={"event": "message.created", "message_id": str(message_id)},
        )
        self.dispatches.append(bundle)
        return bundle

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

    async def _find_existing_reply_for_inbound(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        conversation_id: UUID,
        source_message_id: UUID | None,
    ) -> tuple[UUID, str] | None:
        if source_message_id is None:
            return None
        for row in self.sent:
            if row["metadata"].get("in_reply_to_message_id") == str(source_message_id):
                return cast(UUID, row["id"]), str(row["body_text"])
        return None

    async def _latest_inbound_message_id(
        self, session: AsyncSession, tenant_id: UUID, conversation_id: UUID
    ) -> UUID | None:
        return None


@pytest.mark.asyncio
async def test_run_with_dispatch_is_idempotent_per_inbound_message() -> None:
    conversation = _conversation(channel_type="telegram")
    inbound_id = uuid4()
    service = _IdempotentAutoReplyService(
        conversation=conversation,
        messages=[
            RecentMessage(
                "inbound",
                "customer",
                "Do you have LED-COB-24V-5M in stock? Price for 1000 pcs?",
            )
        ],
        evidence=[_sku_evidence()],
    )
    principal = _principal(uuid4(), uuid4(), UserRole.MANAGER)
    request = AutoReplyRunRequest(mode="auto")

    first, first_bundle = await service.run_with_dispatch(
        _DUMMY_SESSION,
        principal,
        conversation.id,
        request,
        source_message_id=inbound_id,
    )
    assert first.action == "auto_replied"
    assert first.message_id is not None
    assert first_bundle is not None
    assert len(service.sent) == 1
    assert service.sent[0]["metadata"]["in_reply_to_message_id"] == str(inbound_id)

    second, second_bundle = await service.run_with_dispatch(
        _DUMMY_SESSION,
        principal,
        conversation.id,
        request,
        source_message_id=inbound_id,
    )

    # Replay: same stored reply, no duplicate outbound row, no second dispatch bundle.
    assert second.action == "auto_replied"
    assert second.message_id == first.message_id
    assert second.reply_text == first.reply_text
    assert second_bundle is None
    assert len(service.sent) == 1
    assert len(service.dispatches) == 1
    assert service.audits.count("ai.auto_reply.sent") == 1

    # A *different* inbound message is a new turn and does produce a new reply.
    third, third_bundle = await service.run_with_dispatch(
        _DUMMY_SESSION,
        principal,
        conversation.id,
        request,
        source_message_id=uuid4(),
    )
    assert third_bundle is not None
    assert third.message_id != first.message_id
    assert len(service.sent) == 2


@pytest.mark.asyncio
async def test_run_without_source_message_id_still_replies() -> None:
    """Backwards compatible: no turn id means no de-duplication attempt."""
    conversation = _conversation()
    service = _IdempotentAutoReplyService(
        conversation=conversation,
        messages=[RecentMessage("inbound", "customer", "ok thx")],
        evidence=[],
    )
    principal = _principal(uuid4(), uuid4(), UserRole.MANAGER)

    response, bundle = await service.run_with_dispatch(
        _DUMMY_SESSION, principal, conversation.id, AutoReplyRunRequest(mode="auto")
    )
    assert response.action == "auto_replied"
    assert bundle is not None
    assert "in_reply_to_message_id" not in service.sent[0]["metadata"]


@pytest.mark.asyncio
async def test_ingress_worker_passes_inbound_message_id_to_auto_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from erp_core.ai_engine.auto_reply_service import AutoReplyRunResponse
    from erp_core.channels import ingress_worker

    tenant_id, conv_id, msg_id = uuid4(), uuid4(), uuid4()
    captured: dict[str, Any] = {}

    class _FakeSession:
        async def scalar(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

    class _FakeRedis:
        async def xadd(self, *_args: Any, **_kwargs: Any) -> str:
            return "1-0"

        async def publish(self, *_args: Any, **_kwargs: Any) -> int:
            return 1

    class _FakeService:
        async def run_with_dispatch(self, *_args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return (
                AutoReplyRunResponse(
                    conversation_id=conv_id,
                    action="auto_replied",
                    message_id=msg_id,
                    reply_text="ok",
                    confidence=Decimal("0.850"),
                ),
                None,
            )

    @asynccontextmanager
    async def _fake_tenant_session(_factory: Any, _tid: Any) -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(ingress_worker, "_auto_reply_service", lambda: _FakeService())
    monkeypatch.setattr(ingress_worker, "tenant_session", _fake_tenant_session)

    handled = await ingress_worker._maybe_run_auto_reply(
        cast(Any, object()),
        cast(Any, _FakeRedis()),
        tenant_id,
        conv_id,
        source_message_id=msg_id,
    )

    assert handled is True
    assert captured["source_message_id"] == msg_id


# --------------------------------------------------------------------------------------
# 4. AGENT visibility on GET /api/v1/ai/customers/{customer_id}/profile (real PG17)
# --------------------------------------------------------------------------------------


async def _seed_tenant(factory: RuntimeFactory) -> UUID:
    tenant_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Wave4 AI Co', :slug)"),
            {"tid": tenant_id, "slug": f"wave4-ai-{tenant_id.hex[:12]}"},
        )
    return tenant_id


async def _seed_user(factory: RuntimeFactory, tenant_id: UUID, role: UserRole) -> UUID:
    user_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, :name, :ph)"
            ),
            {
                "uid": user_id,
                "email": f"wave4-{role.value}-{user_id.hex[:12]}@factory.test",
                "name": f"Wave4 {role.value.title()}",
                "ph": hash_password("Wave4Pass2026!"),
            },
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:mid, :tid, :uid, :role)"
            ),
            {"mid": uuid4(), "tid": tenant_id, "uid": user_id, "role": role.value},
        )
    return user_id


async def _seed_customer_conversation(
    factory: RuntimeFactory,
    tenant_id: UUID,
    owner_user_id: UUID | None,
    *,
    visibility_scope: str,
    assignee_user_id: UUID | None,
    display_name: str,
) -> tuple[UUID, UUID, UUID]:
    """Customer whose only conversation lives on a personal or shared channel account."""
    account_id, customer_id, conv_id = uuid4(), uuid4(), uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name, status, owner_user_id, visibility_scope) "
                "VALUES (:id, :tid, 'telegram', :ext, :name, 'active', :owner, :scope)"
            ),
            {
                "id": account_id,
                "tid": tenant_id,
                "ext": f"acct-{account_id.hex[:12]}",
                "name": f"{display_name} account",
                "owner": owner_user_id,
                "scope": visibility_scope,
            },
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:cid, :tid, 'b2b_contact', :name)"
            ),
            {"cid": customer_id, "tid": tenant_id, "name": display_name},
        )
        await session.execute(
            text(
                "INSERT INTO conversations (id, tenant_id, customer_id, channel_account_id, "
                "channel_type, external_thread_id, conversation_mode, status, assigned_user_id, "
                "unread_count) VALUES (:id, :tid, :cid, :aid, 'telegram', :thread, "
                "'b2b_inquiry', 'open', :assignee, 1)"
            ),
            {
                "id": conv_id,
                "tid": tenant_id,
                "cid": customer_id,
                "aid": account_id,
                "thread": f"thread-{conv_id.hex[:8]}",
                "assignee": assignee_user_id,
            },
        )
    return account_id, customer_id, conv_id


def _ai_app(factory: RuntimeFactory, principal: CurrentPrincipal) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.include_router(auto_reply_router)

    async def _override_principal() -> CurrentPrincipal:
        return principal

    app.dependency_overrides[get_current_principal] = _override_principal
    return app


async def _cleanup(
    factory: RuntimeFactory,
    tenant_id: UUID,
    user_ids: Sequence[UUID],
) -> None:
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text("DELETE FROM conversations WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
        await session.execute(
            text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
        await session.execute(
            text("DELETE FROM channel_accounts WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
        await session.execute(
            text("DELETE FROM tenant_memberships WHERE tenant_id = :tid"), {"tid": tenant_id}
        )
        await session.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant_id})
    async with tenant_session(factory, uuid4()) as session:
        for user_id in user_ids:
            await session.execute(
                text("DELETE FROM users WHERE id = :uid"), {"uid": user_id}
            )


@pytest.mark.asyncio
async def test_agent_cannot_read_ai_profile_of_another_agents_private_customer(
    runtime_session_factory: RuntimeFactory,
) -> None:
    factory = runtime_session_factory
    tenant_id = await _seed_tenant(factory)
    owner_id = await _seed_user(factory, tenant_id, UserRole.OWNER)
    agent_a = await _seed_user(factory, tenant_id, UserRole.AGENT)
    agent_b = await _seed_user(factory, tenant_id, UserRole.AGENT)
    # agent_b's personal mailbox: invisible to agent_a.
    _, private_customer, _ = await _seed_customer_conversation(
        factory,
        tenant_id,
        agent_b,
        visibility_scope="personal",
        assignee_user_id=agent_b,
        display_name="Private Buyer",
    )
    # Company (shared) mailbox: workable by any unassigned agent.
    _, shared_customer, _ = await _seed_customer_conversation(
        factory,
        tenant_id,
        None,
        visibility_scope="shared",
        assignee_user_id=None,
        display_name="Shared Buyer",
    )
    user_ids = [owner_id, agent_a, agent_b]

    try:
        agent_app = _ai_app(factory, _principal(tenant_id, agent_a, UserRole.AGENT))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=agent_app), base_url="http://test"
        ) as agent_client:
            denied = await agent_client.get(f"/api/v1/ai/customers/{private_customer}/profile")
            assert denied.status_code == 403
            assert denied.json()["code"] == "ERR_CONVERSATION_ACCESS_DENIED"
            # The rule discriminates: a shared-mailbox customer stays readable.
            shared = await agent_client.get(f"/api/v1/ai/customers/{shared_customer}/profile")
            assert shared.status_code == 200

        # The private mailbox owner itself can still read the profile.
        owner_agent_app = _ai_app(factory, _principal(tenant_id, agent_b, UserRole.AGENT))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=owner_agent_app), base_url="http://test"
        ) as agent_b_client:
            own = await agent_b_client.get(f"/api/v1/ai/customers/{private_customer}/profile")
        assert own.status_code == 200
        assert own.json()["customer_id"] == str(private_customer)

        # A tenant OWNER is never mailbox-restricted.
        owner_app = _ai_app(factory, _principal(tenant_id, owner_id, UserRole.OWNER))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=owner_app), base_url="http://test"
        ) as owner_client:
            allowed = await owner_client.get(
                f"/api/v1/ai/customers/{private_customer}/profile"
            )
        assert allowed.status_code == 200
        assert allowed.json()["customer_id"] == str(private_customer)
    finally:
        await _cleanup(factory, tenant_id, user_ids)


@pytest.mark.asyncio
async def test_find_existing_reply_for_inbound_reads_channel_metadata(
    runtime_session_factory: RuntimeFactory,
) -> None:
    """The idempotency lookup is real tenant-scoped SQL over ``channel_metadata``."""
    factory = runtime_session_factory
    tenant_id = await _seed_tenant(factory)
    owner_id = await _seed_user(factory, tenant_id, UserRole.OWNER)
    _, customer_id, conv_id = await _seed_customer_conversation(
        factory,
        tenant_id,
        None,
        visibility_scope="shared",
        assignee_user_id=None,
        display_name="Metadata Buyer",
    )
    conversation = ConversationContext(
        id=conv_id,
        customer_id=customer_id,
        channel_type="telegram",
        assigned_user_id=None,
        visibility_scope="shared",
        owner_user_id=None,
    )
    inbound_id, other_inbound_id = uuid4(), uuid4()
    service = AutoReplyService()

    try:
        async with tenant_session(factory, tenant_id) as session:
            stored_id = await service._insert_outbound_message(
                session,
                tenant_id=tenant_id,
                conversation=conversation,
                body_text="Grounded reply for this turn.",
                metadata={"in_reply_to_message_id": str(inbound_id)},
            )

        async with tenant_session(factory, tenant_id) as session:
            assert await service._find_existing_reply_for_inbound(
                session, tenant_id, conv_id, inbound_id
            ) == (stored_id, "Grounded reply for this turn.")
            assert (
                await service._find_existing_reply_for_inbound(
                    session, tenant_id, conv_id, other_inbound_id
                )
                is None
            )
            assert (
                await service._find_existing_reply_for_inbound(session, tenant_id, conv_id, None)
                is None
            )
            assert (
                await service._find_existing_reply_for_inbound(
                    session, tenant_id, uuid4(), inbound_id
                )
                is None
            )
    finally:
        await _cleanup(factory, tenant_id, [owner_id])
