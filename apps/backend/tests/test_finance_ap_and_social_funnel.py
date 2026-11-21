"""Finance supplier AP (payables) + social attribution funnel, verified on real PostgreSQL.

Nothing is mocked at the data layer: every assertion is made against rows written
by the routers into `payables`, `audit_events`, `receivables_payables` and the
social/CRM tables, inside a tenant-scoped RLS transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.finance.ap_router import router as ap_router
from erp_core.finance.ap_service import (
    CashflowSummaryRead,
    PayableListRead,
    PayablePaymentRequest,
    PayableRead,
)
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.social.attribution_router import router as attribution_router
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
AP_BASE = "/api/v1/finance"
SOCIAL_BASE = "/api/v1/social"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def client_for(factory: Factory, principal: CurrentPrincipal) -> AsyncClient:
    """Isolated app: only the two new routers, principal injected via dependency override."""
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(ap_router)
    app.include_router(attribution_router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _principal(tenant_id: UUID, role: UserRole, user_id: UUID) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=user_id, tenant_id=tenant_id, role=role, email=f"{role.value}@ap.example.com"
    )


async def _seed_tenant(factory: Factory, tid: UUID, slug: str) -> dict[str, UUID]:
    """Tenant, base currency and one user+membership per role plus an outsider."""
    users = {role.value: uuid4() for role in UserRole}
    async with tenant_session(factory, tid) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, :name, 'CNY')"
            ),
            {"id": tid, "slug": slug, "name": f"Tenant {slug}"},
        )
        for role, uid in users.items():
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, :name, 'test')"
                ),
                {"id": uid, "email": f"{uid.hex}@{slug}.example.com", "name": role},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (:tid, :uid, :role)"
                ),
                {"tid": tid, "uid": uid, "role": role},
            )
    return users


@pytest.fixture
async def workspace(runtime_session_factory: Factory) -> AsyncIterator[dict[str, Any]]:
    tenant, other = uuid4(), uuid4()
    users = await _seed_tenant(runtime_session_factory, tenant, f"ap-{tenant.hex}")
    other_users = await _seed_tenant(runtime_session_factory, other, f"ap-{other.hex}")
    yield {
        "factory": runtime_session_factory,
        "tenant": tenant,
        "other_tenant": other,
        "owner": _principal(tenant, UserRole.OWNER, users["owner"]),
        "finance": _principal(tenant, UserRole.FINANCE, users["finance"]),
        "manager": _principal(tenant, UserRole.MANAGER, users["manager"]),
        "agent": _principal(tenant, UserRole.AGENT, users["agent"]),
        "user_id": users["owner"],
        "outsider": _principal(other, UserRole.OWNER, other_users["owner"]),
    }


# --------------------------------------------------------------------------- #
# fixture helpers (direct SQL, exactly like the routers do)
# --------------------------------------------------------------------------- #
async def _supplier(session: AsyncSession, tid: UUID, name: str) -> UUID:
    sid = uuid4()
    await session.execute(
        text("INSERT INTO suppliers (id, tenant_id, code, name) VALUES (:id, :tid, :code, :name)"),
        {"id": sid, "tid": tid, "code": f"S-{sid.hex[:8]}", "name": name},
    )
    return sid


async def _purchase_order(
    session: AsyncSession,
    tid: UUID,
    supplier_id: UUID,
    created_by: UUID,
    po_no: str,
    total: str,
    *,
    currency: str = "CNY",
    status: str = "ordered",
) -> UUID:
    po_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO purchase_orders "
            "(id, tenant_id, po_no, supplier_id, warehouse_id, currency, "
            " total_amount, status, expected_arrival_date, created_by) "
            "VALUES (:id, :tid, :po_no, :sid, :wid, :ccy, :total, :status, "
            " :due_date, :created_by)"
        ),
        {
            "id": po_id,
            "tid": tid,
            "po_no": po_no,
            "sid": supplier_id,
            "wid": uuid4(),
            "ccy": currency,
            "total": Decimal(total),
            "status": status,
            "due_date": datetime(2026, 12, 31, tzinfo=UTC).date(),
            "created_by": created_by,
        },
    )
    return po_id


async def _receivable(
    session: AsyncSession,
    tid: UUID,
    customer_id: UUID,
    order_no: str,
    due: str,
    paid: str,
    *,
    currency: str = "CNY",
) -> None:
    order_id, rp_id = uuid4(), uuid4()
    await session.execute(
        text(
            "INSERT INTO orders (id, tenant_id, customer_id, order_no, order_mode, currency, "
            "fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status, payment_status) "
            "VALUES (:id, :tid, :cid, :order_no, 'b2b_wholesale', :ccy, 7.2, :total, :total, "
            "'pending_payment', 'unpaid')"
        ),
        {"id": order_id, "tid": tid, "cid": customer_id, "order_no": order_no, "ccy": currency,
         "total": Decimal(due)},
    )
    await session.execute(
        text(
            "INSERT INTO receivables_payables (id, tenant_id, order_id, direction, "
            "milestone_label, due_amount, paid_amount, currency, status) "
            "VALUES (:id, :tid, :oid, 'receivable', 'b2b_30_deposit', :due, :paid, :ccy, "
            ":status)"
        ),
        {
            "id": rp_id,
            "tid": tid,
            "oid": order_id,
            "due": Decimal(due),
            "paid": Decimal(paid),
            "ccy": currency,
            "status": "settled" if Decimal(paid) >= Decimal(due) else "partial",
        },
    )


async def _customer(session: AsyncSession, tid: UUID, name: str) -> UUID:
    cid = uuid4()
    await session.execute(
        text(
            "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
            "VALUES (:id, :tid, 'b2b_contact', :name)"
        ),
        {"id": cid, "tid": tid, "name": name},
    )
    return cid


async def _lead(
    session: AsyncSession, tid: UUID, customer_id: UUID, source: str, status: str
) -> UUID:
    lead_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO leads (id, tenant_id, customer_id, source_channel, lead_type, status) "
            "VALUES (:id, :tid, :cid, :source, 'b2b_inquiry', :status)"
        ),
        {"id": lead_id, "tid": tid, "cid": customer_id, "source": source, "status": status},
    )
    return lead_id


async def _post(
    session: AsyncSession, tid: UUID, created_by: UUID, platform: str, status: str
) -> None:
    await session.execute(
        text(
            "INSERT INTO social_posts (tenant_id, platform, caption, status, published_at, "
            "created_by) VALUES (:tid, :platform, 'Work light launch', :status, :published_at, "
            ":created_by)"
        ),
        {
            "tid": tid,
            "platform": platform,
            "status": status,
            "published_at": datetime.now(UTC) if status == "published" else None,
            "created_by": created_by,
        },
    )


async def _mention(
    session: AsyncSession,
    tid: UUID,
    platform: str,
    handle: str,
    *,
    converted_lead_id: UUID | None = None,
) -> UUID:
    mention_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO social_mentions (id, tenant_id, platform, author_handle, content_text, "
            "sentiment, converted_lead_id) VALUES (:id, :tid, :platform, :handle, "
            "'FOB price please', 'lead_intent', :lead)"
        ),
        {"id": mention_id, "tid": tid, "platform": platform, "handle": handle,
         "lead": converted_lead_id},
    )
    return mention_id


async def _quote(
    session: AsyncSession, tid: UUID, customer_id: UUID, owner_user_id: UUID, lead_id: UUID, no: str
) -> UUID:
    opportunity_id, quote_id = uuid4(), uuid4()
    await session.execute(
        text(
            "INSERT INTO opportunities (id, tenant_id, lead_id, customer_id, title, stage, "
            "owner_user_id) VALUES (:id, :tid, :lead, :cid, 'FOB quote', 'quoting', :owner)"
        ),
        {"id": opportunity_id, "tid": tid, "lead": lead_id, "cid": customer_id,
         "owner": owner_user_id},
    )
    await session.execute(
        text(
            "INSERT INTO quotes (id, tenant_id, opportunity_id, quote_no, incoterm, "
            "payment_terms, fx_rate_snapshot, fx_locked_until, total_amount, status) "
            "VALUES (:id, :tid, :oid, :no, 'FOB', 'TT 30/70', 7.2, :locked, 1000.00, 'sent')"
        ),
        {"id": quote_id, "tid": tid, "oid": opportunity_id, "no": no,
         "locked": datetime.now(UTC)},
    )
    return quote_id


# --------------------------------------------------------------------------- #
# 1. PO -> AP auto-booking
# --------------------------------------------------------------------------- #
async def test_purchase_orders_autobook_as_ap_payables_idempotently(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        supplier = await _supplier(session, tid, "Ningbo Lighting Works")
        await _purchase_order(session, tid, supplier, owner, "PO-1001", "1000.00")
        await _purchase_order(session, tid, supplier, owner, "PO-1002", "250.50")
        await _purchase_order(
            session, tid, supplier, owner, "PO-1003", "999.00", status="cancelled"
        )

    # A second supplier payable but in another currency must not pollute CNY totals.
    async with tenant_session(factory, tid) as session:
        usd_supplier = await _supplier(session, tid, "Shenzhen Export Ltd")
        await _purchase_order(
            session, tid, usd_supplier, owner, "PO-2001", "500.00", currency="USD"
        )

    async with client_for(factory, workspace["manager"]) as client:
        first = await client.get(f"{AP_BASE}/payables")
        assert first.status_code == 200, first.text
        ledger = PayableListRead.model_validate(first.json())
        assert ledger.currency == "CNY"
        assert {item.po_no for item in ledger.items} == {"PO-1001", "PO-1002", "PO-2001"}
        assert all(item.status == "open" for item in ledger.items)
        assert all(item.paid_amount == Decimal("0.00") for item in ledger.items)
        assert {item.supplier_name for item in ledger.items} == {
            "Ningbo Lighting Works",
            "Shenzhen Export Ltd",
        }
        assert ledger.total_due == Decimal("1250.50")
        assert ledger.total_paid == Decimal("0.00")
        assert ledger.total_outstanding == Decimal("1250.50")

        # Idempotent: a second read must not double-book the same POs.
        second = await client.get(f"{AP_BASE}/payables")
        assert second.status_code == 200, second.text
        assert PayableListRead.model_validate(second.json()).items == ledger.items

    async with tenant_session(factory, tid) as session:
        booked = await session.scalar(
            text("SELECT COUNT(*) FROM payables WHERE tenant_id = :tid"), {"tid": tid}
        )
        assert int(booked or 0) == 3  # PO-1003 was cancelled, so it was never a liability
        audited = await session.scalar(
            text(
                "SELECT COUNT(*) FROM audit_events WHERE tenant_id = :tid "
                "AND action = 'finance.payable.created_from_purchase_order'"
            ),
            {"tid": tid},
        )
        assert int(audited or 0) == 3


async def test_ap_read_rbac_blocks_agent_and_finance_can_read(workspace) -> None:
    async with client_for(workspace["factory"], workspace["agent"]) as client:
        assert (await client.get(f"{AP_BASE}/payables")).status_code == 403
        assert (await client.get(f"{AP_BASE}/cashflow-summary")).status_code == 403
    async with client_for(workspace["factory"], workspace["finance"]) as client:
        assert (await client.get(f"{AP_BASE}/payables")).status_code == 200
        assert (await client.get(f"{AP_BASE}/cashflow-summary")).status_code == 200


# --------------------------------------------------------------------------- #
# 2. open -> partial -> paid, audit trail, RBAC
# --------------------------------------------------------------------------- #
async def test_payable_status_machine_open_partial_paid_with_audit(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        supplier = await _supplier(session, tid, "Huawei Components")
        await _purchase_order(session, tid, supplier, owner, "PO-3001", "1000.00")

    async with client_for(factory, workspace["finance"]) as client:
        ledger = PayableListRead.model_validate((await client.get(f"{AP_BASE}/payables")).json())
        payable = ledger.items[0]
        assert payable.status == "open"
        url = f"{AP_BASE}/payables/{payable.id}/pay"

        # Partial settlement keeps the payable open, but at `partial`.
        partial = await client.post(
            url, json={"amount": "400.00", "payment_ref": " TT-2026-0001 "}
        )
        assert partial.status_code == 200, partial.text
        after_partial = PayableRead.model_validate(partial.json())
        assert after_partial.status == "partial"
        assert after_partial.paid_amount == Decimal("400.00")
        assert after_partial.payment_ref == "TT-2026-0001"
        assert after_partial.paid_at is not None

        # The balance closes the document exactly: no rounding residue.
        settled = await client.post(
            url,
            json={
                "amount": "600.00",
                "payment_ref": "TT-2026-0002",
                "paid_at": "2026-11-30T10:00:00Z",
            },
        )
        assert settled.status_code == 200, settled.text
        after_settled = PayableRead.model_validate(settled.json())
        assert after_settled.status == "paid"
        assert after_settled.paid_amount == Decimal("1000.00")
        assert after_settled.payment_ref == "TT-2026-0002"
        assert after_settled.paid_at is not None
        assert after_settled.paid_at.year == 2026

        # A settled payable can never be paid again.
        again = await client.post(url, json={"amount": "1.00"})
        assert again.status_code == 409
        assert again.json()["code"] == "ERR_PAYABLE_ALREADY_PAID"

    async with client_for(factory, workspace["manager"]) as client:
        denied = await client.post(
            f"{AP_BASE}/payables/{payable.id}/pay", json={"amount": "1.00"}
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "ERR_INSUFFICIENT_ROLE"

    async with tenant_session(factory, tid) as session:
        events = (
            await session.execute(
                text(
                    "SELECT actor_type, actor_id, before_state, after_state FROM audit_events "
                    "WHERE tenant_id = :tid AND action = 'finance.payable.paid' "
                    "AND resource_id = :rid ORDER BY id"
                ),
                {"tid": tid, "rid": payable.id},
            )
        ).mappings().all()
        assert len(events) == 2
        assert {str(event["actor_type"]) for event in events} == {"human_user"}
        assert {str(event["actor_id"]) for event in events} == {str(workspace["finance"].user_id)}
        assert str(events[0]["before_state"]["status"]) == "open"
        assert str(events[0]["after_state"]["status"]) == "partial"
        assert str(events[1]["after_state"]["status"]) == "paid"

        ledger_totals = (
            await session.execute(
                text(
                    "SELECT SUM(due_amount) AS due, SUM(paid_amount) AS paid "
                    "FROM payables WHERE tenant_id = :tid"
                ),
                {"tid": tid},
            )
        ).mappings().one()
        assert Decimal(str(ledger_totals["due"])) == Decimal("1000.00")
        assert Decimal(str(ledger_totals["paid"])) == Decimal("1000.00")


async def test_unknown_payable_returns_404(workspace) -> None:
    async with client_for(workspace["factory"], workspace["owner"]) as client:
        response = await client.post(
            f"{AP_BASE}/payables/{uuid4()}/pay", json={"amount": "10.00"}
        )
    assert response.status_code == 404
    assert response.json()["code"] == "ERR_PAYABLE_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 3. Decimal precision, float rejection, overpayment
# --------------------------------------------------------------------------- #
def test_payable_payment_request_rejects_float_and_zero() -> None:
    with pytest.raises(ValidationError):
        PayablePaymentRequest(amount=12.5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PayablePaymentRequest(amount=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PayablePaymentRequest(amount=Decimal("0"))
    with pytest.raises(ValidationError):
        PayablePaymentRequest(amount=Decimal("-1.00"))

    accepted = PayablePaymentRequest(amount=Decimal("0.10"))
    assert accepted.amount == Decimal("0.10")
    assert accepted.payment_ref == ""


async def test_api_rejects_json_float_and_overpayment(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        supplier = await _supplier(session, tid, "Zhejiang Mould Co")
        await _purchase_order(session, tid, supplier, owner, "PO-4001", "0.30")

    async with client_for(factory, workspace["owner"]) as client:
        payable = PayableListRead.model_validate(
            (await client.get(f"{AP_BASE}/payables")).json()
        ).items[0]
        url = f"{AP_BASE}/payables/{payable.id}/pay"

        as_float = await client.post(url, json={"amount": 0.1})
        assert as_float.status_code == 422
        assert as_float.json()["code"] == "ERR_VALIDATION"

        overpay = await client.post(url, json={"amount": "0.40"})
        assert overpay.status_code == 422
        assert overpay.json()["code"] == "ERR_PAYABLE_OVERPAYMENT"

        # 0.1 + 0.1 + 0.1 must land exactly on 0.30 - never 0.30000000000000004.
        for _ in range(3):
            response = await client.post(url, json={"amount": "0.10"})
            assert response.status_code == 200, response.text
            paid = PayableRead.model_validate(response.json()).paid_amount
            assert str(paid) == str(paid.quantize(Decimal("0.01")))
        final = PayableRead.model_validate(response.json())
        assert final.paid_amount == Decimal("0.30")
        assert final.status == "paid"


# --------------------------------------------------------------------------- #
# 4. Cash-flow summary: AR collected - AP paid
# --------------------------------------------------------------------------- #
async def test_cashflow_summary_uses_real_ar_and_ap_sql(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        buyer_a = await _customer(session, tid, "Buyer A")
        buyer_b = await _customer(session, tid, "Buyer B")
        await _receivable(session, tid, buyer_a, "B2B-AR-1", "1000.00", "300.00")
        await _receivable(session, tid, buyer_b, "B2B-AR-2", "500.00", "0.00")
        supplier = await _supplier(session, tid, "Dongguan PCB")
        await _purchase_order(session, tid, supplier, owner, "PO-5001", "800.00")
        await _purchase_order(session, tid, supplier, owner, "PO-5002", "400.00")

    async with client_for(factory, workspace["finance"]) as client:
        ledger = PayableListRead.model_validate((await client.get(f"{AP_BASE}/payables")).json())
        target = next(item for item in ledger.items if item.po_no == "PO-5001")
        paid = await client.post(
            f"{AP_BASE}/payables/{target.id}/pay",
            json={"amount": "200.00", "payment_ref": "TT-AP-200"},
        )
        assert paid.status_code == 200, paid.text

        response = await client.get(f"{AP_BASE}/cashflow-summary")
        assert response.status_code == 200, response.text
        summary = CashflowSummaryRead.model_validate(response.json())

    assert summary.currency == "CNY"
    assert summary.ar_total == Decimal("1500.00")
    assert summary.ar_collected == Decimal("300.00")
    assert summary.ar_outstanding == Decimal("1200.00")
    assert summary.ap_total == Decimal("1200.00")
    assert summary.ap_paid == Decimal("200.00")
    assert summary.ap_outstanding == Decimal("1000.00")
    # net cash flow is exactly ar_collected - ap_paid
    assert summary.net_cashflow == Decimal("100.00")
    assert summary.net_cashflow == summary.ar_collected - summary.ap_paid


async def test_cashflow_summary_is_tenant_isolated(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        buyer = await _customer(session, tid, "Buyer C")
        await _receivable(session, tid, buyer, "B2B-AR-9", "9000.00", "9000.00")
        supplier = await _supplier(session, tid, "Suzhou Cable")
        await _purchase_order(session, tid, supplier, owner, "PO-9001", "9000.00")

    async with client_for(factory, workspace["outsider"]) as client:
        other = CashflowSummaryRead.model_validate(
            (await client.get(f"{AP_BASE}/cashflow-summary")).json()
        )
        other_ledger = PayableListRead.model_validate(
            (await client.get(f"{AP_BASE}/payables")).json()
        )
    assert other.ar_total == Decimal("0.00")
    assert other.ap_total == Decimal("0.00")
    assert other.net_cashflow == Decimal("0.00")
    assert other_ledger.items == []


# --------------------------------------------------------------------------- #
# 5. Social attribution funnel
# --------------------------------------------------------------------------- #
async def test_social_attribution_funnel_counts_real_rows(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]

    async with tenant_session(factory, tid) as session:
        # 4 posts, 3 published.
        await _post(session, tid, owner, "instagram", "published")
        await _post(session, tid, owner, "instagram", "published")
        await _post(session, tid, owner, "tiktok", "published")
        await _post(session, tid, owner, "tiktok", "draft")

        # Customer A: captured from an instagram mention and converted.
        buyer_a = await _customer(session, tid, "Buyer From Instagram")
        lead_a = await _lead(session, tid, buyer_a, "social_mention", "converted")
        await _mention(session, tid, "instagram", "@lumenlight_en", converted_lead_id=lead_a)

        # Customer B: captured from a tiktok mention, still qualified (not converted).
        buyer_b = await _customer(session, tid, "Buyer From TikTok")
        lead_b = await _lead(session, tid, buyer_b, "social_mention", "qualified")
        await _mention(session, tid, "tiktok", "@lumen_wholesale", converted_lead_id=lead_b)

        # An un-converted douyin mention: it must not inflate captured leads.
        await _mention(session, tid, "douyin", "@lumen_dy")

        # Customer C: a direct instagram-sourced lead that converted without a mention.
        buyer_c = await _customer(session, tid, "Buyer Direct Instagram")
        await _lead(session, tid, buyer_c, "instagram", "converted")

        # A non-social lead (email) must be excluded entirely.
        buyer_d = await _customer(session, tid, "Buyer From Email")
        await _lead(session, tid, buyer_d, "email_inquiry", "converted")

        # Quotes reachable from the social leads (attributed 2, one per social lead).
        await _quote(session, tid, buyer_a, owner, lead_a, "QT-INSTA-1")
        await _quote(session, tid, buyer_b, owner, lead_b, "QT-TIKTOK-1")
        await _quote(session, tid, buyer_d, owner, lead_a, "QT-EMAIL-1")

    # Any authenticated role may read the funnel - use the read-only agent here.
    async with client_for(factory, workspace["agent"]) as client:
        response = await client.get(f"{SOCIAL_BASE}/attribution-funnel")
        assert response.status_code == 200, response.text
        funnel = response.json()

    assert funnel["total_posts"] == 4
    assert funnel["published_posts"] == 3
    # three social leads: A + C from instagram, B from tiktok (the douyin mention and
    # the email lead were never captured as social leads)
    assert funnel["social_leads_captured"] == 3
    # distinct converted customers with a social source: A + C (B is only qualified)
    assert funnel["converted_crm_customers"] == 2
    assert Decimal(str(funnel["lead_to_customer_rate_pct"])) == Decimal("66.67")
    # QT-INSTA-1, QT-TIKTOK-1 and QT-EMAIL-1; the last one is attributed back to
    # lead_a, which is a social lead, so all three quotes are linked.
    assert funnel["linked_quotes_count"] == 3

    by_platform = {item["platform"]: item for item in funnel["platform_breakdown"]}
    assert set(by_platform) == {"instagram", "tiktok", "douyin"}

    instagram = by_platform["instagram"]
    assert instagram["total_posts"] == 2
    assert instagram["published_posts"] == 2
    assert instagram["social_leads_captured"] == 2
    assert instagram["converted_crm_customers"] == 2
    assert Decimal(str(instagram["lead_to_customer_rate_pct"])) == Decimal("100.00")

    tiktok = by_platform["tiktok"]
    assert tiktok["total_posts"] == 2
    assert tiktok["published_posts"] == 1
    assert tiktok["social_leads_captured"] == 1
    assert tiktok["converted_crm_customers"] == 0
    assert Decimal(str(tiktok["lead_to_customer_rate_pct"])) == Decimal("0.00")

    douyin = by_platform["douyin"]
    assert douyin["total_posts"] == 0
    assert douyin["social_leads_captured"] == 0
    assert Decimal(str(douyin["lead_to_customer_rate_pct"])) == Decimal("0.00")


async def test_social_attribution_funnel_is_tenant_isolated(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        await _post(session, tid, owner, "instagram", "published")
        buyer = await _customer(session, tid, "Buyer X")
        lead = await _lead(session, tid, buyer, "instagram", "converted")
        await _mention(session, tid, "instagram", "@x", converted_lead_id=lead)

    async with client_for(factory, workspace["outsider"]) as client:
        response = await client.get(f"{SOCIAL_BASE}/attribution-funnel")
        assert response.status_code == 200, response.text
        funnel = response.json()

    assert funnel["total_posts"] == 0
    assert funnel["published_posts"] == 0
    assert funnel["social_leads_captured"] == 0
    assert funnel["converted_crm_customers"] == 0
    assert funnel["linked_quotes_count"] == 0
    assert Decimal(str(funnel["lead_to_customer_rate_pct"])) == Decimal("0.00")
    assert funnel["platform_breakdown"] == []
