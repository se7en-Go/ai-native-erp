"""Wave 4 AI guardrails (DS P2-18, GPT #08).

1. The marketplace off-platform filter no longer false-positives on benign business
   numbers (order / tracking / quote IDs, ISO dates, currency amounts, quantities)
   while every real contact handle — phone, WhatsApp, Telegram, email, URL — stays
   blocked on ``tiktok_shop`` / ``amazon_sp`` / ``ebay``.
2. An AGENT can neither list nor open/review an AI proposal whose conversation lives
   in another agent's private mailbox, while OWNER keeps tenant-wide visibility and
   the AGENT still sees their own plus unassigned-shared proposals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Register domain model modules so SQLAlchemy resolves cross-module string FKs
# (mirrors the api_main composition root).
import erp_core.catalog.models  # noqa: F401
import erp_core.channels.models  # noqa: F401
import erp_core.crm.models  # noqa: F401
import erp_core.finance.models  # noqa: F401
import erp_core.identity.models  # noqa: F401
import erp_core.orders.models  # noqa: F401
import erp_core.tenancy.models  # noqa: F401
from erp_core.ai_engine.compliance_filter import OutboundComplianceFilter
from erp_core.ai_engine.router import router as ai_router
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import ChannelType, UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal
from erp_core.tenancy.security import hash_password

RuntimeFactory = async_sessionmaker[AsyncSession]

MARKETPLACE_CHANNELS = (ChannelType.TIKTOK_SHOP, ChannelType.AMAZON_SP, ChannelType.EBAY)

BENIGN_BUSINESS_MESSAGES = (
    "Order ORD-20261121-001 shipped on 2026-11-21, total 12345678.90 USD for 10000000 pcs",
    "Tracking TRK-2026112199 is in transit and arrives in 3 days.",
    "MOQ 10000000 pcs; proforma PI-20261121-001 and AWB-7788991 attached.",
    "Invoice INV-2026-11-21 settled for 1,234,567.00 CNY.",
)

OFF_PLATFORM_MESSAGES = (
    "Call me on +1 415 555 0199",
    "+86 138 0013 8000 works better for me",
    "Try 415-555-0199 for the warehouse",
    "Add my WhatsApp wa.me/8613800138000",
    "Email sales@factory.com",
    "Visit https://factory.com",
    "Message us on telegram t.me/factory",
)


# --------------------------------------------------------------------------------------
# 1. Compliance filter: benign business numbers allowed, real contacts blocked
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("channel", MARKETPLACE_CHANNELS)
@pytest.mark.parametrize("body", BENIGN_BUSINESS_MESSAGES)
def test_benign_business_numbers_are_allowed(channel: ChannelType, body: str) -> None:
    result = OutboundComplianceFilter().check_outbound(channel, body)
    assert result.allowed, (channel, body, result.violations)
    assert result.violations == []
    assert result.blocked_reason is None


@pytest.mark.parametrize("channel", MARKETPLACE_CHANNELS)
@pytest.mark.parametrize("body", OFF_PLATFORM_MESSAGES)
def test_real_off_platform_contacts_stay_blocked(channel: ChannelType, body: str) -> None:
    result = OutboundComplianceFilter().check_outbound(channel, body)
    assert not result.allowed, (channel, body)
    assert result.blocked_reason == "marketplace_off_platform_contact"
    assert result.violations == ["marketplace_off_platform_contact"]


def test_benign_numbers_and_business_copy_keep_other_guards_intact() -> None:
    """Sanitizing business numbers must not disable the email/URL/handle allowlist."""
    result = OutboundComplianceFilter().check_outbound(
        ChannelType.TIKTOK_SHOP,
        "Order ORD-20261121-001 for 10000000 pcs — reach me on wa.me/8613800138000",
    )
    assert not result.allowed
    assert result.blocked_reason == "marketplace_off_platform_contact"

    # A phone number hidden behind a business amount is still caught.
    hidden = OutboundComplianceFilter().check_outbound(
        ChannelType.AMAZON_SP, "Total 1200 USD, call +1 415 555 0199 now"
    )
    assert not hidden.allowed


# --------------------------------------------------------------------------------------
# 2. AGENT personal-mailbox isolation reaches the AI proposal inbox
# --------------------------------------------------------------------------------------


@dataclass
class Harness:
    app: FastAPI
    current: dict[str, CurrentPrincipal] = field(default_factory=dict)

    def act_as(self, principal: CurrentPrincipal) -> None:
        self.current["principal"] = principal

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")


def build_harness(factory: RuntimeFactory) -> Harness:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.state.redis = None
    app.include_router(ai_router)
    harness = Harness(app=app)

    async def _principal() -> CurrentPrincipal:
        return harness.current["principal"]

    app.dependency_overrides[get_current_principal] = _principal
    return harness


def principal(tenant_id: UUID, user_id: UUID, role: UserRole) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email=f"{role.value}-{user_id.hex[:8]}@wave4.test",
    )


async def create_workspace(factory: RuntimeFactory) -> UUID:
    tenant_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Wave4 Co', :slug)"),
            {"tid": tenant_id, "slug": f"wave4-{tenant_id.hex[:12]}"},
        )
    return tenant_id


async def add_member(factory: RuntimeFactory, tenant_id: UUID, role: UserRole) -> UUID:
    user_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, :name, :ph)"
            ),
            {
                "uid": user_id,
                "email": f"{role.value}-{user_id.hex[:12]}@wave4.test",
                "name": f"{role.value.title()} Person",
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


async def seed_customer(factory: RuntimeFactory, tenant_id: UUID, *, display_name: str) -> UUID:
    customer_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:cid, :tid, 'b2c_consumer', :name)"
            ),
            {"cid": customer_id, "tid": tenant_id, "name": display_name},
        )
    return customer_id


async def seed_account(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    owner_user_id: UUID | None,
    visibility_scope: str,
) -> UUID:
    account_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO channel_accounts (id, tenant_id, channel_type, "
                "external_account_id, display_name, status, owner_user_id, visibility_scope) "
                "VALUES (:aid, :tid, 'email', :ext, :name, 'active', :owner, :scope)"
            ),
            {
                "aid": account_id,
                "tid": tenant_id,
                "ext": f"wave4-{account_id.hex[:12]}@mailbox.test",
                "name": "Wave4 mailbox",
                "owner": owner_user_id,
                "scope": visibility_scope,
            },
        )
    return account_id


async def seed_conversation(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    customer_id: UUID,
    account_id: UUID,
    assigned_user_id: UUID | None,
) -> UUID:
    conversation_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO conversations (id, tenant_id, customer_id, channel_account_id, "
                "channel_type, external_thread_id, conversation_mode, status, "
                "assigned_user_id, unread_count) VALUES "
                "(:cid, :tid, :cust, :aid, 'email', :thread, 'b2c_support', 'open', "
                ":assignee, 1)"
            ),
            {
                "cid": conversation_id,
                "tid": tenant_id,
                "cust": customer_id,
                "aid": account_id,
                "thread": f"thread-{conversation_id.hex[:8]}",
                "assignee": assigned_user_id,
            },
        )
    return conversation_id


async def seed_proposal(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    conversation_id: UUID | None,
    body_text: str,
) -> UUID:
    proposal_id = uuid4()
    payload = f'{{"body_text": "{body_text}"}}'
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO ai_proposals (id, tenant_id, capability, proposal_type, "
                "conversation_id, proposed_payload, confidence_score) VALUES "
                "(:pid, :tid, 'cs_support', 'reply_draft', :cid, CAST(:payload AS jsonb), 0.9)"
            ),
            {
                "pid": proposal_id,
                "tid": tenant_id,
                "cid": conversation_id,
                "payload": payload,
            },
        )
    return proposal_id


async def test_agent_cannot_read_another_agents_private_proposal(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    agent_a_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    agent_b_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)

    shared_account = await seed_account(
        runtime_session_factory, tenant_id, owner_user_id=None, visibility_scope="shared"
    )
    private_b_account = await seed_account(
        runtime_session_factory,
        tenant_id,
        owner_user_id=agent_b_id,
        visibility_scope="personal",
    )
    customer_id = await seed_customer(
        runtime_session_factory, tenant_id, display_name="Proposal Buyer"
    )

    shared_conversation = await seed_conversation(
        runtime_session_factory,
        tenant_id,
        customer_id=customer_id,
        account_id=shared_account,
        assigned_user_id=None,
    )
    private_b_conversation = await seed_conversation(
        runtime_session_factory,
        tenant_id,
        customer_id=customer_id,
        account_id=private_b_account,
        assigned_user_id=agent_b_id,
    )
    owned_by_a_conversation = await seed_conversation(
        runtime_session_factory,
        tenant_id,
        customer_id=customer_id,
        account_id=shared_account,
        assigned_user_id=agent_a_id,
    )

    shared_proposal = await seed_proposal(
        runtime_session_factory,
        tenant_id,
        conversation_id=shared_conversation,
        body_text="Shared draft",
    )
    own_proposal = await seed_proposal(
        runtime_session_factory,
        tenant_id,
        conversation_id=owned_by_a_conversation,
        body_text="Agent A draft",
    )
    orphan_proposal = await seed_proposal(
        runtime_session_factory, tenant_id, conversation_id=None, body_text="No conversation"
    )
    private_b_proposal = await seed_proposal(
        runtime_session_factory,
        tenant_id,
        conversation_id=private_b_conversation,
        body_text="Agent B private draft",
    )
    expected_for_agent_a = {str(shared_proposal), str(own_proposal), str(orphan_proposal)}

    harness = build_harness(runtime_session_factory)
    async with harness.client() as client:
        harness.act_as(principal(tenant_id, agent_a_id, UserRole.AGENT))
        listing = await client.get("/api/v1/ai/proposals?limit=100")
        assert listing.status_code == 200, listing.text
        visible = {row["id"] for row in listing.json()["items"]}
        assert visible == expected_for_agent_a
        assert str(private_b_proposal) not in visible
        assert listing.json()["total"] == 3

        assert (await client.get(f"/api/v1/ai/proposals/{shared_proposal}")).status_code == 200
        hidden_detail = await client.get(f"/api/v1/ai/proposals/{private_b_proposal}")
        assert hidden_detail.status_code == 404, hidden_detail.text
        assert hidden_detail.json()["code"] == "ERR_PROPOSAL_NOT_FOUND"

        # Review endpoints are gated by the same mailbox check.
        assert (
            await client.post(f"/api/v1/ai/proposals/{private_b_proposal}/approve")
        ).status_code == 404
        assert (
            await client.post(
                f"/api/v1/ai/proposals/{private_b_proposal}/reject",
                json={"reason": "not mine"},
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/api/v1/ai/proposals/{private_b_proposal}/edit-approve",
                json={"edited_payload": {"body_text": "hijacked"}},
            )
        ).status_code == 404
        assert (
            await client.post(
                "/api/v1/ai/feedback",
                json={"proposal_id": str(private_b_proposal), "feedback_note": "peek"},
            )
        ).status_code == 404

        # The mailbox owner still sees and can open their own private proposal.
        harness.act_as(principal(tenant_id, agent_b_id, UserRole.AGENT))
        b_listing = await client.get("/api/v1/ai/proposals?limit=100")
        assert b_listing.status_code == 200, b_listing.text
        assert {row["id"] for row in b_listing.json()["items"]} == {
            str(shared_proposal),
            str(orphan_proposal),
            str(private_b_proposal),
        }
        assert (await client.get(f"/api/v1/ai/proposals/{private_b_proposal}")).status_code == 200

        # OWNER keeps tenant-wide visibility of every proposal.
        harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER))
        owner_listing = await client.get("/api/v1/ai/proposals?limit=100")
        assert owner_listing.status_code == 200, owner_listing.text
        assert {row["id"] for row in owner_listing.json()["items"]} == {
            str(shared_proposal),
            str(own_proposal),
            str(orphan_proposal),
            str(private_b_proposal),
        }
        assert owner_listing.json()["total"] == 4
        owner_detail = await client.get(f"/api/v1/ai/proposals/{private_b_proposal}")
        assert owner_detail.status_code == 200, owner_detail.text
        assert owner_detail.json()["conversation_id"] == str(private_b_conversation)
