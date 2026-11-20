"""Pack D: live operations patrol, twelve export AI employees, custom employees, playbooks.

Everything asserted here runs against real PostgreSQL with FORCE RLS, real tenant-scoped
SQL and the real routers. No LLM provider is configured, so any AI path would fail closed;
the patrol itself is deterministic SQL and needs no provider.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.capabilities import CAPABILITY_SPECS, min_autonomy
from erp_core.ai_engine.workforce_router import workforce_router
from erp_core.ai_engine.workforce_service import (
    CAPABILITY_TOOLS,
    DEFAULT_EXPORT_EMPLOYEES,
    export_employee,
)
from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import AiCapability, UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.social.playbooks import PLAYBOOKS
from erp_core.social.router import router as social_router
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
AI_BASE = "/api/v1/ai"
SOCIAL_BASE = "/api/v1/social"
LEGACY_EMPLOYEE_CODES = {
    "b2b-sales",
    "b2b-order-desk",
    "logistics-forwarder",
    "growth-scout",
    "b2c-cs",
    "finance-assistant",
    "supply-planner",
    "biz-analyst",
    "content-studio",
}


def _app(factory: Factory, principal: CurrentPrincipal) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.state.crypto_service = EnvelopeCryptoService(get_settings())
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(workforce_router)
    app.include_router(social_router, prefix="/api/v1")
    return app


def client_for(factory: Factory, principal: CurrentPrincipal) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app(factory, principal)), base_url="http://t")


@pytest.fixture
async def workspace(runtime_session_factory: Factory):
    tenant, other = uuid4(), uuid4()
    owner, manager, agent, finance, outsider = (uuid4() for _ in range(5))
    for tid, slug in ((tenant, f"packd-{tenant.hex}"), (other, f"packd-{other.hex}")):
        async with tenant_session(runtime_session_factory, tid) as session:
            await session.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Pack D')"),
                {"id": tid, "slug": slug},
            )
    async with tenant_session(runtime_session_factory, tenant) as session:
        for uid, role in (
            (owner, UserRole.OWNER),
            (manager, UserRole.MANAGER),
            (agent, UserRole.AGENT),
            (finance, UserRole.FINANCE),
        ):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:uid, :email, 'Pack D', 'x')"
                ),
                {"uid": uid, "email": f"{uid.hex}@example.com"},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (:tid, :uid, :role)"
                ),
                {"tid": tenant, "uid": uid, "role": role.value},
            )
    async with tenant_session(runtime_session_factory, other) as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, 'Outsider', 'x')"
            ),
            {"uid": outsider, "email": f"{outsider.hex}@example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                "VALUES (:tid, :uid, 'owner')"
            ),
            {"tid": other, "uid": outsider},
        )

    def principal(user_id: UUID, role: UserRole, tenant_id: UUID = tenant) -> CurrentPrincipal:
        return CurrentPrincipal(
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            email=f"{user_id.hex}@example.com",
        )

    return {
        "tenant": tenant,
        "other": other,
        "owner": principal(owner, UserRole.OWNER),
        "manager": principal(manager, UserRole.MANAGER),
        "agent": principal(agent, UserRole.AGENT),
        "finance": principal(finance, UserRole.FINANCE),
        "outsider": principal(outsider, UserRole.OWNER, other),
        "owner_id": owner,
        "agent_id": agent,
        "factory": runtime_session_factory,
    }


async def _seed_customer(factory: Factory, tenant: UUID, name: str = "Buyer") -> UUID:
    customer = uuid4()
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("INSERT INTO customers (id, tenant_id, display_name) VALUES (:cid, :tid, :name)"),
            {"cid": customer, "tid": tenant, "name": name},
        )
    return customer


async def _seed_opportunity(
    factory: Factory, tenant: UUID, customer: UUID, owner_id: UUID
) -> UUID:
    opportunity = uuid4()
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO opportunities (id, tenant_id, customer_id, title, stage,
                    owner_user_id)
                VALUES (:oid, :tid, :cid, 'RFQ LED strip', 'quoting', :uid)
            """),
            {"oid": opportunity, "tid": tenant, "cid": customer, "uid": owner_id},
        )
    return opportunity


