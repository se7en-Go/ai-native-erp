"""Worker P5: cross-channel analytics tool, grounded claims, and Copilot analytics chat.

Covers the `vw_tenant_channel_sales_analytics` read-only SQL path, the deterministic
`get_live_channel_sales_analytics` tool (Decimal-exact GMV / refund rate / commission /
ad spend / net contribution margin / margin % / ROAS / traffic), source_tool_call_id
provenance through `ToolGroundedNumericGuard`, and the natural-language
`POST /api/v1/ai/assistant/chat` capability gate.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from erp_core.ai_engine.numeric_guard import ToolGroundedNumericGuard
from erp_core.ai_engine.router import router as ai_router
from erp_core.ai_engine.runtime import PolicyRuntime
from erp_core.ai_engine.tools import ErpToolRegistry
from erp_core.finance.channel_analytics_service import ChannelAnalyticsService
from erp_core.shared.config import get_settings
from erp_core.shared.db import create_runtime_engine, create_session_factory, tenant_session
from erp_core.shared.enums import UserRole
from erp_core.shared.errors import register_exception_handlers
from erp_core.tenancy.dependencies import CurrentPrincipal, get_current_principal

_ROOT = "/api/v1/ai"
Factory = async_sessionmaker[AsyncSession]

_SQL_PATH = (
    "SELECT channel_type, sum(gmv_base) AS gmv FROM vw_tenant_channel_sales_analytics "
    "GROUP BY channel_type"
)


async def _cleanup_tenants(tenant_ids: list[UUID]) -> None:
    admin = create_async_engine(
        os.environ.get(
            "DATABASE_ADMIN_URL",
            "postgresql+asyncpg://postgres:postgres_dev_pass@localhost:5432/erp_saas",
        )
    )
    try:
        async with admin.begin() as conn:
            for tid in tenant_ids:
                for stmt in (
                    "DELETE FROM channel_daily_metrics WHERE tenant_id = :tid",
                    "DELETE FROM order_profit_snapshots WHERE tenant_id = :tid",
                    "DELETE FROM receivables_payables WHERE tenant_id = :tid",
                    "DELETE FROM payment_records WHERE tenant_id = :tid",
                    "DELETE FROM order_items WHERE tenant_id = :tid",
                    "DELETE FROM orders WHERE tenant_id = :tid",
                    "DELETE FROM platform_sku_mappings WHERE tenant_id = :tid",
                    "DELETE FROM customers WHERE tenant_id = :tid",
                    "DELETE FROM channel_accounts WHERE tenant_id = :tid",
                    "DELETE FROM credit_wallets WHERE tenant_id = :tid",
                    "DELETE FROM ai_proposals WHERE tenant_id = :tid",
                    "DELETE FROM audit_events WHERE tenant_id = :tid",
                    "DELETE FROM tenant_memberships WHERE tenant_id = :tid",
                ):
                    await conn.execute(text(stmt), {"tid": tid})
                await conn.execute(
                    text("DELETE FROM users WHERE email LIKE :pat"),
                    {"pat": f"%@analytics-ai-{tid.hex}.example.com"},
                )
                await conn.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid})
    finally:
        await admin.dispose()


@pytest.fixture
async def runtime_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_runtime_engine(get_settings())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def analytics_fixture(runtime_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """Tenant A (Shopify + Shopee + Temu) and Tenant B (Shopee) with PG17 RLS in force."""
    factory = create_session_factory(runtime_engine)
    tid_a, tid_b = uuid4(), uuid4()
    ca_shopify, ca_shopee, ca_temu, ca_shopee_b = uuid4(), uuid4(), uuid4(), uuid4()
    customer_a, customer_b = uuid4(), uuid4()
    uid_a = uuid4()
    now = datetime.now(UTC)
    old_order_at = now - timedelta(days=20)

    async with tenant_session(factory, tid_a) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, 'Analytics AI Co', 'USD')"
            ),
            {"id": tid_a, "slug": f"ana-ai-{tid_a.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, full_name, password_hash) "
                "VALUES (:id, :email, 'Analytics Owner', 'hash')"
            ),
            {"id": uid_a, "email": f"owner@analytics-ai-{tid_a.hex}.example.com"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role, is_active) "
                "VALUES (:tid, :uid, 'owner', true)"
            ),
            {"tid": tid_a, "uid": uid_a},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2c_consumer', 'Analytics Buyer A')"
            ),
            {"id": customer_a, "tid": tid_a},
        )
        for ca_id, channel_type, label in (
            (ca_shopify, "shopify", "Shopify US"),
            (ca_shopee, "shopee", "Shopee SG"),
            (ca_temu, "temu", "Temu US"),
        ):
            await session.execute(
                text(
                    "INSERT INTO channel_accounts "
                    "(id, tenant_id, channel_type, external_account_id, display_name, status) "
                    "VALUES (:id, :tid, :ct, :ext, :label, 'active')"
                ),
                {
                    "id": ca_id,
                    "tid": tid_a,
                    "ct": channel_type,
                    "ext": f"ext-{ca_id.hex[:8]}",
                    "label": label,
                },
            )

        # order_no, channel_account_id, total, cogs, commission, order ad, refunded, created_at
        orders: tuple[tuple[str, UUID, str, str, str, str, str, datetime], ...] = (
            ("ORD-SH-1001", ca_shopify, "600.00", "200.00", "30.00", "0.00", "50.00", now),
            ("ORD-SH-0900", ca_shopify, "50.00", "20.00", "2.00", "0.00", "0.00", old_order_at),
            ("ORD-SP-2001", ca_shopee, "300.00", "90.00", "15.00", "10.00", "0.00", now),
            ("ORD-TM-3001", ca_temu, "100.00", "40.00", "8.00", "0.00", "0.00", now),
        )
        for order_no, ca_id, total, cogs, commission, order_ad, refunded, created_at in orders:
            order_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO orders "
                    "(id, tenant_id, order_no, order_mode, channel_account_id, customer_id, "
                    "currency, fx_rate_to_base, subtotal_amount, total_amount, "
                    "fulfillment_status, payment_status, channel_metadata, created_at) "
                    "VALUES (:id, :tid, :order_no, 'b2c_retail', :caid, :cid, 'USD', "
                    "1.000000, :total, :total, 'delivered', 'paid', "
                    "jsonb_build_object('refunded_amount', CAST(:refunded AS text)), :created)"
                ),
                {
                    "id": order_id,
                    "tid": tid_a,
                    "order_no": order_no,
                    "caid": ca_id,
                    "cid": customer_a,
                    "total": Decimal(total),
                    "refunded": refunded,
                    "created": created_at,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO order_profit_snapshots "
                    "(order_id, tenant_id, base_currency, gross_revenue_base, cogs_base, "
                    "platform_commission_base, ad_attribution_cost_base) "
                    "VALUES (:oid, :tid, 'USD', :gross, :cogs, :commission, :order_ad)"
                ),
                {
                    "oid": order_id,
                    "tid": tid_a,
                    "gross": Decimal(total),
                    "cogs": Decimal(cogs),
                    "commission": Decimal(commission),
                    "order_ad": Decimal(order_ad),
                },
            )

        await session.execute(
            text(
                "INSERT INTO channel_daily_metrics "
                "(tenant_id, channel_account_id, variant_id, metric_date, impressions, "
                "clicks, ad_spend_base, external_sessions, source) "
                "VALUES (:tid, :caid, NULL, CURRENT_DATE, 5000, 250, 100.00, 240, 'manual')"
            ),
            {"tid": tid_a, "caid": ca_shopify},
        )

    async with tenant_session(factory, tid_b) as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, slug, name, base_currency) "
                "VALUES (:id, :slug, 'Analytics AI Co B', 'USD')"
            ),
            {"id": tid_b, "slug": f"ana-ai-b-{tid_b.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO customers (id, tenant_id, customer_type, display_name) "
                "VALUES (:id, :tid, 'b2c_consumer', 'Analytics Buyer B')"
            ),
            {"id": customer_b, "tid": tid_b},
        )
        await session.execute(
            text(
                "INSERT INTO channel_accounts "
                "(id, tenant_id, channel_type, external_account_id, display_name, status) "
                "VALUES (:id, :tid, 'shopee', :ext, 'Shopee B', 'active')"
            ),
            {"id": ca_shopee_b, "tid": tid_b, "ext": f"ext-{ca_shopee_b.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO orders "
                "(id, tenant_id, order_no, order_mode, channel_account_id, customer_id, "
                "currency, fx_rate_to_base, subtotal_amount, total_amount, "
                "fulfillment_status, payment_status) "
                "VALUES (:id, :tid, 'ORD-B-SP-1', 'b2c_retail', :caid, :cid, 'USD', "
                "1.000000, 900.00, 900.00, 'delivered', 'paid')"
            ),
            {"id": uuid4(), "tid": tid_b, "caid": ca_shopee_b, "cid": customer_b},
        )

    try:
        yield {
            "factory": factory,
            "tid_a": tid_a,
            "tid_b": tid_b,
            "uid_a": uid_a,
            "ca_shopify": ca_shopify,
            "ca_shopee": ca_shopee,
            "ca_temu": ca_temu,
        }
    finally:
        await _cleanup_tenants([tid_a, tid_b])


def _channel(data: dict[str, Any], channel_type: str) -> dict[str, Any]:
    return next(row for row in data["channels"] if row["channel_type"] == channel_type)


def _assert_matches_service(report: dict[str, Any], overview: Any, channel_type: str) -> None:
    expected = (
        overview.totals
        if channel_type == "all"
        else next(item for item in overview.channels if item.channel_type == channel_type)
    )
    actual = report["totals"] if channel_type == "all" else _channel(report, channel_type)
    for field in (
        "order_count",
        "gmv_base",
        "refunded_amount_base",
        "refund_rate_pct",
        "cogs_base",
        "platform_commission_base",
        "ad_spend_base",
        "net_margin_base",
        "margin_pct",
        "roas",
    ):
        want = getattr(expected, field)
        got = actual[field]
        if want is None:
            assert got is None, (field, got)
        elif isinstance(want, Decimal):
            assert Decimal(got) == want, (field, got, want)
        else:
            assert got == want, (field, got, want)


async def test_analytics_tool_matches_service_and_rls_isolates_tenants(
    analytics_fixture: dict[str, Any],
) -> None:
    factory: Factory = analytics_fixture["factory"]
    tid_a: UUID = analytics_fixture["tid_a"]
    tid_b: UUID = analytics_fixture["tid_b"]
    registry = ErpToolRegistry()

    async with tenant_session(factory, tid_a) as session:
        # 1. Read-only SQL allowlist admits the analytics view and binds the tenant.
        sql_result = await registry.query_readonly_analytics_sql(session, tid_a, _SQL_PATH)
        assert sql_result.ok is True, sql_result.error_message
        by_type = {row["channel_type"]: Decimal(row["gmv"]) for row in sql_result.data["rows"]}
        assert by_type == {
            "shopify": Decimal("650.00"),
            "shopee": Decimal("300.00"),
            "temu": Decimal("100.00"),
        }
        assert sql_result.data["source_tool_call_id"] == sql_result.tool_call_id

        # 2. Decimal-exact 30d KPIs, ordered by GMV desc.
        result = await registry.get_live_channel_sales_analytics(session, tid_a, window="30d")
        assert result.ok is True
        assert result.data["source_tool_call_id"] == result.tool_call_id
        assert result.data["window"] == "30d"
        assert result.data["channel_types_filter"] == []
        assert [row["channel_type"] for row in result.data["channels"]] == [
            "shopify",
            "shopee",
            "temu",
        ]

        totals = result.data["totals"]
        assert totals["order_count"] == 4
        assert Decimal(totals["gmv_base"]) == Decimal("1050.00")
        assert Decimal(totals["refunded_amount_base"]) == Decimal("50.00")
        assert Decimal(totals["refund_rate_pct"]) == Decimal("4.76")
        assert Decimal(totals["cogs_base"]) == Decimal("350.00")
        assert Decimal(totals["platform_commission_base"]) == Decimal("55.00")
        assert Decimal(totals["ad_spend_base"]) == Decimal("110.00")
        assert Decimal(totals["net_margin_base"]) == Decimal("535.00")
        assert Decimal(totals["margin_pct"]) == Decimal("50.95")
        assert Decimal(totals["roas"]) == Decimal("9.55")
        assert totals["impressions"] == 5000
        assert totals["clicks"] == 250
        assert totals["external_sessions"] == 240

        shopify = _channel(result.data, "shopify")
        assert shopify["channel_account_id"] == str(analytics_fixture["ca_shopify"])
        assert shopify["channel_label"] == "Shopify US"
        assert shopify["order_count"] == 2
        assert Decimal(shopify["gmv_base"]) == Decimal("650.00")
        assert Decimal(shopify["refund_rate_pct"]) == Decimal("7.69")
        assert Decimal(shopify["platform_commission_base"]) == Decimal("32.00")
        assert Decimal(shopify["ad_spend_base"]) == Decimal("100.00")
        assert Decimal(shopify["net_margin_base"]) == Decimal("298.00")
        assert Decimal(shopify["margin_pct"]) == Decimal("45.85")
        assert Decimal(shopify["roas"]) == Decimal("6.50")

        temu = _channel(result.data, "temu")
        assert temu["roas"] is None, "no ad spend must yield a null ROAS, never a divide-by-zero"
        assert Decimal(temu["net_margin_base"]) == Decimal("52.00")

        # 3. Tool numbers must equal the finance ChannelAnalyticsService (no drift).
        overview = await ChannelAnalyticsService().get_overview(session, tid_a, window="30d")
        _assert_matches_service(result.data, overview, "all")
        for channel_type in ("shopify", "shopee", "temu"):
            _assert_matches_service(result.data, overview, channel_type)

        # 4. Window and channel filters are applied inside RLS.
        week = await registry.get_live_channel_sales_analytics(session, tid_a, window="7d")
        assert week.ok is True
        assert week.data["totals"]["order_count"] == 3
        assert Decimal(week.data["totals"]["gmv_base"]) == Decimal("1000.00")
        assert Decimal(week.data["totals"]["roas"]) == Decimal("9.09")

        filtered = await registry.get_live_channel_sales_analytics(
            session, tid_a, window="30d", channel_types=["Shopee", "shopify"]
        )
        assert filtered.data["channel_types_filter"] == ["shopee", "shopify"]
        assert [row["channel_type"] for row in filtered.data["channels"]] == [
            "shopify",
            "shopee",
        ]
        assert Decimal(filtered.data["totals"]["gmv_base"]) == Decimal("950.00")
        assert Decimal(filtered.data["totals"]["margin_pct"]) == Decimal("50.84")
        assert Decimal(filtered.data["totals"]["roas"]) == Decimal("8.64")

        invalid = await registry.get_live_channel_sales_analytics(
            session, tid_a, window="15d"
        )
        assert invalid.ok is False
        assert invalid.error_code == "ERR_INVALID_ANALYTICS_WINDOW"

        empty = await registry.get_live_channel_sales_analytics(
            session, tid_a, window="30d", channel_types=["ozon"]
        )
        assert empty.ok is True
        assert empty.data["channels"] == []
        assert empty.data["totals"]["order_count"] == 0
        assert Decimal(empty.data["totals"]["gmv_base"]) == Decimal("0.00")
        assert empty.data["totals"]["roas"] is None

    # 5. Tenant B sees only its own Shopee GMV; Tenant A never leaks into B.
    async with tenant_session(factory, tid_b) as session:
        other = await registry.get_live_channel_sales_analytics(session, tid_b, window="30d")
        assert other.ok is True
        assert [row["channel_type"] for row in other.data["channels"]] == ["shopee"]
        assert Decimal(other.data["totals"]["gmv_base"]) == Decimal("900.00")
        assert Decimal(_channel(other.data, "shopee")["gmv_base"]) == Decimal("900.00")
        other_sql = await registry.query_readonly_analytics_sql(session, tid_b, _SQL_PATH)
        assert other_sql.ok is True
        assert {row["channel_type"]: Decimal(row["gmv"]) for row in other_sql.data["rows"]} == {
            "shopee": Decimal("900.00")
        }

    async with tenant_session(factory, tid_a) as session:
        a_sql = await registry.query_readonly_analytics_sql(session, tid_a, _SQL_PATH)
        assert "shopee" in {row["channel_type"] for row in a_sql.data["rows"]}
        assert Decimal(
            next(row["gmv"] for row in a_sql.data["rows"] if row["channel_type"] == "shopee")
        ) == Decimal("300.00")


async def test_numeric_guard_grounds_analytics_claims(
    analytics_fixture: dict[str, Any],
) -> None:
    factory: Factory = analytics_fixture["factory"]
    tid_a: UUID = analytics_fixture["tid_a"]
    registry = ErpToolRegistry()
    guard = ToolGroundedNumericGuard()

    async with tenant_session(factory, tid_a) as session:
        result = await registry.get_live_channel_sales_analytics(session, tid_a, window="30d")
        totals = result.data["totals"]
        shopify = _channel(result.data, "shopify")

        grounded = guard.verify(
            "",
            [result],
            [
                {
                    "source_tool_call_id": result.tool_call_id,
                    "window": "30d",
                    "channel_type": "all",
                    "gmv_base": totals["gmv_base"],
                    "refund_rate_pct": totals["refund_rate_pct"],
                    "margin_pct": totals["margin_pct"],
                    "roas": totals["roas"],
                }
            ],
        )
        assert grounded.passed is True
        assert grounded.matched_tool_call_ids == [result.tool_call_id]

        channel_claim = guard.verify(
            "",
            [result],
            [
                {
                    "source_tool_call_id": result.tool_call_id,
                    "channel_type": "shopify",
                    "channel_label": "Shopify US",
                    "gmv_base": shopify["gmv_base"],
                    "roas": shopify["roas"],
                }
            ],
        )
        assert channel_claim.passed is True

        fabricated = guard.verify(
            "",
            [result],
            [{"source_tool_call_id": result.tool_call_id, "gmv_base": "9999.99"}],
        )
        assert fabricated.passed is False
        assert fabricated.allow_l2_auto_send is False
        assert "9999.99" in fabricated.ungrounded_tokens

        wrong_platform = guard.verify(
            "",
            [result],
            [{"source_tool_call_id": result.tool_call_id, "channel_type": "walmart"}],
        )
        assert wrong_platform.passed is False

        # Values spread across two different rows cannot be spliced into one claim.
        spliced = guard.verify(
            "",
            [result],
            [
                {
                    "source_tool_call_id": result.tool_call_id,
                    "channel_type": "temu",
                    "gmv_base": totals["gmv_base"],
                }
            ],
        )
        assert spliced.passed is False

        no_provenance = guard.verify("", [result], [{"gmv_base": totals["gmv_base"]}])
        assert no_provenance.passed is False
        assert "missing_or_invalid_source_tool_call_id" in no_provenance.ungrounded_tokens

        forged_source = guard.verify(
            "", [result], [{"source_tool_call_id": "tc_deadbeef", "gmv_base": totals["gmv_base"]}]
        )
        assert forged_source.passed is False


async def test_assistant_chat_natural_language_analytics_and_roles(
    analytics_fixture: dict[str, Any],
) -> None:
    factory: Factory = analytics_fixture["factory"]
    tid_a: UUID = analytics_fixture["tid_a"]
    uid_a: UUID = analytics_fixture["uid_a"]

    holder = [
        CurrentPrincipal(
            user_id=uid_a,
            tenant_id=tid_a,
            role=UserRole.OWNER,
            email=f"owner@analytics-ai-{tid_a.hex}.example.com",
        )
    ]
    app = FastAPI()
    register_exception_handlers(app)
    app.state.session_factory = factory
    app.state.ai_runtime = PolicyRuntime()
    app.include_router(ai_router)
    app.dependency_overrides[get_current_principal] = lambda: holder[0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Chinese natural-language question -> inferred 30d window + platforms.
        zh = await client.post(
            f"{_ROOT}/assistant/chat",
            json={
                "capability": "biz_analytics",
                "message": "近30天 Shopee 和 Shopify 的 GMV 与 ROAS 是多少",
            },
        )
        assert zh.status_code == 200, zh.text
        body = zh.json()
        assert body["numeric_guard_passed"] is True
        assert body["auto_executed"] is True
        assert body["should_abstain"] is False
        assert body["tool_calls"][0]["tool_name"] == "get_live_channel_sales_analytics"
        data = body["tool_calls"][0]["data"]
        assert data["window"] == "30d"
        assert data["channel_types_filter"] == ["shopee", "shopify"]
        assert data["source_tool_call_id"] == body["tool_calls"][0]["tool_call_id"]
        assert Decimal(data["totals"]["gmv_base"]) == Decimal("950.00")
        assert Decimal(data["totals"]["roas"]) == Decimal("8.64")
        assert Decimal(data["totals"]["net_margin_base"]) == Decimal("483.00")
        assert [row["channel_type"] for row in data["channels"]] == ["shopify", "shopee"]
        assert "source_tool_call_id" in body["reply_text"]
        assert "950.00" in body["reply_text"]
        assert "8.64" in body["reply_text"]

        # 2. English question with an explicit 7d window.
        en = await client.post(
            f"{_ROOT}/assistant/chat",
            json={
                "capability": "biz_analytics",
                "message": "Show 7d channel GMV and ROAS",
            },
        )
        assert en.status_code == 200, en.text
        en_body = en.json()
        assert en_body["numeric_guard_passed"] is True
        en_data = en_body["tool_calls"][0]["data"]
        assert en_data["window"] == "7d"
        assert en_data["channel_types_filter"] == []
        assert Decimal(en_data["totals"]["gmv_base"]) == Decimal("1000.00")
        assert Decimal(en_data["totals"]["roas"]) == Decimal("9.09")
        assert "Channel sales analytics for 7d" in en_body["reply_text"]

        # 3. Explicit context override wins over the natural-language default.
        explicit = await client.post(
            f"{_ROOT}/assistant/chat",
            json={
                "capability": "biz_analytics",
                "message": "How is Temu doing?",
                "context": {"window": "90d", "channel_types": ["temu"]},
            },
        )
        assert explicit.status_code == 200, explicit.text
        explicit_data = explicit.json()["tool_calls"][0]["data"]
        assert explicit_data["window"] == "90d"
        assert explicit_data["channel_types_filter"] == ["temu"]
        assert [row["channel_type"] for row in explicit_data["channels"]] == ["temu"]
        assert Decimal(explicit_data["totals"]["gmv_base"]) == Decimal("100.00")

        # 4. An explicit SQL context keeps the read-only analytics SQL path intact.
        sql_chat = await client.post(
            f"{_ROOT}/assistant/chat",
            json={
                "capability": "biz_analytics",
                "message": "Run the approved analytics report",
                "context": {"sql": _SQL_PATH},
            },
        )
        assert sql_chat.status_code == 200, sql_chat.text
        sql_body = sql_chat.json()
        assert sql_body["tool_calls"][0]["tool_name"] == "query_readonly_analytics_sql"
        assert sql_body["tool_calls"][0]["data"]["row_count"] == 3

        # 5. Invalid window abstains instead of fabricating a report.
        bad_window = await client.post(
            f"{_ROOT}/assistant/chat",
            json={
                "capability": "biz_analytics",
                "message": "GMV for the last 15 days",
                "context": {"window": "15d"},
            },
        )
        assert bad_window.status_code == 200, bad_window.text
        assert bad_window.json()["should_abstain"] is True
        assert bad_window.json()["reason"] == "ERR_INVALID_ANALYTICS_WINDOW"
        assert bad_window.json()["reply_text"] == ""

        # 6. OWNER / MANAGER / FINANCE may read commercial analytics; AGENT may not.
        for role in (UserRole.MANAGER, UserRole.FINANCE):
            holder[0] = CurrentPrincipal(
                user_id=uid_a,
                tenant_id=tid_a,
                role=role,
                email=f"owner@analytics-ai-{tid_a.hex}.example.com",
            )
            allowed = await client.post(
                f"{_ROOT}/assistant/chat",
                json={
                    "capability": "biz_analytics",
                    "message": "近30天 Shopee 的 GMV",
                },
            )
            assert allowed.status_code == 200, allowed.text
            assert Decimal(
                allowed.json()["tool_calls"][0]["data"]["totals"]["gmv_base"]
            ) == Decimal("300.00")

        holder[0] = CurrentPrincipal(
            user_id=uid_a,
            tenant_id=tid_a,
            role=UserRole.AGENT,
            email=f"owner@analytics-ai-{tid_a.hex}.example.com",
        )
        denied = await client.post(
            f"{_ROOT}/assistant/chat",
            json={"capability": "biz_analytics", "message": "Show 30d GMV and ROAS"},
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "ERR_INSUFFICIENT_ROLE"
