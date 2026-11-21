"""Wave 2 RBAC hardening (Muse P1-01/P1-02, GPT #08).

Covers three P1 leaks:
1. AGENT cost masking on CRM quotes and orders (landed cost stays Decimal for
   OWNER/FINANCE, is masked to ``None`` for AGENT) plus margin-floor error redaction.
2. Inbox supervision: an AGENT may not flip ``ai_autonomy_override`` nor reassign a
   conversation onto another salesperson; MANAGER/OWNER still can.
3. AGENT personal-mailbox isolation extended to the customer directory (list + detail),
   implemented with tenant-scoped SQL only so ``identity_isolated`` stays intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import redis.asyncio as aioredis
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
from erp_core.channels.inbox_router import inbox_router
from erp_core.crm.quote_service import QuoteService
from erp_core.crm.router import router as crm_router
from erp_core.crm.schemas import QuoteItemInput
from erp_core.identity.router import customer_router
from erp_core.orders.router import router as orders_router
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError, register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal
from erp_core.tenancy.security import hash_password

RuntimeFactory = async_sessionmaker[AsyncSession]

VARIANT_COST = Decimal("42.0000")
VARIANT_FLOOR = Decimal("15.00")


# --------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------


@dataclass
class Harness:
    app: FastAPI
    current: dict[str, CurrentPrincipal] = field(default_factory=dict)

    def act_as(self, principal: CurrentPrincipal) -> None:
        self.current["principal"] = principal

    def client(self) -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test"
        )


def build_harness(
    factory: RuntimeFactory, *, redis: aioredis.Redis | None = None
) -> Harness:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    if redis is not None:
        app.state.redis = redis
    app.include_router(crm_router, prefix="/api/v1")
    app.include_router(orders_router, prefix="/api/v1")
    app.include_router(customer_router)
    app.include_router(inbox_router)
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
        email=f"{role.value}-{user_id.hex[:8]}@wave2.test",
    )


# --------------------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------------------


async def create_workspace(factory: RuntimeFactory) -> UUID:
    tenant_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Wave2 Co', :slug)"),
            {"tid": tenant_id, "slug": f"wave2-{tenant_id.hex[:12]}"},
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
                "email": f"{role.value}-{user_id.hex[:12]}@wave2.test",
                "name": f"{role.value.title()} Person",
                "ph": hash_password("Wave2Pass2026!"),
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


async def seed_variant(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    cost: Decimal = VARIANT_COST,
    floor: Decimal = VARIANT_FLOOR,
) -> UUID:
    product_id, variant_id = uuid4(), uuid4()
    sku = f"LED-{variant_id.hex[:10].upper()}"
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category, origin_country, "
                "i18n_specs, certifications, ai_faq_facts) VALUES "
                "(:pid, :tid, :spu, 'led_lighting', 'CN', "
                "CAST(:specs AS jsonb), ARRAY[]::text[], '{}'::jsonb)"
            ),
            {
                "pid": product_id,
                "tid": tenant_id,
                "spu": f"SPU-{product_id.hex[:10].upper()}",
                "specs": '{"en": {"title": "5M Smart LED Strip 24V"}}',
            },
        )
        await session.execute(
            text(
                "INSERT INTO product_variants (id, tenant_id, product_id, sku, attributes, "
                "cost_price_base, base_currency, min_margin_floor_pct, retail_price_usd, "
                "b2b_moq_tiers, weight_grams) VALUES "
                "(:vid, :tid, :pid, :sku, '{}'::jsonb, :cost, 'CNY', :floor, 18.50, "
                "'[]'::jsonb, 300)"
            ),
            {
                "vid": variant_id,
                "tid": tenant_id,
                "pid": product_id,
                "sku": sku,
                "cost": cost,
                "floor": floor,
            },
        )
    return variant_id


async def seed_customer(
    factory: RuntimeFactory, tenant_id: UUID, *, display_name: str
) -> UUID:
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


async def seed_quote(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    variant_id: UUID,
    owner_user_id: UUID,
) -> UUID:
    customer_id = await seed_customer(factory, tenant_id, display_name="Quote Buyer")
    opportunity_id, quote_id = uuid4(), uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO opportunities (id, tenant_id, customer_id, title, stage, "
                "expected_amount, currency, owner_user_id) VALUES "
                "(:oid, :tid, :cid, 'Wave2 quote opp', 'quoting', 5400.00, 'USD', :owner)"
            ),
            {"oid": opportunity_id, "tid": tenant_id, "cid": customer_id,
             "owner": owner_user_id},
        )
        await session.execute(
            text(
                "INSERT INTO quotes (id, tenant_id, opportunity_id, quote_no, version, "
                "is_proforma_invoice, incoterm, port_of_loading, port_of_destination, "
                "payment_terms, base_currency, quote_currency, fx_rate_snapshot, "
                "fx_locked_until, freight_fee, insurance_fee, customs_duty_estimate, "
                "total_amount, status) VALUES "
                "(:qid, :tid, :oid, :qno, 1, true, 'FOB', 'Xiamen, China', "
                "'Valencia, Spain', '30% T/T deposit', 'CNY', 'USD', 7.200000, :fx_until, "
                "200.00, 30.00, 0.00, 5400.00, 'draft')"
            ),
            {
                "qid": quote_id,
                "tid": tenant_id,
                "oid": opportunity_id,
                "qno": f"PI-W2-{quote_id.hex[:8].upper()}",
                "fx_until": datetime.now(UTC) + timedelta(days=1),
            },
        )
        await session.execute(
            text(
                "INSERT INTO quote_items (id, tenant_id, quote_id, variant_id, sku_snapshot, "
                "description_i18n, quantity, moq_tier_applied, unit_price_quote_ccy, "
                "unit_cost_base_snapshot, line_total_quote_ccy) VALUES "
                "(:iid, :tid, :qid, :vid, 'LED-WAVE2', '5M Smart LED Strip 24V', 500, 500, "
                "10.8000, :cost, 5400.00)"
            ),
            {
                "iid": uuid4(),
                "tid": tenant_id,
                "qid": quote_id,
                "vid": variant_id,
                "cost": VARIANT_COST,
            },
        )
    return quote_id


async def seed_order(
    factory: RuntimeFactory,
    tenant_id: UUID,
    *,
    variant_id: UUID,
    owner_user_id: UUID,
) -> UUID:
    customer_id = await seed_customer(factory, tenant_id, display_name="Order Buyer")
    order_id = uuid4()
    async with tenant_session(factory, tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, "
                "currency, fx_rate_to_base, subtotal_amount, shipping_charged, "
                "discount_amount, tax_collected, total_amount, fulfillment_status, "
                "payment_status, channel_metadata) VALUES "
                "(:oid, :tid, :ono, 'b2b_wholesale', :cid, 'USD', 7.200000, 5400.00, "
                "230.00, 0.00, 0.00, 5630.00, 'pending_payment', 'unpaid', '{}'::jsonb)"
            ),
            {
                "oid": order_id,
                "tid": tenant_id,
                "ono": f"ORD-W2-{order_id.hex[:8].upper()}",
                "cid": customer_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO order_items (id, tenant_id, order_id, variant_id, sku_snapshot, "
                "quantity, unit_price, unit_cost_base_snapshot, line_total) VALUES "
                "(:iid, :tid, :oid, :vid, 'LED-WAVE2', 500, 10.8000, :cost, 5400.00)"
            ),
            {
                "iid": uuid4(),
                "tid": tenant_id,
                "oid": order_id,
                "vid": variant_id,
                "cost": VARIANT_COST,
            },
        )
    return order_id


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
                "ext": f"wave2-{account_id.hex[:12]}@mailbox.test",
                "name": "Wave2 mailbox",
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


# --------------------------------------------------------------------------------------
# 1. AGENT cost masking on quotes and orders
# --------------------------------------------------------------------------------------


async def test_agent_quote_and_order_cost_masking(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    agent_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    owner_id = await add_member(runtime_session_factory, tenant_id, UserRole.OWNER)
    finance_id = await add_member(runtime_session_factory, tenant_id, UserRole.FINANCE)
    variant_id = await seed_variant(runtime_session_factory, tenant_id)
    quote_id = await seed_quote(
        runtime_session_factory, tenant_id, variant_id=variant_id, owner_user_id=owner_id
    )
    order_id = await seed_order(
        runtime_session_factory, tenant_id, variant_id=variant_id, owner_user_id=owner_id
    )

    harness = build_harness(runtime_session_factory)
    async with harness.client() as client:
        harness.act_as(principal(tenant_id, agent_id, UserRole.AGENT))
        quotes = await client.get("/api/v1/b2b/quotes")
        assert quotes.status_code == 200, quotes.text
        agent_quote = next(
            row for row in quotes.json()["items"] if row["id"] == str(quote_id)
        )
        assert [item["unit_cost_base_snapshot"] for item in agent_quote["items"]] == [None]
        # Selling prices stay visible: the AGENT still needs to quote.
        assert agent_quote["items"][0]["unit_price_quote_ccy"] is not None

        orders = await client.get("/api/v1/orders")
        assert orders.status_code == 200, orders.text
        agent_order = next(
            row for row in orders.json()["items"] if row["id"] == str(order_id)
        )
        assert [item["unit_cost_base_snapshot"] for item in agent_order["items"]] == [None]

        detail = await client.get(f"/api/v1/orders/{order_id}")
        assert detail.status_code == 200, detail.text
        assert [i["unit_cost_base_snapshot"] for i in detail.json()["items"]] == [None]

        # OWNER and FINANCE still receive the exact Decimal snapshot (no masking).
        for privileged_id, role in ((owner_id, UserRole.OWNER), (finance_id, UserRole.FINANCE)):
            harness.act_as(principal(tenant_id, privileged_id, role))
            owner_quotes = await client.get("/api/v1/b2b/quotes")
            owner_quote = next(
                row for row in owner_quotes.json()["items"] if row["id"] == str(quote_id)
            )
            assert Decimal(str(owner_quote["items"][0]["unit_cost_base_snapshot"])) == (
                VARIANT_COST
            )
            owner_detail = await client.get(f"/api/v1/orders/{order_id}")
            assert Decimal(
                str(owner_detail.json()["items"][0]["unit_cost_base_snapshot"])
            ) == VARIANT_COST


# --------------------------------------------------------------------------------------
# 2. Margin-floor errors never leak the exact margin/floor to an AGENT
# --------------------------------------------------------------------------------------


class _StubCatalog:
    """Minimal CatalogService stand-in for the pure margin computation."""

    def __init__(self, pricing: Any) -> None:
        self._pricing = pricing

    async def get_variant_pricing_internal(
        self, session: AsyncSession, tenant_id: UUID, variant_id: UUID
    ) -> Any:
        return self._pricing


def _pricing_for(variant_id: UUID) -> Any:
    return SimpleNamespace(
        id=variant_id,
        product_id=uuid4(),
        sku="LED-STRIP-5M-IP65",
        cost_price_base=VARIANT_COST,
        base_currency="CNY",
        min_margin_floor_pct=VARIANT_FLOOR,
        retail_price_usd=Decimal("18.50"),
        b2b_moq_tiers=[],
        i18n_specs={"en": {"title": "5M Smart LED Strip 24V"}},
        hs_code="9405429000",
    )


async def test_margin_floor_breach_detail_redacted_for_agent() -> None:
    tenant_id = uuid4()
    variant_id = uuid4()
    service = QuoteService(
        catalog_service=cast(Any, _StubCatalog(_pricing_for(variant_id)))
    )
    items = [
        QuoteItemInput(
            variant_id=variant_id, quantity=500, unit_price_quote_ccy=Decimal("6.00")
        )
    ]
    # 6.00 USD * 7.2 = 43.20 CNY vs 42.00 cost -> 2.78% margin, below the 15% floor.
    agent = principal(tenant_id, uuid4(), UserRole.AGENT)
    with pytest.raises(AppError) as agent_exc:
        await service._build_quote_items_and_check_margin(
            cast(Any, None), tenant_id, agent, items, Decimal("7.200000"), False
        )
    assert agent_exc.value.status_code == 422
    assert agent_exc.value.code == "ERR_MARGIN_FLOOR_VIOLATION"
    assert "2.78" not in agent_exc.value.detail
    assert "15.00" not in agent_exc.value.detail
    assert "%" not in agent_exc.value.detail

    # Managers keep the actionable numbers so they can reprice the quote.
    manager = principal(tenant_id, uuid4(), UserRole.MANAGER)
    with pytest.raises(AppError) as manager_exc:
        await service._build_quote_items_and_check_margin(
            cast(Any, None), tenant_id, manager, items, Decimal("7.200000"), False
        )
    assert manager_exc.value.code == "ERR_MARGIN_FLOOR_VIOLATION"
    assert "2.78%" in manager_exc.value.detail
    assert "15.00%" in manager_exc.value.detail


# --------------------------------------------------------------------------------------
# 3. Inbox: autonomy override + reassignment stay MANAGER+
# --------------------------------------------------------------------------------------


async def test_agent_cannot_override_autonomy_or_reassign_conversation(
    runtime_session_factory: RuntimeFactory,
) -> None:
    tenant_id = await create_workspace(runtime_session_factory)
    agent_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    other_agent_id = await add_member(runtime_session_factory, tenant_id, UserRole.AGENT)
    manager_id = await add_member(runtime_session_factory, tenant_id, UserRole.MANAGER)
    account_id = await seed_account(
        runtime_session_factory, tenant_id, owner_user_id=None, visibility_scope="shared"
    )
    customer_id = await seed_customer(
        runtime_session_factory, tenant_id, display_name="Inbox Buyer"
    )
    conversation_id = await seed_conversation(
        runtime_session_factory,
        tenant_id,
        customer_id=customer_id,
        account_id=account_id,
        assigned_user_id=None,
    )

    redis = aioredis.from_url("redis://localhost:6379/0")
    harness = build_harness(runtime_session_factory, redis=redis)
    try:
        async with harness.client() as client:
            harness.act_as(principal(tenant_id, agent_id, UserRole.AGENT))

            autonomy = await client.patch(
                f"/api/v1/inbox/conversations/{conversation_id}",
                json={"ai_autonomy_override": "L2_auto"},
            )
            assert autonomy.status_code == 403, autonomy.text
            assert autonomy.json()["code"] == "ERR_INSUFFICIENT_ROLE"

            reassign = await client.patch(
                f"/api/v1/inbox/conversations/{conversation_id}",
                json={"assigned_user_id": str(other_agent_id)},
            )
            assert reassign.status_code == 403, reassign.text
            assert reassign.json()["detail"] == "ERR_CONVERSATION_ACCESS_DENIED"

            # Self-claim and status/mark-read stay available to an AGENT.
            claimed = await client.patch(
                f"/api/v1/inbox/conversations/{conversation_id}",
                json={"assigned_user_id": str(agent_id), "mark_read": True},
            )
            assert claimed.status_code == 200, claimed.text
            assert claimed.json()["assigned_user_id"] == str(agent_id)

            resolved = await client.patch(
                f"/api/v1/inbox/conversations/{conversation_id}",
                json={"status": "resolved"},
            )
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["status"] == "resolved"

            # Nothing leaked: autonomy is still unset and the assignee never moved.
            async with tenant_session(runtime_session_factory, tenant_id) as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT assigned_user_id, ai_autonomy_override FROM conversations "
                            "WHERE tenant_id = :tid AND id = :cid"
                        ),
                        {"tid": tenant_id, "cid": conversation_id},
                    )
                ).one()
            assert row == (agent_id, None)

            # MANAGER may raise autonomy and hand the conversation to another salesperson.
            harness.act_as(principal(tenant_id, manager_id, UserRole.MANAGER))
            manager_patch = await client.patch(
                f"/api/v1/inbox/conversations/{conversation_id}",
                json={
                    "ai_autonomy_override": "L2_auto",
                    "assigned_user_id": str(other_agent_id),
                },
            )
            assert manager_patch.status_code == 200, manager_patch.text
            assert manager_patch.json()["ai_autonomy_override"] == "L2_auto"
            assert manager_patch.json()["assigned_user_id"] == str(other_agent_id)
    finally:
        await redis.aclose()


# --------------------------------------------------------------------------------------
# 4. AGENT personal-mailbox isolation reaches the customer directory
# --------------------------------------------------------------------------------------


async def test_agent_cannot_see_another_agents_private_mailbox_customer(
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

    # (a) customer only ever seen by agent B's personal mailbox
    private_b_customer = await seed_customer(
        runtime_session_factory, tenant_id, display_name="Private B Buyer"
    )
    await seed_conversation(
        runtime_session_factory,
        tenant_id,
        customer_id=private_b_customer,
        account_id=private_b_account,
        assigned_user_id=agent_b_id,
    )
    # (b) shared, unassigned inbox work is visible to every agent
    shared_customer = await seed_customer(
        runtime_session_factory, tenant_id, display_name="Shared Buyer"
    )
    await seed_conversation(
        runtime_session_factory,
        tenant_id,
        customer_id=shared_customer,
        account_id=shared_account,
        assigned_user_id=None,
    )
    # (c) a customer explicitly assigned to agent A
    assigned_customer = await seed_customer(
        runtime_session_factory, tenant_id, display_name="Assigned A Buyer"
    )
    await seed_conversation(
        runtime_session_factory,
        tenant_id,
        customer_id=assigned_customer,
        account_id=shared_account,
        assigned_user_id=agent_a_id,
    )
    # (d) a customer with no conversation at all stays visible (nothing to leak)
    untouched_customer = await seed_customer(
        runtime_session_factory, tenant_id, display_name="Untouched Buyer"
    )

    harness = build_harness(runtime_session_factory)
    async with harness.client() as client:
        harness.act_as(principal(tenant_id, agent_a_id, UserRole.AGENT))
        listing = await client.get("/api/v1/customers?limit=100")
        assert listing.status_code == 200, listing.text
        visible = {row["id"] for row in listing.json()["items"]}
        assert str(private_b_customer) not in visible
        assert visible == {
            str(shared_customer),
            str(assigned_customer),
            str(untouched_customer),
        }
        assert listing.json()["total"] == 3

        hidden_detail = await client.get(f"/api/v1/customers/{private_b_customer}")
        assert hidden_detail.status_code == 404, hidden_detail.text
        allowed_detail = await client.get(f"/api/v1/customers/{shared_customer}")
        assert allowed_detail.status_code == 200, allowed_detail.text
        assert allowed_detail.json()["display_name"] == "Shared Buyer"

        # The personal owner still sees their own customer.
        harness.act_as(principal(tenant_id, agent_b_id, UserRole.AGENT))
        own = await client.get(f"/api/v1/customers/{private_b_customer}")
        assert own.status_code == 200, own.text

        # OWNER has tenant-wide visibility, including the private mailbox customer.
        harness.act_as(principal(tenant_id, owner_id, UserRole.OWNER))
        owner_list = await client.get("/api/v1/customers?limit=100")
        assert owner_list.status_code == 200, owner_list.text
        assert {row["id"] for row in owner_list.json()["items"]} == {
            str(private_b_customer),
            str(shared_customer),
            str(assigned_customer),
            str(untouched_customer),
        }
        assert (await client.get(f"/api/v1/customers/{private_b_customer}")).status_code == 200