async def _seed_quote(
    factory: Factory,
    tenant: UUID,
    opportunity: UUID,
    *,
    quote_no: str,
    status: str,
    hours_to_lock_expiry: float,
    total_amount: str,
) -> UUID:
    quote = uuid4()
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO quotes (id, tenant_id, opportunity_id, quote_no, incoterm,
                    payment_terms, base_currency, quote_currency, fx_rate_snapshot,
                    fx_locked_until, total_amount, status)
                VALUES (:qid, :tid, :oid, :no, 'FOB', 'T/T 30% deposit', 'CNY', 'USD', 7.1,
                    :locked, :total, :status)
            """),
            {
                "qid": quote,
                "tid": tenant,
                "oid": opportunity,
                "no": quote_no,
                "locked": datetime.now(UTC) + timedelta(hours=hours_to_lock_expiry),
                "total": total_amount,
                "status": status,
            },
        )
    return quote


async def _seed_variant(
    factory: Factory,
    tenant: UUID,
    *,
    sku: str,
    stock: list[tuple[int, int]],
    safety_stock_qty: int | None = None,
) -> UUID:
    product, variant = uuid4(), uuid4()
    attributes = {} if safety_stock_qty is None else {"safety_stock_qty": str(safety_stock_qty)}
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:pid, :tid, :spu, 'LED strips')"
            ),
            {"pid": product, "tid": tenant, "spu": f"SPU-{uuid4().hex[:8]}"},
        )
        await session.execute(
            text("""
                INSERT INTO product_variants (id, tenant_id, product_id, sku, cost_price_base,
                    base_currency, retail_price_usd, weight_grams, lead_time_days, attributes)
                VALUES (:vid, :tid, :pid, :sku, 5, 'USD', 29.90, 400, 7, CAST(:attrs AS jsonb))
            """),
            {"vid": variant, "tid": tenant, "pid": product, "sku": sku,
             "attrs": json.dumps(attributes)},
        )
        for on_hand, reserved in stock:
            await session.execute(
                text("""
                    INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id,
                        on_hand_qty, reserved_qty)
                    VALUES (:tid, :wid, :vid, :on_hand, :reserved)
                """),
                {
                    "tid": tenant,
                    "wid": uuid4(),
                    "vid": variant,
                    "on_hand": on_hand,
                    "reserved": reserved,
                },
            )
    return variant


async def _seed_order(
    factory: Factory,
    tenant: UUID,
    customer: UUID,
    *,
    order_no: str,
    fulfillment_status: str = "shipped",
) -> UUID:
    order = uuid4()
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, currency,
                    fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status,
                    payment_status)
                VALUES (:oid, :tid, :no, 'b2b_wholesale', :cid, 'USD', 7.1, 1000, 1000,
                    :status, 'unpaid')
            """),
            {
                "oid": order,
                "tid": tenant,
                "no": order_no,
                "cid": customer,
                "status": fulfillment_status,
            },
        )
    return order


async def _seed_shipment(
    factory: Factory,
    tenant: UUID,
    order: UUID,
    *,
    tracking_number: str,
    standardized_status: str,
    hours_since_event: float | None,
) -> UUID:
    shipment = uuid4()
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO shipments (id, tenant_id, order_id, tracking_provider,
                    tracking_number, carrier_code, standardized_status, last_event_at)
                VALUES (:sid, :tid, :oid, '17track', :tn, 'DHL', :status, :last_event)
            """),
            {
                "sid": shipment,
                "tid": tenant,
                "oid": order,
                "tn": tracking_number,
                "status": standardized_status,
                "last_event": (
                    datetime.now(UTC) - timedelta(hours=hours_since_event)
                    if hours_since_event is not None
                    else None
                ),
            },
        )
    return shipment


async def _seed_receivable(
    factory: Factory,
    tenant: UUID,
    order: UUID,
    *,
    label: str,
    status: str,
    direction: str = "receivable",
) -> UUID:
    rp = uuid4()
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO receivables_payables (id, tenant_id, order_id, direction,
                    milestone_label, due_amount, currency, status, due_date)
                VALUES (:rid, :tid, :oid, :direction, :label, 1000, 'USD', :status,
                    (now() + interval '10 days')::date)
            """),
            {
                "rid": rp,
                "tid": tenant,
                "oid": order,
                "direction": direction,
                "label": label,
                "status": status,
            },
        )
    return rp


