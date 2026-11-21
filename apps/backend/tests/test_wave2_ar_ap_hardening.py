"""Wave-2 finance hardening: AR overpayment/duplicate-ref guards, AP idempotency, FX honesty.

Every assertion runs against real PostgreSQL through the same tenant-scoped RLS session
the routers use - nothing is mocked at the data layer:

* AR overpayment is rejected on both the direct (`PaymentService.record_payment`) and the
  receipt-verification (`submit_payment_receipt` -> `verify_payment`) paths, and one bank
  transaction reference can only ever be booked once (409, backed by the 0013 unique index).
* AP partial payments are written to the immutable `payable_payments` ledger, keyed by
  ``(tenant_id, idempotency_key)``, so a retried request never double-counts `paid_amount`
  while a *different* amount under the same key is a 409 conflict.
* `draft` purchase orders are never booked as AP liabilities (nor are `cancelled` ones),
  and an unpaid orphan payable of a PO that fell back to draft/cancelled is purged.
* `GET /cashflow-summary` reports a per-currency breakdown, so USD/EUR flows are never
  hidden behind a base-currency-only total.
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.api_main import app as _app  # noqa: F401  (registers every ORM model on Base)
from erp_core.finance.ap_router import router as ap_router
from erp_core.finance.ap_service import CashflowSummaryRead, PayableListRead, PayableRead
from erp_core.finance.payment_service import PaymentService
from erp_core.finance.schemas import PaymentCreate
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import AppError, register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
AP_BASE = "/api/v1/finance"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def client_for(factory: Factory, principal: CurrentPrincipal) -> AsyncClient:
    """Isolated app: only the AP router, principal injected via dependency override."""
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(ap_router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _principal(tenant_id: UUID, role: UserRole, user_id: UUID) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=user_id, tenant_id=tenant_id, role=role, email=f"{role.value}@w2c.example.com"
    )


async def _seed_tenant(factory: Factory, tid: UUID, slug: str) -> dict[str, UUID]:
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
    tenant = uuid4()
    users = await _seed_tenant(runtime_session_factory, tenant, f"w2c-{tenant.hex}")
    yield {
        "factory": runtime_session_factory,
        "tenant": tenant,
        "owner": _principal(tenant, UserRole.OWNER, users["owner"]),
        "finance": _principal(tenant, UserRole.FINANCE, users["finance"]),
        "manager": _principal(tenant, UserRole.MANAGER, users["manager"]),
        "user_id": users["owner"],
    }


# --------------------------------------------------------------------------- #
# fixture helpers (direct SQL, exactly like the routers do)
# --------------------------------------------------------------------------- #
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


async def _receivable(
    session: AsyncSession,
    tid: UUID,
    order_no: str,
    due: str,
    paid: str = "0.00",
    *,
    currency: str = "CNY",
) -> UUID:
    """One B2B order plus its deposit receivable; returns the `receivables_payables` id."""
    order_id, rp_id = uuid4(), uuid4()
    customer_id = await _customer(session, tid, f"Buyer {order_no}")
    await session.execute(
        text(
            "INSERT INTO orders (id, tenant_id, customer_id, order_no, order_mode, currency, "
            "fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status, payment_status) "
            "VALUES (:id, :tid, :cid, :order_no, 'b2b_wholesale', :ccy, 7.2, :total, :total, "
            "'pending_payment', 'unpaid')"
        ),
        {
            "id": order_id,
            "tid": tid,
            "cid": customer_id,
            "order_no": order_no,
            "ccy": currency,
            "total": Decimal(due),
        },
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
            "status": "settled" if Decimal(paid) >= Decimal(due) else "open",
        },
    )
    return rp_id


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


def _slip(
    rp_id: UUID, amount: str, *, ref: str | None = None, currency: str = "CNY"
) -> PaymentCreate:
    return PaymentCreate(
        rp_id=rp_id,
        payment_method="tt_wire",
        external_tx_ref=ref,
        amount_received=Decimal(amount),
        currency=currency,
        actual_fx_rate=Decimal("1"),
        gateway_fee=Decimal("0.00"),
    )


# --------------------------------------------------------------------------- #
# 1. AR overpayment guard (direct + receipt verification) and duplicate refs
# --------------------------------------------------------------------------- #
async def test_ar_direct_payment_rejects_overpayment_and_resettling(workspace) -> None:
    factory, tid = workspace["factory"], workspace["tenant"]
    payments = PaymentService()
    async with tenant_session(factory, tid) as session:
        rp_id = await _receivable(session, tid, "W2C-AR-1", "300.00")

    with pytest.raises(AppError) as overpay:
        async with tenant_session(factory, tid) as session:
            await payments.record_payment(
                session,
                tenant_id=tid,
                rp_id=rp_id,
                amount_received=Decimal("400.00"),
                currency="CNY",
                actual_fx_rate=Decimal("1"),
            )
    assert overpay.value.status_code == 422
    assert overpay.value.code == "ERR_RECEIVABLE_OVERPAYMENT"
    assert overpay.value.detail == "Payment exceeds remaining receivable balance"

    # The rejected attempt must leave the ledger untouched.
    async with tenant_session(factory, tid) as session:
        paid = await session.scalar(
            text(
                "SELECT paid_amount FROM receivables_payables "
                "WHERE tenant_id = :tid AND id = :rid"
            ),
            {"tid": tid, "rid": rp_id},
        )
        assert Decimal(str(paid)) == Decimal("0.00")

    # Settle exactly, then any further settlement is a 409 - never a silent overpay.
    async with tenant_session(factory, tid) as session:
        await payments.record_payment(
            session,
            tenant_id=tid,
            rp_id=rp_id,
            amount_received=Decimal("300.00"),
            currency="CNY",
            actual_fx_rate=Decimal("1"),
        )
    with pytest.raises(AppError) as resettled:
        async with tenant_session(factory, tid) as session:
            await payments.record_payment(
                session,
                tenant_id=tid,
                rp_id=rp_id,
                amount_received=Decimal("1.00"),
                currency="CNY",
                actual_fx_rate=Decimal("1"),
            )
    assert resettled.value.status_code == 409
    assert resettled.value.code == "ERR_RECEIVABLE_ALREADY_SETTLED"


async def test_ar_verified_receipt_rejects_overpayment_at_verification(workspace) -> None:
    factory, tid, finance = workspace["factory"], workspace["tenant"], workspace["finance"]
    payments = PaymentService()
    async with tenant_session(factory, tid) as session:
        rp_id = await _receivable(session, tid, "W2C-AR-2", "100.00")

    # Two receipts are each individually valid when submitted (60 + 50 against 100 due).
    async with tenant_session(factory, tid) as session:
        first = await payments.submit_payment_receipt(
            session, tid, finance, _slip(rp_id, "60.00", ref="W2C-SLIP-A")
        )
    async with tenant_session(factory, tid) as session:
        second = await payments.submit_payment_receipt(
            session, tid, finance, _slip(rp_id, "50.00", ref="W2C-SLIP-B")
        )

    async with tenant_session(factory, tid) as session:
        await payments.verify_payment(session, tid, finance, first.id)
    with pytest.raises(AppError) as overpay:
        async with tenant_session(factory, tid) as session:
            await payments.verify_payment(session, tid, finance, second.id)
    assert overpay.value.status_code == 422
    assert overpay.value.code == "ERR_RECEIVABLE_OVERPAYMENT"

    # Verification rolled back atomically: 60 collected, second slip still unverified.
    async with tenant_session(factory, tid) as session:
        row = (
            await session.execute(
                text(
                    "SELECT rp.paid_amount, rp.status FROM receivables_payables rp "
                    "WHERE rp.tenant_id = :tid AND rp.id = :rid"
                ),
                {"tid": tid, "rid": rp_id},
            )
        ).mappings().one()
        assert Decimal(str(row["paid_amount"])) == Decimal("60.00")
        assert str(row["status"]) == "partial"
        unverified = await session.scalar(
            text(
                "SELECT verified_by_user_id FROM payment_records "
                "WHERE tenant_id = :tid AND id = :pid"
            ),
            {"tid": tid, "pid": second.id},
        )
        assert unverified is None


async def test_ar_duplicate_bank_reference_is_rejected(workspace) -> None:
    factory, tid, finance = workspace["factory"], workspace["tenant"], workspace["finance"]
    payments = PaymentService()
    async with tenant_session(factory, tid) as session:
        rp_id = await _receivable(session, tid, "W2C-AR-3", "1000.00")

    async with tenant_session(factory, tid) as session:
        await payments.record_payment(
            session,
            tenant_id=tid,
            rp_id=rp_id,
            amount_received=Decimal("100.00"),
            currency="CNY",
            actual_fx_rate=Decimal("1"),
            external_tx_ref=" W2C-TT-1 ",
        )

    # Whitespace-normalised duplicate on the direct path.
    with pytest.raises(AppError) as duplicate:
        async with tenant_session(factory, tid) as session:
            await payments.record_payment(
                session,
                tenant_id=tid,
                rp_id=rp_id,
                amount_received=Decimal("100.00"),
                currency="CNY",
                actual_fx_rate=Decimal("1"),
                external_tx_ref="W2C-TT-1",
            )
    assert duplicate.value.status_code == 409
    assert duplicate.value.code == "ERR_DUPLICATE_PAYMENT_REF"

    # The same bank slip cannot be submitted again on the receipt path either.
    with pytest.raises(AppError) as duplicate_receipt:
        async with tenant_session(factory, tid) as session:
            await payments.submit_payment_receipt(
                session, tid, finance, _slip(rp_id, "100.00", ref="W2C-TT-1")
            )
    assert duplicate_receipt.value.status_code == 409
    assert duplicate_receipt.value.code == "ERR_DUPLICATE_PAYMENT_REF"

    async with tenant_session(factory, tid) as session:
        rows = await session.scalar(
            text(
                "SELECT COUNT(*) FROM payment_records "
                "WHERE tenant_id = :tid AND external_tx_ref = 'W2C-TT-1'"
            ),
            {"tid": tid},
        )
        assert int(rows or 0) == 1


# --------------------------------------------------------------------------- #
# 2. AP partial payment idempotency ledger
# --------------------------------------------------------------------------- #
async def test_ap_idempotent_retry_never_double_counts(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        supplier = await _supplier(session, tid, "Ningbo Idempotent Works")
        await _purchase_order(session, tid, supplier, owner, "PO-W2C-1", "1000.00")

    async with client_for(factory, workspace["finance"]) as client:
        payable = PayableListRead.model_validate(
            (await client.get(f"{AP_BASE}/payables")).json()
        ).items[0]
        url = f"{AP_BASE}/payables/{payable.id}/pay"

        body = {"amount": "400.00", "payment_ref": "W2C-AP-TT-1", "idempotency_key": "w2c-key-1"}
        first = await client.post(url, json=body)
        assert first.status_code == 200, first.text
        assert PayableRead.model_validate(first.json()).paid_amount == Decimal("400.00")

        # Same key + same amount: idempotent replay, no second ledger row.
        replay = await client.post(url, json=body)
        assert replay.status_code == 200, replay.text
        replayed = PayableRead.model_validate(replay.json())
        assert replayed.paid_amount == Decimal("400.00")
        assert replayed.status == "partial"

        # Same key, different amount: a hard conflict, not a second payment.
        conflict = await client.post(
            url, json={"amount": "600.00", "idempotency_key": "w2c-key-1"}
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "ERR_AP_IDEMPOTENCY_CONFLICT"

        # `Idempotency-Key` header wins and behaves identically.
        header_body = {"amount": "100.00", "payment_ref": "W2C-AP-TT-2"}
        header_first = await client.post(
            url, json=header_body, headers={"Idempotency-Key": "w2c-hdr-1"}
        )
        assert header_first.status_code == 200, header_first.text
        header_replay = await client.post(
            url, json=header_body, headers={"Idempotency-Key": "w2c-hdr-1"}
        )
        assert header_replay.status_code == 200, header_replay.text
        assert PayableRead.model_validate(header_replay.json()).paid_amount == Decimal("500.00")

        # No header and no body key: `payment_ref` is the natural ledger key.
        ref_body = {"amount": "50.00", "payment_ref": "W2C-AP-TT-3"}
        await client.post(url, json=ref_body)
        ref_replay = await client.post(url, json=ref_body)
        assert ref_replay.status_code == 200, ref_replay.text
        final = PayableRead.model_validate(ref_replay.json())
        assert final.paid_amount == Decimal("550.00")
        assert final.status == "partial"

    async with tenant_session(factory, tid) as session:
        ledger = (
            await session.execute(
                text(
                    "SELECT idempotency_key, amount FROM payable_payments "
                    "WHERE tenant_id = :tid ORDER BY idempotency_key"
                ),
                {"tid": tid},
            )
        ).mappings().all()
        assert sorted(str(row["idempotency_key"]) for row in ledger) == [
            "W2C-AP-TT-3",
            "w2c-hdr-1",
            "w2c-key-1",
        ]
        amounts = {str(row["idempotency_key"]): Decimal(str(row["amount"])) for row in ledger}
        assert amounts == {
            "w2c-key-1": Decimal("400.00"),
            "w2c-hdr-1": Decimal("100.00"),
            "W2C-AP-TT-3": Decimal("50.00"),
        }
        assert len(ledger) == 3
        paid = await session.scalar(
            text("SELECT paid_amount FROM payables WHERE tenant_id = :tid AND id = :pid"),
            {"tid": tid, "pid": payable.id},
        )
        assert Decimal(str(paid)) == Decimal("550.00")


async def test_ap_same_key_on_another_payable_is_a_conflict(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        supplier = await _supplier(session, tid, "Wenzhou Ledger Co")
        await _purchase_order(session, tid, supplier, owner, "PO-W2C-2", "500.00")
        await _purchase_order(session, tid, supplier, owner, "PO-W2C-3", "500.00")

    async with client_for(factory, workspace["owner"]) as client:
        items = PayableListRead.model_validate(
            (await client.get(f"{AP_BASE}/payables")).json()
        ).items
        assert len(items) == 2
        first, second = items[0], items[1]

        ok = await client.post(
            f"{AP_BASE}/payables/{first.id}/pay",
            json={"amount": "100.00", "idempotency_key": "w2c-shared-key"},
        )
        assert ok.status_code == 200, ok.text

        reused = await client.post(
            f"{AP_BASE}/payables/{second.id}/pay",
            json={"amount": "100.00", "idempotency_key": "w2c-shared-key"},
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "ERR_AP_IDEMPOTENCY_CONFLICT"

    async with tenant_session(factory, tid) as session:
        untouched = await session.scalar(
            text("SELECT paid_amount FROM payables WHERE tenant_id = :tid AND id = :pid"),
            {"tid": tid, "pid": second.id},
        )
        assert Decimal(str(untouched)) == Decimal("0.00")


# --------------------------------------------------------------------------- #
# 3. Only committed POs become AP liabilities
# --------------------------------------------------------------------------- #
async def test_draft_purchase_order_is_not_booked_but_ordered_is(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        supplier = await _supplier(session, tid, "Draft Excluder Ltd")
        await _purchase_order(
            session, tid, supplier, owner, "PO-W2C-DRAFT", "500.00", status="draft"
        )
        await _purchase_order(
            session, tid, supplier, owner, "PO-W2C-ORDERED", "700.00", status="ordered"
        )

    async with client_for(factory, workspace["manager"]) as client:
        ledger = PayableListRead.model_validate((await client.get(f"{AP_BASE}/payables")).json())
    assert {item.po_no for item in ledger.items} == {"PO-W2C-ORDERED"}
    assert ledger.total_due == Decimal("700.00")

    async with tenant_session(factory, tid) as session:
        assert int(await session.scalar(
            text("SELECT COUNT(*) FROM payables WHERE tenant_id = :tid"), {"tid": tid}
        ) or 0) == 1

    # A committed PO that is cancelled later leaves no untouched liability behind.
    async with tenant_session(factory, tid) as session:
        await session.execute(
            text(
                "UPDATE purchase_orders SET status = 'cancelled', updated_at = now() "
                "WHERE tenant_id = :tid AND po_no = 'PO-W2C-ORDERED'"
            ),
            {"tid": tid},
        )

    async with client_for(factory, workspace["manager"]) as client:
        after_cancel = PayableListRead.model_validate(
            (await client.get(f"{AP_BASE}/payables")).json()
        )
    assert after_cancel.items == []
    assert after_cancel.total_due == Decimal("0.00")

    async with tenant_session(factory, tid) as session:
        assert int(await session.scalar(
            text("SELECT COUNT(*) FROM payables WHERE tenant_id = :tid"), {"tid": tid}
        ) or 0) == 0
        audit = await session.scalar(
            text(
                "SELECT COUNT(*) FROM audit_events WHERE tenant_id = :tid AND action = "
                "'finance.payable.purged_from_uncommitted_purchase_order'"
            ),
            {"tid": tid},
        )
        assert int(audit or 0) == 1

    # A draft PO that came back is booked again, exactly once.
    async with tenant_session(factory, tid) as session:
        await session.execute(
            text(
                "UPDATE purchase_orders SET status = 'ordered', updated_at = now() "
                "WHERE tenant_id = :tid AND po_no = 'PO-W2C-DRAFT'"
            ),
            {"tid": tid},
        )

    async with client_for(factory, workspace["manager"]) as client:
        rebooked = PayableListRead.model_validate(
            (await client.get(f"{AP_BASE}/payables")).json()
        )
    assert {item.po_no for item in rebooked.items} == {"PO-W2C-DRAFT"}
    assert rebooked.items[0].status == "open"


async def test_cancelled_po_keeps_a_partially_paid_payable(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        supplier = await _supplier(session, tid, "Auditable Records Ltd")
        await _purchase_order(session, tid, supplier, owner, "PO-W2C-PAID", "800.00")

    async with client_for(factory, workspace["finance"]) as client:
        payable = PayableListRead.model_validate(
            (await client.get(f"{AP_BASE}/payables")).json()
        ).items[0]
        paid = await client.post(
            f"{AP_BASE}/payables/{payable.id}/pay", json={"amount": "300.00"}
        )
        assert paid.status_code == 200, paid.text

    async with tenant_session(factory, tid) as session:
        await session.execute(
            text(
                "UPDATE purchase_orders SET status = 'cancelled', updated_at = now() "
                "WHERE tenant_id = :tid AND po_no = 'PO-W2C-PAID'"
            ),
            {"tid": tid},
        )

    async with client_for(factory, workspace["finance"]) as client:
        kept = PayableListRead.model_validate(
            (await client.get(f"{AP_BASE}/payables")).json()
        )
    assert [item.po_no for item in kept.items] == ["PO-W2C-PAID"]
    assert kept.items[0].paid_amount == Decimal("300.00")
    assert kept.items[0].status == "partial"


# --------------------------------------------------------------------------- #
# 4. Per-currency cash-flow breakdown
# --------------------------------------------------------------------------- #
async def test_cashflow_summary_reports_per_currency_breakdown(workspace) -> None:
    factory, tid, owner = workspace["factory"], workspace["tenant"], workspace["user_id"]
    async with tenant_session(factory, tid) as session:
        # AR: USD 1000 due / 400 collected, EUR 500 due / 0 collected, CNY 200/200.
        await _receivable(session, tid, "W2C-USD-AR", "1000.00", "400.00", currency="USD")
        await _receivable(session, tid, "W2C-EUR-AR", "500.00", "0.00", currency="EUR")
        await _receivable(session, tid, "W2C-CNY-AR", "200.00", "200.00", currency="CNY")
        # AP: USD 300 PO, EUR 400 PO.
        supplier = await _supplier(session, tid, "FX Honest Supplier")
        await _purchase_order(
            session, tid, supplier, owner, "PO-W2C-USD", "300.00", currency="USD"
        )
        await _purchase_order(
            session, tid, supplier, owner, "PO-W2C-EUR", "400.00", currency="EUR"
        )

    async with client_for(factory, workspace["finance"]) as client:
        ledger = PayableListRead.model_validate((await client.get(f"{AP_BASE}/payables")).json())
        usd_payable = next(item for item in ledger.items if item.po_no == "PO-W2C-USD")
        paid = await client.post(
            f"{AP_BASE}/payables/{usd_payable.id}/pay",
            json={"amount": "120.00", "idempotency_key": "w2c-fx-1"},
        )
        assert paid.status_code == 200, paid.text

        response = await client.get(f"{AP_BASE}/cashflow-summary")
        assert response.status_code == 200, response.text
        summary = CashflowSummaryRead.model_validate(response.json())

    # Base-currency summary stays exactly what it was: CNY-denominated only.
    assert summary.currency == "CNY"
    assert summary.ar_total == Decimal("200.00")
    assert summary.ar_collected == Decimal("200.00")
    assert summary.ar_outstanding == Decimal("0.00")
    assert summary.ap_total == Decimal("0.00")
    assert summary.ap_paid == Decimal("0.00")
    assert summary.net_cashflow == Decimal("200.00")

    by_currency = {entry.currency: entry for entry in summary.by_currency}
    assert set(by_currency) == {"CNY", "USD", "EUR"}

    assert by_currency["USD"].ar_total == Decimal("1000.00")
    assert by_currency["USD"].ar_collected == Decimal("400.00")
    assert by_currency["USD"].ar_outstanding == Decimal("600.00")
    assert by_currency["USD"].ap_total == Decimal("300.00")
    assert by_currency["USD"].ap_paid == Decimal("120.00")
    assert by_currency["USD"].ap_outstanding == Decimal("180.00")
    assert by_currency["USD"].net_cashflow == Decimal("280.00")

    assert by_currency["EUR"].ar_total == Decimal("500.00")
    assert by_currency["EUR"].ar_collected == Decimal("0.00")
    assert by_currency["EUR"].ap_total == Decimal("400.00")
    assert by_currency["EUR"].ap_paid == Decimal("0.00")
    assert by_currency["EUR"].net_cashflow == Decimal("0.00")

    assert by_currency["CNY"].ar_collected == Decimal("200.00")
    assert by_currency["CNY"].net_cashflow == Decimal("200.00")

    # Exact 2-dp decimal strings on the wire - no float drift anywhere.
    raw = response.json()["by_currency"]
    assert all(isinstance(entry["net_cashflow"], str) for entry in raw)
    assert {entry["currency"]: entry["net_cashflow"] for entry in raw} == {
        "CNY": "200.00",
        "EUR": "0.00",
        "USD": "280.00",
    }
