"""Dual-basis settlement reconciliation, verified against real PostgreSQL 17.

Nothing is mocked at the data layer. Every assertion is made against rows written
by `SettlementService` through the real routers into `settlement_batches`,
`settlement_lines`, `payout_statements`, `orders`, `order_profit_snapshots` and
`audit_events`, inside a tenant-scoped RLS transaction running as `app_runtime`.

Covered invariants:

* PG17 `ENABLE` + `FORCE` ROW LEVEL SECURITY with the `RESTRICTIVE` tenant policy
  and the `app_runtime` grants on all three tables, plus real cross-tenant
  isolation (read, update, delete and cross-tenant insert).
* Strict `Decimal`/`ROUND_HALF_UP` cent rounding - a discriminating
  `ROUND_HALF_EVEN` case is asserted explicitly.
* CSV and JSON settlement import, `external_order_id` / `order_no` auto-linking,
  and per-batch Decimal totals.
* Dual-basis variance: estimated commission / fulfillment+storage / ad versus the
  settled statement, plus the settled net profit after COGS and FX variance.
* PingPong / LianLian payout import, actual-vs-expected FX and the exact
  `fx_variance_base` gain, then auto-match onto an open batch (`reconciled`) and
  onto a paid B2B order.
* Balanced `Debit == Credit` on the journal export in `standard`, `quickbooks`,
  `xero` and `kingdee` dialects.
* RBAC: `AGENT` is refused with 403 on every route; `MANAGER` may read but never
  mutate a payout.
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Composition root: importing it registers every domain table, so the
# `settlement_*` foreign keys (tenants / orders / channel_accounts) resolve.
import erp_core.api_main  # noqa: F401
from erp_core.finance.settlement_router import router as settlement_router
from erp_core.finance.settlement_service import SETTLEMENT_READ_ROLES, SETTLEMENT_WRITE_ROLES
from erp_core.shared.db import tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

Factory = async_sessionmaker[AsyncSession]
BASE = "/api/v1/finance"
TABLES = ("settlement_batches", "settlement_lines", "payout_statements")

SETTLEMENT_CSV_HEADER = (
    "external_order_id,sku,gross_sales_ccy,commission_ccy,"
    "fulfillment_fee_ccy,storage_fee_ccy,ad_fee_ccy,refund_ccy"
)
PAYOUT_CSV_HEADER = (
    "external_tx_ref,payout_currency,payout_amount,settled_base_currency,"
    "settled_base_amount,expected_fx_rate"
)


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def client_for(factory: Factory, principal: CurrentPrincipal) -> AsyncClient:
    """Isolated app carrying only the settlement router with an injected principal."""
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.include_router(settlement_router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _principal(tenant_id: UUID, role: UserRole, user_id: UUID) -> CurrentPrincipal:
    return CurrentPrincipal(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email=f"{role.value}@settlement.example.com",
    )


async def _seed_tenant(factory: Factory, tid: UUID, slug: str) -> dict[str, UUID]:
    """Tenant with CNY base currency and one user+membership per role."""
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
    users = await _seed_tenant(runtime_session_factory, tenant, f"stl-{tenant.hex[:12]}")
    other_users = await _seed_tenant(runtime_session_factory, other, f"stl-{other.hex[:12]}")
    yield {
        "factory": runtime_session_factory,
        "tenant": tenant,
        "other_tenant": other,
        "owner": _principal(tenant, UserRole.OWNER, users["owner"]),
        "finance": _principal(tenant, UserRole.FINANCE, users["finance"]),
        "admin": _principal(tenant, UserRole.ADMIN, users["admin"]),
        "manager": _principal(tenant, UserRole.MANAGER, users["manager"]),
        "agent": _principal(tenant, UserRole.AGENT, users["agent"]),
        "user_id": users["owner"],
        "outsider": _principal(other, UserRole.OWNER, other_users["owner"]),
    }


# --------------------------------------------------------------------------- #
# seed helpers (raw SQL, exactly the shape the routers write)
# --------------------------------------------------------------------------- #
async def _seed_customer(session: AsyncSession, tid: UUID) -> UUID:
    customer_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
            "VALUES (:id, :tid, 'b2c_consumer', :name)"
        ),
        {"id": customer_id, "tid": tid, "name": f"Customer {customer_id.hex[:6]}"},
    )
    return customer_id


async def _seed_order(
    session: AsyncSession,
    tid: UUID,
    *,
    order_no: str,
    currency: str = "USD",
    fx: str = "7.100000",
    total: str = "100.00",
    external_order_id: str | None = None,
    order_mode: str = "b2c_retail",
    payment_status: str = "paid",
) -> UUID:
    customer_id = await _seed_customer(session, tid)
    order_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO orders (
                id, tenant_id, order_no, order_mode, external_order_id, customer_id,
                currency, fx_rate_to_base, subtotal_amount, total_amount,
                fulfillment_status, payment_status
            ) VALUES (
                :id, :tid, :order_no, :order_mode, :ext, :customer,
                :currency, :fx, :total, :total, 'shipped', :payment_status
            )
            """
        ),
        {
            "id": order_id,
            "tid": tid,
            "order_no": order_no,
            "order_mode": order_mode,
            "ext": external_order_id,
            "customer": customer_id,
            "currency": currency,
            "fx": Decimal(fx),
            "total": Decimal(total),
            "payment_status": payment_status,
        },
    )
    return order_id