async def _seed_proposal(factory: Factory, tenant: UUID, *, status: str, kind: str) -> None:
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO ai_proposals (tenant_id, capability, proposal_type, proposed_payload,
                    confidence_score, status, expires_at)
                VALUES (:tid, 'sales_quoting', :kind, CAST(:payload AS jsonb), 0.9, :status,
                    now() + interval '7 days')
            """),
            {
                "tid": tenant,
                "kind": kind,
                "status": status,
                "payload": json.dumps({"body_text": "draft"}),
            },
        )


async def _seed_channel_account(factory: Factory, tenant: UUID) -> UUID:
    account = uuid4()
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO channel_accounts (id, tenant_id, channel_type, external_account_id,
                    display_name, status)
                VALUES (:aid, :tid, 'email', :external, :external, 'active')
            """),
            {"aid": account, "tid": tenant, "external": f"{tenant.hex}@inbound.example.com"},
        )
    return account


async def _seed_conversation(
    factory: Factory,
    tenant: UUID,
    *,
    customer: UUID,
    account: UUID,
    subject: str,
    unread: int,
    assigned_user_id: UUID | None,
    status: str = "open",
) -> UUID:
    conversation = uuid4()
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO conversations (id, tenant_id, customer_id, channel_account_id,
                    channel_type, conversation_mode, status, subject, unread_count,
                    assigned_user_id)
                VALUES (:cid, :tid, :cust, :aid, 'email', 'b2b_inquiry', :status, :subject,
                    :unread, :assigned)
            """),
            {
                "cid": conversation,
                "tid": tenant,
                "cust": customer,
                "aid": account,
                "status": status,
                "subject": subject,
                "unread": unread,
                "assigned": assigned_user_id,
            },
        )
    return conversation


async def _seed_patrol_fixture(workspace) -> dict[str, str]:
    """Real rows whose counts the patrol must report exactly."""
    factory = workspace["factory"]
    tenant = workspace["tenant"]
    customer = await _seed_customer(factory, tenant)
    opportunity = await _seed_opportunity(factory, tenant, customer, workspace["owner_id"])
    await _seed_quote(
        factory, tenant, opportunity, quote_no="Q-EXP-1", status="sent",
        hours_to_lock_expiry=24, total_amount="1234.50",
    )
    await _seed_quote(
        factory, tenant, opportunity, quote_no="Q-EXP-2", status="draft",
        hours_to_lock_expiry=48, total_amount="500.00",
    )
    await _seed_quote(
        factory, tenant, opportunity, quote_no="Q-SAFE-3", status="draft",
        hours_to_lock_expiry=240, total_amount="900.00",
    )
    await _seed_quote(
        factory, tenant, opportunity, quote_no="Q-ACCEPTED-4", status="accepted",
        hours_to_lock_expiry=12, total_amount="300.00",
    )
    # 8 available (default 50 safety stock) -> low.
    await _seed_variant(factory, tenant, sku="LED-COB-5M", stock=[(10, 2)])
    # 20 available vs its own 5-unit safety stock -> healthy.
    await _seed_variant(factory, tenant, sku="LED-PANEL-60", stock=[(20, 0)],
                        safety_stock_qty=5)
    # 30 available vs its own 40-unit safety stock -> low.
    await _seed_variant(factory, tenant, sku="LED-FLOOD-100", stock=[(30, 0)],
                        safety_stock_qty=40)
    # 11 available across two warehouses vs 10 safety stock -> healthy (proves aggregation).
    await _seed_variant(factory, tenant, sku="LED-TRACK-2M", stock=[(6, 0), (5, 0)],
                        safety_stock_qty=10)
    order = await _seed_order(factory, tenant, customer, order_no="ORD-PACKD-1")
    await _seed_shipment(
        factory, tenant, order, tracking_number="TRK-ALERT-1",
        standardized_status="alert", hours_since_event=1,
    )
    await _seed_shipment(
        factory, tenant, order, tracking_number="TRK-STALL-2",
        standardized_status="in_transit", hours_since_event=100,
    )
    await _seed_shipment(
        factory, tenant, order, tracking_number="TRK-FRESH-3",
        standardized_status="in_transit", hours_since_event=10,
    )
    await _seed_shipment(
        factory, tenant, order, tracking_number="TRK-DONE-4",
        standardized_status="delivered", hours_since_event=400,
    )
    exception_order = await _seed_order(
        factory, tenant, customer, order_no="ORD-PACKD-EXC", fulfillment_status="exception"
    )
    await _seed_receivable(factory, tenant, exception_order, label="30% deposit", status="open")
    await _seed_receivable(factory, tenant, exception_order, label="70% balance", status="partial")
    await _seed_receivable(factory, tenant, exception_order, label="settled", status="settled")
    await _seed_receivable(
        factory, tenant, exception_order, label="supplier payable", status="open",
        direction="payable",
    )
    await _seed_proposal(factory, tenant, status="pending", kind="quote_draft")
    await _seed_proposal(factory, tenant, status="pending", kind="reply_draft")
    await _seed_proposal(factory, tenant, status="approved", kind="quote_draft")
    account = await _seed_channel_account(factory, tenant)
    await _seed_conversation(
        factory, tenant, customer=customer, account=account, subject="Agent own thread",
        unread=3, assigned_user_id=workspace["agent_id"],
    )
    await _seed_conversation(
        factory, tenant, customer=customer, account=account, subject="Unassigned shared thread",
        unread=1, assigned_user_id=None,
    )
    await _seed_conversation(
        factory, tenant, customer=customer, account=account, subject="Owner thread",
        unread=2, assigned_user_id=workspace["owner_id"],
    )
    await _seed_conversation(
        factory, tenant, customer=customer, account=account, subject="Read thread",
        unread=0, assigned_user_id=workspace["owner_id"],
    )
    return {"order": str(order), "exception_order": str(exception_order)}


async def test_daily_patrol_brief_reports_live_sql_counts(workspace) -> None:
    await _seed_patrol_fixture(workspace)
    async with client_for(workspace["factory"], workspace["owner"]) as client:
        response = await client.get(f"{AI_BASE}/patrol/daily-brief")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["metrics"] == {
        "expiring_quotes_count": 2,
        "low_stock_skus_count": 2,
        "stalled_shipments_count": 2,
        "pending_payments_count": 2,
        "pending_proposals_count": 2,
        "unread_conversations_count": 3,
    }
    assert 0 <= body["health_score"] < 100
    assert isinstance(body["health_score"], int)
    assert body["summary_zh"].startswith("今日巡检")
    assert "health score" in body["summary_en"].lower()

    alerts = {alert["code"]: alert for alert in body["alerts"]}
    assert set(alerts) == {
        "expiring_quotes",
        "low_stock_skus",
        "stalled_shipments",
        "pending_payments",
        "pending_proposals",
        "unread_conversations",
    }
    # High severity first, and every alert names the employee that owns the follow-up.
    assert [alert["severity"] for alert in body["alerts"]][:3] == ["high", "high", "high"]
    assert [alert["severity"] for alert in body["alerts"]][-1] == "info"
    assert alerts["expiring_quotes"]["employee_code"] == "b2b-sales"
    assert alerts["expiring_quotes"]["employee_name"] == "小光"
    assert alerts["expiring_quotes"]["action_href"] == "/b2b/quotes"
    assert alerts["expiring_quotes"]["count"] == 2
    assert "Q-EXP-1" in alerts["expiring_quotes"]["detail_zh"]
    assert "Q-EXP-1" in alerts["expiring_quotes"]["detail_en"]
    assert alerts["low_stock_skus"]["employee_code"] == "supply-planner"
    assert "LED-COB-5M" in alerts["low_stock_skus"]["detail_zh"]
    assert alerts["stalled_shipments"]["employee_code"] == "logistics-forwarder"
    assert "TRK-STALL-2" in alerts["stalled_shipments"]["detail_en"]
    assert alerts["pending_payments"]["employee_code"] == "finance-assistant"
    assert alerts["pending_proposals"]["employee_code"] == "biz-analyst"
    assert alerts["unread_conversations"]["employee_code"] == "b2c-cs"
    for alert in body["alerts"]:
        assert alert["title_zh"] and alert["title_en"]
        assert alert["detail_zh"] and alert["detail_en"]
        assert alert["count"] > 0


async def test_patrol_masks_financials_for_agent_and_scopes_unread_inbox(workspace) -> None:
    await _seed_patrol_fixture(workspace)
    async with client_for(workspace["factory"], workspace["agent"]) as client:
        agent_body = (await client.get(f"{AI_BASE}/patrol/daily-brief")).json()
    async with client_for(workspace["factory"], workspace["finance"]) as client:
        finance_body = (await client.get(f"{AI_BASE}/patrol/daily-brief")).json()

    assert agent_body["metrics"]["pending_payments_count"] == 0
    assert "pending_payments" not in {alert["code"] for alert in agent_body["alerts"]}
    # The agent only sees its own thread plus the unassigned shared one.
    assert agent_body["metrics"]["unread_conversations_count"] == 2
    assert agent_body["metrics"]["expiring_quotes_count"] == 2
    # Non-financial live data is never masked for the desk.
    assert agent_body["metrics"]["stalled_shipments_count"] == 2
    assert agent_body["health_score"] > 0

    # Finance keeps the real exposure and is never masked.
    assert finance_body["metrics"]["pending_payments_count"] == 2
    payments = next(
        alert for alert in finance_body["alerts"] if alert["code"] == "pending_payments"
    )
    assert payments["severity"] == "medium"
    assert "30% deposit" in payments["detail_zh"]


async def test_daily_patrol_brief_is_tenant_isolated(workspace) -> None:
    await _seed_patrol_fixture(workspace)
    async with client_for(workspace["factory"], workspace["outsider"]) as client:
        response = await client.get(f"{AI_BASE}/patrol/daily-brief")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["metrics"].values()) == {0}
    assert body["alerts"] == []
    assert body["health_score"] == 100
    assert "未发现需人工介入" in body["summary_zh"]


async def _create_employee(client: AsyncClient, payload: dict[str, object]) -> dict[str, object]:
    response = await client.post(f"{AI_BASE}/employees", json=payload)
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


async def test_patrol_dispatch_creates_a_real_follow_up_task(workspace) -> None:
    await _seed_patrol_fixture(workspace)
    factory = workspace["factory"]
    tenant = workspace["tenant"]
    async with client_for(factory, workspace["owner"]) as client:
        employee = await _create_employee(
            client,
            {
                "code": "b2b-sales",
                "display_name": "小光",
                "role_title": "B2B 外贸业务员",
                "department": "商务中心",
                "description": "整理 RFQ 并起草报价草案。",
                "capability_codes": ["sales_quoting"],
                "autonomy_level": "L1_proposal",
                "system_persona": "只引用 ERP 实时价格。",
            },
        )
        response = await client.post(f"{AI_BASE}/patrol/dispatch/b2b-sales")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["employee_code"] == employee["code"]
        assert body["employee_name"] == "小光"
        assert body["metrics"] == {"expiring_quotes_count": 2}
        assert body["recommended_href"] == "/b2b/quotes"
        assert body["findings"][0]["count"] == 2
        assert body["findings"][0]["samples"][0]["quote_no"] == "Q-EXP-1"
        assert body["created_task_id"] is not None
        assert "报价" in body["action_summary_zh"]
        assert "follow-up task" in body["action_summary_en"]
        task_id = UUID(body["created_task_id"])

        # A pinyin alias resolves onto the same registered employee.
        await _create_employee(
            client,
            {
                "code": "supply-planner",
                "display_name": "小供",
                "role_title": "供应链专员",
                "department": "供应链中心",
                "description": "跟踪安全库存并输出补货建议。",
                "capability_codes": ["biz_analytics", "order_logistics"],
                "autonomy_level": "L1_proposal",
            },
        )
        alias = await client.post(f"{AI_BASE}/patrol/dispatch/xiaocang")
        assert alias.status_code == 200, alias.text
        alias_body = alias.json()
        assert alias_body["employee_code"] == "supply-planner"
        assert alias_body["metrics"] == {"low_stock_skus_count": 2}
        assert alias_body["recommended_href"] == "/wms"
        assert alias_body["created_task_id"] is not None

        unknown = await client.post(f"{AI_BASE}/patrol/dispatch/ghost-employee")
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "ERR_RECORD_NOT_FOUND"

    async with tenant_session(factory, tenant) as session:
        task = (
            await session.execute(
                text("""
                    SELECT title, priority, status, due_at FROM task_reminders WHERE id = :id
                """),
                {"id": task_id},
            )
        ).one()
        audit = (
            await session.execute(
                text(
                    "SELECT actor_type, actor_id, action FROM audit_events "
                    "WHERE tenant_id = :tid AND action = 'ai.patrol.dispatched'"
                ),
                {"tid": tenant},
            )
        ).all()
    assert "2 张" in task.title
    assert task.priority == "high"
    assert task.status == "open"
    assert task.due_at is not None
    assert {row.actor_type for row in audit} == {"ai_agent"}
    assert {row.actor_id for row in audit} >= {"b2b-sales", "supply-planner"}


async def _seed_support_and_finance_employees(client: AsyncClient) -> None:
    await _create_employee(
        client,
        {
            "code": "b2c-cs",
            "display_name": "小暖",
            "role_title": "B2C 贴心客服",
            "department": "客户成功",
            "description": "回答零售买家售前售后问题。",
            "capability_codes": ["cs_support"],
            "autonomy_level": "L2_auto",
        },
    )
    await _create_employee(
        client,
        {
            "code": "cs-desk",
            "display_name": "小助",
            "role_title": "客服主管助理",
            "department": "客户成功",
            "description": "汇总未读会话并输出跟进建议。",
            "capability_codes": ["cs_support"],
            "autonomy_level": "L1_proposal",
        },
    )
    await _create_employee(
        client,
        {
            "code": "finance-assistant",
            "display_name": "小账",
            "role_title": "财务助理",
            "department": "财务中心",
            "description": "匹配应收与银行水单候选。",
            "capability_codes": ["finance_recon"],
            "autonomy_level": "L0_copilot",
        },
    )


async def test_patrol_dispatch_rbac_and_tenant_isolation(workspace) -> None:
    await _seed_patrol_fixture(workspace)
    factory = workspace["factory"]
    async with client_for(factory, workspace["owner"]) as owner_client:
        await _seed_support_and_finance_employees(owner_client)
        created = await owner_client.post(f"{AI_BASE}/patrol/dispatch/cs-desk")
        assert created.status_code == 200, created.text
        # Support dispatch reports the inbox but never auto-creates a reply task.
        assert created.json()["metrics"] == {"unread_conversations_count": 3}
        assert created.json()["created_task_id"] is None

        disabled = await owner_client.patch(
            f"{AI_BASE}/employees/{await _employee_id(owner_client, 'b2c-cs')}",
            json={"is_enabled": False},
        )
        assert disabled.status_code == 200, disabled.text
        blocked = await owner_client.post(f"{AI_BASE}/patrol/dispatch/b2c-cs")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "ERR_AI_EMPLOYEE_DISABLED"

    async with client_for(factory, workspace["agent"]) as agent_client:
        # Agent inbox scope is narrower than the owner's.
        scoped = await agent_client.post(f"{AI_BASE}/patrol/dispatch/cs-desk")
        assert scoped.status_code == 200, scoped.text
        assert scoped.json()["metrics"] == {"unread_conversations_count": 2}
        finance = await agent_client.post(f"{AI_BASE}/patrol/dispatch/finance-assistant")
        assert finance.status_code == 403
        assert finance.json()["code"] == "ERR_PATROL_FINANCE_RESTRICTED"

    async with client_for(factory, workspace["outsider"]) as outsider_client:
        hidden = await outsider_client.post(f"{AI_BASE}/patrol/dispatch/cs-desk")
        assert hidden.status_code == 404


async def _employee_id(client: AsyncClient, code: str) -> str:
    listing = await client.get(f"{AI_BASE}/employees")
    items = {item["code"]: item["id"] for item in listing.json()["items"]}
    return items[code]


async def test_create_custom_ai_employee_with_tool_scope_and_l0_guard(workspace) -> None:
    payload = {
        "code": "cross-border-ops",
        "display_name": "小合 / Aria",
        "role_title": "跨境运营统筹 Cross-border Operations Lead",
        "department": "跨境运营",
        "avatar_emoji": "🧩",
        "description": "统筹选品、内容与客服排期，只输出草稿与建议。",
        "capability_codes": ["content_studio", "cs_support"],
        "autonomy_level": "L1_proposal",
        "tool_count": 3,
        "system_persona": "多语言跨境运营助理，不编造价格与认证。",
        "allowed_tools": ["get_live_sku_pricing_and_stock", "get_live_order_tracking"],
    }
    async with client_for(workspace["factory"], workspace["manager"]) as client:
        response = await client.post(f"{AI_BASE}/employees", json=payload)
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["display_name"] == "小合 / Aria"
        assert created["autonomy_level"] == "L1_proposal"
        assert created["system_persona"] == payload["system_persona"]
        assert created["allowed_tools"] == [
            "get_live_sku_pricing_and_stock",
            "get_live_order_tracking",
        ]

        outside = await client.post(
            f"{AI_BASE}/employees",
            json={
                **payload,
                "code": "overreach",
                "capability_codes": ["cs_support"],
                "allowed_tools": ["match_open_receivables"],
            },
        )
        assert outside.status_code == 422
        assert outside.json()["code"] == "ERR_TOOL_OUTSIDE_CAPABILITY"

        # A financial capability is hard-capped at L0: no configuration may auto-execute it.
        above_ceiling = await client.post(
            f"{AI_BASE}/employees",
            json={
                **payload,
                "code": "auto-cfo",
                "capability_codes": ["finance_recon"],
                "autonomy_level": "L2_auto",
                "allowed_tools": None,
            },
        )
        assert above_ceiling.status_code == 422
        assert above_ceiling.json()["code"] == "ERR_AUTONOMY_ABOVE_CAPABILITY_CEILING"

        copilot = await client.post(
            f"{AI_BASE}/employees",
            json={
                **payload,
                "code": "finance-copilot",
                "capability_codes": ["finance_recon"],
                "autonomy_level": "L0_copilot",
                "allowed_tools": ["match_open_receivables"],
            },
        )
        assert copilot.status_code == 201, copilot.text
        assert copilot.json()["allowed_tools"] == ["match_open_receivables"]

    async with client_for(workspace["factory"], workspace["agent"]) as agent_client:
        forbidden = await agent_client.post(f"{AI_BASE}/employees", json=payload)
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "ERR_INSUFFICIENT_ROLE"


def test_locked_export_roster_keeps_nine_legacy_roles_and_extends_to_twelve() -> None:
    codes = [employee.code for employee in DEFAULT_EXPORT_EMPLOYEES]
    assert len(codes) == 12
    assert len(set(codes)) == 12
    assert set(codes) >= LEGACY_EMPLOYEE_CODES
    for employee in DEFAULT_EXPORT_EMPLOYEES:
        ceiling = min_autonomy(
            *(CAPABILITY_SPECS[AiCapability(code)].max_allowed_autonomy
              for code in employee.capability_codes)
        )
        assert employee.autonomy_level == min_autonomy(employee.autonomy_level, ceiling)
        assert employee.display_name and employee.role_title and employee.system_persona
        for tool in CAPABILITY_TOOLS[AiCapability(employee.capability_codes[0])]:
            assert tool
    order_desk = export_employee("xiaodan")
    warehouse = export_employee("xiaocang")
    assert order_desk is not None and order_desk.code == "b2b-order-desk"
    assert warehouse is not None and warehouse.code == "supply-planner"
    assert export_employee("nope") is None


async def test_social_playbooks_return_fourteen_bilingual_cards(workspace) -> None:
    async with client_for(workspace["factory"], workspace["agent"]) as client:
        response = await client.get(f"{SOCIAL_BASE}/playbooks")
    assert response.status_code == 200, response.text
    cards = response.json()
    assert len(cards) == 14
    by_platform = {card["platform"]: card for card in cards}
    assert set(by_platform) == {playbook.platform for playbook in PLAYBOOKS}
    assert len([card for card in cards if card["region"] == "overseas"]) == 7
    assert len([card for card in cards if card["region"] == "domestic"]) == 7

    locked_modes = {playbook.platform: playbook.publish_mode for playbook in PLAYBOOKS}
    for card in cards:
        assert card["name_zh"] and card["name_en"]
        assert card["category_zh"] and card["category_en"]
        assert card["audience_zh"] and card["audience_en"]
        assert card["lead_funnel_zh"] and card["lead_funnel_en"]
        assert len(card["best_practices_zh"]) >= 3
        assert len(card["best_practices_en"]) == len(card["best_practices_zh"])
        assert card["compliance_note_zh"] and card["compliance_note_en"]
        # The honest API claim is bound to the locked publish mode, never to a UI preference.
        assert card["official_api_supported"] == (locked_modes[card["platform"]] == "api")

    assert by_platform["instagram"]["official_api_supported"] is True
    assert by_platform["wechat_oa"]["official_api_supported"] is True
    for manual in ("xiaohongshu", "douyin", "kuaishou", "bilibili", "wechat_video", "weibo"):
        assert by_platform[manual]["official_api_supported"] is False
    assert "write_notes" in by_platform["xiaohongshu"]["compliance_note_zh"]


async def test_social_playbooks_region_filter(workspace) -> None:
    async with client_for(workspace["factory"], workspace["owner"]) as client:
        overseas = await client.get(f"{SOCIAL_BASE}/playbooks", params={"region": "overseas"})
        domestic = await client.get(f"{SOCIAL_BASE}/playbooks", params={"region": "domestic"})
        everything = await client.get(f"{SOCIAL_BASE}/playbooks", params={"region": "all"})
        invalid = await client.get(f"{SOCIAL_BASE}/playbooks", params={"region": "mars"})
    assert overseas.status_code == 200, overseas.text
    assert domestic.status_code == 200, domestic.text
    assert everything.status_code == 200, everything.text
    assert invalid.status_code == 422

    overseas_cards = overseas.json()
    domestic_cards = domestic.json()
    assert {card["region"] for card in overseas_cards} == {"overseas"}
    assert {card["region"] for card in domestic_cards} == {"domestic"}
    assert len(overseas_cards) == len(domestic_cards) == 7
    assert {card["platform"] for card in overseas_cards} >= {"instagram", "tiktok", "linkedin"}
    assert {card["platform"] for card in domestic_cards} >= {"xiaohongshu", "douyin", "weibo"}
    assert len(everything.json()) == 14
    assert domestic_cards[0]["name_zh"] == "小红书"
    assert domestic_cards[0]["name_en"] == "Xiaohongshu (RED)"
