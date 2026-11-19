"""Phase 3B workforce APIs verified against real PostgreSQL, Redis and Decimal money."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.ai_engine.router_llm import LlmProviderResponse, TieredModelRouter
from erp_core.ai_engine.runtime import PolicyRuntime
from erp_core.ai_engine.workforce_router import workforce_router
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
BASE = "/api/v1/ai"
STUB_TEXT = "Polite rewrite without any figure."


class StubProvider:
    """Deterministic stand-in for a configured LLM provider (no network, no API key)."""

    def __init__(self, text: str = STUB_TEXT) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model_id: str,
        provider: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        self.calls.append({"model_id": model_id, "provider": provider, "messages": messages})
        return LlmProviderResponse(
            text=self.text,
            prompt_tokens=200,
            cached_tokens=0,
            completion_tokens=80,
            model_id=model_id,
            provider=provider,
        )


def client_for(
    factory: Factory,
    principal: CurrentPrincipal,
    *,
    provider: StubProvider | None = None,
) -> AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    if provider is not None:
        app.state.ai_runtime = PolicyRuntime(
            llm_router=TieredModelRouter(provider_client=provider)
        )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(workforce_router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def workspace(runtime_session_factory: Factory):
    tenant, other = uuid4(), uuid4()
    owner, manager, agent, outsider = (uuid4() for _ in range(4))
    for tid, slug in ((tenant, f"wf-{tenant.hex}"), (other, f"wf-{other.hex}")):
        async with tenant_session(runtime_session_factory, tid) as session:
            await session.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Workforce')"),
                {"id": tid, "slug": slug},
            )
    async with tenant_session(runtime_session_factory, tenant) as session:
        for uid, role in (
            (owner, UserRole.OWNER),
            (manager, UserRole.MANAGER),
            (agent, UserRole.AGENT),
        ):
            email = f"{uid.hex}@example.com"
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:uid, :email, 'WF', 'x')"
                ),
                {"uid": uid, "email": email},
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

    def principal(role_user: UUID, role: UserRole, tenant_id: UUID = tenant) -> CurrentPrincipal:
        return CurrentPrincipal(
            user_id=role_user,
            tenant_id=tenant_id,
            role=role,
            email=f"{role_user.hex}@example.com",
        )

    return {
        "tenant": tenant,
        "other": other,
        "owner": principal(owner, UserRole.OWNER),
        "manager": principal(manager, UserRole.MANAGER),
        "agent": principal(agent, UserRole.AGENT),
        "outsider": principal(outsider, UserRole.OWNER, other),
        "owner_id": owner,
        "factory": runtime_session_factory,
    }


EMPLOYEE = {
    "code": "b2b-sales",
    "display_name": "小光",
    "role_title": "B2B 外贸业务员",
    "department": "商务中心",
    "avatar_emoji": "💡",
    "description": "整理 RFQ 并起草报价草案。",
    "capability_codes": ["sales_quoting"],
    "autonomy_level": "L1_proposal",
    "tool_count": 5,
    "system_persona": "只引用 ERP 实时价格。",
}


async def test_employee_crud_rbac_and_cross_tenant_isolation(workspace) -> None:
    factory = workspace["factory"]
    async with client_for(factory, workspace["agent"]) as agent_client:
        forbidden = await agent_client.post(f"{BASE}/employees", json=EMPLOYEE)
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "ERR_INSUFFICIENT_ROLE"

    async with client_for(factory, workspace["owner"]) as owner_client:
        created = await owner_client.post(f"{BASE}/employees", json=EMPLOYEE)
        assert created.status_code == 201, created.text
        body = created.json()
        employee_id = body["id"]
        assert body["capability_codes"] == ["sales_quoting"]
        assert body["autonomy_level"] == "L1_proposal"
        assert body["actions_7d"] == 0 and body["tasks_7d"] == 0

        duplicate = await owner_client.post(f"{BASE}/employees", json=EMPLOYEE)
        assert duplicate.status_code == 409

        ceiling = await owner_client.post(
            f"{BASE}/employees",
            json={**EMPLOYEE, "code": "cfo", "capability_codes": ["finance_recon"],
                  "autonomy_level": "L2_auto"},
        )
        assert ceiling.status_code == 422
        assert ceiling.json()["code"] == "ERR_AUTONOMY_ABOVE_CAPABILITY_CEILING"

        invalid = await owner_client.post(
            f"{BASE}/employees", json={**EMPLOYEE, "code": "bad", "capability_codes": ["nope"]}
        )
        assert invalid.status_code == 422

    async with client_for(factory, workspace["manager"]) as manager_client:
        disabled = await manager_client.patch(
            f"{BASE}/employees/{employee_id}", json={"is_enabled": False}
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["is_enabled"] is False

        missing = await manager_client.patch(
            f"{BASE}/employees/{uuid4()}", json={"is_enabled": True}
        )
        assert missing.status_code == 404

    async with client_for(factory, workspace["agent"]) as agent_client:
        listing = await agent_client.get(f"{BASE}/employees")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["display_name"] == "小光"

    async with client_for(factory, workspace["outsider"]) as outsider_client:
        hidden = await outsider_client.patch(
            f"{BASE}/employees/{employee_id}", json={"is_enabled": True}
        )
        assert hidden.status_code == 404
        assert (await outsider_client.get(f"{BASE}/employees")).json()["total"] == 0


async def _insert_proposal(
    factory: Factory,
    tenant: UUID,
    *,
    capability: str,
    proposal_type: str,
    status: str = "pending",
    reviewed: bool = False,
) -> None:
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO ai_proposals (tenant_id, capability, proposal_type, proposed_payload,
                    confidence_score, status, reviewed_at, expires_at)
                VALUES (:tid, :capability, :kind, CAST(:payload AS jsonb), 0.9, :status,
                    :reviewed, now() + interval '7 days')
            """),
            {
                "tid": tenant,
                "capability": capability,
                "kind": proposal_type,
                "status": status,
                "reviewed": datetime.now(UTC) if reviewed else None,
                "payload": json.dumps({"body_text": "draft"}),
            },
        )