async def _seed_snapshot(
    session: AsyncSession,
    tid: UUID,
    order_id: UUID,
    *,
    gross: str,
    cogs: str,
    commission: str,
    gateway: str,
    shipping: str,
    ad: str,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO order_profit_snapshots (
                order_id, tenant_id, base_currency, gross_revenue_base, cogs_base,
                platform_commission_base, payment_gateway_fee_base, shipping_and_duty_base,
                channel_msg_cost_base, ad_attribution_cost_base, fx_gain_loss_base
            ) VALUES (
                :oid, :tid, 'CNY', :gross, :cogs, :commission, :gateway, :shipping,
                '0.00', :ad, '0.00'
            )
            """
        ),
        {
            "oid": order_id,
            "tid": tid,
            "gross": Decimal(gross),
            "cogs": Decimal(cogs),
            "commission": Decimal(commission),
            "gateway": Decimal(gateway),
            "shipping": Decimal(shipping),
            "ad": Decimal(ad),
        },
    )


def _parse_csv(body: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(body))
    return list(reader.fieldnames or []), list(reader)


# --------------------------------------------------------------------------- #
# migration + RLS
# --------------------------------------------------------------------------- #
def test_migration_revision_chain_is_locked() -> None:
    script = ScriptDirectory(str(Path(__file__).parents[1] / "alembic"))
    revision = script.get_revision("0022_finance_dual_settlement")
    assert revision is not None
    assert revision.down_revision == "0021_b2b_buyer_portal"


async def test_pg17_force_rls_policies_and_runtime_grants(
    migrator_session_factory: Factory,
) -> None:
    async with migrator_session_factory() as session:
        assert 170000 <= int(await session.scalar(text("SHOW server_version_num"))) < 190000
        for name in TABLES:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT c.relrowsecurity, c.relforcerowsecurity,
                               EXISTS (
                                   SELECT 1 FROM pg_policy p
                                   WHERE p.polrelid = c.oid
                                     AND p.polname = 'tenant_allow_runtime'
                                     AND p.polpermissive
                               ) AS has_permissive,
                               EXISTS (
                                   SELECT 1 FROM pg_policy p
                                   WHERE p.polrelid = c.oid
                                     AND p.polname = 'tenant_isolation_policy'
                                     AND NOT p.polpermissive
                                     AND p.polqual IS NOT NULL
                                     AND p.polwithcheck IS NOT NULL
                               ) AS has_restrictive
                        FROM pg_class c WHERE c.oid = to_regclass(:name)
                        """
                    ),
                    {"name": name},
                )
            ).one()
            assert row.relrowsecurity is True, name
            assert row.relforcerowsecurity is True, name
            assert row.has_permissive is True, name
            assert row.has_restrictive is True, name

            grants = {
                grant
                for (grant,) in (
                    await session.execute(
                        text(
                            "SELECT privilege_type FROM information_schema.role_table_grants "
                            "WHERE table_name = :name AND grantee = 'app_runtime'"
                        ),
                        {"name": name},
                    )
                ).all()
            }
            assert {"SELECT", "INSERT", "UPDATE", "DELETE"} <= grants, name


async def test_cross_tenant_rls_isolation_and_fail_closed_insert(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    tenant: UUID = workspace["tenant"]
    other: UUID = workspace["other_tenant"]

    async with tenant_session(factory, tenant) as session:
        assert await session.scalar(text("SELECT current_user")) == "app_runtime"
        batch_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO settlement_batches "
                "(id, tenant_id, channel_type, batch_ref, currency, period_start, period_end, "
                " fx_rate_to_base) VALUES "
                "(:id, :tid, 'amazon_sp', 'RLS-1', 'USD', :p0, :p1, 7.100000)"
            ),
            {
                "id": batch_id,
                "tid": tenant,
                "p0": date(2026, 11, 1),
                "p1": date(2026, 11, 30),
            },
        )
        await session.execute(
            text(
                "INSERT INTO settlement_lines "
                "(tenant_id, batch_id, external_order_id, fx_rate_to_base) "
                "VALUES (:tid, :bid, 'RLS-LINE', 7.100000)"
            ),
            {"tid": tenant, "bid": batch_id},
        )
        await session.execute(
            text(
                "INSERT INTO payout_statements "
                "(tenant_id, provider, external_tx_ref, payout_currency, payout_amount, "
                " settled_base_currency, settled_base_amount, actual_fx_rate, "
                " expected_fx_rate, fx_variance_base, paid_at) VALUES "
                "(:tid, 'pingpong', 'RLS-PAY', 'USD', 10.00, 'CNY', 71.00, "
                " 7.100000, 7.100000, 0.00, :paid_at)"
            ),
            {"tid": tenant, "paid_at": datetime(2026, 11, 20, tzinfo=UTC)},
        )
        for name in TABLES:
            assert await session.scalar(text(f"SELECT COUNT(*) FROM {name}")) == 1, name

    async def assert_hidden(session: AsyncSession) -> None:
        for name in TABLES:
            assert await session.scalar(text(f"SELECT COUNT(*) FROM {name}")) == 0, name
            assert (
                await session.execute(text(f"UPDATE {name} SET id=id RETURNING id"))
            ).all() == []
            assert (await session.execute(text(f"DELETE FROM {name} RETURNING id"))).all() == []

    async with tenant_session(factory, other) as session:
        await assert_hidden(session)
    async with factory() as session, session.begin():
        await assert_hidden(session)

    async with tenant_session(factory, other) as session:
        with pytest.raises(DBAPIError, match="row-level security"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO settlement_batches "
                        "(tenant_id, channel_type, batch_ref, currency, period_start, "
                        " period_end, fx_rate_to_base) VALUES "
                        "(:tid, 'amazon_sp', 'CROSS-1', 'USD', :p0, :p1, 7.100000)"
                    ),
                    {
                        "tid": tenant,
                        "p0": date(2026, 11, 1),
                        "p1": date(2026, 11, 30),
                    },
                )


