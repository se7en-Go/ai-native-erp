from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_core.shared.config import get_settings
from erp_core.shared.crypto import EnvelopeCryptoService
from erp_core.shared.db import tenant_session
from erp_core.shared.errors import AppError

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_SCRIPT_PATH = REPO_ROOT / "apps" / "backend" / "scripts" / "seed_hybrid_factory.py"


def _load_seed_module():
    assert SEED_SCRIPT_PATH.exists(), "seed_hybrid_factory.py does not exist"
    spec = importlib.util.spec_from_file_location("seed_hybrid_factory", SEED_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def test_seed_hybrid_factory_is_idempotent_and_isolated(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed_mod = _load_seed_module()
    settings = get_settings()

    summary_1 = await seed_mod.seed_hybrid_factory(runtime_session_factory, settings)
    summary_2 = await seed_mod.seed_hybrid_factory(runtime_session_factory, settings)

    assert summary_1.tenant_a_id == summary_2.tenant_a_id
    assert summary_1.tenant_b_id == summary_2.tenant_b_id
    assert summary_2.tenant_a_slug == "quanzhou-led-factory"
    assert summary_2.tenant_b_slug == "shenzhen-audio-dtc"

    crypto = EnvelopeCryptoService(settings)

    async with tenant_session(runtime_session_factory, summary_2.tenant_a_id) as session:
        tenants = (await session.execute(text("SELECT slug FROM tenants"))).all()
        assert [row.slug for row in tenants] == ["quanzhou-led-factory"]

        members = (await session.execute(text("SELECT role FROM tenant_memberships"))).all()
        assert sorted(row.role for row in members) == sorted(
            ["owner", "admin", "manager", "agent", "finance"]
        )

        creds = (
            await session.execute(
                text(
                    "SELECT channel_account_id, credential_type, ciphertext, nonce, "
                    "auth_tag, key_version "
                    "FROM channel_credentials ORDER BY credential_type"
                )
            )
        ).all()
        assert len(creds) == 3

        first = creds[0]
        decrypted = await crypto.decrypt_secret(
            session,
            summary_2.tenant_a_id,
            UUID(str(first.channel_account_id)),
            str(first.credential_type),
            bytes(first.ciphertext),
            bytes(first.nonce),
            bytes(first.auth_tag),
            int(first.key_version),
        )
        assert decrypted

        stage = await session.scalar(text("SELECT current_stage FROM onboarding_states"))
        assert stage == "LiveProduction"

    async with tenant_session(runtime_session_factory, summary_2.tenant_b_id) as session:
        tenants_b = (await session.execute(text("SELECT slug FROM tenants"))).all()
        assert [row.slug for row in tenants_b] == ["shenzhen-audio-dtc"]

        members_b = (await session.execute(text("SELECT role FROM tenant_memberships"))).all()
        assert [row.role for row in members_b] == ["owner"]

        creds_b = (await session.execute(text("SELECT id FROM channel_credentials"))).all()
        assert len(creds_b) == 1

        with pytest.raises(AppError) as err:
            await crypto.decrypt_secret(
                session,
                summary_2.tenant_b_id,
                UUID(str(first.channel_account_id)),
                str(first.credential_type),
                bytes(first.ciphertext),
                bytes(first.nonce),
                bytes(first.auth_tag),
                int(first.key_version),
            )
        assert err.value.code == "ERR_INVALID_CREDENTIAL_TAG"

    owner_login = await async_client.post(
        "/api/v1/auth/login",
        json={
            "email": "owner@quanzhou-led.example.com",
            "password": seed_mod.SEED_DEMO_PASSWORD,
            "tenant_slug": "quanzhou-led-factory",
        },
    )
    assert owner_login.status_code == 200
    assert owner_login.json()["principal"]["role"] == "owner"
    assert owner_login.json()["principal"]["can_view_cost"] is True

    agent_login = await async_client.post(
        "/api/v1/auth/login",
        json={
            "email": "agent@quanzhou-led.example.com",
            "password": seed_mod.SEED_DEMO_PASSWORD,
            "tenant_slug": "quanzhou-led-factory",
        },
    )
    assert agent_login.status_code == 200
    assert agent_login.json()["principal"]["role"] == "agent"
    assert agent_login.json()["principal"]["can_view_cost"] is False


async def test_phase3b_workforce_seed_is_idempotent(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed_mod = _load_seed_module()
    from uuid import uuid4

    tenant, owner = uuid4(), uuid4()
    async with tenant_session(runtime_session_factory, tenant) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, '3B Seed')"),
            {"id": tenant, "slug": f"seed3b-{tenant.hex}"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:uid, :email, 'Owner', 'x')"
            ),
            {"uid": owner, "email": f"{owner.hex}@example.com"},
        )
        for _ in range(2):
            await seed_mod._seed_phase3b_workforce(session, tenant_id=tenant, owner_id=owner)

    async with tenant_session(runtime_session_factory, tenant) as session:
        employees = (
            await session.execute(
                text("SELECT code, autonomy_level, capability_codes FROM ai_employees")
            )
        ).all()
        assert len(employees) == 9
        assert {row.code for row in employees} == {
            employee["code"] for employee in seed_mod._PHASE3B_EMPLOYEES
        }
        assert all(
            row.autonomy_level in {"L0_copilot", "L1_proposal", "L2_auto"} for row in employees
        )
        replies = (await session.execute(text("SELECT language FROM quick_reply_templates"))).all()
        assert len(replies) == 5
        assert {row.language for row in replies} == {"zh", "en"}
        assets = (
            await session.execute(text("SELECT asset_type, status FROM ai_creative_assets"))
        ).all()
        assert len(assets) == 2
        assert {row.asset_type for row in assets} == {"product_copy", "listing"}

    from erp_core.ai_engine.capabilities import CAPABILITY_SPECS, min_autonomy
    from erp_core.shared.enums import AiCapability, AutonomyLevel

    for employee in seed_mod._PHASE3B_EMPLOYEES:
        ceiling = min_autonomy(
            *(CAPABILITY_SPECS[AiCapability(code)].max_allowed_autonomy
              for code in employee["capability_codes"])
        )
        assert AutonomyLevel(employee["autonomy_level"]).value == employee["autonomy_level"]
        assert employee["autonomy_level"] == min_autonomy(
            AutonomyLevel(employee["autonomy_level"]), ceiling
        ).value