async def _insert_usage(factory: Factory, tenant: UUID, key: str, cost: str) -> None:
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO usage_ledger (tenant_id, idempotency_key, metric_type,
                    units_consumed, raw_provider_cost_usd, model_or_provider)
                VALUES (:tid, :key, 'ai_credit', 1, :cost, 'gemini-3.1-flash-lite')
            """),
            {"tid": tenant, "key": key, "cost": Decimal(cost)},
        )


async def test_matrix_reports_real_proposals_ledger_cost_and_red_lines(workspace) -> None:
    factory = workspace["factory"]
    tenant = workspace["tenant"]
    await _insert_proposal(
        factory, tenant, capability="sales_quoting", proposal_type="quote_draft"
    )
    await _insert_proposal(
        factory, tenant, capability="sales_quoting", proposal_type="quote_draft"
    )
    await _insert_proposal(factory, tenant, capability="cs_support", proposal_type="reply_draft")
    await _insert_proposal(
        factory,
        tenant,
        capability="order_logistics",
        proposal_type="follow_up_suggestion",
        status="approved",
        reviewed=True,
    )
    await _insert_usage(factory, tenant, "wf-usage-1", "0.012345")
    await _insert_usage(factory, tenant, "wf-usage-2", "0.000655")

    async with client_for(factory, workspace["owner"]) as client:
        await client.post(f"{BASE}/employees", json=EMPLOYEE)
        await client.post(
            f"{BASE}/employees",
            json={**EMPLOYEE, "code": "finance", "display_name": "小账",
                  "capability_codes": ["finance_recon"], "autonomy_level": "L0_copilot"},
        )
        response = await client.get(f"{BASE}/employees/matrix")
        assert response.status_code == 200, response.text
        body = response.json()

    kpis = body["kpis"]
    assert kpis["total_employees"] == 2
    assert kpis["active_employees"] == 2
    # 4 real proposals created + 2 real ledger rows in the last 7 days.
    assert kpis["actions_7d"] == 6
    assert kpis["today_llm_cost_usd"] == "0.013000"
    assert isinstance(kpis["today_llm_cost_usd"], str)
    assert kpis["autonomy_summary"] == {"l2_auto": 0, "l1_proposal": 1, "l0_copilot": 1}

    executed = {(row["capability"], row["auto_executed_7d"]) for row in body["l2_auto"]}
    assert executed == {("order_logistics", 1)}
    assert all(row["href"] == "/ai/proposals" for row in body["l2_auto"])

    pending = {(row["proposal_type"], row["count"]) for row in body["l1_pending"]}
    assert pending == {("quote_draft", 2), ("reply_draft", 1)}
    assert all(row["href"] == "/ai/proposals" for row in body["l1_pending"])

    assert [row["code"] for row in body["l0_locked"]] == [
        "send_formal_quote_or_email",
        "finance_receipt_settlement",
        "refund_and_high_discount",
    ]
    assert all(row["href"] == "/ai/capabilities" for row in body["l0_locked"])

    async with client_for(factory, workspace["agent"]) as agent_client:
        agent_body = (await agent_client.get(f"{BASE}/employees/matrix")).json()
    # Agents review reply drafts only; commercial quote drafts stay hidden from them.
    assert {row["proposal_type"] for row in agent_body["l1_pending"]} == {"reply_draft"}

    async with client_for(factory, workspace["outsider"]) as other_client:
        other_body = (await other_client.get(f"{BASE}/employees/matrix")).json()
    assert other_body["kpis"]["total_employees"] == 0
    assert other_body["kpis"]["today_llm_cost_usd"] == "0.000000"
    assert other_body["l1_pending"] == []
    assert other_body["l2_auto"] == []


async def test_employee_activity_metrics_are_computed_from_real_proposals(workspace) -> None:
    factory = workspace["factory"]
    tenant = workspace["tenant"]
    await _insert_proposal(
        factory, tenant, capability="order_logistics", proposal_type="reply_draft"
    )
    await _insert_proposal(
        factory,
        tenant,
        capability="order_logistics",
        proposal_type="follow_up_suggestion",
        status="approved",
        reviewed=True,
    )
    async with client_for(factory, workspace["owner"]) as client:
        await client.post(
            f"{BASE}/employees",
            json={**EMPLOYEE, "code": "logistics", "capability_codes": ["order_logistics"]},
        )
        items = (await client.get(f"{BASE}/employees")).json()["items"]
    employee = next(item for item in items if item["code"] == "logistics")
    assert employee["actions_7d"] == 2
    assert employee["tasks_7d"] == 1


async def test_inbox_assist_fails_closed_without_provider_and_spends_real_credits(
    workspace,
) -> None:
    factory = workspace["factory"]
    tenant = workspace["tenant"]
    payload = {"action": "polish", "text": "请把这句话改得更礼貌一些。", "language": "zh"}

    async with client_for(factory, workspace["agent"]) as no_provider:
        unavailable = await no_provider.post(f"{BASE}/inbox/assist", json=payload)
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "ERR_AI_PROVIDER_UNAVAILABLE"
    assert "Polite rewrite" not in unavailable.text
    async with tenant_session(factory, tenant) as session:
        assert await session.scalar(text("SELECT COUNT(*) FROM usage_ledger")) == 0

    provider = StubProvider()
    async with client_for(factory, workspace["agent"], provider=provider) as client:
        empty = await client.post(f"{BASE}/inbox/assist", json={"action": "polish"})
        assert empty.status_code == 422

        assisted = await client.post(f"{BASE}/inbox/assist", json=payload)
        assert assisted.status_code == 200, assisted.text
        body = assisted.json()
        assert body["action"] == "polish"
        assert body["text"] == STUB_TEXT
        assert body["model_used"]
        assert Decimal(body["credits_consumed"]) > 0
        assert body["tool_calls"] == []
    assert len(provider.calls) == 1

    async with tenant_session(factory, tenant) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT metric_type, raw_provider_cost_usd, model_or_provider "
                    "FROM usage_ledger"
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].metric_type == "ai_credit"
    assert rows[0].raw_provider_cost_usd > 0

    async with client_for(factory, workspace["owner"]) as client:
        matrix = (await client.get(f"{BASE}/employees/matrix")).json()
    assert Decimal(matrix["kpis"]["today_llm_cost_usd"]) == rows[0].raw_provider_cost_usd


async def _seed_catalog(factory: Factory, tenant: UUID) -> tuple[UUID, UUID, UUID]:
    """Real product/variant plus the authoritative incoterm pricing policy."""
    product, variant, warehouse = uuid4(), uuid4(), uuid4()
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text(
                "UPDATE tenants SET ai_policy_settings = CAST(:policy AS jsonb) WHERE id = :tid"
            ),
            {
                "tid": tenant,
                "policy": json.dumps({"quote_pricing": {"incoterm_surcharge_pct": {"FOB": "0"}}}),
            },
        )
        await session.execute(
            text("INSERT INTO products (id, tenant_id, spu_code, category) "
                 "VALUES (:pid, :tid, 'LED-COB-24V', 'LED strips')"),
            {"pid": product, "tid": tenant},
        )
        await session.execute(
            text("""
                INSERT INTO product_variants (id, tenant_id, product_id, sku, cost_price_base,
                    base_currency, retail_price_usd, weight_grams, lead_time_days, b2b_moq_tiers)
                VALUES (:vid, :tid, :pid, 'LED-COB-24V-5M', 5, 'USD', 29.90, 400, 7,
                    CAST(:tiers AS jsonb))
            """),
            {
                "vid": variant,
                "tid": tenant,
                "pid": product,
                "tiers": json.dumps([{"min_qty": 100, "unit_price_usd": "14.50"}]),
            },
        )
    return product, variant, warehouse


async def test_daily_brief_returns_real_operational_counts(workspace) -> None:
    factory = workspace["factory"]
    tenant = workspace["tenant"]
    await _insert_proposal(factory, tenant, capability="cs_support", proposal_type="reply_draft")
    await _insert_usage(factory, tenant, "brief-usage", "0.000500")
    _, variant, _ = await _seed_catalog(factory, tenant)
    async with tenant_session(factory, tenant) as session:
        customer = uuid4()
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, display_name) VALUES (:cid, :tid, 'Buyer')"
            ),
            {"cid": customer, "tid": tenant},
        )
        account = uuid4()
        await session.execute(
            text("""
                INSERT INTO channel_accounts (id, tenant_id, channel_type, external_account_id,
                    display_name, status)
                VALUES (:aid, :tid, 'email', :external, :external, 'active')
            """),
            {"aid": account, "tid": tenant, "external": f"{tenant.hex}@inbound.example.com"},
        )
        for status in ("open", "pending_human", "resolved"):
            await session.execute(
                text("""
                    INSERT INTO conversations (id, tenant_id, customer_id, channel_type,
                        conversation_mode, status, subject, channel_account_id)
                    VALUES (gen_random_uuid(), :tid, :cid, 'email', 'b2b_inquiry', :status,
                        'RFQ', :wid)
                """),
                {"tid": tenant, "cid": customer, "status": status, "wid": account},
            )
        await session.execute(
            text("""
                INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id,
                    on_hand_qty, reserved_qty)
                VALUES (:tid, :wid, :vid, 5, 0), (:tid, :wid2, :vid, 500, 0)
            """),
            {"tid": tenant, "wid": uuid4(), "vid": variant, "wid2": uuid4()},
        )
        await session.execute(
            text("""
                INSERT INTO orders (id, tenant_id, order_no, order_mode, customer_id, currency,
                    fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status,
                    payment_status)
                VALUES (gen_random_uuid(), :tid, 'ORD-WF-1', 'b2c_retail', :cid, 'USD', 1, 10,
                    10, 'exception', 'paid')
            """),
            {"tid": tenant, "cid": customer},
        )

    async with client_for(factory, workspace["agent"]) as client:
        response = await client.post(
            f"{BASE}/inbox/assist", json={"action": "daily_brief", "language": "zh"}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model_used"] == "deterministic_sql_brief"
    assert "今日 AI 日报" in body["text"]
    data = body["data"]
    assert data["pending_proposals_total"] == 1
    assert data["pending_proposals_by_type"] == {"reply_draft": 1}
    assert data["today_llm_cost_usd"] == "0.000500"
    assert data["open_conversations"] == 2
    assert data["low_stock_skus"] == 1
    assert data["shipment_exceptions"] == 1


async def test_quick_reply_templates_crud_and_tenant_isolation(workspace) -> None:
    factory = workspace["factory"]
    payload = {
        "title": "MOQ 回复",
        "category": "报价",
        "language": "zh",
        "body_text": "起订量 100 条起。",
        "shortcut_code": "moq-zh",
    }
    async with client_for(factory, workspace["agent"]) as client:
        created = await client.post(f"{BASE}/quick-replies", json=payload)
        assert created.status_code == 201, created.text
        template_id = created.json()["id"]
        duplicate = await client.post(f"{BASE}/quick-replies", json=payload)
        assert duplicate.status_code == 409
        listing = await client.get(f"{BASE}/quick-replies", params={"language": "zh"})
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["shortcut_code"] == "moq-zh"
        deleted = await client.delete(f"{BASE}/quick-replies/{template_id}")
        assert deleted.status_code == 204
        assert (await client.get(f"{BASE}/quick-replies")).json()["total"] == 0
        assert (await client.delete(f"{BASE}/quick-replies/{template_id}")).status_code == 404

    async with client_for(factory, workspace["outsider"]) as other_client:
        hidden = await other_client.delete(f"{BASE}/quick-replies/{template_id}")
        assert hidden.status_code == 404


async def test_studio_assets_manual_save_generation_and_review(workspace) -> None:
    factory = workspace["factory"]
    tenant = workspace["tenant"]
    _, variant, warehouse = await _seed_catalog(factory, tenant)
    async with tenant_session(factory, tenant) as session:
        await session.execute(
            text("""
                INSERT INTO inventory_balances (tenant_id, warehouse_id, variant_id, on_hand_qty)
                VALUES (:tid, :wid, :vid, 4000)
            """),
            {"tid": tenant, "wid": warehouse, "vid": variant},
        )

    async with client_for(factory, workspace["owner"]) as no_provider:
        unavailable = await no_provider.post(
            f"{BASE}/studio/assets",
            json={
                "asset_type": "product_copy",
                "language": "de",
                "sku_ref": "LED-COB-24V-5M",
                "prompt": "German copy",
                "generate": True,
            },
        )
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "ERR_AI_PROVIDER_UNAVAILABLE"

    async with client_for(factory, workspace["agent"]) as client:
        manual = await client.post(
            f"{BASE}/studio/assets",
            json={
                "asset_type": "listing",
                "title": "Amazon bullets",
                "language": "en",
                "sku_ref": "LED-COB-24V-5M",
                "content_text": "- Seamless COB light line",
            },
        )
        assert manual.status_code == 201, manual.text
        asset = manual.json()
        assert asset["status"] == "draft"
        assert asset["numeric_guard_passed"] is True

        conflict = await client.post(
            f"{BASE}/studio/assets",
            json={
                "asset_type": "listing",
                "language": "en",
                "content_text": "text",
                "prompt": "p",
                "generate": True,
            },
        )
        assert conflict.status_code == 422

        unknown_sku = await client.post(
            f"{BASE}/studio/assets",
            json={
                "asset_type": "listing",
                "language": "en",
                "sku_ref": "NOPE-SKU-1",
                "prompt": "p",
                "generate": True,
            },
        )
        assert unknown_sku.status_code == 422
        assert unknown_sku.json()["code"] == "ERR_ASSET_SKU_UNKNOWN"

        approved = await client.patch(
            f"{BASE}/studio/assets/{asset['id']}", json={"status": "approved"}
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        filtered = await client.get(f"{BASE}/studio/assets", params={"status": "approved"})
        assert filtered.json()["total"] == 1

    provider = StubProvider("COB strip copy for the German market.")
    async with client_for(factory, workspace["agent"], provider=provider) as client:
        generated = await client.post(
            f"{BASE}/studio/assets",
            json={
                "asset_type": "product_copy",
                "language": "de",
                "sku_ref": "LED-COB-24V-5M",
                "prompt": "German copy",
                "generate": True,
            },
        )
        assert generated.status_code == 201, generated.text
        body = generated.json()
        assert body["content_text"] == "COB strip copy for the German market."
        assert body["status"] == "draft"
        assert body["tool_calls"][0]["tool_name"] == "get_live_sku_pricing_and_stock"
        assert body["tool_calls"][0]["ok"] is True
        # Free prose without a tool-grounded claim can never claim numeric grounding.
        assert body["numeric_guard_passed"] is False

    async with client_for(factory, workspace["outsider"]) as other_client:
        assert (await other_client.get(f"{BASE}/studio/assets")).json()["total"] == 0
        hidden_asset = await other_client.patch(
            f"{BASE}/studio/assets/{asset['id']}", json={"status": "draft"}
        )
        assert hidden_asset.status_code == 404