# --------------------------------------------------------------------------- #
# settlement import
# --------------------------------------------------------------------------- #
async def test_settlement_csv_import_autolinks_orders_and_sums_in_decimal(
    workspace: dict[str, Any],
) -> None:
    factory: Factory = workspace["factory"]
    tenant: UUID = workspace["tenant"]
    async with tenant_session(factory, tenant) as session:
        order_a = await _seed_order(
            session,
            tenant,
            order_no="AMZ-1001",
            external_order_id="111-2223334-5556667",
            total="100.00",
        )
        order_b = await _seed_order(
            session,
            tenant,
            order_no="AMZ-1002",
            external_order_id="111-2223334-5556668",
            total="50.00",
        )

    csv_text = (
        f"{SETTLEMENT_CSV_HEADER}\n"
        "111-2223334-5556667,LED-COB-24V,100.00,16.00,4.00,1.00,3.00,0.00\n"
        "AMZ-1002,LED-COB-12V,50.00,7.00,2.00,0.50,1.50,0.00\n"
        "UNKNOWN-REF-9,LED-COB-5V,10.00,1.50,0.40,0.10,0.30,0.00\n"
    )
    async with client_for(factory, workspace["finance"]) as client:
        response = await client.post(
            f"{BASE}/settlements/import",
            content=csv_text.encode(),
            headers={"Content-Type": "text/csv"},
            params={
                "channel_type": "amazon_sp",
                "batch_ref": "AMZ-SET-202611",
                "currency": "USD",
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "fx_rate_to_base": "7.100000",
            },
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["batch_ref"] == "AMZ-SET-202611"
    assert payload["currency"] == "USD"
    assert payload["status"] == "imported"
    assert payload["line_count"] == 3
    # Decimal totals: gross 160.00, commission 24.50, ff 6.40, storage 1.60, ad 4.80
    assert payload["gross_sales_ccy"] == "160.00"
    assert payload["commission_ccy"] == "24.50"
    assert payload["fulfillment_fee_ccy"] == "6.40"
    assert payload["storage_fee_ccy"] == "1.60"
    assert payload["ad_fee_ccy"] == "4.80"
    # net = 160.00 - 24.50 - 6.40 - 1.60 - 4.80 = 122.70 -> *7.1 = 871.17
    assert payload["net_payout_ccy"] == "122.70"
    assert payload["net_payout_base"] == "871.17"

    async with client_for(factory, workspace["owner"]) as client:
        lines_response = await client.get(
            f"{BASE}/settlements/{payload['id']}/lines",
        )
        list_response = await client.get(
            f"{BASE}/settlements", params={"channel_type": "amazon_sp"}
        )
    assert lines_response.status_code == 200, lines_response.text
    lines = lines_response.json()
    assert lines["total_count"] == 3
    assert lines["matched_count"] == 2
    by_ext = {line["external_order_id"]: line for line in lines["items"]}
    assert by_ext["111-2223334-5556667"]["order_id"] == str(order_a)
    assert by_ext["AMZ-1002"]["order_id"] == str(order_b)
    assert by_ext["UNKNOWN-REF-9"]["order_id"] is None
    # line net: 100 - 16 - 4 - 1 - 3 = 76.00 -> *7.1 = 539.60
    assert by_ext["111-2223334-5556667"]["net_settlement_ccy"] == "76.00"
    assert by_ext["111-2223334-5556667"]["net_settlement_base"] == "539.60"
    # line net: 50 - 7 - 2 - 0.5 - 1.5 = 39.00 -> *7.1 = 276.90
    assert by_ext["AMZ-1002"]["net_settlement_base"] == "276.90"

    listing = list_response.json()
    assert listing["total_count"] == 1
    assert listing["base_currency"] == "CNY"
    assert listing["items"][0]["line_count"] == 3
    assert listing["total_net_payout_base"] == "871.17"

    async with tenant_session(factory, tenant) as session:
        audit_actions = {
            action
            for (action,) in (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE tenant_id = :tid AND resource_id = :rid"
                    ),
                    {"tid": tenant, "rid": UUID(payload["id"])},
                )
            ).all()
        }
    assert "finance.settlement_batch.imported" in audit_actions