async def test_stage1_finance_demo_seed_is_idempotent_and_decimal_exact(
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two seed runs must not duplicate quotes/orders/milestones and must stay Decimal exact."""
    from decimal import Decimal
    from uuid import uuid4

    seed_mod = _load_seed_module()
    tenant, owner, finance, product, variant = (uuid4() for _ in range(5))
    async with tenant_session(runtime_session_factory, tenant) as session:
        await session.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'Finance Seed')"),
            {"id": tenant, "slug": f"seedfin-{tenant.hex}"},
        )
        for user_id, email, role in (
            (owner, f"owner-{tenant.hex}@example.com", "owner"),
            (finance, f"finance-{tenant.hex}@example.com", "finance"),
        ):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, 'Seed User', 'x')"
                ),
                {"id": user_id, "email": email},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                    "VALUES (:mid, :tid, :uid, :role)"
                ),
                {"mid": uuid4(), "tid": tenant, "uid": user_id, "role": role},
            )
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:pid, :tid, 'LED-COB-24V', 'LED strips')"
            ),
            {"pid": product, "tid": tenant},
        )
        await session.execute(
            text(
                "INSERT INTO product_variants (id, tenant_id, product_id, sku, cost_price_base, "
                "base_currency, retail_price_usd, weight_grams, lead_time_days, b2b_moq_tiers) "
                "VALUES (:vid, :tid, :pid, 'LED-COB-24V-5M', 5, 'USD', 29.90, 400, 7, "
                "CAST(:tiers AS jsonb))"
            ),
            {
                "vid": variant,
                "tid": tenant,
                "pid": product,
                "tiers": '[{"min_qty": 100, "unit_price_usd": "14.50"}]',
            },
        )
        for _ in range(2):
            await seed_mod._seed_stage1_finance_demo(
                session,
                tenant_id=tenant,
                owner_id=owner,
                finance_user_id=finance,
                variant_id=variant,
            )
        snapshot_times_after_first = None

    async with tenant_session(runtime_session_factory, tenant) as session:
        counts = {
            "quotes": int(await session.scalar(text("SELECT count(*) FROM quotes"))),
            "quote_items": int(await session.scalar(text("SELECT count(*) FROM quote_items"))),
            "orders": int(await session.scalar(text("SELECT count(*) FROM orders"))),
            "order_items": int(await session.scalar(text("SELECT count(*) FROM order_items"))),
            "receivables": int(
                await session.scalar(text("SELECT count(*) FROM receivables_payables"))
            ),
            "payment_records": int(
                await session.scalar(text("SELECT count(*) FROM payment_records"))
            ),
            "snapshots": int(
                await session.scalar(text("SELECT count(*) FROM order_profit_snapshots"))
            ),
        }
        assert counts == {
            "quotes": 2,
            "quote_items": 4,
            "orders": 2,
            "order_items": 4,
            "receivables": 4,
            "payment_records": 3,
            "snapshots": 2,
        }
        snapshot_times_after_first = {
            str(row.order_no): row.recalculated_at
            for row in (
                await session.execute(
                    text(
                        "SELECT o.order_no, s.recalculated_at FROM order_profit_snapshots s "
                        "JOIN orders o ON o.id = s.order_id"
                    )
                )
            ).all()
        }

    # A third run must be a no-op for every business row and never re-dates the snapshots.
    async with tenant_session(runtime_session_factory, tenant) as session:
        await seed_mod._seed_stage1_finance_demo(
            session,
            tenant_id=tenant,
            owner_id=owner,
            finance_user_id=finance,
            variant_id=variant,
        )

    async with tenant_session(runtime_session_factory, tenant) as session:
        assert int(await session.scalar(text("SELECT count(*) FROM quotes"))) == 2
        assert int(await session.scalar(text("SELECT count(*) FROM quote_items"))) == 4
        assert int(await session.scalar(text("SELECT count(*) FROM orders"))) == 2
        assert int(await session.scalar(text("SELECT count(*) FROM order_items"))) == 4
        assert int(await session.scalar(text("SELECT count(*) FROM receivables_payables"))) == 4
        assert int(await session.scalar(text("SELECT count(*) FROM payment_records"))) == 3
        assert int(await session.scalar(text("SELECT count(*) FROM order_profit_snapshots"))) == 2
        assert snapshot_times_after_first == {
            str(row.order_no): row.recalculated_at
            for row in (
                await session.execute(
                    text(
                        "SELECT o.order_no, s.recalculated_at FROM order_profit_snapshots s "
                        "JOIN orders o ON o.id = s.order_id"
                    )
                )
            ).all()
        }

        # Quotes keep tiered lines and exact Decimal totals.
        quotes = (
            await session.execute(
                text(
                    "SELECT quote_no, incoterm, status, quote_currency, fx_rate_snapshot, "
                    "freight_fee, insurance_fee, total_amount FROM quotes ORDER BY quote_no"
                )
            )
        ).all()
        assert [row.quote_no for row in quotes] == ["PI-DEMO-1001", "PI-DEMO-1002"]
        fob, cif = quotes
        assert (fob.incoterm, fob.status) == ("FOB", "sent")
        assert Decimal(str(fob.total_amount)) == Decimal("17240.00")
        assert Decimal(str(fob.freight_fee)) == Decimal("0.00")
        assert (cif.incoterm, cif.status) == ("CIF", "pending_approval")
        assert Decimal(str(cif.total_amount)) == Decimal("14824.60")
        assert Decimal(str(cif.freight_fee)) == Decimal("520.00")
        assert Decimal(str(cif.insurance_fee)) == Decimal("84.60")

        tier_rows = (
            await session.execute(
                text(
                    "SELECT q.quote_no, qi.quantity, qi.moq_tier_applied, "
                    "qi.unit_price_quote_ccy, qi.line_total_quote_ccy "
                    "FROM quote_items qi JOIN quotes q ON q.id = qi.quote_id "
                    "ORDER BY q.quote_no, qi.quantity DESC"
                )
            )
        ).all()
        assert [
            (row.quote_no, row.quantity, row.moq_tier_applied) for row in tier_rows
        ] == [
            ("PI-DEMO-1001", 1200, 1000),
            ("PI-DEMO-1001", 800, 500),
            ("PI-DEMO-1002", 1200, 1000),
            ("PI-DEMO-1002", 300, 100),
        ]
        assert sum(Decimal(str(row.line_total_quote_ccy)) for row in tier_rows) == Decimal(
            "31460.00"
        )

        # 30% deposit verified with a bank-slip reference, 70% balance still pending.
        milestones = (
            await session.execute(
                text(
                    "SELECT o.order_no, rp.milestone_label, rp.due_amount, rp.paid_amount, "
                    "rp.status, rp.currency FROM receivables_payables rp "
                    "JOIN orders o ON o.id = rp.order_id "
                    "ORDER BY o.order_no, rp.milestone_label"
                )
            )
        ).all()
        assert [
            (row.order_no, row.milestone_label, Decimal(str(row.due_amount)), row.status)
            for row in milestones
        ] == [
            ("DEMO-B2B-2001", "b2b_30_deposit", Decimal("5172.00"), "settled"),
            ("DEMO-B2B-2001", "b2b_70_balance", Decimal("12068.00"), "open"),
            ("DEMO-B2B-2002", "b2b_30_deposit", Decimal("4447.38"), "settled"),
            ("DEMO-B2B-2002", "b2b_70_balance", Decimal("10377.22"), "open"),
        ]
        assert all(row.currency == "USD" for row in milestones)

        payments = (
            await session.execute(
                text(
                    "SELECT o.order_no, rp.milestone_label, pr.external_tx_ref, "
                    "pr.amount_received, pr.gateway_fee, pr.actual_fx_rate, "
                    "pr.verified_by_user_id, pr.payment_method FROM payment_records pr "
                    "JOIN receivables_payables rp ON rp.id = pr.rp_id "
                    "JOIN orders o ON o.id = rp.order_id "
                    "ORDER BY o.order_no, rp.milestone_label"
                )
            )
        ).all()
        assert len(payments) == 3
        verified = [row for row in payments if row.verified_by_user_id is not None]
        pending = [row for row in payments if row.verified_by_user_id is None]
        assert [row.external_tx_ref for row in verified] == [
            "TT-EUROLIGHT-26-0413",
            "TT-NORDIC-26-0197",
        ]
        assert all(row.payment_method == "tt_wire" for row in payments)
        assert all(Decimal(str(row.gateway_fee)) == Decimal("35.00") for row in payments)
        assert len(pending) == 1
        assert pending[0].external_tx_ref == "TT-EUROLIGHT-26-0520"
        assert Decimal(str(pending[0].amount_received)) == Decimal("12068.00")

        # CVP snapshots are Decimal exact and land inside the 28%-36% demo band.
        snapshots = {
            row.order_no: row
            for row in (
                await session.execute(
                    text(
                        "SELECT o.order_no, s.base_currency, s.gross_revenue_base, s.cogs_base, "
                        "s.platform_commission_base, s.payment_gateway_fee_base, "
                        "s.shipping_and_duty_base, s.channel_msg_cost_base, "
                        "s.ad_attribution_cost_base, s.fx_gain_loss_base, "
                        "s.net_contribution_margin_base FROM order_profit_snapshots s "
                        "JOIN orders o ON o.id = s.order_id"
                    )
                )
            ).all()
        }
        fob_snap = snapshots["DEMO-B2B-2001"]
        assert Decimal(str(fob_snap.gross_revenue_base)) == Decimal("17240.00")
        assert Decimal(str(fob_snap.cogs_base)) == Decimal("10000.00")
        assert Decimal(str(fob_snap.platform_commission_base)) == Decimal("1034.40")
        assert Decimal(str(fob_snap.payment_gateway_fee_base)) == Decimal("35.00")
        assert Decimal(str(fob_snap.shipping_and_duty_base)) == Decimal("0.00")
        assert Decimal(str(fob_snap.channel_msg_cost_base)) == Decimal("120.00")
        assert Decimal(str(fob_snap.ad_attribution_cost_base)) == Decimal("0.00")
        assert Decimal(str(fob_snap.net_contribution_margin_base)) == Decimal("6050.60")

        cif_snap = snapshots["DEMO-B2B-2002"]
        assert Decimal(str(cif_snap.gross_revenue_base)) == Decimal("14824.60")
        assert Decimal(str(cif_snap.cogs_base)) == Decimal("7500.00")
        assert Decimal(str(cif_snap.platform_commission_base)) == Decimal("592.98")
        assert Decimal(str(cif_snap.payment_gateway_fee_base)) == Decimal("35.00")
        assert Decimal(str(cif_snap.shipping_and_duty_base)) == Decimal("604.60")
        assert Decimal(str(cif_snap.channel_msg_cost_base)) == Decimal("96.00")
        assert Decimal(str(cif_snap.ad_attribution_cost_base)) == Decimal("700.00")
        assert Decimal(str(cif_snap.net_contribution_margin_base)) == Decimal("5296.02")

        from erp_core.finance.profit_service import OrderProfitService

        summary = await OrderProfitService().get_profit_summary(session, tenant)
        assert summary.total_orders == 2
        assert summary.gross_revenue_base == Decimal("32064.60")
        assert summary.net_contribution_margin_base == Decimal("11346.62")
        assert summary.contribution_margin_pct == Decimal("35.39")
        assert Decimal("28") <= summary.contribution_margin_pct <= Decimal("36")


async def test_stage1_finance_demo_powers_finance_and_quote_apis(
    async_client: AsyncClient,
    runtime_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The seeded export pipeline must surface real, non-zero Decimal money on the APIs."""
    from decimal import Decimal
    from uuid import uuid4

    from erp_core.tenancy.security import hash_password

    seed_mod = _load_seed_module()
    tenant, owner, finance, product, variant = (uuid4() for _ in range(5))
    password = "FinanceSeedPass2026!"
    owner_email = f"owner-{tenant.hex[:10]}@example.com"
    finance_email = f"finance-{tenant.hex[:10]}@example.com"
    async with tenant_session(runtime_session_factory, tenant) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, 'Finance Seed API', 'USD')"
            ),
            {"id": tenant, "slug": f"seedfinapi-{tenant.hex}"},
        )
        for user_id, email, role in (
            (owner, owner_email, "owner"),
            (finance, finance_email, "finance"),
        ):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, full_name, password_hash) "
                    "VALUES (:id, :email, 'Seed User', :ph)"
                ),
                {"id": user_id, "email": email, "ph": hash_password(password)},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships (id, tenant_id, user_id, role) "
                    "VALUES (:mid, :tid, :uid, :role)"
                ),
                {"mid": uuid4(), "tid": tenant, "uid": user_id, "role": role},
            )
        await session.execute(
            text(
                "INSERT INTO products (id, tenant_id, spu_code, category) "
                "VALUES (:pid, :tid, 'LED-COB-24V', 'LED strips')"
            ),
            {"pid": product, "tid": tenant},
        )
        await session.execute(
            text(
                "INSERT INTO product_variants (id, tenant_id, product_id, sku, cost_price_base, "
                "base_currency, retail_price_usd, weight_grams, lead_time_days, b2b_moq_tiers) "
                "VALUES (:vid, :tid, :pid, 'LED-COB-24V-5M', 5, 'USD', 29.90, 400, 7, "
                "CAST(:tiers AS jsonb))"
            ),
            {
                "vid": variant,
                "tid": tenant,
                "pid": product,
                "tiers": '[{"min_qty": 100, "unit_price_usd": "14.50"}]',
            },
        )
        await seed_mod._seed_stage1_finance_demo(
            session,
            tenant_id=tenant,
            owner_id=owner,
            finance_user_id=finance,
            variant_id=variant,
        )

    async def login(email: str) -> dict[str, str]:
        response = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password, "tenant_slug": f"seedfinapi-{tenant.hex}"},
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    finance_headers = await login(finance_email)
    overview = await async_client.get("/api/v1/finance/overview", headers=finance_headers)
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert Decimal(body["summary"]["total_gross_revenue_base"]) == Decimal("32064.60")
    assert Decimal(body["summary"]["total_net_margin_base"]) == Decimal("11346.62")
    assert Decimal(body["summary"]["avg_margin_pct"]) == Decimal("35.39")
    assert body["summary"]["base_currency"] == "USD"
    assert len(body["receivables"]) == 4
    assert len(body["cvp_snapshots"]) == 2
    balance = next(
        item
        for item in body["receivables"]
        if item["order_no"] == "DEMO-B2B-2001" and item["milestone_label"] == "b2b_70_balance"
    )
    assert Decimal(balance["due_amount"]) == Decimal("12068.00")
    assert balance["status"] == "open"
    assert balance["shipment_blocked_70_unpaid"] is True
    assert balance["pending_payment_id"]
    assert balance["pending_tx_ref"] == "TT-EUROLIGHT-26-0520"
    deposit = next(
        item
        for item in body["receivables"]
        if item["order_no"] == "DEMO-B2B-2001" and item["milestone_label"] == "b2b_30_deposit"
    )
    assert deposit["status"] == "settled"
    assert Decimal(deposit["paid_amount"]) == Decimal("5172.00")

    summary = await async_client.get("/api/v1/finance/profit-summary", headers=finance_headers)
    assert summary.status_code == 200, summary.text
    assert summary.json()["total_orders"] == 2
    assert Decimal(summary.json()["gross_revenue_base"]) == Decimal("32064.60")
    assert Decimal(summary.json()["net_contribution_margin_base"]) == Decimal("11346.62")
    assert Decimal(summary.json()["contribution_margin_pct"]) == Decimal("35.39")

    owner_headers = await login(owner_email)
    quotes = await async_client.get("/api/v1/b2b/quotes", headers=owner_headers)
    assert quotes.status_code == 200, quotes.text
    quote_rows = {item["quote_no"]: item for item in quotes.json()["items"]}
    assert set(quote_rows) == {"PI-DEMO-1001", "PI-DEMO-1002"}
    assert quote_rows["PI-DEMO-1001"]["incoterm"] == "FOB"
    assert Decimal(quote_rows["PI-DEMO-1001"]["total_amount"]) == Decimal("17240.00")
    assert len(quote_rows["PI-DEMO-1001"]["items"]) == 2
    assert quote_rows["PI-DEMO-1002"]["incoterm"] == "CIF"
    assert Decimal(quote_rows["PI-DEMO-1002"]["total_amount"]) == Decimal("14824.60")
    opportunities = await async_client.get("/api/v1/b2b/opportunities", headers=owner_headers)
    assert opportunities.status_code == 200, opportunities.text
    assert opportunities.json()["total"] == 2

    # Finance verification of the seeded balance slip must settle the order and re-price CVP.
    verified = await async_client.post(
        f"/api/v1/finance/payments/{balance['pending_payment_id']}/verify",
        headers=finance_headers,
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["payment"]["verified_by_user_id"] is not None
    after = await async_client.get("/api/v1/finance/overview", headers=finance_headers)
    assert after.status_code == 200, after.text
    after_body = after.json()
    settled_balance = next(
        item
        for item in after_body["receivables"]
        if item["order_no"] == "DEMO-B2B-2001" and item["milestone_label"] == "b2b_70_balance"
    )
    assert settled_balance["status"] == "settled"
    assert settled_balance["pending_payment_id"] is None
    assert settled_balance["shipment_blocked_70_unpaid"] is False
    fob_snapshot = next(
        item for item in after_body["cvp_snapshots"] if item["order_no"] == "DEMO-B2B-2001"
    )
    assert Decimal(fob_snapshot["payment_gateway_fee_base"]) == Decimal("70.00")
    assert Decimal(fob_snapshot["net_contribution_margin_base"]) == Decimal("6015.60")
    assert Decimal(after_body["summary"]["total_net_margin_base"]) == Decimal("11311.62")
    orders = await async_client.get("/api/v1/orders", headers=owner_headers)
    assert orders.status_code == 200, orders.text
    order_rows = {item["order_no"]: item for item in orders.json()["items"]}
    assert order_rows["DEMO-B2B-2001"]["payment_status"] == "paid"
