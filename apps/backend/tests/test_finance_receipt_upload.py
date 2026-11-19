from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.db import tenant_session
from erp_core.tenancy.security import hash_password


@pytest.mark.asyncio
async def test_receipt_multipart_binds_each_form_field_to_its_own_parameter(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A shared Form(...) instance used to alias every field to payment_method."""
    tenant_id, customer_id, order_id, receivable_id = (uuid4() for _ in range(4))
    email = f"finance-{tenant_id.hex[:8]}@example.com"
    password = "FinancePass2026!"
    async with tenant_session(runtime_session_factory, tenant_id) as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:tid, 'Finance', :slug)"),
            {"tid": tenant_id, "slug": f"fin-{tenant_id.hex[:8]}"},
        )
        user_id = await session.scalar(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, 'Finance', :ph) RETURNING id"
            ),
            {"id": uuid4(), "email": email, "ph": hash_password(password)},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (:id, :tid, :uid, 'finance')"
            ),
            {"id": uuid4(), "tid": tenant_id, "uid": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:cid, :tid, 'b2b_contact', 'Buyer')"
            ),
            {"cid": customer_id, "tid": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO orders (id, tenant_id, customer_id, order_no, order_mode, "
                "currency, fx_rate_to_base, subtotal_amount, total_amount, fulfillment_status, "
                "payment_status) VALUES (:oid, :tid, :cid, 'FIN-FORM', 'b2b_wholesale', 'USD', "
                "1, 100, 100, 'pending_payment', 'unpaid')"
            ),
            {"oid": order_id, "tid": tenant_id, "cid": customer_id},
        )
        await session.execute(
            text(
                "INSERT INTO receivables_payables (id, tenant_id, order_id, direction, "
                "milestone_label, due_amount, currency, status) "
                "VALUES (:rid, :tid, :oid, 'receivable', 'b2b_30_deposit', 30, 'USD', 'open')"
            ),
            {"rid": receivable_id, "tid": tenant_id, "oid": order_id},
        )
        slug = await session.scalar(
            text("SELECT slug FROM tenants WHERE id = :tid"), {"tid": tenant_id}
        )

    login = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "tenant_slug": slug},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await async_client.post(
        f"/api/v1/finance/receivables/{receivable_id}/payments",
        headers=headers,
        data={
            "payment_method": "wire_transfer",
            "external_tx_ref": "TT-FORM-1",
            "amount_received": "30.00",
            "currency": "USD",
            "actual_fx_rate": "1",
            "gateway_fee": "0.00",
        },
        files={"receipt_file": ("slip.txt", b"T/T slip", "text/plain")},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert UUID(body["rp_id"]) == receivable_id
    assert body["payment_method"] == "wire_transfer"
    assert body["amount_received"] == "30.00"
    assert body["actual_fx_rate"] == "1.000000"