async def test_settlement_json_import_rounds_half_up_at_the_cent(workspace: dict[str, Any]) -> None:
    """`12.25 * 0.5 = 6.125` must become `6.13`: the discriminating ROUND_HALF_UP case."""
    factory: Factory = workspace["factory"]
    body = {
        "channel_type": "shopify",
        "batch_ref": "SHOP-ROUND-1",
        "currency": "USD",
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
        "fx_rate_to_base": "0.500000",
        "lines": [
            {
                "external_order_id": "SHP-1",
                "sku": "KIT-1",
                "gross_sales_ccy": "20.00",
                "commission_ccy": "2.00",
                "fulfillment_fee_ccy": "2.00",
                "storage_fee_ccy": "1.00",
                "ad_fee_ccy": "2.50",
                "refund_ccy": "0.25",
            }
        ],
    }
    async with client_for(factory, workspace["finance"]) as client:
        response = await client.post(f"{BASE}/settlements/import", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["net_payout_ccy"] == "12.25"
    assert payload["net_payout_base"] == "6.13"

    async with client_for(factory, workspace["finance"]) as client:
        lines = (await client.get(f"{BASE}/settlements/{payload['id']}/lines")).json()["items"]
    assert lines[0]["net_settlement_base"] == "6.13"
    assert lines[0]["fx_rate_to_base"] == "0.500000"


async def test_settlement_import_rejects_json_float_and_formula_cells(
    workspace: dict[str, Any],
) -> None:
    factory: Factory = workspace["factory"]
    base_body = {
        "channel_type": "shopify",
        "batch_ref": "SHOP-BAD-1",
        "currency": "USD",
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
        "fx_rate_to_base": "7.100000",
        "lines": [],
    }
    async with client_for(factory, workspace["finance"]) as client:
        float_rate = await client.post(
            f"{BASE}/settlements/import",
            json={**base_body, "fx_rate_to_base": 7.1},
        )
        injection = await client.post(
            f"{BASE}/settlements/import",
            content=(
                f"{SETTLEMENT_CSV_HEADER}\n"
                "SKU-ROW-1,SKU-1,=1+1,1.00,0.10,0.10,0.10,0.00\n"
            ).encode(),
            headers={"Content-Type": "text/csv"},
            params={
                "channel_type": "shopify",
                "batch_ref": "SHOP-BAD-2",
                "currency": "USD",
                "period_start": "2026-10-01",
                "period_end": "2026-10-31",
                "fx_rate_to_base": "7.100000",
            },
        )
    assert float_rate.status_code == 422, float_rate.text
    # A formula cell is not a decimal, so the numeric column parser refuses it.
    assert injection.status_code == 422, injection.text
    assert "gross_sales_ccy" in injection.text


async def test_settlement_import_is_idempotent_per_batch_ref(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    body = {
        "channel_type": "amazon_sp",
        "batch_ref": "AMZ-IDEMPOTENT",
        "currency": "USD",
        "period_start": "2026-11-01",
        "period_end": "2026-11-30",
        "fx_rate_to_base": "7.100000",
        "lines": [{"external_order_id": "X-1", "gross_sales_ccy": "10.00"}],
    }
    async with client_for(factory, workspace["finance"]) as client:
        first = await client.post(f"{BASE}/settlements/import", json=body)
        second = await client.post(f"{BASE}/settlements/import", json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["line_count"] == 1


# --------------------------------------------------------------------------- #
# dual-basis profit
# --------------------------------------------------------------------------- #
async def _seed_dual_basis_fixture(workspace: dict[str, Any]) -> dict[str, UUID]:
    """Two Amazon orders estimated vs one 2026-11 statement with a matching payout."""
    factory: Factory = workspace["factory"]
    tenant: UUID = workspace["tenant"]
    async with tenant_session(factory, tenant) as session:
        order_a = await _seed_order(
            session,
            tenant,
            order_no="AMZ-2001",
            external_order_id="111-2223334-5556667",
            total="100.00",
        )
        order_b = await _seed_order(
            session,
            tenant,
            order_no="AMZ-2002",
            external_order_id="111-2223334-5556668",
            total="50.00",
        )
        await _seed_snapshot(
            session,
            tenant,
            order_a,
            gross="710.00",
            cogs="300.00",
            commission="106.50",
            gateway="10.00",
            shipping="30.00",
            ad="21.30",
        )
        await _seed_snapshot(
            session,
            tenant,
            order_b,
            gross="355.00",
            cogs="150.00",
            commission="53.25",
            gateway="5.00",
            shipping="20.00",
            ad="12.00",
        )
    return {"order_a": order_a, "order_b": order_b}


async def _import_dual_basis_batch(client: AsyncClient) -> dict[str, Any]:
    body = {
        "channel_type": "amazon_sp",
        "batch_ref": "AMZ-SET-202611",
        "currency": "USD",
        "period_start": "2026-11-01",
        "period_end": "2026-11-30",
        "fx_rate_to_base": "7.100000",
        "lines": [
            {
                "external_order_id": "111-2223334-5556667",
                "sku": "LED-COB-24V",
                "gross_sales_ccy": "100.00",
                "commission_ccy": "16.00",
                "fulfillment_fee_ccy": "4.00",
                "storage_fee_ccy": "1.00",
                "ad_fee_ccy": "3.00",
                "refund_ccy": "0.00",
            },
            {
                "external_order_id": "AMZ-2002",
                "sku": "LED-COB-12V",
                "gross_sales_ccy": "50.00",
                "commission_ccy": "7.00",
                "fulfillment_fee_ccy": "2.00",
                "storage_fee_ccy": "0.50",
                "ad_fee_ccy": "1.50",
                "refund_ccy": "0.00",
            },
        ],
    }
    response = await client.post(f"{BASE}/settlements/import", json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_dual_basis_profit_variance(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    orders = await _seed_dual_basis_fixture(workspace)
    async with client_for(factory, workspace["finance"]) as client:
        batch = await _import_dual_basis_batch(client)
        response = await client.get(f"{BASE}/profit/dual-basis")
    assert response.status_code == 200, response.text
    report = response.json()

    assert batch["net_payout_ccy"] == "115.00"
    assert batch["net_payout_base"] == "816.50"

    assert report["base_currency"] == "CNY"
    assert report["matched_orders_count"] == 2
    assert report["unmatched_lines_count"] == 0
    # Estimated basis: 710.00 + 355.00 revenue, 242.20 + 114.75 net.
    assert report["estimated_revenue_base"] == "1065.00"
    assert report["estimated_net_margin_base"] == "356.95"
    # Settled basis: 539.60 + 276.90 payout, minus 450.00 COGS, no order FX variance.
    assert report["settled_payout_base"] == "816.50"
    assert report["settled_net_profit_base"] == "366.50"
    assert report["fx_variance_base"] == "0.00"
    # commission: settled 113.60 + 49.70 = 163.30 vs estimated 106.50 + 53.25 = 159.75
    assert report["commission_delta_base"] == "3.55"
    # fulfillment+storage: settled 35.50 + 17.75 = 53.25 vs estimated 30.00 + 20.00 = 50.00
    assert report["fulfillment_storage_delta_base"] == "3.25"
    assert report["ad_spend_base"] == "31.95"
    assert report["total_profit_variance_base"] == "9.55"

    rows = {row["order_no"]: row for row in report["rows"]}
    row_a = rows["AMZ-2001"]
    assert row_a["order_id"] == str(orders["order_a"])
    assert row_a["estimated_revenue_base"] == "710.00"
    assert row_a["settled_revenue_base"] == "710.00"
    assert row_a["settled_commission_base"] == "113.60"
    assert row_a["commission_delta_base"] == "7.10"
    assert row_a["settled_fulfillment_storage_base"] == "35.50"
    assert row_a["fulfillment_storage_delta_base"] == "5.50"
    assert row_a["estimated_ad_spend_base"] == "21.30"
    assert row_a["ad_delta_base"] == "0.00"
    assert row_a["estimated_cogs_base"] == "300.00"
    assert row_a["settled_payout_base"] == "539.60"
    assert row_a["settled_net_profit_base"] == "239.60"
    assert row_a["profit_variance_base"] == "-2.60"

    row_b = rows["AMZ-2002"]
    assert row_b["commission_delta_base"] == "-3.55"
    assert row_b["fulfillment_storage_delta_base"] == "-2.25"
    assert row_b["ad_delta_base"] == "-1.35"
    assert row_b["settled_payout_base"] == "276.90"
    assert row_b["settled_net_profit_base"] == "126.90"
    assert row_b["profit_variance_base"] == "12.15"


async def test_dual_basis_counts_unmatched_settlement_lines(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    await _seed_dual_basis_fixture(workspace)
    async with client_for(factory, workspace["finance"]) as client:
        await _import_dual_basis_batch(client)
        extra = await client.post(
            f"{BASE}/settlements/import",
            json={
                "channel_type": "amazon_sp",
                "batch_ref": "AMZ-SET-202612",
                "currency": "USD",
                "period_start": "2026-12-01",
                "period_end": "2026-12-31",
                "fx_rate_to_base": "7.100000",
                "lines": [
                    {
                        "external_order_id": "NOT-IN-ERP-77",
                        "gross_sales_ccy": "10.00",
                        "commission_ccy": "1.50",
                    }
                ],
            },
        )
        assert extra.status_code == 200, extra.text
        report = (await client.get(f"{BASE}/profit/dual-basis")).json()
    assert report["matched_orders_count"] == 2
    assert report["unmatched_lines_count"] == 1


# --------------------------------------------------------------------------- #
# payout import + auto-match
# --------------------------------------------------------------------------- #
async def test_pingpong_payout_csv_import_computes_fx_variance_and_matches_batch(
    workspace: dict[str, Any],
) -> None:
    factory: Factory = workspace["factory"]
    await _seed_dual_basis_fixture(workspace)
    async with client_for(factory, workspace["finance"]) as client:
        batch = await _import_dual_basis_batch(client)
        # 115.50 USD * 7.1 = 820.05 CNY expected; 823.55 settled -> +3.50 FX gain.
        payout_csv = f"{PAYOUT_CSV_HEADER}\nPP-20261125-0001,USD,115.50,CNY,823.55,7.100000\n"
        imported = await client.post(
            f"{BASE}/payouts/import",
            content=payout_csv.encode(),
            headers={"Content-Type": "text/csv"},
            params={"provider": "pingpong"},
        )
        assert imported.status_code == 200, imported.text
        payout = imported.json()["items"][0]
        # actual = 823.55 / 115.50 = 7.130303 (ROUND_HALF_UP to 6 dp)
        assert payout["actual_fx_rate"] == "7.130303"
        assert payout["expected_fx_rate"] == "7.100000"
        assert payout["fx_variance_base"] == "3.50"
        assert payout["match_status"] == "unmatched"

        matched = await client.post(f"{BASE}/payouts/auto-match")
        assert matched.status_code == 200, matched.text
        matched_payload = matched.json()
        assert matched_payload["matched_count"] == 1
        assert matched_payload["remaining_unmatched_count"] == 0
        assert matched_payload["total_fx_variance_base"] == "3.50"
        assert matched_payload["items"][0]["matched_settlement_batch_id"] == batch["id"]
        assert matched_payload["items"][0]["matched_order_id"] is None

        listing = (await client.get(f"{BASE}/settlements")).json()
        assert listing["items"][0]["status"] == "reconciled"
        payouts = (await client.get(f"{BASE}/payouts")).json()
        assert payouts["matched_count"] == 1
        assert payouts["unmatched_count"] == 0
        assert payouts["total_fx_variance_base"] == "3.50"

    async with tenant_session(factory, workspace["tenant"]) as session:
        actions = {
            action
            for (action,) in (
                await session.execute(
                    text("SELECT action FROM audit_events WHERE tenant_id = :tid"),
                    {"tid": workspace["tenant"]},
                )
            ).all()
        }
    assert "finance.payout_statement.imported" in actions
    assert "finance.payout_statement.matched" in actions


async def test_lianlian_payout_json_matches_by_exact_reference_hint(
    workspace: dict[str, Any],
) -> None:
    factory: Factory = workspace["factory"]
    await _seed_dual_basis_fixture(workspace)
    async with client_for(factory, workspace["finance"]) as client:
        batch = await _import_dual_basis_batch(client)
        response = await client.post(
            f"{BASE}/payouts/import",
            json={
                "provider": "lianlian",
                "rows": [
                    {
                        "external_tx_ref": "LL-2026-11-0007",
                        "payout_currency": "USD",
                        "payout_amount": "999.00",
                        "settled_base_currency": "CNY",
                        "settled_base_amount": "7092.90",
                        "expected_fx_rate": "7.100000",
                        "paid_at": "2026-11-27T03:15:00Z",
                        "reference_hint": "AMZ-SET-202611",
                    }
                ],
            },
        )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["imported_count"] == 1
    assert result["matched_count"] == 1
    payout = result["items"][0]
    assert payout["matched_settlement_batch_id"] == batch["id"]
    # 999.00 * 7.1 = 7092.90 exactly, so the FX variance is zero.
    assert payout["fx_variance_base"] == "0.00"
    assert payout["actual_fx_rate"] == "7.100000"


async def test_payout_matches_a_paid_b2b_order_and_books_order_level_fx(
    workspace: dict[str, Any],
) -> None:
    factory: Factory = workspace["factory"]
    tenant: UUID = workspace["tenant"]
    async with tenant_session(factory, tenant) as session:
        order_id = await _seed_order(
            session,
            tenant,
            order_no="B2B-9001",
            external_order_id=None,
            total="2000.00",
            order_mode="b2b_wholesale",
            payment_status="paid",
        )
        await _seed_snapshot(
            session,
            tenant,
            order_id,
            gross="14200.00",
            cogs="9000.00",
            commission="0.00",
            gateway="120.00",
            shipping="400.00",
            ad="0.00",
        )

    async with client_for(factory, workspace["finance"]) as client:
        imported = await client.post(
            f"{BASE}/payouts/import",
            json={
                "provider": "payoneer",
                "rows": [
                    {
                        "external_tx_ref": "PAY-2026-11-XYZ",
                        "payout_currency": "USD",
                        "payout_amount": "2000.00",
                        "settled_base_currency": "CNY",
                        "settled_base_amount": "14280.00",
                        "expected_fx_rate": "7.100000",
                        "paid_at": "2026-11-28T09:00:00Z",
                    }
                ],
            },
        )
        assert imported.status_code == 200, imported.text
        payout = imported.json()["items"][0]
        assert payout["fx_variance_base"] == "80.00"
        matched = await client.post(f"{BASE}/payouts/auto-match")
        assert matched.status_code == 200, matched.text
        assert matched.json()["items"][0]["matched_order_id"] == str(order_id)
        report = (await client.get(f"{BASE}/profit/dual-basis")).json()

    assert report["matched_orders_count"] == 0
    # The B2B order has no settlement lines, so only the payout FX is booked.
    assert report["fx_variance_base"] == "0.00"
    assert report["unmatched_lines_count"] == 0

    async with tenant_session(factory, tenant) as session:
        fx_total = await session.scalar(
            text("SELECT fx_variance_base FROM payout_statements WHERE tenant_id = :tid"),
            {"tid": tenant},
        )
    assert Decimal(str(fx_total)) == Decimal("80.00")


async def test_dual_basis_books_order_level_fx_variance_into_settled_profit(
    workspace: dict[str, Any],
) -> None:
    """A payout matched to the order moves the settled net profit by its FX gain."""
    factory: Factory = workspace["factory"]
    tenant: UUID = workspace["tenant"]
    async with tenant_session(factory, tenant) as session:
        order_id = await _seed_order(
            session,
            tenant,
            order_no="AMZ-FX-1",
            external_order_id="111-9999999-0000001",
            total="100.00",
        )
        await _seed_snapshot(
            session,
            tenant,
            order_id,
            gross="710.00",
            cogs="300.00",
            commission="106.50",
            gateway="10.00",
            shipping="35.50",
            ad="21.30",
        )

    async with client_for(factory, workspace["finance"]) as client:
        await client.post(
            f"{BASE}/settlements/import",
            json={
                "channel_type": "amazon_sp",
                "batch_ref": "AMZ-FX-BATCH",
                "currency": "USD",
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "fx_rate_to_base": "7.100000",
                "lines": [
                    {
                        "external_order_id": "111-9999999-0000001",
                        "gross_sales_ccy": "100.00",
                        "commission_ccy": "15.00",
                        "fulfillment_fee_ccy": "5.00",
                        "ad_fee_ccy": "3.00",
                    }
                ],
            },
        )
        payout = await client.post(
            f"{BASE}/payouts/import",
            json={
                "provider": "pingpong",
                "rows": [
                    {
                        "external_tx_ref": "PP-FX-1",
                        "payout_currency": "USD",
                        "payout_amount": "77.00",
                        "settled_base_currency": "CNY",
                        "settled_base_amount": "552.09",
                        "expected_fx_rate": "7.100000",
                        "paid_at": "2026-11-26T00:00:00Z",
                        "reference_hint": "AMZ-FX-1",
                    }
                ],
            },
        )
        assert payout.status_code == 200, payout.text
        assert payout.json()["items"][0]["matched_order_id"] == str(order_id)
        report = (await client.get(f"{BASE}/profit/dual-basis")).json()

    row = report["rows"][0]
    # 77.00 * 7.1 = 546.70 expected; 552.09 settled -> +5.39 FX gain.
    assert row["fx_variance_base"] == "5.39"
    assert row["settled_payout_base"] == "546.70"
    # 546.70 - 300.00 COGS + 5.39 FX = 252.09
    assert row["settled_net_profit_base"] == "252.09"
    assert report["fx_variance_base"] == "5.39"


# --------------------------------------------------------------------------- #
# journal export
# --------------------------------------------------------------------------- #
async def test_journal_export_is_balanced_in_every_dialect(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    await _seed_dual_basis_fixture(workspace)
    async with client_for(factory, workspace["finance"]) as client:
        await _import_dual_basis_batch(client)
        await client.post(
            f"{BASE}/payouts/import",
            json={
                "provider": "pingpong",
                "rows": [
                    {
                        "external_tx_ref": "PP-JOURNAL-1",
                        "payout_currency": "USD",
                        "payout_amount": "115.50",
                        "settled_base_currency": "CNY",
                        "settled_base_amount": "823.55",
                        "expected_fx_rate": "7.100000",
                        "paid_at": "2026-11-25T02:30:00Z",
                    }
                ],
            },
        )
        await client.post(f"{BASE}/payouts/auto-match")
        responses = {
            fmt: await client.get(f"{BASE}/journal-export", params={"format": fmt})
            for fmt in ("standard", "quickbooks", "xero", "kingdee")
        }
        unsupported = await client.get(f"{BASE}/journal-export", params={"format": "sage"})

    assert unsupported.status_code == 422, unsupported.text
    for fmt, response in responses.items():
        assert response.status_code == 200, (fmt, response.text)
        assert response.headers["content-type"].startswith("text/csv")
        assert f'filename="journal-{fmt}.csv"' in response.headers["content-disposition"]
        header, rows = _parse_csv(response.text)
        assert rows, fmt
        if fmt == "standard":
            debit_key, credit_key = "debit_base", "credit_base"
        elif fmt in ("xero", "quickbooks"):
            debit_key, credit_key = "Debit", "Credit"
        else:
            debit_key, credit_key = "借方本位币", "贷方本位币"
        assert debit_key in header and credit_key in header, fmt
        debits = sum(Decimal(row[debit_key]) for row in rows)
        credits = sum(Decimal(row[credit_key]) for row in rows)
        assert debits == credits, (fmt, debits, credits)
        # gross 1065.00 + commission 163.30 + ff/storage 53.25 + ad 31.95
        # + COGS 450.00 + net payout 816.50 + FX gain 3.50
        assert debits == Decimal("2583.50"), fmt

    standard_rows = _parse_csv(responses["standard"].text)[1]
    accounts = {row["account_name"] for row in standard_rows}
    assert {
        "Marketplace Clearing",
        "Sales Revenue",
        "Cost of Goods Sold",
        "Inventory",
        "Platform Commission Expense",
        "Fulfillment & Storage Expense",
        "Advertising Expense",
        "FX Gain / Loss",
        "Cash / Bank",
    } <= accounts


async def test_journal_export_neutralises_formula_cells(workspace: dict[str, Any]) -> None:
    """A vendor-supplied reference that looks like a formula must never export raw."""
    factory: Factory = workspace["factory"]
    async with client_for(factory, workspace["finance"]) as client:
        imported = await client.post(
            f"{BASE}/settlements/import",
            json={
                "channel_type": '=HYPERLINK("http://evil")',
                "batch_ref": "@BATCH-1",
                "currency": "USD",
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "fx_rate_to_base": "7.100000",
                "lines": [{"external_order_id": "X-1", "gross_sales_ccy": "10.00"}],
            },
        )
        assert imported.status_code == 200, imported.text
        batch_id = imported.json()["id"]
        async with tenant_session(factory, workspace["tenant"]) as session:
            await session.execute(
                text("UPDATE settlement_batches SET status='reconciled' WHERE id = :bid"),
                {"bid": UUID(batch_id)},
            )
        exported = await client.get(f"{BASE}/journal-export", params={"format": "standard"})
    assert exported.status_code == 200, exported.text
    _header, rows = _parse_csv(exported.text)
    descriptions = [row["description"] for row in rows]
    assert any(description.startswith("'=HYPERLINK") for description in descriptions)
    assert not any(description.startswith("=") for description in descriptions)
    # A voucher number that itself starts with a formula character is quoted too.
    assert all(not row["voucher_no"].startswith(("=", "+", "@", "-")) for row in rows)
    debits = sum(Decimal(row["debit_base"]) for row in rows)
    credits = sum(Decimal(row["credit_base"]) for row in rows)
    assert debits == credits


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
def test_role_contract_is_locked() -> None:
    assert SETTLEMENT_READ_ROLES == (
        UserRole.OWNER,
        UserRole.ADMIN,
        UserRole.MANAGER,
        UserRole.FINANCE,
    )
    assert SETTLEMENT_WRITE_ROLES == (UserRole.OWNER, UserRole.FINANCE, UserRole.ADMIN)


async def test_agent_is_denied_403_on_every_settlement_route(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    async with client_for(factory, workspace["agent"]) as client:
        results = {
            "settlements": await client.get(f"{BASE}/settlements"),
            "lines": await client.get(f"{BASE}/settlements/{uuid4()}/lines"),
            "payouts": await client.get(f"{BASE}/payouts"),
            "dual-basis": await client.get(f"{BASE}/profit/dual-basis"),
            "journal": await client.get(f"{BASE}/journal-export"),
            "import-settlements": await client.post(
                f"{BASE}/settlements/import",
                json={
                    "channel_type": "amazon_sp",
                    "batch_ref": "NOPE",
                    "currency": "USD",
                    "period_start": "2026-11-01",
                    "period_end": "2026-11-30",
                    "fx_rate_to_base": "7.100000",
                    "lines": [{"gross_sales_ccy": "1.00"}],
                },
            ),
            "import-payouts": await client.post(
                f"{BASE}/payouts/import",
                json={"provider": "pingpong", "rows": []},
            ),
            "auto-match": await client.post(f"{BASE}/payouts/auto-match"),
        }
    for name, response in results.items():
        assert response.status_code == 403, (name, response.status_code, response.text)
        assert response.json()["code"] == "ERR_INSUFFICIENT_ROLE", name


async def test_manager_reads_but_cannot_mutate_payout_state(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    async with client_for(factory, workspace["manager"]) as client:
        assert (await client.get(f"{BASE}/settlements")).status_code == 200
        assert (await client.get(f"{BASE}/payouts")).status_code == 200
        assert (await client.get(f"{BASE}/profit/dual-basis")).status_code == 200
        assert (await client.get(f"{BASE}/journal-export")).status_code == 200
        denied_import = await client.post(
            f"{BASE}/settlements/import",
            json={
                "channel_type": "amazon_sp",
                "batch_ref": "MGR-1",
                "currency": "USD",
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "fx_rate_to_base": "7.100000",
                "lines": [{"gross_sales_ccy": "1.00"}],
            },
        )
        denied_match = await client.post(f"{BASE}/payouts/auto-match")
    assert denied_import.status_code == 403, denied_import.text
    assert denied_match.status_code == 403, denied_match.text


async def test_admin_and_owner_can_mutate(workspace: dict[str, Any]) -> None:
    factory: Factory = workspace["factory"]
    async with client_for(factory, workspace["admin"]) as client:
        imported = await client.post(
            f"{BASE}/settlements/import",
            json={
                "channel_type": "amazon_sp",
                "batch_ref": "ADMIN-1",
                "currency": "USD",
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "fx_rate_to_base": "7.100000",
                "lines": [{"gross_sales_ccy": "1.00"}],
            },
        )
        assert imported.status_code == 200, imported.text
    async with client_for(factory, workspace["owner"]) as client:
        matched = await client.post(f"{BASE}/payouts/auto-match")
        assert matched.status_code == 200, matched.text
