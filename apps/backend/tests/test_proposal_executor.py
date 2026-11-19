from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_auth_rbac import _register_owner

from erp_core.shared.db import tenant_session

_ROOT = "/api/v1/ai"


async def test_review_is_tenant_scoped_audited_and_idempotent(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tid_raw = await _register_owner(async_client)
    other_token, _, _ = await _register_owner(async_client)
    tenant, customer, proposal = UUID(tid_raw), uuid4(), uuid4()
    headers = {"Authorization": f"Bearer {token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    async with tenant_session(runtime_session_factory, tenant) as session:
        params = {"tid": tenant, "customer": customer, "pid": proposal}
        await session.execute(
            text("""
            INSERT INTO customers (id, tenant_id, customer_type, display_name)
            VALUES (:customer, :tid, 'b2b_contact', 'OEM buyer')
        """),
            params,
        )
        await session.execute(
            text("""
            INSERT INTO ai_proposals (id, tenant_id, capability, proposal_type,
                                      proposed_payload, confidence_score)
            VALUES (:pid, :tid, 'sales_quoting', 'crm_entity_extract',
                '{"source_channel":"email", "requirements":"Original"}', 0.8)
        """),
            params,
        )

    detail = f"{_ROOT}/proposals/{proposal}"
    listing = await async_client.get(f"{_ROOT}/proposals?status=pending", headers=headers)
    assert listing.status_code == 200, listing.text
    assert [item["id"] for item in listing.json()["items"]] == [str(proposal)]
    other = await async_client.get(f"{_ROOT}/proposals", headers=other_headers)
    assert other.json()["total"] == 0
    assert (await async_client.get(detail, headers=other_headers)).status_code == 404
    assert (await async_client.post(f"{detail}/approve", headers=other_headers)).status_code == 404
    assert (await async_client.get(detail, headers=headers)).json()["status"] == "pending"

    edit = {"customer_id": str(customer), "source_channel": "email", "requirements": "Updated"}
    edits = await asyncio.gather(
        *(
            async_client.post(
                f"{detail}/edit-approve", headers=headers, json={"edited_payload": edit}
            )
            for _ in range(2)
        )
    )
    assert [response.status_code for response in edits] == [200, 200], [r.text for r in edits]
    data = edits[0].json()
    assert data == edits[1].json() and data["status"] == "edited_and_approved"
    assert data["execution_result"]["lead_id"]
    conflict = await async_client.post(f"{detail}/reject", headers=headers, json={"reason": "late"})
    assert conflict.status_code == 409
    feedback = await async_client.post(
        f"{_ROOT}/feedback",
        headers=headers,
        json={
            "proposal_id": str(proposal),
            "feedback_note": "Buyer matched verified customer",
        },
    )
    assert feedback.status_code == 200, feedback.text
    async with tenant_session(runtime_session_factory, tenant) as session:
        assert await session.scalar(text("SELECT count(*) FROM leads")) == 1
        assert await session.scalar(text("SELECT count(*) FROM ai_eval_feedback")) == 1
        assert (
            await session.scalar(
                text("""
            SELECT count(*) FROM audit_events WHERE action = 'ai.proposal.edited_and_approved'
        """)
            )
            == 1
        )


async def test_governance_caps_and_live_membership_permissions(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, tenant_raw = await _register_owner(async_client)
    headers = {"Authorization": f"Bearer {token}"}
    invalid = await async_client.patch(
        f"{_ROOT}/capabilities",
        headers=headers,
        json={
            "settings": {"finance_recon": "L2_auto"},
        },
    )
    assert invalid.status_code == 422
    updated = await async_client.patch(
        f"{_ROOT}/capabilities",
        headers=headers,
        json={
            "settings": {"cs_support": "L0_copilot"},
        },
    )
    assert updated.status_code == 200, updated.text
    options = (await async_client.get(f"{_ROOT}/capabilities", headers=headers)).json()
    assert len(options["capabilities"]) == 6
    reply = await async_client.post(
        f"{_ROOT}/assistant/chat",
        headers=headers,
        json={
            "message": "What is the warranty?",
            "context": {"override_reply_text": "unsafe"},
        },
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["l0_locked"] and not reply.json()["auto_executed"]

    async with tenant_session(runtime_session_factory, UUID(tenant_raw)) as session:
        await session.execute(text("UPDATE tenant_memberships SET role = 'agent'"))
    denied = await async_client.patch(
        f"{_ROOT}/capabilities",
        headers=headers,
        json={
            "settings": {"cs_support": "L2_auto"},
        },
    )
    assert denied.status_code == 403
    unauthorized = await async_client.post(
        f"{_ROOT}/translate",
        json={
            "text": "Good morning",
            "target_lang": "fr",
        },
    )
    assert unauthorized.status_code == 401


async def test_quote_approval_rechecks_prices_and_uses_plan03_quote_service(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, _, raw_tid = await _register_owner(async_client)
    tid, customer, product, variant = UUID(raw_tid), uuid4(), uuid4(), uuid4()
    headers = {"Authorization": f"Bearer {token}"}
    params = {"tid": tid, "cid": customer, "pid": product, "vid": variant}
    async with tenant_session(runtime_session_factory, tid) as session:
        for stmt in (
            """UPDATE tenants SET ai_policy_settings =
                '{"quote_pricing":{"incoterm_surcharge_pct":{"FOB":"0"}}}'::jsonb
                WHERE id = :tid""",
            """INSERT INTO customers (id, tenant_id, customer_type, display_name)
                VALUES (:cid, :tid, 'b2b_contact', 'Wholesale buyer')""",
            """INSERT INTO products (id, tenant_id, spu_code, category)
                VALUES (:pid, :tid, 'PLAN03-LED', 'Floodlight')""",
            """INSERT INTO product_variants (id, tenant_id, product_id, sku, base_currency,
                        cost_price_base, retail_price_usd, weight_grams, lead_time_days)
                VALUES (:vid, :tid, :pid, 'TEST-LED-EU', 'USD', 5, 12, 2000, 7)""",
        ):
            await session.execute(text(stmt), params)
    body = {
        "capability": "sales_quoting",
        "message": "RFQ for 20 LED floodlights",
        "context": {"items": [{"sku": "TEST-LED-EU", "qty": 20}], "currency": "USD"},
    }
    draft = await async_client.post(f"{_ROOT}/assistant/chat", headers=headers, json=body)
    assert draft.status_code == 200 and draft.json()["proposal_type"] == "quote_draft", draft.text
    quote_id = draft.json()["proposal_id"]
    async with tenant_session(runtime_session_factory, tid) as session:
        # Record the verified buyer without changing the generated price snapshot.
        await session.execute(
            text("""UPDATE ai_proposals SET proposed_payload = proposed_payload ||
                jsonb_build_object('customer_id', CAST(:cid AS text)) WHERE id = :qid"""),
            {"cid": str(customer), "qid": UUID(quote_id)},
        )
        await session.execute(text("UPDATE product_variants SET retail_price_usd = 16"))
    review = await async_client.post(f"{_ROOT}/proposals/{quote_id}/approve", headers=headers)
    assert review.status_code == 200 and review.json()["status"] == "failed", review.text
    assert review.json()["execution_result"]["code"] == "ERR_PROPOSAL_STALE"
    fresh = await async_client.post(f"{_ROOT}/assistant/chat", headers=headers, json=body)
    fresh_id = fresh.json()["proposal_id"]
    detail = await async_client.get(f"{_ROOT}/proposals/{fresh_id}", headers=headers)
    payload = {**detail.json()["proposed_payload"], "customer_id": str(customer)}
    approval = await async_client.post(
        f"{_ROOT}/proposals/{fresh_id}/edit-approve",
        headers=headers,
        json={"edited_payload": payload},
    )
    assert approval.status_code == 200, approval.text
    executed = approval.json()["execution_result"]
    assert executed["status"] == "draft"
    async with tenant_session(runtime_session_factory, tid) as session:
        created = (await session.execute(text("SELECT id, total_amount, status FROM quotes"))).one()
        assert str(created.id) == executed["quote_id"]
        assert created.status == "draft" and created.total_amount == 320
